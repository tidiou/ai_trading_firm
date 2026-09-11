"""
orchestrator.py — the daily cycle runner for MBY-Trading.

Runs the 5-phase daily cycle (Operating Manual §6) across all eight
agents, in the fixed run order established from the start:

    Atlas -> Vera -> Solomon -> [Nora review -> Marcus -> Ada, if triggered]
          -> Otis -> Nora monitor -> Clara

Phase 3/4 only run if Solomon flags action_needed — most days that is
false, and the cycle goes straight from Solomon to Otis/Clara. This is
the "runs daily, changes the portfolio only when necessary" principle,
expressed structurally rather than only as a prompt rule.

NORA APPEARS TWICE, ON PURPOSE. Her proposal review is gated with the
rest of Phase 3 — with nothing escalated there is nothing to review.
Her portfolio monitoring is NOT gated: it runs every trading day at the
close, after Otis has shut the books, and re-checks every limit against
the book as it actually stands. Risk that only convenes when the front
office wants to trade is not a control.


=====================================================================
THE CYCLE IS THREE RUNS, NOT ONE (D10)
=====================================================================

It used to be one process at 16:30 ET, after the close. That is a
defensible shape for analysis and an indefensible one for execution,
and the reason is specific rather than aesthetic:

  Ada submits DAY limit orders. Submitted after the close, the broker
  queues them for the NEXT session's open — so the desk was sizing
  against this evening's numbers and executing against tomorrow's
  opening print, with a night of news in between and nobody watching.

  Worse, Otis's end-of-day sweep ran minutes later in Phase 5 and
  cancelled those orders before the bell they were queued for, marking
  them `expired_unfilled`. The desk cancelled its own orders and
  recorded it as the market not reaching the limit. (The sweep's guard
  is fixed too — see agents/otis.py — because a schedule change should
  never be the only thing standing between a control and a defect.)

So the day is now split along the seams the Operating Manual already
describes, and each group runs when a desk would actually do it:

    PREMARKET  ~08:30 ET   Phases 1-2   Atlas, Vera, Solomon
                                        The pre-market review and the
                                        morning meeting, before the open.

    EXECUTION  ~09:45 ET   Phases 3-4   Nora review, Marcus, Ada
                                        Fifteen minutes after the bell:
                                        the opening auction is the least
                                        representative price of the day
                                        and Ada's spread guard would
                                        refuse most of it anyway. Orders
                                        now go into a live session with
                                        the whole day to fill.

    CLOSE      ~16:30 ET   Phase 5      Otis, Nora monitor, Clara
                                        Books close, risk re-checks the
                                        closed book, the report is written.

The split costs almost nothing in code because the agents were already
written for it: nora.review_proposals, marcus.run and ada.run each take
only a date and read their inputs from the database. The one thing that
lived in memory was Solomon's verdict, and that is now read back from
the run ledger (core.logging_utils.load_agent_output).


=====================================================================
SCHEDULING: `auto` DECIDES, THE SCHEDULER JUST TICKS
=====================================================================

The obvious way to schedule three runs is three cron entries at three
fixed local times. On a laptop that is wrong twice a year and wrong
every time the lid is shut at the wrong minute:

  - cron and launchd fire on LOCAL time. Eastern is the desk's clock,
    and the offset between the two moves on two different DST schedules
    (the US and Europe change on different dates; much of West Africa
    never changes at all). A fixed local time silently drifts an hour
    off the market four times a year, in both directions.

  - A single fixed instant is a single point of failure. Miss 09:45
    because the machine was asleep, and there is no execution that day
    and no indication of why.

So `python -m orchestrator auto` is designed to be run OFTEN — every
fifteen minutes — and to do nothing almost every time. It reads the
Eastern clock itself, works out which group is due, checks the run
ledger to see whether that group has already completed today, and runs
it or exits quietly. DST stops being anybody's problem, and a missed
tick catches up on the next one.

The windows are wide (see PHASE_GROUPS) for the same reason: a run that
starts at 09:47 instead of 09:45 is fine, and one that never starts is
not.
"""

import logging
import sys
import traceback
from datetime import date, datetime
from typing import Callable, Optional

