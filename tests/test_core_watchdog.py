"""The morning check on the core router: it speaks when something is wrong or a plan executes today, and
is silent on an ordinary healthy day. Pure judgment (``assess``) plus the two small parsers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from risk_router import core_watchdog as wd

ISTANBUL = timezone(timedelta(hours=3))
JAN_4_MORNING = datetime(2027, 1, 4, 16, 0, tzinfo=ISTANBUL)      # 08:00 in New York, Monday
JAN_5 = datetime(2027, 1, 5, 16, 0, tzinfo=ISTANBUL)


def healthy(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"status": "ok", "trading_enabled": True, "halted": False, "attention": [],
                            "tick": {"last_age_seconds": 2.5, "failures": 0}, "journal": {"ok": True, "reason": None},
                            "plan": None}
    return {**base, **changes}


def plan(status: str = "approved", execute_on: str = "2027-01-04") -> dict[str, Any]:
    return {"id": "abc123", "kind": "rebalance", "status": status, "execute_on": execute_on}


def titles(findings: list[wd.Finding]) -> list[str]:
    return [f.title for f in findings]


def test_a_healthy_router_with_nothing_due_says_nothing() -> None:
    assert wd.assess(healthy(), None, JAN_4_MORNING, True) == []


def test_a_router_that_does_not_answer_is_a_problem_with_the_restart_command() -> None:
    [f] = wd.assess(None, "URLError: connection refused", JAN_4_MORNING, True)
    assert f.problem and "not answering" in f.title and "kickstart" in f.message and "connection refused" in f.message


def test_a_halted_disabled_or_stalled_router_is_each_reported() -> None:
    found = wd.assess(healthy(halted=True, halt_reason="manual", trading_enabled=False,
                              tick={"last_age_seconds": 900, "failures": 3}, journal={"ok": False, "reason": "cut"}),
                      None, JAN_4_MORNING, True)
    assert all(f.problem for f in found)
    assert {"Core: the kill switch is on", "Core: trading is disabled", "Core: the execution loop is not ticking",
            "Core: the journal failed its check"} <= set(titles(found))


def test_attention_items_are_passed_on() -> None:
    [f] = wd.assess(healthy(status="attention", attention=["pfw sync stuck"]), None, JAN_4_MORNING, True)
    assert f.problem and "pfw sync stuck" in f.message


def test_a_plan_today_on_battery_says_to_plug_in_and_names_the_window_in_local_time() -> None:
    [f] = wd.assess(healthy(plan=plan()), None, JAN_4_MORNING, False)
    assert f.problem and "plug the Mac in" in f.title and "17:20" in f.message


def test_a_plan_today_awaiting_approval_is_urgent() -> None:
    [f] = wd.assess(healthy(plan=plan("awaiting_approval")), None, JAN_4_MORNING, True)
    assert f.problem and "approval" in f.title and "17:20" in f.message


def test_a_plan_today_with_everything_fine_is_one_calm_line() -> None:
    [f] = wd.assess(healthy(plan=plan()), None, JAN_4_MORNING, True)
    assert not f.problem and "executes today" in f.title and "on AC power" in f.message


def test_a_plan_for_another_day_is_not_announced() -> None:
    assert wd.assess(healthy(plan=plan(execute_on="2027-01-05")), None, JAN_4_MORNING, False) == []
    assert wd.assess(healthy(plan=plan(execute_on="2027-01-04")), None, JAN_5, False) == []


def test_a_problem_suppresses_the_calm_line() -> None:
    found = wd.assess(healthy(plan=plan(), tick={"last_age_seconds": 600, "failures": 0}), None, JAN_4_MORNING, True)
    assert found and all(f.problem for f in found)


def test_the_window_in_summer_is_an_hour_earlier_on_the_same_clock() -> None:
    july = datetime(2027, 7, 12, 16, 0, tzinfo=ISTANBUL)
    assert wd.window_local(july.date(), july) == "16:20"
    assert wd.window_local(JAN_4_MORNING.date(), JAN_4_MORNING) == "17:20"


def test_power_state_is_read_from_the_first_pmset_line() -> None:
    assert wd.on_ac_power("Now drawing from 'AC Power'\n -InternalBattery-0 61%; charging") is True
    assert wd.on_ac_power("Now drawing from 'Battery Power'\n -InternalBattery-0 61%; discharging") is False
    assert wd.on_ac_power("") is None and wd.on_ac_power("something else") is None
