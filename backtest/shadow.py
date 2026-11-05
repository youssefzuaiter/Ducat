"""The long-term core's shadow portfolios: a registered comparison set, run forward on published bars.

The paper account tests the *machinery*; it cannot also test the *strategy* against alternatives, because Alpaca
allows only a few paper accounts. A shadow portfolio needs no account: it holds a mix in a ledger, decides at each
published close with ``core_paper.decide`` (the function the live Allocator and router call, which calls
``core_alloc``, the backtest's own arithmetic) and fills at the next session's official open. Four portfolios run
side by side from the session the real account was built, so a difference between them is a difference of
strategy, not of timing.

The set and the reading rules below were written on 2026-11-05, before any shadow number existed. They are printed
at the top of every report; changing one means a new version of this file, stated as such.

    ./.venv/bin/python -m backtest.shadow                      # update shadow-state/ and print the report
    ./.venv/bin/python -m backtest.shadow --out reports/shadow.md --with-account

Read-only: it reads Alpaca's calendar and bars (and, with ``--with-account``, the core account's equity). It
places and changes nothing, and it does not touch the registered backtest's caches.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any, Final

import core_alloc
import core_paper
import tier0
import tier0_core

logger = logging.getLogger("backtest.shadow")

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DEFAULT_POLICY: Final[Path] = ROOT / "policy" / "core.toml"
DEFAULT_STATE: Final[Path] = ROOT / "shadow-state"
ZERO: Final[Decimal] = Decimal(0)
#: The session whose open filled the real account's initial build (journal: plan a69aacd2, 2026-10-12).
START: Final[date] = date(2026, 10, 12)
CAPITAL: Final[Decimal] = Decimal(10000)
#: Sessions before which the numbers are descriptive only (rule S5): one trading year.
EVIDENCE_SESSIONS: Final[int] = 252


@dataclass(frozen=True)
class Definition:
    name: str
    label: str
    mix: tuple[tuple[str, str], ...]
    rule: str
    buffer_symbol: str | None = None

    @property
    def weights(self) -> dict[str, Decimal]:
        return {symbol: Decimal(weight) for symbol, weight in self.mix}


FIVE: Final[tuple[str, ...]] = ("BND", "IAU", "VNQ", "VTI", "VXUS")
DEFINITIONS: Final[tuple[Definition, ...]] = (
    Definition("twin", "Owner's policy: M3 + 5% BIL, quarterly",
               tuple((s, "0.19") for s in FIVE) + (("BIL", "0.05"),), "quarterly", "BIL"),
    Definition("m3", "M3: 20% each of five ETFs, quarterly", tuple((s, "0.2") for s in FIVE), "quarterly"),
    Definition("m2", "M2: 60% VTI / 40% BND, quarterly", (("BND", "0.4"), ("VTI", "0.6")), "quarterly"),
    Definition("m1", "M1: 100% VTI, buy and hold (benchmark)", (("VTI", "1"),), "none"),
)
SYMBOLS: Final[tuple[str, ...]] = tuple(sorted({s for d in DEFINITIONS for s, _ in d.mix}))

READING_RULES: Final[str] = """\
These rules were written on 2026-11-05, before any shadow number existed.

* **S1 A closed set, a fixed start.** Four portfolios (the twin of the owner's policy, M3 without the buffer, M2
  and M1 as the benchmark), each starting with $10,000 and filling its initial build at the open of 2026-10-12,
  the session the real account's build filled. The start is the real account's, not chosen from any result.
  Adding, removing or changing a portfolio needs a new version of this file, and the new set's own start date.
* **S2 The live rules, not a copy.** Each close is decided by `core_paper.decide`, the function the Allocator and
  the router call; plans fill at the next session's official open. The loss breaker is modelled as the backtest
  engine models it: a plan with buys is deferred, whole, if the portfolio is down 2.5% or more at the open, and
  re-decided at the next close.
* **S3 No costs, stated.** Fills carry no spread or fee, which is what Alpaca's paper simulator charges. The
  registered backtest puts the cost of this policy at about 1 bp a year (P4 in the paper report), so the omission
  cannot move a conclusion.
* **S4 Total return.** Prices are adjusted for splits and dividends (Alpaca `adjustment=all`), so a bond-heavy
  mix is not penalised for its yield. The whole series is recomputed from freshly fetched bars on every run;
  if Alpaca restates an adjustment, past days restate with it.
* **S5 Descriptive until 252 sessions.** Numbers are printed from the first day and labelled not evidence until
  the portfolios have run for 252 sessions. Until then the report prints no ranking, no paired difference and no
  recommendation: over a short window the ordering of these mixes is noise. At 252 sessions this file gets a new
  version that adds the registered interval (stationary bootstrap, mean block 5, 10,000 resamples, seed 0) on the
  paired daily-return difference between each portfolio and the owner's policy.
* **S6 The twin checks the machinery.** The twin's value should track the real account's equity. A gap larger than
  dividend timing and the cent-level fills explain is a plumbing fault to look at, not a finding about markets.
