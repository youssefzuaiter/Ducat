"""The paper period's report: the out-of-sample test the long-term core's design promised.

The registered backtest is in-sample by construction: it froze its grid and its reading rules before any run,
but every number came from one 10.5-year history (docs/long-term-core-design.md §4: "the paper period is the
out-of-sample test"). The paper account is the only data that design never saw, so this turns its journal into
evidence, under rules **written before any paper order existed** (2026-10-03). They are printed at the top of
every report, and changing one means saying so in a new version of this file, not quietly in a number.

    ./.venv/bin/python -m backtest.paper_report                 # the whole journal, with Alpaca for opens and holdings
    ./.venv/bin/python -m backtest.paper_report --since 2026-10-01 --out report.md
    ./.venv/bin/python -m backtest.paper_report --offline       # the journal alone: behaviour and integrity only

Read-only: it reads the journal, the state files and Alpaca; it places and changes nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import core_alloc
import core_paper
import tier0_core
from backtest.costs import BPS, CORE_LEVELS
from risk_router.core_alerts import ALERT_EVENTS
from risk_router.core_gatekeeper import CoreStore, Journal

DEFAULT_POLICY: Final[Path] = Path(__file__).resolve().parent.parent / "policy" / "core.toml"
LEVEL_NAMES: Final[tuple[str, ...]] = ("optimistic", "central", "pessimistic")
#: Fewer fills than this cannot tell the registered cost levels apart: their one-way costs differ by a few basis
#: points, and one fill's noise is of the same size. The report then states the number and refuses to place it.
MIN_FILLS_FOR_PLACEMENT: Final[int] = 30
#: A fill this far from the session's open is outside anything the registered model allows (twice its widest
#: one-way cost, VXUS at the pessimistic level: 8.3 + 5 bp). It is listed as a plumbing fault to look at.
OUTLIER_BPS: Final[Decimal] = Decimal(25)
Z95: Final[float] = 1.96
#: Two kinds of fill (P6). Orders sent before the open fill at the opening print; a rebalance's buys are sent only
#: after its sells have filled, so they fill minutes into the session.
PRE_OPEN: Final[str] = "pre-open limit"
AFTER_SELLS: Final[str] = "after the sells"
#: P7: how far outside its minute's high-low range a fill may sit and still count as a price that traded. Fixed on
#: 2026-11-05 before any rebalance fill existed; not tuned to a fill.
RANGE_TOLERANCE_BPS: Final[Decimal] = Decimal(5)
TERMINAL_EVENTS: Final[dict[str, str]] = {"plan_done": "done", "plan_abandoned": "abandoned", "plan_expired": "expired",
                                          "plan_deferred": "deferred", "plan_halted": "halted"}

READING_RULES: Final[str] = """\
These rules were written on 2026-10-03, before any paper order existed.

* **P1 Integrity first.** If the journal's hash chain fails, or its last entry disagrees with the state file,
  nothing below is evidence and the report says so instead of printing it. Exactly one policy hash may appear in
  the period, and it must be the committed policy's: a changed policy starts a new period.
* **P2 Behaviour is counted, not interpreted.** Plans are listed with how they ended. Any `plan_mismatch`,
  `order_rejected`, abandoned or expired plan is an incident to look at: each means a bug or a changed
  environment, not market luck.
* **P3 Cost is a plumbing check.** Alpaca's paper fills come from its own simulator, not the market, so the
  observed cost shows whether orders fill at the open and inside the collar, not what the real market would
  charge. With fewer than 30 fills the report gives the number and refuses to place it among the registered cost
  levels; with 30 or more it places the mean (95% interval) against them. A fill more than 25 bp from the open is
  listed as a fault.
* **P4 Materiality.** The registered backtest puts the whole cost of running this policy at about 1 bp a year at
  the central level (0.5 to 2.3 across the three levels; run x-20261001-121225-9c4ebec5, section 3, M3
  quarterly). No plausible cost result can move a conclusion about the strategy, so differences between cost
  levels are never reported as evidence for or against it. (This is the materiality threshold the registered
  rule R1 lacked, applied only to evidence gathered from now on.)
* **P5 Nothing here predicts the future.** A few quarters of a policy that rebalances four times a year is a
  test of the machinery, not of the portfolio's returns. The report does not rank, recommend or judge returns.