from core.cycle_schedule import (
    EASTERN, GROUP_ORDER, MAX_ATTEMPTS_PER_GROUP, PHASE_GROUPS,
    cycle_row_name, due_group, now_et,
)
from core.logging_utils import load_agent_output, log_agent_run, track_agent_run
from core.market_calendar import is_trading_day
from core.trading_control import get_state as get_trading_control_state
from agents import atlas, vera, solomon, nora, marcus, ada, otis, clara

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("orchestrator")


# =================================================================
# THE THREE GROUPS
# =================================================================
def run_premarket(today: date) -> dict:
    """Phases 1-2. Reads the world, holds the morning meeting, and
    writes a verdict. Touches no money and submits no orders."""
    results = {"date": today, "atlas": None, "vera": None, "solomon": None, "errors": {}}

    logger.info("[Phase 1] Atlas...")
    with track_agent_run(today, "atlas", 1) as run:
        atlas_result = atlas.run(today)
        run.output = atlas_result
    results["atlas"] = atlas_result
    logger.info("[Phase 1] Atlas: regime=%s, change=%s",
                atlas_result["regime_signal"], atlas_result["change_from_yesterday"])

    logger.info("[Phase 1] Vera...")
    with track_agent_run(today, "vera", 1) as run:
        vera_result = vera.run(today, atlas_result)
        run.output = vera_result
    results["vera"] = vera_result
    logger.info("[Phase 1] Vera: %d new candidate(s), %d position(s) monitored",
                len(vera_result["candidates"]), len(vera_result["monitoring"]))

    logger.info("[Phase 2] Solomon...")
    with track_agent_run(today, "solomon", 2) as run:
        solomon_result = solomon.run(today, atlas_result, vera_result)
        run.output = solomon_result
    results["solomon"] = solomon_result
    logger.info("[Phase 2] Solomon: action_needed=%s", solomon_result["action_needed"])

    return results


def run_execution(today: date) -> dict:
    """
    Phases 3-4, gated on the morning meeting's verdict.

    THE VERDICT IS READ, NOT RE-DERIVED. Re-running Solomon here would
    give a second opinion from a different hour with different data,
    and the desk would have no single answer to "what did we decide
    this morning". The morning meeting decides once; execution carries
    out that decision or does nothing.
    """
    results = {"date": today, "nora_review": None, "marcus": None, "ada": None,
               "skipped": None, "errors": {}}

    solomon_result = load_agent_output(today, "solomon")
    if solomon_result is None:
        # NOT the same as "no action needed", and the difference is the
        # whole point of refusing here. A missing verdict means the
        # morning meeting did not happen or did not finish; trading on
        # that would be trading on nothing at all.
        msg = ("No completed Solomon run for %s — the morning meeting has not "
               "happened, so there is nothing to execute. Run the premarket "
               "group first: python -m orchestrator premarket" % today)
        logger.error(msg)
        results["skipped"] = "no morning meeting on record"
        results["errors"]["execution_gate"] = msg
        return results

    if not solomon_result.get("action_needed"):
        logger.info("Solomon: no action needed today — Phases 3-4 skipped.")
        results["skipped"] = "solomon: no action needed"
        return results

    logger.info("[Phase 3] Nora (proposal review)...")
    # Logged as "nora_review", not "nora": agent_runs is unique on
    # (run_date, agent_name), and her close-of-day monitoring pass would
    # otherwise overwrite this row.
    with track_agent_run(today, "nora_review", 3) as run:
        nora_result = nora.review_proposals(today)
        run.output = nora_result
    results["nora_review"] = nora_result
    logger.info("[Phase 3] Nora: portfolio_status=%s, circuit_breaker=%s",
                nora_result["portfolio_status"], nora_result["circuit_breaker_active"])

    logger.info("[Phase 3] Marcus...")
    with track_agent_run(today, "marcus", 3) as run:
        marcus_result = marcus.run(today)
        run.output = marcus_result
    results["marcus"] = marcus_result
    logger.info("[Phase 3] Marcus: %d allocation(s)", len(marcus_result["allocations"]))

    if any(a["target_size_pct"] > 0 for a in marcus_result["allocations"]):
        logger.info("[Phase 4] Ada...")
        with track_agent_run(today, "ada", 4) as run:
            ada_result = ada.run(today)
            run.output = ada_result
        results["ada"] = ada_result
        logger.info("[Phase 4] Ada: %d order(s) placed", len(ada_result["orders"]))
    else:
        logger.info("[Phase 4] Marcus produced no actionable allocations — skipping Ada.")
        results["skipped"] = "marcus: no actionable allocations"

    return results


