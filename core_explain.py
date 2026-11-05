"""Why the core did, or did not, plan a trade at one close, in plain sentences.

``core_paper.decide`` answers *what*; this answers *why*: which rule looked at the close, how far each holding
sits from its target, why each leg exists, whether the plan needs the owner, and, on the many nights with no
plan, what the rule is waiting for. It reads a ``Snapshot`` and the plan ``decide`` returned and changes nothing.

It never re-implements a decision. Every yes/no here comes from ``core_alloc`` (the same ``period_ends``,
``Rule.checked``, ``drift`` and ``current_weights`` the backtest, the Allocator and the router use) or from
``tier0_core.needs_approval``; this module only puts words around their answers (check C12 forbids a second copy
of those rules). Pure standard library, like ``core_paper``.
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

import core_alloc
import core_paper
import tier0_core
from tier0_core import CorePolicy

CENT = Decimal("0.01")


def usd(x: Decimal) -> str:
    return f"${x.quantize(CENT):,.2f}"


def pct(x: Decimal, places: int = 1) -> str:
    return f"{float(x) * 100:.{places}f}%"


def next_check_hint(period: str | None, session: date) -> str | None:
    """The calendar end of the period after ``session``'s: when the rule will next look. The rule itself decides on
    the last *session* of that period, which can fall a day or two earlier (a weekend, a holiday)."""
    if period is None:
        return None
    if period == "year":
        end = date(session.year, 12, 31)
        return (end if session < end else date(session.year + 1, 12, 31)).isoformat()
    step = 3 if period == "quarter" else 1
    month, year = ((session.month - 1) // step + 1) * step, session.year
    end = date(year, month, calendar.monthrange(year, month)[1])
    if end <= session:
        month, year = (month + step, year) if month + step <= 12 else (month + step - 12, year + 1)
        end = date(year, month, calendar.monthrange(year, month)[1])
    return end.isoformat()


def explain(policy: CorePolicy, snap: core_paper.Snapshot, plan: core_alloc.Plan | None) -> list[str]:
    """The reasoning behind ``plan`` (or its absence) at ``snap``'s close. ``plan`` is what ``decide`` returned."""
    head = f"{snap.session} close, for the {snap.next_session} open"
    why_not = core_paper.skip_reason(policy, snap)
    if why_not is not None:
        return [head, f"No plan: {why_not}."]
    if snap.initial:
        return _initial(policy, snap, plan, head)

    targets = snap.targets or dict(policy.mix)
    weights = core_alloc.current_weights(snap.qty, snap.closes, snap.cash)
    value = snap.cash + sum((q * snap.closes[s] for s, q in snap.qty.items()), Decimal(0))
    ends = core_alloc.period_ends(snap.session, snap.next_session)
    rule = policy.rule
    gap = core_alloc.drift(weights, targets)
    lines = [head, f"Portfolio {usd(value)}; largest gap from target {float(gap) * 100:.2f} points; cash {usd(snap.cash)} "
                   f"(reserve {usd(policy.cash_reserve_usd)})."]

    if snap.forced:
        lines.append("The last plan did not complete (deferred, expired or abandoned), so this close re-decides "
                     "from the real positions whatever the calendar says"
                     + (f", though the {float(rule.band) * 100:.0f}-point band still has to be exceeded." if rule.band is not None
                        else "."))
    elif rule.never:
        lines.append(f"Rule `{rule.name}`: never rebalances after the initial build.")
    elif not rule.checked(ends):
        hint = next_check_hint(rule.period, snap.session)
        lines.append(f"Rule `{rule.name}` only looks at the last session of a {rule.period}; {snap.session} is not "
                     f"one, so it did not look" + (f". Next look: the last session on or before {hint}." if hint else "."))
    else:
        lines.append(f"Rule `{rule.name}`: {snap.session} is the last session of the {rule.period}, so it looked.")
        if rule.band is not None:
            lines.append(f"The band is {float(rule.band) * 100:.0f} points and the largest gap is {float(gap) * 100:.2f}: "
                         + ("outside it, so it rebalances." if gap > rule.band else "inside it, so it leaves things."))

    if plan is None:
        if core_alloc.rebalance_due(rule, ends, weights, targets, forced=snap.forced):
            lines.append(f"It looked and found nothing to do: every leg is under the {usd(policy.min_order_usd)} "
                         f"minimum order, so the gaps stay.")
        else:
            lines.append("No plan, and nothing to do tonight.")
        return lines + _weights(policy, weights, targets)

    lines += _weights(policy, weights, targets)
    lines += _legs(policy, snap, plan, weights, targets, value)
    return lines + _approval(policy, snap, plan)