P6 and P7 were added on 2026-11-05, before any rebalance fill existed (the only fills then were the six pre-open
buys of the 2026-10-12 build). P1 to P5 are unchanged, and P3 applies to the pre-open group of P6.

* **P6 Two kinds of fill, never pooled.** Orders sent before the open (the initial build's buys, every plan's sells)
  fill at the opening print, so P3 applies to them as written. A rebalance's buys are sent only after its sells have
  filled, as marketable limits at the ask plus 0.5%, so they fill minutes into the session, and their distance from
  the open includes those minutes of market movement, which is not a cost. They are reported separately, as a plain
  number with that caveat, are never placed among the registered cost levels, and the 25 bp fault rule does not
  apply to them.
* **P7 After-sells fills must be prices that traded.** Each is looked up (its order's fill time, then Alpaca's
  one-minute SIP bar for that minute). Its price must lie inside the bar's low-high range widened by 5 bp each way,
  a tolerance fixed now, below the 25 bp fault line and not tuned to any fill. A fill outside it is listed as a
  plumbing fault to look at. A fill that could not be checked (no fill time, no bar, a failed lookup) is counted as
  unchecked and is never reported as passing. P7 checks that the simulator's fills are prices that existed; it
  says nothing about what the real market would charge.\
"""


@dataclass
class PlanRow:
    plan_id: str
    kind: str
    decided_on: str
    execute_on: str
    redecision: bool
    approval_wait: timedelta | None = None
    outcome: str = "open"
    orders: int = 0
    filled: int = 0
    traded_usd: Decimal = Decimal(0)
    reason: str | None = None


@dataclass(frozen=True)
class Incident:
    at: str
    event: str
    detail: str


@dataclass(frozen=True)
class Fill:
    plan_id: str
    session: date
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    client_id: str = ""
    mode: str = PRE_OPEN                            # P6: PRE_OPEN or AFTER_SELLS


@dataclass(frozen=True)
class MinuteRange:
    """The high-low range of the one-minute bar an order filled in (P7)."""
    minute: datetime
    low: Decimal
    high: Decimal


@dataclass(frozen=True)
class AfterSellsStats:
    """A rebalance's buys, placed after its sells (P6, P7). Descriptive: no placement, no fault rule."""
    n: int
    unpriced: int                                   # fills with no session open to compare against
    mean_vs_open_bps: float | None                  # includes the minutes between the open and the fill
    inside: int                                     # P7: a price that traded
    outside: list[tuple[Fill, MinuteRange]]         # P7: a plumbing fault
    unchecked: list[tuple[Fill, str]]               # P7: could not be checked; never counted as passing
    traded_usd: Decimal


@dataclass(frozen=True)
class ExecutionStats:
    n: int
    unpriced: int                                   # fills with no session open to compare against
    mean_bps: float | None
    ci_bps: tuple[float, float] | None
    by_side: dict[str, tuple[int, float]]
    modelled_bps: dict[str, float]                  # level -> the mean one-way cost the model charges these fills
    placement: str
    outliers: list[tuple[Fill, float]]
    traded_usd: Decimal


@dataclass(frozen=True)
class StandRow:
    symbol: str
    target: Decimal
    now: Decimal

    @property
    def gap(self) -> Decimal:
        """Signed: positive means over-weight. (Not ``drift``: that name is ``core_alloc``'s, and check C12 forbids a
        second definition of it anywhere else; the portfolio-level figure below calls the shared one.)"""
        return self.now - self.target


@dataclass
class Report:
    generated: datetime
    policy_sha: str
    journal: dict[str, Any]
    entries: int = 0
    first: str | None = None
    last: str | None = None
    shas_seen: list[str] = field(default_factory=list)
    plans: list[PlanRow] = field(default_factory=list)
    incidents: list[Incident] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)
    execution: ExecutionStats | None = None
    after_sells: AfterSellsStats | None = None
    standing: list[StandRow] | None = None
    cash_share: Decimal | None = None
    standing_session: date | None = None
    invalid: str | None = None


# --- reading the journal ----------------------------------------------------------------------------------------