def run_close(today: date) -> dict:
    """
    Phase 5. ALWAYS runs, and each step is guarded separately.

    Two failure modes this structure exists to prevent:

      1. A cycle that died in Phase 1 used to take Otis and Clara with
         it — so on precisely the days something went wrong, the books
         never closed and no report was written. The desk went blind
         exactly when you needed to see. Splitting the day into
         separately-scheduled groups makes this structural rather than
         merely careful: the close run does not know or care whether
         the morning ran.

      2. A half-completed cycle left no trace of how far it got. Every
         call writes a row to agent_runs — running on entry, completed
         or failed on exit — so "what happened today" is a query rather
         than an inference.
    """
    results = {"date": today, "otis": None, "nora_monitor": None,
               "clara": None, "errors": {}}

    def guarded(name: str, phase: int, fn: Callable):
        try:
            with track_agent_run(today, name, phase) as run:
                out = fn()
                run.output = out
                return out
        except Exception as exc:  # noqa: BLE001
            logger.exception("[Phase %s] %s FAILED: %s", phase, name, exc)
            results["errors"][name] = f"{type(exc).__name__}: {exc}"
            return None

    logger.info("[Phase 5] Otis...")
    otis_result = guarded("otis", 5, lambda: otis.run(today))
    results["otis"] = otis_result
    if otis_result:
        logger.info("[Phase 5] Otis: reconciled=%s, %d discrepancy(ies), "
                    "realized=%s, unrealized_change=%s",
                    otis_result["reconciled"], len(otis_result["discrepancies"]),
                    otis_result["realized_pnl_today"],
                    otis_result["unrealized_change_today"])
        if otis_result.get("cost_basis_unknown"):
            logger.warning("[Phase 5] Otis: %d sale(s) had no cost basis on record — "
                           "their realized P&L is NULL, not zero.",
                           otis_result["cost_basis_unknown"])

    # Nora's SECOND job, and the one that runs unconditionally: re-check
    # the book as it stands against every limit, whether or not anybody
    # proposed anything today.
    #
    # AFTER Otis, deliberately — he has just rebuilt the positions table
    # and recorded today's NAV, so risk is measured on the closed book
    # rather than on yesterday's. Same order a real desk uses.
    #
    # It also sits outside the action_needed branch entirely, which is
    # now enforced by the calendar rather than by an if: risk is not
    # downstream of the front office's appetite to trade. A position
    # drifting from 7% to 11% on price alone has no proposal attached to
    # it, and that is precisely what daily monitoring exists to catch
    # (Operating Manual §7.4).
    logger.info("[Phase 5] Nora (daily portfolio monitoring)...")
    nora_monitor = guarded("nora_monitor", 5, lambda: nora.monitor_portfolio(today))
    results["nora_monitor"] = nora_monitor
    if nora_monitor:
        logger.info("[Phase 5] Nora: status=%s, circuit_breaker=%s, %d breach(es), "
                    "%d position(s)",
                    nora_monitor["portfolio_status"], nora_monitor["circuit_breaker_active"],
                    len(nora_monitor["breaches"]), nora_monitor["position_count"])
        if nora_monitor["circuit_breaker_active"]:
            logger.warning("[Phase 5] CIRCUIT BREAKER ACTIVE — %s. New positions and adds "
                           "are frozen until drawdown recovers; reductions remain permitted.",
                           nora_monitor["drawdown"]["basis"])

    logger.info("[Phase 5] Clara...")
    clara_result = guarded("clara", 5, lambda: clara.run(today))
    results["clara"] = clara_result
    if clara_result:
        logger.info("[Phase 5] Clara: process_check=%s", clara_result["process_check"])

    report = guarded("clara_report", 5, lambda: clara.compile_daily_report(today))
    if report:
        print("\n" + "=" * 60)
        print(report["full_report_md"])
        print("=" * 60)

    return results