* **S7 The backfilled start is disclosed.** Sessions from 2026-10-12 to the day these rules were written were
  computed after the fact, from published bars, by rules fixed before the computation.\
"""


def registration_sha256() -> str:
    body = {"start": START.isoformat(), "capital": str(CAPITAL), "evidence_sessions": EVIDENCE_SESSIONS,
            "definitions": [asdict(d) for d in DEFINITIONS], "rules": READING_RULES}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class ShadowError(Exception):
    """The inputs cannot support an honest run; nothing is written."""


Bars = dict[str, dict[date, tuple[Decimal, Decimal]]]  # symbol -> session -> (open, close)


def parse_bars(raw: dict[str, list[dict[str, Any]]]) -> Bars:
    out: Bars = {}
    for symbol, rows in raw.items():
        out[symbol] = {date.fromisoformat(str(b["t"])[:10]): (Decimal(str(b["o"])), Decimal(str(b["c"])))
                       for b in rows}
    return out


@dataclass
class Day:
    date: date
    value: Decimal
    cash: Decimal
    rebalanced: bool = False
    deferred: bool = False


@dataclass
class Result:
    definition: Definition
    days: list[Day] = field(default_factory=list)
    qty: dict[str, Decimal] = field(default_factory=dict)
    cash: Decimal = ZERO
    pending: core_alloc.Plan | None = None   # decided at the last close; fills at the next open

    @property
    def value(self) -> Decimal:
        return self.days[-1].value

    @property
    def rebalances(self) -> int:
        return sum(d.rebalanced for d in self.days)

    @property
    def deferrals(self) -> int:
        return sum(d.deferred for d in self.days)

    @property
    def worst_fall(self) -> Decimal:
        peak, worst = ZERO, ZERO
        for d in self.days:
            peak = max(peak, d.value)
            if peak > 0:
                worst = min(worst, d.value / peak - 1)
        return worst


def _policy(base: tier0_core.CorePolicy, d: Definition) -> tier0_core.CorePolicy:
    return replace(base, account="SHADOW", mix=d.weights, rule=core_alloc.RULES[d.rule],
                   buffer_symbol=d.buffer_symbol, buffer_target=d.weights.get(d.buffer_symbol or "", ZERO))


def _fill(plan: core_alloc.Plan, opens: dict[str, Decimal], qty: dict[str, Decimal], cash: Decimal,
          policy: tier0_core.CorePolicy) -> Decimal:
    """Sells at the open, then buys scaled to the cash that exists: the router's order, with no costs (S3)."""
    for symbol, units in sorted(plan.sells.items()):
        units = min(units, qty[symbol])
        qty[symbol] -= units
        cash += units * opens[symbol]
    buys = core_alloc.scale_buys(plan.buys, cash - policy.cash_reserve_usd, {})
    for symbol, notional in sorted(buys.items()):
        units = (notional / opens[symbol]).quantize(core_alloc.QTY_QUANTUM, ROUND_DOWN)
        qty[symbol] = qty.get(symbol, ZERO) + units
        cash -= units * opens[symbol]
    return cash


def simulate(d: Definition, bars: Bars, published: list[date], next_after: date,
             base: tier0_core.CorePolicy) -> Result:
    """``published``: ascending sessions from the one before ``START`` (the initial build is decided at its close)
    to the last session whose close is published; ``next_after``: the session after the last one."""
    policy = _policy(base, d)
    result = Result(d, qty=dict.fromkeys(d.weights, ZERO), cash=CAPITAL)
    sessions = [*published, next_after]
    pending: core_alloc.Plan | None = None
    pending_initial = forced = False
    prev_value = CAPITAL
    for i, day in enumerate(published):
        for symbol in d.weights:
            if day not in bars.get(symbol, {}):
                raise ShadowError(f"no bar for {symbol} on {day}")
        row = Day(day, ZERO, result.cash)
        if pending is not None:
            opens = {s: bars[s][day][0] for s in d.weights}
            at_open = result.cash + sum((q * opens[s] for s, q in result.qty.items()), ZERO)
            if (not pending_initial and pending.buys
                    and (at_open / prev_value - 1) * 100 <= tier0.CIRCUIT_BREAKER_DAILY_PNL_PCT):
                row.deferred = forced = True
            else:
                result.cash = _fill(pending, opens, result.qty, result.cash, policy)
                row.rebalanced = True
            pending = None
        closes = {s: bars[s][day][1] for s in d.weights}
        row.value = result.cash + sum((q * closes[s] for s, q in result.qty.items()), ZERO)
        row.cash = result.cash
        result.days.append(row)
        prev_value = row.value
        snap = core_paper.Snapshot(day, sessions[i + 1], dict(result.qty), result.cash, closes, forced=forced,
                                   initial=not any(result.qty.values()))
        forced = False
        pending = core_paper.decide(policy, snap)
        pending_initial = snap.initial
    result.pending = pending
    return result


