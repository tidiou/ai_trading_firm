"""
orchestrator.py — Daily cycle runner for the AI Trading Firm.

Design notes (see Operating Manual §6-7 for the full rationale):

- Runs ONCE per NYSE trading day. Skips weekends/holidays automatically
  via the NYSE trading calendar — never hardcode a day-of-week check,
  the calendar knows about Thanksgiving, Christmas, etc.

- Anchored to US/Eastern via zoneinfo, NOT a fixed CET clock time.
  US and EU shift daylight saving on different dates, so a hardcoded
  CET trigger time would silently drift by an hour for a few weeks
  each spring/fall. Scheduling against "America/New_York" and letting
  the timezone library resolve DST avoids that entirely.

- Sequential by design (Atlas -> Vera -> Solomon -> [Nora -> Marcus ->
  Ada, only if triggered] -> Otis -> Clara). No need for a heavyweight
  agent-orchestration framework at this scale/cadence — a plain
  sequential script is easier to debug, log, and reason about.

- Phase 3/4 only execute if Solomon flags action_needed=True, and
  Phase 4 only executes if Marcus actually produced allocations.
  Most days this means the pipeline is short: Atlas, Vera, Solomon,
  Otis, Clara — five calls, no risk/sizing/execution overhead on a
  no-action day. This is the "runs daily, changes the portfolio only
  when necessary" principle, expressed structurally.

- Every agent call is wrapped so its start/end/status/output lands
  in the `agent_runs` table regardless of success or failure — this
  IS the audit trail Clara's process-compliance checks depend on.
"""

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

# from agents import atlas, vera, solomon, nora, marcus, ada, otis, clara
# from core.db import get_session
# from core.logging_utils import log_agent_run

EASTERN = ZoneInfo("America/New_York")
NYSE = mcal.get_calendar("NYSE")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("orchestrator")


def is_trading_day(d: date) -> bool:
    """True if the NYSE is open on this date (handles weekends + holidays)."""
    schedule = NYSE.schedule(start_date=d, end_date=d)
    return not schedule.empty


def run_agent(agent_name: str, phase: int, fn, *args, **kwargs):
    """
    Wraps a single agent call with logging/error handling.
    Every invocation is recorded in agent_runs, success or failure —
    this is non-negotiable, it's the audit trail the whole
    Performance/Compliance function (Clara) depends on.
    """
    logger.info(f"[Phase {phase}] Starting {agent_name}")
    started_at = datetime.now(EASTERN)
    try:
        output = fn(*args, **kwargs)
        # log_agent_run(agent_name, phase, "completed", started_at, output=output)
        logger.info(f"[Phase {phase}] {agent_name} completed")
        return output
    except Exception as exc:
        # log_agent_run(agent_name, phase, "failed", started_at, error=str(exc))
        logger.error(f"[Phase {phase}] {agent_name} FAILED: {exc}")
        raise


def run_daily_cycle():
    today = datetime.now(EASTERN).date()

    if not is_trading_day(today):
        logger.info(f"{today} is not an NYSE trading day — skipping run.")
        return None

    logger.info(f"=== Starting daily cycle for {today} ===")
    # session = get_session()

    # ---- Phase 1: Pre-market scan ----
    atlas_output = run_agent("atlas", 1, lambda: None)      # atlas.run(session, today)
    vera_output = run_agent("vera", 1, lambda: None)        # vera.run(session, today, atlas_output)

    # ---- Phase 2: Morning-meeting synthesis ----
    solomon_output = run_agent(
        "solomon", 2, lambda: {"action_needed": False}      # solomon.run(session, today, atlas_output, vera_output)
    )

    marcus_output = None

    # ---- Phase 3: Risk & sizing (only if Solomon flags action) ----
    if solomon_output.get("action_needed"):
        nora_output = run_agent("nora", 3, lambda: None)    # nora.run(session, today, solomon_output)
        marcus_output = run_agent("marcus", 3, lambda: None)  # marcus.run(session, today, nora_output, vera_output)

        # ---- Phase 4: Execution (only if Marcus produced allocations) ----
        if marcus_output and marcus_output.get("allocations"):
            run_agent("ada", 4, lambda: None)                # ada.run(session, today, marcus_output)
    else:
        logger.info("Solomon: no action needed today — skipping Phases 3-4.")

    # ---- Phase 5: Close & reconciliation (always runs) ----
    otis_output = run_agent("otis", 5, lambda: None)         # otis.run(session, today)
    clara_output = run_agent("clara", 5, lambda: None)       # clara.run(session, today)

    logger.info(f"=== Daily cycle complete for {today} ===")
    return {
        "date": today,
        "atlas": atlas_output,
        "vera": vera_output,
        "solomon": solomon_output,
        "marcus": marcus_output,
        "otis": otis_output,
        "clara": clara_output,
    }


if __name__ == "__main__":
    run_daily_cycle()


# ---------------------------------------------------------------
# Scheduling (not wired up yet — pick one when ready to deploy):
#
# Option A — APScheduler, in-process:
#     from apscheduler.schedulers.blocking import BlockingScheduler
#     sched = BlockingScheduler(timezone="America/New_York")
#     sched.add_job(run_daily_cycle, "cron", hour=16, minute=30)  # 30 min after NYSE close
#     sched.start()
#
# Option B — OS-level cron on Railway, using TZ=America/New_York
#     in the job's environment so the schedule doesn't need manual
#     DST adjustment twice a year.
#
# 16:30 America/New_York (30 min after close, to give Alpaca/FMP
# data time to settle) lands at roughly 22:30 CET in winter and
# 22:30-23:30 CET depending on the DST mismatch window in spring/fall.
# ---------------------------------------------------------------