GROUP_RUNNERS: dict[str, Callable[[date], dict]] = {
    "premarket": run_premarket,
    "execution": run_execution,
    "close": run_close,
}


# =================================================================
# RUNNING ONE GROUP
# =================================================================
def _announce_control_state() -> bool:
    """
    State the halt state at the top of every run, whether or not it is
    set. A control you only hear about when it fires is one you forget
    exists — and a forgotten halt looks exactly like a broken desk,
    right up until someone checks.
    """
    control = get_trading_control_state()
    if control.trading_enabled:
        logger.info("Trading control: %s", control.describe())
    else:
        logger.warning("=" * 68)
        logger.warning("TRADING HALTED BY OPERATOR — %s", control.describe())
        logger.warning("Analysis, reconciliation and reporting run as normal.")
        logger.warning("Ada will submit NOTHING — buys or sells — until resumed with:")
        logger.warning('    python -m core.trading_control resume --reason "..."')
        logger.warning("=" * 68)
    return control.trading_enabled


def run_group(group: str, today: Optional[date] = None) -> dict:
    """
    Run one phase group and record it as `cycle:<group>` in agent_runs.

    The cycle row is what makes the whole thing observable: it carries
    the attempt count so retries are bounded, it is how `auto` knows a
    group is done, and it is how the dashboard knows the desk ran at all
    today. Each agent still writes its own row; this is the row about
    the RUN, not about any one agent.

    IT DOES NOT USE `track_agent_run`, AND THE REASON IS THE ATTEMPT
    COUNT. That helper writes `raw_output=None` on the failure path —
    correct for an agent, whose output does not exist if it raised, and
    fatal here: the attempt count would vanish on exactly the runs that
    need counting, `_attempts_so_far` would read zero every time, and
    the cap meant to stop a broken group retrying every fifteen minutes
    until the window shut would never engage. So the row is written
    directly, with the attempt number preserved on both paths.

    The row also carries a SUMMARY rather than the whole nested result.
    Vera's output alone can run to tens of kilobytes; nesting every
    agent's output inside the group's row would breach the 64k
    truncation limit in core.logging_utils, and a truncated row reads
    back as no row at all — which would have silently broken the
    attempt count a second way. The agents' own rows hold the detail.
    """
    today = today or now_et().date()
    if group not in GROUP_RUNNERS:
        raise ValueError(f"unknown phase group {group!r} — "
                         f"expected one of {', '.join(GROUP_ORDER)}")

    spec = PHASE_GROUPS[group]
    trading_enabled = _announce_control_state()
    attempt = _attempts_so_far(today, group) + 1
    started = now_et()

    logger.info("=== %s: %s (phases %s), attempt %d, %s ===",
                today, spec.label, spec.phases, attempt,
                started.strftime("%H:%M ET"))

    log_agent_run(today, cycle_row_name(group), 0, "running",
                  started_at=started,
                  output={"group": group, "attempt": attempt})

    try:
        result = GROUP_RUNNERS[group](today)
    except Exception as exc:  # noqa: BLE001
        logger.exception("=== %s %s FAILED ===", today, group)
        log_agent_run(
            today, cycle_row_name(group), 0, "failed",
            started_at=started, completed_at=now_et(),
            output={"group": group, "attempt": attempt,
                    "trading_enabled": trading_enabled},
            error=f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}",
        )
        raise

    result["group"] = group
    result["attempt"] = attempt
    result["trading_enabled"] = trading_enabled

    # A group can finish without raising and still have failed inside:
    # the close group deliberately swallows per-agent failures so that
    # Otis cannot stop Clara. That is a completed run with errors, not a
    # failed one — but it must not read as a clean day.
    errors = result.get("errors") or {}
    if errors:
        logger.error("=== %s %s finished WITH ERRORS: %s ===",
                     today, group, ", ".join(errors))
    else:
        logger.info("=== %s %s complete ===", today, group)

    log_agent_run(
        today, cycle_row_name(group), 0, "completed",
        started_at=started, completed_at=now_et(),
        output=_cycle_summary(result, group, attempt, trading_enabled),
        error="; ".join(f"{k}: {v}" for k, v in errors.items()) or None,
    )
    return result


