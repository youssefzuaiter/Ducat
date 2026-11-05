"""P6 and P7 of the paper report: fills sent before the open and a rebalance's buys (sent after its sells) are
never pooled, and each after-sells fill must be a price that traded in its own minute."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from test_core_paper import policy
from test_core_report import a_build_and_a_rebalance, entries_of, restore_the_calendar, status_of  # noqa: F401

from backtest import paper_report as pr
from risk_router.alpaca_async import AlpacaError, AsyncAlpaca

D = Decimal
MINUTE = datetime(2027, 1, 4, 14, 33, tzinfo=UTC)          # 09:33 New York


def after_fill(price: str = "100", client_id: str = "core-x-VTI-b") -> pr.Fill:
    return pr.Fill("p", date(2027, 1, 4), "VTI", "buy", D(1), D(price), client_id, pr.AFTER_SELLS)


def rng(low: str = "99.90", high: str = "100.10") -> pr.MinuteRange:
    return pr.MinuteRange(MINUTE, D(low), D(high))


# --- P6: which fills are which ------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_rebalances_buys_are_after_the_sells_and_everything_else_is_pre_open(tmp_path: Any) -> None:
    await a_build_and_a_rebalance(tmp_path)
    fills = pr.extract_fills(entries_of(tmp_path))
    by = {(f.plan_id, f.side): f.mode for f in fills}
    assert len(fills) == 12 and all(f.client_id.startswith("core-") for f in fills)
    plans = {f.plan_id for f in fills}
    build, rebalance = sorted(plans, key=lambda p: min(f.session for f in fills if f.plan_id == p))
    assert by[(build, "buy")] == pr.PRE_OPEN, "a build has no sells: its buys go in before the open"
    assert by[(rebalance, "sell")] == pr.PRE_OPEN
    assert by[(rebalance, "buy")] == pr.AFTER_SELLS


@pytest.mark.asyncio
async def test_the_report_keeps_the_two_groups_apart_and_places_only_the_pre_open_one(tmp_path: Any) -> None:
    router, _ = await a_build_and_a_rebalance(tmp_path)
    entries = entries_of(tmp_path)
    fills = pr.extract_fills(entries)
    # Every after-sells fill is 80 bp from the open: far beyond the 25 bp fault line, and it must not be called one.
    opens = {(f.symbol, f.session): (f.price / D("1.0003") if f.mode == pr.PRE_OPEN else f.price / D("1.0080"))
             for f in fills}
    report = pr.build_report(entries, policy(), journal_status=status_of(router), opens=opens, generated=datetime(2027, 1, 5, tzinfo=UTC))
    assert report.execution is not None and report.execution.outliers == []
    assert report.after_sells is not None and report.after_sells.mean_vs_open_bps == pytest.approx(80, abs=0.5)
    text = pr.render(report)
    assert "Rebalance buys, placed after the sells (P6, P7)" in text
    assert "not placed among the registered levels" in text
    assert "Fills more than 25 bp from the open" not in text


# --- P7: a price that traded -----------------------------------------------------------------------------------------------

def test_a_fill_inside_its_minutes_range_passes_and_the_tolerance_is_five_basis_points() -> None:
    assert pr.inside_range(D("100.00"), rng())
    assert pr.inside_range(D("100.10") * D("1.0005"), rng()), "exactly 5 bp above the high is still inside"
    assert not pr.inside_range(D("100.10") * D("1.00051"), rng())
    assert pr.inside_range(D("99.90") * D("0.9995"), rng()) and not pr.inside_range(D("99.90") * D("0.99949"), rng())


def test_outside_unchecked_and_inside_are_counted_apart_and_unchecked_never_passes() -> None:
    fills = [after_fill("100", "a"), after_fill("101", "b"), after_fill("100", "c"), after_fill("100", "d")]
    ranges: dict[str, pr.MinuteRange | str] = {"a": rng(), "b": rng(), "c": "the order has no fill time"}
    stats = pr.analyse_after_sells(fills, {("VTI", date(2027, 1, 4)): D("100")}, ranges)
    assert stats is not None and stats.n == 4 and stats.inside == 1
    assert [f.client_id for f, _ in stats.outside] == ["b"]
    assert {f.client_id: why for f, why in stats.unchecked} == {"c": "the order has no fill time", "d": "no lookup was made"}


def test_the_rendered_report_lists_faults_and_unchecked_fills() -> None:
    report = pr.Report(datetime(2027, 1, 5, tzinfo=UTC), "a" * 64, {"ok": True})
    report.after_sells = pr.analyse_after_sells([after_fill("101", "b"), after_fill("100", "c")], {},
                                                {"b": rng(), "c": "the order has no fill time"})
    text = pr.render(report)
    assert "0 of 2 lie inside" in text and "1 outside" in text and "1 could not be checked" in text
    assert "Not checked (never counted as passing)" in text and "plumbing fault to look at" in text


class Lookup:
    """An order lookup and a minute-bar read, each answering from a table."""

    def __init__(self, orders: dict[str, Any], bars: dict[str, list[dict[str, Any]]]) -> None:
        self.orders, self.bars = orders, bars
        self.asked: list[tuple[str, str, str]] = []

    async def order_by_client_id(self, cid: str) -> Any:
        if isinstance(self.orders.get(cid), Exception):
            raise self.orders[cid]
        return self.orders.get(cid)

    async def minute_bars(self, symbol: str, start: str, end: str) -> list[dict[str, Any]]:
        self.asked.append((symbol, start, end))
        return self.bars.get(symbol, [])


@pytest.mark.asyncio
async def test_the_minute_is_the_one_the_order_filled_in() -> None:
    broker = Lookup({"a": {"filled_at": "2027-01-04T14:33:21.987654Z"}},
                    {"VTI": [{"t": "2027-01-04T14:32:00Z", "l": "90", "h": "91"},
                             {"t": "2027-01-04T14:33:00Z", "l": "99.95", "h": "100.05"},
                             {"t": "2027-01-04T14:34:00Z", "l": "70", "h": "71"}]})
    out = await pr.fetch_minute_ranges(broker, [after_fill("100", "a")])
    assert out == {"a": pr.MinuteRange(MINUTE, D("99.95"), D("100.05"))}
    assert broker.asked == [("VTI", "2027-01-04T14:33:00+00:00", "2027-01-04T14:34:00+00:00")]


@pytest.mark.asyncio
async def test_every_way_the_lookup_can_fail_becomes_a_reason_and_none_passes() -> None:
    broker = Lookup({"missing": None, "nostamp": {"filled_at": None}, "nobar": {"filled_at": "2027-01-04T14:33:21Z"},
                     "boom": AlpacaError("HTTP 500")}, {"VTI": []})
    out = await pr.fetch_minute_ranges(broker, [after_fill("100", c) for c in ("missing", "nostamp", "nobar", "boom")])
    assert out["missing"] == "the order was not found at Alpaca" and out["nostamp"] == "the order has no fill time"
    assert out["nobar"].startswith("no one-minute bar") and out["boom"] == "could not be read: AlpacaError"
    assert all(isinstance(v, str) for v in out.values())


@pytest.mark.asyncio
async def test_pre_open_fills_are_not_looked_up() -> None:
    broker = Lookup({}, {})
    pre = pr.Fill("p", date(2027, 1, 4), "VTI", "sell", D(1), D(100), "core-x-VTI-s", pr.PRE_OPEN)
    assert await pr.fetch_minute_ranges(broker, [pre]) == {} and broker.asked == []


@pytest.mark.asyncio
async def test_minute_bars_ask_alpaca_for_raw_one_minute_sip_bars() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"bars": {"VTI": [{"t": "2027-01-04T14:33:00Z", "l": 99.95, "h": 100.05}]}})

    alpaca = AsyncAlpaca("k", "s", transport=httpx.MockTransport(handler))
    try:
        bars = await alpaca.minute_bars("VTI", "2027-01-04T14:33:00+00:00", "2027-01-04T14:34:00+00:00")
    finally:
        await alpaca.aclose()
    params = dict(seen[0].url.params)
    assert params["timeframe"] == "1Min" and params["feed"] == "sip" and params["adjustment"] == "raw"
    assert params["symbols"] == "VTI" and bars[0]["l"] == D("99.95")


# --- the registration ------------------------------------------------------------------------------------------------------------

def test_p6_and_p7_are_in_the_rules_and_the_rules_are_pinned() -> None:
    """Changing any reading rule changes this hash: say so in a new dated version of backtest/paper_report.py and
    update the pin on purpose, never by accident."""
    rules = pr.READING_RULES
    assert "written on 2026-10-03, before any paper order existed" in rules
    assert "P6 and P7 were added on 2026-11-05, before any rebalance fill existed" in rules
    assert "P6 Two kinds of fill, never pooled" in rules and "P7 After-sells fills must be prices that traded" in rules
    assert hashlib.sha256(rules.encode()).hexdigest() == (
        "22497926acc46fa67663e3443c8610c05a8d347427ec022915acb5b280a21a74")