def build(bars: Bars, published: list[date], next_after: date,
          base: tier0_core.CorePolicy) -> dict[str, Result]:
    return {d.name: simulate(d, bars, published, next_after, base) for d in DEFINITIONS}


# --- the report ----------------------------------------------------------------------------------------------

def _usd(x: Decimal) -> str:
    return f"${x:,.2f}"


def _pct(x: Decimal) -> str:
    return f"{x * 100:+.2f}%"


def _next(plan: core_alloc.Plan | None) -> str:
    return "nothing" if plan is None else f"{plan.reason}: {len(plan.sells)} sells, {len(plan.buys)} buys"


def render(results: dict[str, Result], *, now: datetime, account_equity: Decimal | None = None) -> str:
    any_result = next(iter(results.values()))
    sessions = len(any_result.days) - 1
    last = any_result.days[-1].date
    lines = ["# Long-term core: shadow portfolios", "",
             f"Generated {now:%Y-%m-%d %H:%M} UTC · registration sha256 `{registration_sha256()[:12]}…` · "
             f"last published close {last}", "",
             "> Forward simulation of a registered comparison set. Not financial advice: it reports what each "
             "rule did; it does not rank, recommend or judge returns.", "",
             "## Reading rules", "", READING_RULES, "",
             "## Status", ""]
    left = max(EVIDENCE_SESSIONS - sessions, 0)
    lines.append(f"- {sessions} sessions since the start ({START}); "
                 + (f"**not evidence**: {left} more sessions until the 252-session reading (S5)." if left
                    else "the 252-session reading is reached: this file needs its new version (S5)."))
    lines += ["", "## The portfolios (descriptive, S5)", "",
              "| Portfolio | Value | Since start | Worst fall | Rebalances | Deferred | At the next open |",
              "|---|---|---|---|---|---|---|"]
    for r in results.values():
        lines.append(f"| {r.definition.label} | {_usd(r.value)} | {_pct(r.value / CAPITAL - 1)} | "
                     f"{_pct(r.worst_fall)} | {r.rebalances} | {r.deferrals} | {_next(r.pending)} |")
    if account_equity is not None:
        twin = results["twin"].value
        gap = account_equity - twin
        lines += ["", "## The twin against the real account (S6)", "",
                  f"- Real account equity {_usd(account_equity)}; twin {_usd(twin)}; gap {_usd(gap)} "
                  f"({_pct(gap / twin)}). Compare only after the close: equity read earlier in the session includes "
                  "that day's moves."]
    return "\n".join(lines) + "\n"


def write_days(results: dict[str, Result], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "shadow-days.csv"
    names = list(results)
    dates = [d.date for d in results[names[0]].days]
    with path.open("w", newline="") as fh:
        out = csv.writer(fh)
        out.writerow(["date", *names])
        for i, day in enumerate(dates):
            out.writerow([day.isoformat(), *(str(results[n].days[i].value.quantize(Decimal("0.01")))
                                              for n in names)])
    return path


# --- running it ----------------------------------------------------------------------------------------------

async def run(broker: Any, now: datetime, *, with_account: bool) -> tuple[dict[str, Result], Decimal | None]:
    calendar = await broker.calendar((START - timedelta(days=10)).isoformat(),
                                     (now.date() + timedelta(days=12)).isoformat())
    sessions = sorted(date.fromisoformat(s["date"]) for s in calendar)
    last = core_paper.last_published(sessions, now)
    if last is None or last < START:
        raise ShadowError(f"the shadow portfolios start at the open of {START}; no later close is published yet")
    decision = max(d for d in sessions if d < START)
    published = [d for d in sessions if decision <= d <= last]
    later = [d for d in sessions if d > last]
    if not later:
        raise ShadowError("the calendar does not reach the next session")
    raw = await broker.daily_bars(list(SYMBOLS), decision.isoformat(), last.isoformat(), adjustment="all")
    base = tier0_core.load_policy(DEFAULT_POLICY)
    results = build(parse_bars(raw), published, min(later), base)
    equity = Decimal(str((await broker.account())["equity"])) if with_account else None
    return results, equity


async def main_async(args: argparse.Namespace) -> int:
    from risk_router.alpaca_async import AsyncAlpaca
    broker = AsyncAlpaca(os.environ["CORE_ALPACA_KEY_ID"], os.environ["CORE_ALPACA_SECRET_KEY"])
    try:
        now = datetime.now(UTC)
        results, equity = await run(broker, now, with_account=args.with_account)
    except ShadowError as exc:
        logger.error("not updated: %s", exc)
        return 1
    finally:
        await broker.aclose()
    report = render(results, now=now, account_equity=equity)
    days = write_days(results, args.state_dir)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report)
    print(report)
    logger.info("daily values written to %s", days)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, help="also write the report to this file")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--with-account", action="store_true", help="also read the core account's equity (read-only)")
    return asyncio.run(main_async(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