def _cycle_summary(result: dict, group: str, attempt: int,
                   trading_enabled: bool) -> dict:
    """What the cycle row keeps: enough to answer "what happened in this
    run" without duplicating output the agents already stored."""
    ran = [name for name in PHASE_GROUPS[group].agents
           if isinstance(result.get(name), dict)]
    return {
        "group": group,
        "attempt": attempt,
        "trading_enabled": trading_enabled,
        "agents_completed": ran,
        "skipped": result.get("skipped"),
        "errors": {k: str(v) for k, v in (result.get("errors") or {}).items()},
    }


def _attempts_so_far(today: date, group: str) -> int:
    """
    How many times this group has already been started today.

    Reads a FAILED row deliberately — `require_completed=False`. The
    count exists precisely to bound retries after failures, so refusing
    to read a failed row would zero the counter on every attempt and
    leave the cap permanently disengaged.
    """
    prior = load_agent_output(today, cycle_row_name(group), require_completed=False)
    if isinstance(prior, dict):
        return int(prior.get("attempt") or 0)
    return 0


# =================================================================
# `auto` — WHAT THE SCHEDULER CALLS
# =================================================================
def group_state(today: date, group: str) -> str:
    """One of: not_run | running | completed | failed."""
    from core.logging_utils import get_run_status
    return get_run_status(today, cycle_row_name(group)) or "not_run"


def run_auto(now: Optional[datetime] = None) -> dict:
    """
    Decide what, if anything, is due right now, and run it.

    Designed to be called every fifteen minutes and to do nothing almost
    every time. Returns a dict describing the decision — always, never
    raising for the ordinary "nothing to do" cases, so a scheduler's log
    stays readable and a non-zero exit means something real.
    """
    now = now or now_et()
    today = now.astimezone(EASTERN).date()

    if not is_trading_day(today):
        logger.info("%s is not an NYSE trading day — nothing to do.", today)
        return {"action": "skipped", "reason": "not a trading day", "date": today}

    group = due_group(now)
    if group is None:
        logger.info("%s: nothing due at %s.", today, now.astimezone(EASTERN).strftime("%H:%M ET"))
        return {"action": "skipped", "reason": "outside every window", "date": today}

    state = group_state(today, group)
    if state == "completed":
        logger.info("%s: %s already completed today.", today, group)
        return {"action": "skipped", "reason": "already completed",
                "group": group, "date": today}

    if state == "running":
        # A previous tick is still going — the premarket group can take
        # several minutes of model calls. Starting a second one would
        # have two Adas racing for the same allocations, and while her
        # idempotency guard would probably hold, "probably" is not a
        # control.
        logger.warning("%s: %s is still running from an earlier tick — standing down.",
                       today, group)
        return {"action": "skipped", "reason": "already running",
                "group": group, "date": today}

    attempts = _attempts_so_far(today, group)
    if attempts >= MAX_ATTEMPTS_PER_GROUP:
        logger.error("%s: %s has failed %d time(s) — not retrying automatically. "
                     "Run it by hand once the cause is understood: "
                     "python -m orchestrator %s", today, group, attempts, group)
        return {"action": "skipped", "reason": "attempt limit reached",
                "group": group, "attempts": attempts, "date": today}

    result = run_group(group, today)
    return {"action": "ran", "group": group, "date": today, "result": result}


# =================================================================
# "DID THE DESK RUN TODAY?" — read by the dashboard
# =================================================================
def cycle_status(today: Optional[date] = None, now: Optional[datetime] = None) -> dict:
    """
    The state of today's three groups, and whether any of them is
    overdue.

    THE FAILURE MODE THIS EXISTS FOR is the one that leaves no trace
    anywhere else: the run that never fired. A crashed agent writes a
    `failed` row; a laptop asleep at 09:45 writes nothing at all, and
    every table in the database looks exactly like a quiet day. Absence
    is only detectable against an expectation, so this is where the
    expectation lives.
    """
    now = now or now_et()
    today = today or now.astimezone(EASTERN).date()
    t = now.astimezone(EASTERN).time()
    trading_day = is_trading_day(today)

    groups = []
    for key in GROUP_ORDER:
        spec = PHASE_GROUPS[key]
        state = group_state(today, key) if trading_day else "not_applicable"
        overdue = bool(trading_day and state in ("not_run", "failed", "running")
                       and t > spec.due_by)
        groups.append({
            "key": key, "label": spec.label, "phases": spec.phases,
            "state": state, "overdue": overdue,
            "due_by": spec.due_by.strftime("%H:%M ET"),
        })

    return {
        "date": today,
        "trading_day": trading_day,
        "now_et": now.astimezone(EASTERN).strftime("%H:%M ET"),
        "groups": groups,
        "any_overdue": any(g["overdue"] for g in groups),
    }


