"""
orchestrator.py — The real daily cycle runner for MBY-Trading.

Runs the full 5-phase daily cycle (Operating Manual §6) across all
eight agents, in the fixed run order established from the start:

    Atlas -> Vera -> Solomon -> [Nora -> Marcus -> Ada, if triggered] -> Otis -> Clara

Phase 3/4 only run if Solomon flags action_needed — most days, that's
false, and the cycle goes straight from Solomon to Otis/Clara. This
is the "runs daily, changes the portfolio only when necessary"
principle, expressed structurally rather than just as a prompt rule.

Scheduling: anchored to America/New_York, not a fixed CET time, so
daylight saving doesn't need manual adjustment twice a year (see the
scheduling notes at the bottom of this file).
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from core.market_calendar import is_trading_day
from agents import atlas, vera, solomon, nora, marcus, ada, otis, clara

EASTERN = ZoneInfo("America/New_York")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("orchestrator")


def run_daily_cycle():
    today = datetime.now(EASTERN).date()

    if not is_trading_day(today):
        logger.info(f"{today} is not an NYSE trading day — skipping run.")
        return None

    logger.info(f"=== Starting daily cycle for {today} ===")

    # ---- Phase 1: Pre-market scan ----
    logger.info("[Phase 1] Atlas...")
    atlas_result = atlas.run(today)
    logger.info(f"[Phase 1] Atlas: regime={atlas_result['regime_signal']}, change={atlas_result['change_from_yesterday']}")

    logger.info("[Phase 1] Vera...")
    vera_result = vera.run(today, atlas_result)
    logger.info(f"[Phase 1] Vera: {len(vera_result['candidates'])} new candidate(s), {len(vera_result['monitoring'])} position(s) monitored")

    # ---- Phase 2: Morning-meeting synthesis ----
    logger.info("[Phase 2] Solomon...")
    solomon_result = solomon.run(today, atlas_result, vera_result)
    logger.info(f"[Phase 2] Solomon: action_needed={solomon_result['action_needed']}")

    # ---- Phase 3 & 4: Risk, sizing, execution — only if triggered ----
    if solomon_result["action_needed"]:
        logger.info("[Phase 3] Nora...")
        nora_result = nora.run(today)
        logger.info(f"[Phase 3] Nora: portfolio_status={nora_result['portfolio_status']}")

        logger.info("[Phase 3] Marcus...")
        marcus_result = marcus.run(today)
        logger.info(f"[Phase 3] Marcus: {len(marcus_result['allocations'])} allocation(s)")

        if any(a["target_size_pct"] > 0 for a in marcus_result["allocations"]):
            logger.info("[Phase 4] Ada...")
            ada_result = ada.run(today)
            logger.info(f"[Phase 4] Ada: {len(ada_result['orders'])} order(s) placed")
        else:
            logger.info("[Phase 4] Marcus produced no actionable allocations — skipping Ada.")
    else:
        logger.info("Solomon: no action needed today — skipping Phases 3-4.")

    # ---- Phase 5: Close & reconciliation (always runs) ----
    logger.info("[Phase 5] Otis...")
    otis_result = otis.run(today)
    logger.info(f"[Phase 5] Otis: reconciled={otis_result['reconciled']}, {len(otis_result['discrepancies'])} discrepancy(ies)")

    logger.info("[Phase 5] Clara...")
    clara_result = clara.run(today)
    report = clara.compile_daily_report(today)
    logger.info(f"[Phase 5] Clara: process_check={clara_result['process_check']}")

    logger.info(f"=== Daily cycle complete for {today} ===")
    print("\n" + "=" * 60)
    print(report["full_report_md"])
    print("=" * 60)

    return {
        "date": today, "atlas": atlas_result, "vera": vera_result,
        "solomon": solomon_result, "otis": otis_result, "clara": clara_result,
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
# ---------------------------------------------------------------