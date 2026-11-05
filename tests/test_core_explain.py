"""Why the core did, or did not, plan a trade: the explanation agrees with the decision, says what each rule
looked at, and can never stop the Allocator from proposing."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_core_paper import SESSIONS, FakeBroker, policy

import core_alloc
import core_explain
import core_paper
from swarm import core_allocator

D = Decimal
HELD = {"BIL": "5.462412587", "BND": "27.071245369", "IAU": "24.234213547",
        "VNQ": "20.947403241", "VTI": "4.971186307", "VXUS": "22.441318993"}
CLOSES = {"BIL": "91.42", "BND": "71.50", "IAU": "76.37", "VNQ": "98.03", "VTI": "376.92", "VXUS": "83.88"}


def snap(session: str, next_session: str, **kw: Any) -> core_paper.Snapshot:
    base: dict[str, Any] = {"qty": {s: D(q) for s, q in HELD.items()}, "cash": D("8.42"),
                            "closes": {s: D(c) for s, c in CLOSES.items()}, "targets": dict(policy().mix)}
    return core_paper.Snapshot(date.fromisoformat(session), date.fromisoformat(next_session), **{**base, **kw})


def text(s: core_paper.Snapshot, pol: Any = None) -> str:
    pol = pol or policy()
    return "\n".join(core_explain.explain(pol, s, core_paper.decide(pol, s)))


def test_a_mid_quarter_close_says_the_rule_did_not_look_and_when_it_will() -> None:
    out = text(snap("2026-11-04", "2026-11-05"))
    assert "did not look" in out and "2026-12-31" in out and "nothing to do tonight" in out
    assert "The plan (" not in out


def test_the_quarters_last_session_looks_and_explains_every_leg() -> None:
    out = text(snap("2026-12-31", "2027-01-04"))
    assert "is the last session of the quarter, so it looked" in out
    assert "sell 1.366440861 VNQ" in out and "20.3% of the portfolio against a 19% target" in out
    assert "buy $68.75 of IAU" in out and "Total: sell about $150.02, buy $157.43" in out
    assert "runs by itself at the 2027-01-04 open" in out and "09:20-09:27" in out


def test_the_explanation_agrees_with_the_decision_on_every_kind_of_close() -> None:
    """The words say 'plan' exactly when decide returned one, across mid-quarter, quarter-end, forced and held-over closes."""
    cases = [snap("2026-11-04", "2026-11-05"), snap("2026-12-31", "2027-01-04"), snap("2027-01-04", "2027-01-05", forced=True),
             snap("2026-12-30", "2026-12-31"), snap("2027-03-31", "2027-04-01")]
    for s in cases:
        plan = core_paper.decide(policy(), s)
        out = "\n".join(core_explain.explain(policy(), s, plan))
        assert ("The plan (" in out) is (plan is not None), s.session
        assert ("nothing to do tonight" in out) is (plan is None), s.session


def test_a_forced_close_says_why_it_re_decides() -> None:
    out = text(snap("2027-01-04", "2027-01-05", forced=True))
    assert "did not complete" in out and "whatever the calendar says" in out


def test_a_band_rule_that_is_forced_still_needs_its_drift() -> None:
    pol = policy(rule=core_alloc.RULES["quarterly_band5"])
    out = text(snap("2027-01-04", "2027-01-05", forced=True), pol)
    assert "5-point band still has to be exceeded" in out and "nothing to do tonight" in out


def test_a_band_rule_reports_which_side_of_the_band_the_drift_is_on() -> None:
    pol = policy(rule=core_alloc.RULES["quarterly_band5"])
    inside = text(snap("2026-12-31", "2027-01-04"), pol)
    assert "The band is 5 points and the largest gap is 1.32: inside it, so it leaves things." in inside


def test_a_leg_under_the_minimum_order_is_named_and_left_alone() -> None:
    out = text(snap("2026-12-31", "2027-01-04"), policy(min_order_usd=D("50")))
    assert "sell 1.366440861 VNQ" in out and "buy $68.75 of IAU" in out          # the legs that clear $50
    assert "VTI is off target by $45.97" in out and "under the $50.00 minimum order, so it is left alone" in out
    assert "buy $45.78 of VTI" not in out


def test_when_every_leg_is_under_the_minimum_it_says_it_looked_and_found_nothing_to_do() -> None:
    out = text(snap("2026-12-31", "2027-01-04"), policy(min_order_usd=D("200")))
    assert "It looked and found nothing to do: every leg is under the $200.00 minimum order" in out
    assert "The plan (" not in out


def test_a_big_plan_says_it_waits_for_the_owner() -> None:
    held = {s: D(q) for s, q in HELD.items()}
    held["VNQ"] = held["VNQ"] * 3                      # VNQ triples: a large rebalance
    out = text(snap("2026-12-31", "2027-01-04", qty=held, cash=D("8.42")))
    assert "waits for your signed approval" in out and "09:27 New York on 2027-01-04" in out


def test_the_initial_build_says_it_waits_for_the_fund_command() -> None:
    out = text(snap("2026-10-09", "2026-10-12", qty=dict.fromkeys(HELD, D(0)), cash=D("10000"), initial=True, targets={}))
    assert "initial build" in out and "$9,999.00" in out and "signed fund command" in out and "buy $1,899.81 of VTI" in out


def test_a_close_before_the_policy_takes_effect_says_so() -> None:
    pol = policy(effective_from=date(2027, 1, 1))
    out = text(snap("2026-12-30", "2026-12-31"), pol)
    assert "No plan: the policy takes effect 2027-01-01" in out


@pytest.mark.parametrize(("period", "session", "expected"), [
    ("quarter", "2026-11-04", "2026-12-31"), ("quarter", "2026-12-30", "2026-12-31"), ("quarter", "2026-12-31", "2027-03-31"),
    ("quarter", "2027-01-05", "2027-03-31"), ("quarter", "2027-05-10", "2027-06-30"), ("month", "2026-11-04", "2026-11-30"),
    ("month", "2026-11-30", "2026-12-31"), ("month", "2026-12-31", "2027-01-31"), ("year", "2026-11-04", "2026-12-31"),
    ("year", "2026-12-31", "2027-12-31"), (None, "2026-11-04", None)])
def test_the_next_look_hint(period: str | None, session: str, expected: str | None) -> None:
    assert core_explain.next_check_hint(period, date.fromisoformat(session)) == expected


# --- the Allocator ------------------------------------------------------------------------------------------------------

class Router:
    """The router's side of the Allocator's two calls, recording what was sent."""

    def __init__(self) -> None:
        self.posted: list[Any] = []

    async def get(self, path: str, headers: dict[str, str]) -> Any:
        return type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: {"forced": False, "targets": {}}})()

    async def post(self, path: str, content: bytes, headers: dict[str, str]) -> Any:
        self.posted.append(content)
        return type("R", (), {"status_code": 200, "content": b"{}", "json": lambda self: {"status": "approved"}})()


def an_account() -> FakeBroker:
    broker = FakeBroker(cash="8.42", closes=CLOSES)
    broker.qty = {s: D(q) for s, q in HELD.items()}
    return broker


@pytest.mark.asyncio
async def test_explain_only_reads_and_explains_but_sends_nothing() -> None:
    router = Router()
    answer = await core_allocator.propose(an_account(), router, policy(), date(2026, 10, 5), "secret", explain_only=True)
    assert router.posted == [] and answer["explained"] is True and any("did not look" in line for line in answer["why"])


@pytest.mark.asyncio
async def test_a_normal_run_still_proposes_and_logs_why(caplog: pytest.LogCaptureFixture) -> None:
    router = Router()
    with caplog.at_level("INFO", logger="swarm.core_allocator"):
        answer = await core_allocator.propose(an_account(), router, policy(), date(2026, 10, 5), "secret")
    assert len(router.posted) == 1 and answer["http_status"] == 200
    assert any(r.message.startswith("why:") and "did not look" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_a_bug_in_the_explanation_never_stops_the_proposal(monkeypatch: pytest.MonkeyPatch,
                                                                 caplog: pytest.LogCaptureFixture) -> None:
    def broken(*a: Any, **k: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(core_explain, "explain", broken)
    router = Router()
    answer = await core_allocator.propose(an_account(), router, policy(), date(2026, 10, 5), "secret")
    assert len(router.posted) == 1 and answer["http_status"] == 200
    assert any("could not explain the decision" in r.message for r in caplog.records)


def test_the_explanation_module_has_no_decision_logic_of_its_own() -> None:
    """Check C12 for this module: it calls core_alloc/tier0_core for every yes/no and defines no rule function."""
    source = (Path(__file__).resolve().parents[1] / "core_explain.py").read_text()
    for forbidden in ("def drift", "def rebalance_due", "def period_ends", "def current_weights", "def plan_rebalance"):
        assert forbidden not in source
    for used in ("core_alloc.period_ends", "core_alloc.drift", "core_alloc.rebalance_due", "tier0_core.needs_approval"):
        assert used in source


def test_the_calendar_fixture_is_untouched() -> None:
    assert SESSIONS[-1] == "2026-10-06"
