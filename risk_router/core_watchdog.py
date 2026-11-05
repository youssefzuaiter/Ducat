"""A morning check that the core router will be there when its window opens.

The router can alert about a plan that missed its session, but only after it is running again: a router that
is down (the process died, the Mac slept) cannot tell anyone. On 2026-10-05 exactly that happened and nobody
knew until the next day. This runs from outside the router, on a launchd timer, reads ``GET /health`` and
says something only when something is wrong or the owner has something to do today:

* the router does not answer, is halted or disabled, has stopped ticking, or its journal fails the chain check;
* a plan executes today and the Mac is on battery (``caffeinate -s`` only holds off sleep on AC power);
* a plan executes today and is still waiting for the owner's signed approval;
* a plan executes today and everything is fine (one line, so a quiet morning is not mistaken for a dead check).

It is read-only: it never calls a control endpoint and never touches the account. Delivery is a macOS
notification plus, when ``CORE_ALERT_WEBHOOK_URL`` is set, the same push the router uses.

    python -m risk_router.core_watchdog          # run once; exit 1 when it found a problem
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Final
from zoneinfo import ZoneInfo

logger = logging.getLogger("risk_router.core_watchdog")

NEW_YORK: Final[ZoneInfo] = ZoneInfo("America/New_York")
WINDOW_OPENS: Final[time] = time(9, 20)          # tier0_core.SUBMIT_FROM, in New York
STALE_TICK_SECONDS: Final[float] = 60.0          # the router ticks every few seconds
DEFAULT_URL: Final[str] = "http://127.0.0.1:8080"


@dataclass(frozen=True)
class Finding:
    problem: bool        # False = information the owner wants once, True = something to fix
    title: str
    message: str


def on_ac_power(pmset_output: str) -> bool | None:
    """True on AC, False on battery, None when the output says neither (a desktop, an unexpected format)."""
    first = pmset_output.splitlines()[0] if pmset_output.strip() else ""
    if "AC Power" in first:
        return True
    if "Battery Power" in first:
        return False
    return None


def window_local(day: date, now: datetime) -> str:
    """The plan day's 09:20 New York as a clock time on the machine's own clock (17:20 in Istanbul in winter)."""
    opens = datetime.combine(day, WINDOW_OPENS, NEW_YORK).astimezone(now.tzinfo)
    return f"{opens:%H:%M}"


def assess(health: dict[str, Any] | None, error: str | None, now: datetime, on_ac: bool | None) -> list[Finding]:
    """What, if anything, the owner should hear. Pure: ``now`` is passed in, nothing is read here."""
    if health is None:
        return [Finding(True, "Core: the router is not answering",
                        f"{error or 'no answer'}. Restart it: launchctl kickstart -k gui/$(id -u)/com.pfw.core-router "
                        f"and read ~/Library/Logs/pfw-core-router.log")]
    out: list[Finding] = []
    if health.get("halted"):
        out.append(Finding(True, "Core: the kill switch is on", f"halted: {health.get('halt_reason')}"))
    if not health.get("trading_enabled", False):
        out.append(Finding(True, "Core: trading is disabled", str(health.get("disabled_reason"))))
    if health.get("status") != "ok" and health.get("attention"):
        out.append(Finding(True, "Core: the router needs attention", "; ".join(map(str, health["attention"]))[:400]))
    tick = health.get("tick") or {}
    age = tick.get("last_age_seconds")
    if age is None or float(age) > STALE_TICK_SECONDS or tick.get("failures"):
        out.append(Finding(True, "Core: the execution loop is not ticking",
                           f"last tick {age}s ago, {tick.get('failures')} failures in a row"))
    if not (health.get("journal") or {}).get("ok", False):
        out.append(Finding(True, "Core: the journal failed its check", str((health.get("journal") or {}).get("reason"))))

    today = now.astimezone(NEW_YORK).date()
    plan = health.get("plan")
    if plan and plan.get("execute_on") == today.isoformat():
        when = window_local(today, now)
        if plan.get("status") == "awaiting_approval":
            out.append(Finding(True, "Core: a plan needs your approval today",
                               f"plan {plan.get('id')} ({plan.get('kind')}) executes at {when} your time and will "
                               f"expire unsent without the signed approval"))
        elif plan.get("status") == "approved":
            if on_ac is False:
                out.append(Finding(True, "Core: plug the Mac in, a plan executes today",
                                   f"plan {plan.get('id')} executes at {when} your time and the Mac is on battery: "
                                   f"it can sleep and miss the 7-minute window"))
            elif not any(f.problem for f in out):
                power = {True: "on AC power", None: "power state unknown"}.get(on_ac, "")
                out.append(Finding(False, "Core: a plan executes today",
                                   f"plan {plan.get('id')} ({plan.get('kind')}) at {when} your time; the router is "
                                   f"healthy and the Mac is {power}"))
    return out


# --- the edges: read the router and the power state, deliver the findings ------------------------------------------

def fetch_health(url: str, timeout: float = 5.0) -> tuple[dict[str, Any] | None, str | None]:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=timeout) as response:  # noqa: S310 - the owner's own URL
            return json.loads(response.read()), None
    except Exception as exc:  # noqa: BLE001 - any failure to read it is the finding
        return None, f"{type(exc).__name__}: {exc}"[:200]


def read_power() -> bool | None:
    try:
        result = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5, check=False)  # noqa: S607
        return on_ac_power(result.stdout)
    except Exception:  # noqa: BLE001
        return None


def macos_notify(title: str, message: str) -> None:
    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.run(["osascript", "-e", f'display notification "{esc(message)}" with title "{esc(title)}"'],  # noqa: S607
                       timeout=10, check=False, capture_output=True)
    except Exception:  # noqa: BLE001
        logger.warning("could not show a macOS notification")


async def deliver(findings: list[Finding]) -> None:
    from risk_router.core_alerts import Alerter     # imported late: httpx is only needed when something is sent
    alerter = Alerter.from_env()
    for f in findings:
        macos_notify(f.title, f.message)
        await alerter.send(f.title, f.message)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    health, error = fetch_health(os.getenv("CORE_ROUTER_URL", DEFAULT_URL))
    findings = assess(health, error, datetime.now().astimezone(), read_power())
    for f in findings:
        logger.log(logging.ERROR if f.problem else logging.INFO, "%s: %s", f.title, f.message)
    if not findings:
        logger.info("router healthy; no plan executes today")
        return 0
    asyncio.run(deliver(findings))
    return 1 if any(f.problem for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