def _initial(policy: CorePolicy, snap: core_paper.Snapshot, plan: core_alloc.Plan | None, head: str) -> list[str]:
    usable = snap.cash - policy.cash_reserve_usd
    lines = [head, f"Nothing is held yet: this is the initial build, {usd(usable)} (cash {usd(snap.cash)} less the "
                   f"{usd(policy.cash_reserve_usd)} reserve) to the policy mix."]
    if plan is not None:
        lines += [f"  buy {usd(n)} of {s} (target {pct(plan.targets[s], 0)})" for s, n in sorted(plan.buys.items())]
        lines.append("The initial build always waits for the owner's signed fund command.")
    return lines


def _weights(policy: CorePolicy, weights: dict[str, Decimal], targets: dict[str, Decimal]) -> list[str]:
    rows = ["Holdings against target:"]
    for s in sorted(set(policy.mix) | set(weights)):
        now, tgt = weights.get(s, Decimal(0)), targets.get(s, Decimal(0))
        rows.append(f"  {s:5} {pct(now):>6} now, {pct(tgt):>6} target, {float(now - tgt) * 100:+.2f} pt")
    return rows


def _legs(policy: CorePolicy, snap: core_paper.Snapshot, plan: core_alloc.Plan, weights: dict[str, Decimal],
          targets: dict[str, Decimal], value: Decimal) -> list[str]:
    lines = [f"The plan ({plan.reason}) sells what is over target first, then buys what is under with the proceeds "
             f"and cash above the reserve:"]
    sold = Decimal(0)
    for s, q in sorted(plan.sells.items()):
        worth = q * snap.closes[s]
        sold += worth
        lines.append(f"  sell {q} {s} (about {usd(worth)}): it is {pct(weights.get(s, Decimal(0)))} of the portfolio "
                     f"against a {pct(targets.get(s, Decimal(0)), 0)} target")
    bought = Decimal(0)
    for s, n in sorted(plan.buys.items()):
        bought += n
        lines.append(f"  buy {usd(n)} of {s}: it is {pct(weights.get(s, Decimal(0)))} against a "
                     f"{pct(targets.get(s, Decimal(0)), 0)} target")
    for s in sorted(set(targets) | set(weights)):
        leg_usd = abs(weights.get(s, Decimal(0)) - targets.get(s, Decimal(0))) * value
        if s not in plan.sells and s not in plan.buys and 0 < leg_usd < policy.min_order_usd:
            lines.append(f"  {s} is off target by {usd(leg_usd)}, under the {usd(policy.min_order_usd)} minimum order, "
                         f"so it is left alone")
    lines.append(f"Total: sell about {usd(sold)}, buy {usd(bought)} ({pct((sold + bought) / value, 1)} of the portfolio traded).")
    return lines


def _approval(policy: CorePolicy, snap: core_paper.Snapshot, plan: core_alloc.Plan) -> list[str]:
    if tier0_core.needs_approval(plan, policy, snap.closes):
        return [f"It trades more than {usd(policy.approval_needed_above_usd)}, so it waits for your signed approval "
                f"and expires unsent if it does not get it by 09:27 New York on {snap.next_session}."]
    return [f"It trades under {usd(policy.approval_needed_above_usd)}, so it runs by itself at the {snap.next_session} "
            f"open (09:20-09:27 New York). If the account is down 2.5% or more at the open the loss breaker defers the "
            f"buys and the next close re-decides."]