def parse_entries(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _when(entry: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(entry["at"])


def build_plans(entries: list[dict[str, Any]]) -> list[PlanRow]:
    rows: dict[str, PlanRow] = {}
    accepted_at: dict[str, datetime] = {}
    for e in entries:
        event, pid = e["event"], e.get("plan_id")
        if event == "plan_accepted" and pid:
            plan = e.get("plan") or {}
            rows[pid] = PlanRow(pid, str(e.get("kind")), str(plan.get("decided_on", "")), str(e.get("execute_on", "")),
                                bool(e.get("redecision")))
            accepted_at[pid] = _when(e)
        elif event == "plan_approved" and pid in rows:
            rows[pid].approval_wait = _when(e) - accepted_at[pid]
        elif event == "plan_superseded" and pid in rows:
            rows[pid].outcome = "superseded"
        elif event in TERMINAL_EVENTS and pid in rows:
            row, orders = rows[pid], e.get("orders") or {}
            row.outcome = TERMINAL_EVENTS[event]
            row.reason = e.get("reason")
            row.orders = len(orders)
            row.filled = sum(1 for o in orders.values() if o.get("status") == "filled")
            if event == "plan_done" and row.filled < row.orders:
                row.outcome = "done, with unfilled orders"
            for o in orders.values():
                qty, price = Decimal(str(o.get("filled_qty") or 0)), Decimal(str(o.get("filled_avg_price") or 0))
                row.traded_usd += qty * price
    return list(rows.values())


def build_incidents(entries: list[dict[str, Any]]) -> list[Incident]:
    out = []
    for e in entries:
        if e["event"] in ALERT_EVENTS:
            detail = e.get("reason") or e.get("error") or e.get("code") or e.get("problems") or e.get("detail") or ""
            out.append(Incident(e["at"][:19], e["event"], str(detail)[:160]))
    return out


def extract_fills(entries: list[dict[str, Any]]) -> list[Fill]:
    accepted = {e["plan_id"]: e for e in entries if e["event"] == "plan_accepted"}
    fills = []
    for e in entries:
        if e["event"] not in ("plan_done", "plan_abandoned") or e["plan_id"] not in accepted:
            continue
        plan = accepted[e["plan_id"]]
        has_sells = bool((plan.get("plan") or {}).get("sells"))
        for client_id, o in (e.get("orders") or {}).items():
            qty, price = Decimal(str(o.get("filled_qty") or 0)), Decimal(str(o.get("filled_avg_price") or 0))
            if qty > 0 and price > 0:
                # Buys wait for the sells only in a plan that has sells; otherwise they go in before the open (P6).
                mode = AFTER_SELLS if o["side"] == "buy" and has_sells else PRE_OPEN
                fills.append(Fill(e["plan_id"], date.fromisoformat(plan["execute_on"]), o["symbol"], o["side"],
                                  qty, price, client_id, mode))
    return fills


# --- execution quality ------------------------------------------------------------------------------------------

def _modelled_bps(level: str, symbol: str) -> float:
    cost = CORE_LEVELS[level]
    return float((cost.half_spread(symbol) + cost.slippage_bps * BPS) / BPS)


def placement(n: int, mean: float, ci: tuple[float, float], modelled: dict[str, float]) -> str:
    """Where the observed mean sits among the registered levels, or why it cannot be placed."""
    if n < MIN_FILLS_FOR_PLACEMENT:
        return (f"{n} fills is too few to say which registered cost level they resemble "
                f"(it takes {MIN_FILLS_FOR_PLACEMENT}); the number above is a plumbing check only")
    opt, cen, pes = (modelled[name] for name in LEVEL_NAMES)
    if ci[1] < opt:
        where = "better than the optimistic level"
    elif ci[0] > pes:
        where = "worse than the pessimistic level: investigate"
    elif mean <= opt:
        where = "at or near the optimistic level"
    elif mean <= cen:
        where = "between the optimistic and central levels"
    elif mean <= pes:
        where = "between the central and pessimistic levels"
    else:
        where = "beyond the pessimistic level, though its interval reaches back inside"
    return f"with {n} fills the mean sits {where}"


def analyse_execution(fills: list[Fill], opens: dict[tuple[str, date], Decimal]) -> ExecutionStats | None:
    if not fills:
        return None
    priced = [(f, opens[(f.symbol, f.session)]) for f in fills if (f.symbol, f.session) in opens]
    shortfalls = [(f, float(core_paper.shortfall_bps(f.side, f.price, o, Decimal(0), Decimal(0))["vs_open_bps"]))
                  for f, o in priced]
    values = [v for _, v in shortfalls]
    n = len(values)
    mean = statistics.fmean(values) if n else None
    ci = None
    if n >= 2:
        half = Z95 * statistics.stdev(values) / n ** 0.5
        ci = (mean - half, mean + half)  # type: ignore[operator]
    by_side: dict[str, tuple[int, float]] = {}
    for side in ("buy", "sell"):
        side_values = [v for f, v in shortfalls if f.side == side]
        if side_values:
            by_side[side] = (len(side_values), statistics.fmean(side_values))
    modelled = {name: statistics.fmean(_modelled_bps(name, f.symbol) for f, _ in priced) for name in LEVEL_NAMES} \
        if priced else {}
    where = placement(n, mean, ci, modelled) if n >= 2 and ci and mean is not None else \
        (f"{n} fill(s) with a session open: nothing to compare yet" if n else "no fill has a session open to compare against")
    outliers = [(f, v) for f, v in shortfalls if abs(Decimal(str(v))) > OUTLIER_BPS]
    return ExecutionStats(n, len(fills) - n, mean, ci, by_side, modelled, where, outliers,
                          sum((f.qty * f.price for f in fills), Decimal(0)))


def inside_range(price: Decimal, rng: MinuteRange) -> bool:
    """P7: ``price`` lies in the bar's low-high range widened by the registered tolerance."""
    tol = RANGE_TOLERANCE_BPS * BPS
    return rng.low * (1 - tol) <= price <= rng.high * (1 + tol)


def analyse_after_sells(fills: list[Fill], opens: dict[tuple[str, date], Decimal],
                        ranges: dict[str, MinuteRange | str]) -> AfterSellsStats | None:
    """``ranges`` maps a fill's client order id to its minute's range, or to the reason it could not be read."""
    if not fills:
        return None
    priced = [(f, opens[(f.symbol, f.session)]) for f in fills if (f.symbol, f.session) in opens]
    values = [float(core_paper.shortfall_bps(f.side, f.price, o, Decimal(0), Decimal(0))["vs_open_bps"]) for f, o in priced]
    inside, outside, unchecked = 0, [], []
    for f in fills:
        got = ranges.get(f.client_id, "no lookup was made")
        if isinstance(got, str):
            unchecked.append((f, got))
        elif inside_range(f.price, got):
            inside += 1
        else:
            outside.append((f, got))
    return AfterSellsStats(len(fills), len(fills) - len(priced), statistics.fmean(values) if values else None, inside,
                           outside, unchecked, sum((f.qty * f.price for f in fills), Decimal(0)))


# --- where the portfolio stands -----------------------------------------------------------------------------------

def standing(policy: tier0_core.CorePolicy, snap: core_paper.Snapshot) -> tuple[list[StandRow], Decimal]:
    weights = core_alloc.current_weights(snap.qty, snap.closes, snap.cash)
    value = snap.cash + sum((q * snap.closes[s] for s, q in snap.qty.items()), Decimal(0))
    return ([StandRow(s, policy.mix[s], weights.get(s, Decimal(0))) for s in sorted(policy.mix)],
            snap.cash / value if value > 0 else Decimal(0))


# --- the report -------------------------------------------------------------------------------------------------------

def windowed_fills(entries: list[dict[str, Any]], since: str | None) -> list[Fill]:
    """Fills of the plans decided on or after ``since``. Filtering plans (not raw entries) keeps the fills of a plan
    that was accepted before the window and closed inside it."""
    if since is None:
        return extract_fills(entries)
    keep = {p.plan_id for p in build_plans(entries) if p.decided_on >= since}
    return [f for f in extract_fills(entries) if f.plan_id in keep]


def build_report(entries: list[dict[str, Any]], policy: tier0_core.CorePolicy, *, journal_status: dict[str, Any],
                 opens: dict[tuple[str, date], Decimal] | None = None, snap: core_paper.Snapshot | None = None,
                 generated: datetime | None = None, since: str | None = None,
                 ranges: dict[str, MinuteRange | str] | None = None) -> Report:
    """``entries`` is the whole journal (its integrity covers all of it); ``since`` narrows what is *reported*."""
    report = Report(generated or datetime.now(UTC), policy.sha256, journal_status, entries=len(entries))
    if not journal_status["ok"]:
        report.invalid = f"the journal failed its integrity check: {journal_status['reason']}"
        return report
    shown = [e for e in entries if since is None or e["at"][:10] >= since]
    report.shas_seen = sorted({e["policy_sha256"] for e in shown})
    if report.shas_seen not in ([], [policy.sha256]):
        report.invalid = (f"the period records {len(report.shas_seen)} policy hash(es) "
                          f"({', '.join(s[:12] for s in report.shas_seen)}) and the committed policy is "
                          f"{policy.sha256[:12]}: a changed policy starts a new period, so report one period at a "
                          f"time with --since the day it took effect")
        return report
    if shown:
        report.first, report.last = shown[0]["at"][:10], shown[-1]["at"][:10]
    report.counts = Counter(e["event"] for e in shown)
    report.plans = [p for p in build_plans(entries) if since is None or p.decided_on >= since]
    report.incidents = build_incidents(shown)
    fills = windowed_fills(entries, since)
    report.execution = analyse_execution([f for f in fills if f.mode == PRE_OPEN], opens or {})
    report.after_sells = analyse_after_sells([f for f in fills if f.mode == AFTER_SELLS], opens or {}, ranges or {})
    if snap is not None:
        report.standing, report.cash_share = standing(policy, snap)
        report.standing_session = snap.session
    return report


def _pct(x: Decimal | float, places: int = 2) -> str:
    return f"{float(x) * 100:.{places}f}%"


def _bps(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.1f} bp"


def render(report: Report) -> str:
    lines = ["# Long-term core: paper period report", "",
             f"Generated {report.generated:%Y-%m-%d %H:%M} UTC · committed policy sha256 `{report.policy_sha[:12]}…`", "",
             "> Paper trading is the out-of-sample test of the registered backtest. Not financial advice: this reports "
             "what the machinery did; it does not rank, recommend or judge returns.", "",
             "## Reading rules", "", READING_RULES, ""]
    if report.invalid:
        lines += ["## INVALID", "", f"**{report.invalid}.** Nothing below this line is evidence, so nothing is printed.", ""]
        return "\n".join(lines)
    lines += ["## 1. Integrity", "",
              f"- Journal: {report.entries} {'entry' if report.entries == 1 else 'entries'} from {report.first or 'n/a'} "
              f"to {report.last or 'n/a'}, hash chain "
              f"intact, ending where the state file says (P1).",
              f"- Policy: {'one hash, the committed policy’s' if report.shas_seen else 'no entries yet'}.", ""]
    if not report.plans:
        lines += ["## 2. Behaviour", "", "No plan has been proposed yet.", ""]
    else:
        lines += ["## 2. Behaviour (P2)", "",
                  "| Plan | Kind | Decided | Executes | How it ended | Orders filled | Traded | Approval wait |",
                  "|---|---|---|---|---|---|---|---|"]
        for p in report.plans:
            wait = "n/a" if p.approval_wait is None else f"{p.approval_wait.total_seconds() / 3600:.1f} h"
            lines.append(f"| `{p.plan_id[:8]}`{' (re-decision)' if p.redecision else ''} | {p.kind} | {p.decided_on} | "
                         f"{p.execute_on} | {p.outcome} | {p.filled}/{p.orders} | ${p.traded_usd:.2f} | {wait} |")
        lines.append("")
        if report.incidents:
            lines += ["Incidents to look at:", ""] + [f"- {i.at} `{i.event}` {i.detail}" for i in report.incidents] + [""]
        else:
            lines += ["No incident: no mismatch, refused order, abandoned or expired plan, breaker block or failing loop.", ""]
    ex = report.execution
    lines += ["## 3. Execution against the registered cost model (P3, P4, P6)", ""]
    if ex is None:
        lines += ["No order has filled yet." if report.after_sells is None else "No pre-open fill yet.", ""]
    else:
        lines += ["Fills sent before the open (the build's buys, every plan's sells), which fill at the opening print:", ""]
        lines += [f"- {ex.n} fills with a session open to compare against" + (f", {ex.unpriced} without one" if ex.unpriced else "")
                  + f"; ${ex.traded_usd:,.2f} traded.",
                  f"- Observed cost against the open, paid per side: mean {_bps(ex.mean_bps)}"
                  + (f", 95% interval {_bps(ex.ci_bps[0])} to {_bps(ex.ci_bps[1])}" if ex.ci_bps else "") + ".",
                  ] + [f"  - {side}: {n} fills, mean {_bps(mean)}" for side, (n, mean) in ex.by_side.items()]
        if ex.modelled_bps:
            lines.append("- The registered model charges these same fills (one-way, half-spread + slippage): "
                         + ", ".join(f"{name} {ex.modelled_bps[name]:.1f} bp" for name in LEVEL_NAMES) + ".")
        lines += [f"- {ex.placement[0].upper() + ex.placement[1:]}.",
                  "- Alpaca's paper fills come from its simulator: this checks the plumbing (fills at the open, inside the "
                  "collar), not the real market.", ""]
        if ex.outliers:
            lines += ["Fills more than 25 bp from the open:", ""] + \
                     [f"- {f.session} {f.side} {f.symbol}: {_bps(v)}" for f, v in ex.outliers] + [""]
    a = report.after_sells
    if a is not None:
        lines += ["### Rebalance buys, placed after the sells (P6, P7)", "",
                  f"- {a.n} fills, ${a.traded_usd:,.2f} traded; mean distance from the open {_bps(a.mean_vs_open_bps)}"
                  + (f" ({a.unpriced} without a session open)" if a.unpriced else "") + ".",
                  "- That distance includes the minutes between the open and the fill, which is market movement and not "
                  "a cost: it is not placed among the registered levels and the 25 bp fault rule does not apply (P6).",
                  f"- Real prices (P7): {a.inside} of {a.n} lie inside their fill minute's range (widened by "
                  f"{RANGE_TOLERANCE_BPS:.0f} bp)"
                  + (f", {len(a.outside)} outside" if a.outside else "")
                  + (f", {len(a.unchecked)} could not be checked" if a.unchecked else "") + ".", ""]
        if a.outside:
            lines += ["Fills outside their minute's range, a plumbing fault to look at:", ""] + \
                     [f"- {f.session} {f.symbol} filled at {f.price}; {r.minute:%H:%M} UTC range {r.low} to {r.high}"
                      for f, r in a.outside] + [""]
        if a.unchecked:
            lines += ["Not checked (never counted as passing):", ""] + \
                     [f"- {f.session} {f.symbol}: {why}" for f, why in a.unchecked] + [""]
    lines += ["## 4. Where the portfolio stands", ""]
    if report.standing is None:
        lines += ["Not read (offline, or Alpaca unavailable).", ""]
    else:
        lines += [f"As of the {report.standing_session} close.", "", "| Instrument | Target | Now | Drift |", "|---|---|---|---|"]
        lines += [f"| {r.symbol} | {_pct(r.target)} | {_pct(r.now)} | {r.gap * 100:+.2f} pt |" for r in report.standing]
        if all(r.now == 0 for r in report.standing):
            lines += ["", f"Nothing is held yet: the initial build has not run. Cash: {_pct(report.cash_share or 0)}.", ""]
        else:
            largest = core_alloc.drift({r.symbol: r.now for r in report.standing},
                                       {r.symbol: r.target for r in report.standing})    # the shared definition (D6)
            lines += ["", f"Cash outside the mix: {_pct(report.cash_share or 0)}. "
                          f"Largest drift: {largest * 100:.2f} pt (the policy rebalances at quarter-end).", ""]
    return "\n".join(lines)


# --- the command line ---------------------------------------------------------------------------------------------------

async def fetch_opens(broker: Any, fills: list[Fill]) -> dict[tuple[str, date], Decimal]:
    """Each fill's session open (the official daily bar's open), one request per session."""
    opens: dict[tuple[str, date], Decimal] = {}
    for session in sorted({f.session for f in fills}):
        symbols = sorted({f.symbol for f in fills if f.session == session})
        bars = await broker.daily_bars(symbols, session.isoformat(), session.isoformat())
        for symbol in symbols:
            day = [b for b in bars.get(symbol, []) if str(b["t"])[:10] == session.isoformat()]
            if day:
                opens[(symbol, session)] = Decimal(str(day[-1]["o"]))
    return opens


def _instant(stamp: Any) -> datetime:
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone(UTC)


async def fetch_minute_ranges(broker: Any, fills: list[Fill]) -> dict[str, MinuteRange | str]:
    """P7: for each after-sells fill, the high-low range of the one-minute bar its order filled in. A fill whose
    range cannot be read maps to the reason, so it is reported as unchecked and can never pass by default."""
    out: dict[str, MinuteRange | str] = {}
    for f in fills:
        if f.mode != AFTER_SELLS:
            continue
        try:
            order = await broker.order_by_client_id(f.client_id)
            if order is None:
                out[f.client_id] = "the order was not found at Alpaca"
                continue
            if not order.get("filled_at"):
                out[f.client_id] = "the order has no fill time"
                continue
            minute = _instant(order["filled_at"]).replace(second=0, microsecond=0)
            bars = await broker.minute_bars(f.symbol, minute.isoformat(), (minute + timedelta(minutes=1)).isoformat())
            bar = next((b for b in bars if _instant(b["t"]) == minute), None)
            if bar is None:
                out[f.client_id] = f"no one-minute bar for {minute:%H:%M} UTC"
                continue
            out[f.client_id] = MinuteRange(minute, Decimal(str(bar["l"])), Decimal(str(bar["h"])))
        except Exception as exc:  # noqa: BLE001 - one unreadable lookup must not take the report down, nor pass
            out[f.client_id] = f"could not be read: {type(exc).__name__}"
    return out


async def main_async(args: argparse.Namespace) -> int:
    policy = tier0_core.load_policy(Path(os.getenv("CORE_POLICY_PATH", str(DEFAULT_POLICY))))
    state_dir = Path(args.state_dir)
    journal_path = state_dir / "core-journal.jsonl"
    if not journal_path.exists():
        print(f"no journal at {journal_path}: nothing has happened yet")
        return 1
    status = Journal(journal_path, CoreStore(state_dir / "core-plan-state.json"), policy.sha256).status(repair=False)
    entries = parse_entries(journal_path.read_text(encoding="utf-8"))
    opens: dict[tuple[str, date], Decimal] = {}
    ranges: dict[str, MinuteRange | str] = {}
    snap = None
    key, secret = os.getenv("CORE_ALPACA_KEY_ID", "").strip(), os.getenv("CORE_ALPACA_SECRET_KEY", "").strip()
    if status["ok"] and not args.offline and key and secret:
        from risk_router.alpaca_async import AlpacaError, AsyncAlpaca
        alpaca = AsyncAlpaca(key, secret)
        try:
            shown_fills = windowed_fills(entries, args.since)
            opens = await fetch_opens(alpaca, shown_fills)
            ranges = await fetch_minute_ranges(alpaca, shown_fills)
            today = datetime.now(UTC).date()
            days = sorted(date.fromisoformat(s["date"]) for s in await alpaca.calendar(
                (today - timedelta(days=14)).isoformat(), today.isoformat()))
            last = core_paper.last_published(days, datetime.now(UTC))
            if last is not None:
                snap = await core_paper.read_snapshot(alpaca, policy, last, forced=False, targets={})
        except (AlpacaError, core_paper.SnapshotError, OSError) as exc:
            print(f"(Alpaca could not be read: {exc}; the report covers the journal only)", file=sys.stderr)
        finally:
            await alpaca.aclose()
    text = render(build_report(entries, policy, journal_status=status, opens=opens, snap=snap, since=args.since,
                                ranges=ranges))
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"written to {args.out}")
    else:
        print(text)
    return 0 if status["ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m backtest.paper_report", description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-dir", default=os.getenv("CORE_STATE_DIR", "core-state"))
    parser.add_argument("--since", help="only entries from this date (YYYY-MM-DD) on")
    parser.add_argument("--offline", action="store_true", help="the journal alone: do not call Alpaca")
    parser.add_argument("--out", help="write the report to this file instead of printing it")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