# =================================================================
# THE WHOLE DAY IN ONE PROCESS — for a human, not for a scheduler
# =================================================================
def run_daily_cycle(today: Optional[date] = None) -> Optional[dict]:
    """
    All three groups back to back, ignoring the clock.

    KEPT FOR MANUAL AND DEVELOPMENT USE, and it is the wrong thing to
    schedule. Run at any single hour it reproduces exactly the defect
    the split was made to fix: whichever hour you pick, two of the three
    groups happen at the wrong time, and if that hour is after the close
    then Ada's orders are queued for a session Otis then sweeps them out
    of. Useful for a catch-up after an outage, or for watching the whole
    chain run on demand.
    """
    today = today or now_et().date()

    if not is_trading_day(today):
        logger.info("%s is not an NYSE trading day — skipping run.", today)
        return None

    results = {"date": today, "groups": {}, "errors": {}}
    for key in GROUP_ORDER:
        try:
            group_result = run_group(key, today)
            results["groups"][key] = group_result
            results["errors"].update(group_result.get("errors") or {})
        except Exception as exc:  # noqa: BLE001
            # One group failing must not stop the books closing. This is
            # the same reasoning as the old `finally`, generalised.
            logger.exception("Group %s aborted: %s", key, exc)
            results["errors"][key] = f"{type(exc).__name__}: {exc}"

    if results["errors"]:
        logger.error("=== Daily cycle for %s finished WITH ERRORS: %s ===",
                     today, ", ".join(results["errors"]))
    else:
        logger.info("=== Daily cycle complete for %s ===", today)
    return results


# =================================================================
# CLI
# =================================================================
USAGE = """usage: python -m orchestrator <command>

  auto        Run whichever group is due right now, if any, and exit.
              Safe and cheap to call every 15 minutes — this is what
              the scheduler runs. Does nothing outside the windows, on
              a non-trading day, or if the group already completed.

  premarket   Phases 1-2  — Atlas, Vera, Solomon      (~08:30 ET)
  execution   Phases 3-4  — Nora, Marcus, Ada         (~09:45 ET)
  close       Phase 5     — Otis, Nora, Clara         (~16:30 ET)
              Each runs immediately, ignoring the clock and the window.

  all         All three back to back. For a catch-up or a demo — not
              for a scheduler; see run_daily_cycle's docstring.

  status      What has and has not run today, and what is overdue.
"""


def _main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "auto"

    if command in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    if command == "auto":
        outcome = run_auto()
        if outcome["action"] == "ran" and outcome["result"].get("errors"):
            return 1
        return 0

    if command == "status":
        state = cycle_status()
        print(f"{state['date']} — {state['now_et']}"
              f"{'' if state['trading_day'] else '  (not an NYSE trading day)'}")
        for g in state["groups"]:
            flag = "  OVERDUE" if g["overdue"] else ""
            print(f"  {g['key']:<10} phases {g['phases']:<4} {g['state']:<12}"
                  f" due by {g['due_by']}{flag}")
        return 1 if state["any_overdue"] else 0

    if command == "all":
        result = run_daily_cycle()
        return 1 if (result and result.get("errors")) else 0

    if command in GROUP_RUNNERS:
        result = run_group(command)
        return 1 if result.get("errors") else 0

    print(f"unknown command {command!r}\n", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    # Exit non-zero when anything failed, so a scheduler can tell a bad
    # day from a quiet one. The run still completes and still closes the
    # books either way — this only reports.
    sys.exit(_main(sys.argv))
