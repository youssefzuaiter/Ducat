"""A rehearsal of the first real rebalance: the 2026-12-31 close, executed at the 2027-01-04 open.

Everything the live account has done so far is the initial build. This drives the real ``CoreRouter`` over
the owner's *actual* holdings (the positions the account held at the 2026-11-04 close), with the account's
real journal history entry kept in place (no clearing of the turnover caps), across the real winter calendar
(New Year's Day closes the market, so the plan decided on Thursday 12-31 executes on Monday 01-04).

Three things are rehearsed:

* the quarter-end rebalance of real drift runs without the owner (it is far below the approval threshold);
* the router that is not running in its 09:20-09:27 window (what happened on 2026-10-05) misses the session
  cleanly: no orders, an alert, and the next close re-decides what is left;
* the window's edges, so the 7-minute window is a documented fact and not a surprise.

Fake broker, throwaway state directory: nothing here can touch the real account."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from test_core_paper import SESSIONS, Clock, FakeBroker, make_router, policy, proposal
from test_core_resilience import QUARTER_END, RecordingAlerter

import core_alloc
from risk_router.core_gatekeeper import CoreRouter, Journal

D = Decimal
START = datetime(2026, 12, 31, 20, 0, tzinfo=UTC)

# The real account at the 2026-11-04 close (units from the journal's fills; closes from Alpaca's bars).
HELD = {"BIL": "5.462412587", "BND": "27.071245369", "IAU": "24.234213547",
        "VNQ": "20.947403241", "VTI": "4.971186307", "VXUS": "22.441318993"}
CLOSES = {"BIL": "91.42", "BND": "71.50", "IAU": "76.37", "VNQ": "98.03", "VTI": "376.92", "VXUS": "83.88"}
CASH = "8.42"
THE_BUILD = {"day": "2026-10-12", "kind": "initial", "orders": 6, "redecision": True, "status": "done",
             "traded_usd": "9998.52"}


@pytest.fixture(autouse=True)
def the_winter_calendar() -> Iterator[None]:
    backup = list(SESSIONS)
    SESSIONS[:] = QUARTER_END
    yield
    SESSIONS[:] = backup


def the_real_account() -> FakeBroker:
    broker = FakeBroker(cash=CASH, closes=CLOSES)
    broker.qty = {s: D(q) for s, q in HELD.items()}
    broker.equity = broker.last_equity = broker.cash + sum((broker.qty[s] * broker.closes[s] for s in HELD), D(0))
    return broker


def the_real_router(tmp_path: Path, broker: FakeBroker, clock: Clock) -> CoreRouter:
    router = make_router(tmp_path, broker, clock=clock)
    router.store.data.history.append(dict(THE_BUILD))      # the live journal's one executed plan
    router.store.data.targets = {s: str(w) for s, w in policy().mix.items()}
    return router


def events(tmp_path: Path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "journal.jsonl").read_text().splitlines()]


def weights(broker: FakeBroker) -> dict[str, Decimal]:
    return core_alloc.current_weights(broker.qty, broker.closes, broker.cash)


@pytest.mark.asyncio
async def test_the_first_real_rebalance_runs_without_the_owner(tmp_path: Path) -> None:
    broker, clock = the_real_account(), Clock(START)
    router = the_real_router(tmp_path, broker, clock)
    clock.set("2026-12-31", 16, 30)
    answer = await router.receive_plan(await proposal(router, "2026-12-31"))
    assert answer["status"] == "approved", "real drift is ~$150 of trades: no signed approval may be needed"
    assert router.store.data.plan["plan"]["reason"] == "rebalance"
    assert answer["execute_on"] == "2027-01-04", "New Year's Day is closed: Thursday's close executes on Monday"

    clock.set("2027-01-04", 9, 22)
    await router.tick()
    sells = [o for o in broker.submitted if o["side"] == "sell"]
    assert sells and not [o for o in broker.submitted if o["side"] == "buy"], "sells first; buys wait for the cash"
    assert all(o["type"] == "limit" and o["time_in_force"] == "day" for o in sells)

    broker.fill_all("sell")
    clock.set("2027-01-04", 9, 33)
    await router.tick()
    buys = [o for o in broker.submitted if o["side"] == "buy"]
    assert buys and sum(D(o["notional"]) for o in buys) <= broker.cash
    broker.fill_all("buy")
    clock.set("2027-01-04", 9, 50)
    await router.tick()

    assert router.store.data.plan is None and router.store.data.forced is False
    assert [h["day"] for h in router.store.data.history] == ["2026-10-12", "2027-01-04"]
    target = policy().mix
    assert all(abs(weights(broker)[s] - target[s]) < D("0.005") for s in target), weights(broker)
    assert broker.cash >= policy().cash_reserve_usd - D("1")
    assert Journal.verify(tmp_path / "journal.jsonl")


@pytest.mark.asyncio
async def test_a_router_that_was_not_running_misses_the_session_cleanly(tmp_path: Path) -> None:
    """What happened on 2026-10-05, replayed on the rebalance: the plan is approved and the process is down
    through 09:20-09:27. When it comes back it closes the plan unsent, alerts, and the next close re-decides."""
    broker, clock, alerter = the_real_account(), Clock(START), RecordingAlerter()
    router = the_real_router(tmp_path, broker, clock)
    router.alerter = alerter
    clock.set("2026-12-31", 16, 30)
    await router.receive_plan(await proposal(router, "2026-12-31"))

    clock.set("2027-01-04", 9, 37)                         # the process comes back ten minutes late
    await router.tick()
    assert broker.submitted == [], "nothing is sent outside the window"
    assert router.store.data.plan is None and router.store.data.forced is True
    assert [e["event"] for e in events(tmp_path)].count("plan_expired") == 1
    assert any("missed its session" in title for title, _ in alerter.sent)

    clock.set("2027-01-04", 16, 30)                        # the next close: forced, so it re-decides now
    answer = await router.receive_plan(await proposal(router, "2027-01-04"))
    assert answer["status"] == "approved" and router.store.data.plan["plan"]["reason"] == "rebalance"
    assert answer["execute_on"] == "2027-01-05"


@pytest.mark.asyncio
async def test_a_router_that_comes_back_after_the_session_never_trades_late(tmp_path: Path) -> None:
    """Back at 14:00, the same day: still no orders. The portfolio waits one more close rather than trading
    into the afternoon on a plan made for the open."""
    broker, clock = the_real_account(), Clock(START)
    router = the_real_router(tmp_path, broker, clock)
    clock.set("2026-12-31", 16, 30)
    await router.receive_plan(await proposal(router, "2026-12-31"))
    clock.set("2027-01-04", 14, 0)
    await router.tick()
    assert broker.submitted == [] and router.store.data.forced is True


@pytest.mark.asyncio
@pytest.mark.parametrize(("minute", "sends"), [(19, False), (20, True), (27, True), (28, False)])
async def test_the_submit_window_is_09_20_to_09_27_new_york(tmp_path: Path, minute: int, sends: bool) -> None:
    """Seven minutes, edges included. A router that starts at 09:28 has missed this session. The Mac's
    clock does not matter, only New York's: in winter 09:20 is 17:20 Istanbul."""
    broker, clock = the_real_account(), Clock(START)
    router = the_real_router(tmp_path, broker, clock)
    clock.set("2026-12-31", 16, 30)
    await router.receive_plan(await proposal(router, "2026-12-31"))
    clock.set("2027-01-04", 9, minute)
    await router.tick()
    assert bool(broker.submitted) is sends
    if minute == 19:
        assert router.store.data.plan is not None, "too early: the plan simply waits"
    if minute == 28:
        assert router.store.data.forced is True, "too late: closed unsent and re-decided at the next close"


def test_in_winter_the_window_opens_at_17_20_in_istanbul() -> None:
    """The fact the launch agents are built around: New York is on EST in January, Istanbul is UTC+3."""
    from zoneinfo import ZoneInfo
    open_ny = datetime(2027, 1, 4, 9, 20, tzinfo=ZoneInfo("America/New_York"))
    assert open_ny.astimezone(ZoneInfo("Europe/Istanbul")).strftime("%H:%M") == "17:20"
    allocator_run = datetime(2027, 1, 1, 0, 30, tzinfo=ZoneInfo("Europe/Istanbul"))     # 00:30 local, the day after
    assert allocator_run.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M") == "2026-12-31 16:30"
