"""The shadow portfolios: the registered set matches the owner's policy, the simulation runs the live decision
path, the breaker and the quarter-end rebalance behave, and bad inputs stop the run instead of guessing."""

from __future__ import annotations

import csv
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import core_alloc
import tier0_core
from backtest import shadow

D = Decimal
ROOT = Path(__file__).resolve().parents[1]
POLICY_FILE = ROOT / "policy" / "core.toml"


def weekdays(first: date, last: date) -> list[date]:
    out, day = [], first
    while day <= last:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def make_bars(sessions: list[date], price: Any) -> shadow.Bars:
    return {s: {d: (D(price(s, d)), D(price(s, d))) for d in sessions} for s in shadow.SYMBOLS}


def flat(symbol: str, day: date) -> str:
    return "100"


def vti_up(symbol: str, day: date) -> str:
    return "110" if symbol == "VTI" and day >= date(2026, 11, 2) else "100"


BASE = tier0_core.load_policy(POLICY_FILE)
SESSIONS = [d for d in weekdays(date(2026, 10, 9), date(2027, 1, 4)) if d != date(2027, 1, 1)]  # New Year's Day
NEXT_AFTER = date(2027, 1, 5)


def test_the_twin_is_the_owners_policy() -> None:
    twin = next(d for d in shadow.DEFINITIONS if d.name == "twin")
    assert twin.weights == BASE.mix
    assert twin.rule == BASE.rule.name
    assert twin.buffer_symbol == BASE.buffer_symbol
    assert shadow.START == date(2026, 10, 12)


def test_every_definition_sums_to_one_and_uses_registered_symbols() -> None:
    for d in shadow.DEFINITIONS:
        assert sum(d.weights.values(), D(0)) == 1
        assert set(d.weights) <= tier0_core.ALLOWED_SYMBOLS
        assert d.rule in core_alloc.RULES


def test_registration_is_pinned() -> None:
    """Changing the set, the start, the capital or a reading rule changes this hash: say so in a new version of
    backtest/shadow.py and update the pin on purpose, never by accident."""
    assert shadow.registration_sha256() == "585bd5c889b46868231fc4463b95e768e53a16ba507887224a229b8193e414d2"


def test_initial_build_invests_the_capital_less_the_reserve() -> None:
    published = [d for d in SESSIONS if d <= date(2026, 10, 14)]
    results = shadow.build(make_bars(SESSIONS, flat), published, date(2026, 10, 15), BASE)
    for r in results.values():
        assert r.days[0].date == date(2026, 10, 9) and r.days[0].value == shadow.CAPITAL
        assert r.rebalances == 1
        assert shadow.CAPITAL - D(2) <= r.value <= shadow.CAPITAL
        assert r.cash <= BASE.cash_reserve_usd + D(1)
        assert not r.deferrals


def test_buy_and_hold_never_rebalances_again() -> None:
    results = shadow.build(make_bars(SESSIONS, vti_up), SESSIONS, NEXT_AFTER, BASE)
    assert results["m1"].rebalances == 1
    assert results["m1"].pending is None


def test_quarter_end_rebalances_after_drift() -> None:
    results = shadow.build(make_bars(SESSIONS, vti_up), SESSIONS, NEXT_AFTER, BASE)
    for name in ("twin", "m3", "m2"):
        r = results[name]
        assert r.rebalances == 2, name
        rebalanced_on = [d.date for d in r.days if d.rebalanced]
        assert rebalanced_on[-1] == date(2027, 1, 4)


def test_loss_breaker_defers_a_plan_with_buys() -> None:
    def crash(symbol: str, day: date) -> str:
        base = vti_up(symbol, day)
        return str(D(base) * D("0.94")) if day >= date(2027, 1, 4) else base

    results = shadow.build(make_bars(SESSIONS, crash), SESSIONS, NEXT_AFTER, BASE)
    for name in ("twin", "m3", "m2"):
        r = results[name]
        assert r.days[-1].date == date(2027, 1, 4) and r.days[-1].deferred, name
        assert r.rebalances == 1, name
        assert r.pending is not None, name   # re-decided at the same close, forced


def test_a_missing_bar_stops_the_run() -> None:
    bars = make_bars(SESSIONS, flat)
    del bars["VTI"][date(2026, 10, 20)]
    with pytest.raises(shadow.ShadowError, match="no bar for VTI"):
        shadow.build(bars, SESSIONS, NEXT_AFTER, BASE)


def test_parse_bars_reads_open_and_close_by_date() -> None:
    raw = {"VTI": [{"t": "2026-10-12T04:00:00Z", "o": 250.5, "c": 251.25}]}
    assert shadow.parse_bars(raw) == {"VTI": {date(2026, 10, 12): (D("250.5"), D("251.25"))}}


def test_render_says_not_evidence_and_checks_the_twin() -> None:
    results = shadow.build(make_bars(SESSIONS, vti_up), SESSIONS, NEXT_AFTER, BASE)
    text = shadow.render(results, now=datetime(2027, 1, 5, 1, tzinfo=UTC), account_equity=D("10000.00"))
    assert "not evidence" in text
    assert shadow.registration_sha256()[:12] in text
    assert "The twin against the real account" in text


def test_write_days_has_one_row_per_session(tmp_path: Path) -> None:
    results = shadow.build(make_bars(SESSIONS, flat), SESSIONS, NEXT_AFTER, BASE)
    path = shadow.write_days(results, tmp_path)
    rows = list(csv.reader(path.open()))
    assert rows[0] == ["date", "twin", "m3", "m2", "m1"]
    assert len(rows) == len(SESSIONS) + 1


class FakeBroker:
    def __init__(self, sessions: list[date], bars: shadow.Bars) -> None:
        self.sessions, self.bars, self.adjustments = sessions, bars, []

    async def calendar(self, start: str, end: str) -> list[dict[str, Any]]:
        return [{"date": d.isoformat()} for d in self.sessions if start <= d.isoformat() <= end]

    async def daily_bars(self, symbols: list[str], start: str, end: str, *, adjustment: str = "raw") -> Any:
        self.adjustments.append(adjustment)
        return {s: [{"t": f"{d.isoformat()}T04:00:00Z", "o": str(o), "c": str(c)}
                    for d, (o, c) in self.bars[s].items() if start <= d.isoformat() <= end] for s in symbols}

    async def account(self) -> dict[str, Any]:
        return {"equity": "10003.10"}


@pytest.mark.asyncio
async def test_run_asks_for_total_return_bars_and_reads_the_account() -> None:
    broker = FakeBroker(SESSIONS + [NEXT_AFTER], make_bars(SESSIONS + [NEXT_AFTER], flat))
    now = datetime(2027, 1, 4, 23, 0, tzinfo=UTC)   # 18:00 New York: the close is published
    results, equity = await shadow.run(broker, now, with_account=True)
    assert set(broker.adjustments) == {"all"}
    assert equity == D("10003.10")
    assert results["twin"].days[-1].date == date(2027, 1, 4)


@pytest.mark.asyncio
async def test_run_refuses_before_the_start() -> None:
    broker = FakeBroker(SESSIONS, make_bars(SESSIONS, flat))
    with pytest.raises(shadow.ShadowError, match="start at the open"):
        await shadow.run(broker, datetime(2026, 10, 12, 12, 0, tzinfo=UTC), with_account=False)
