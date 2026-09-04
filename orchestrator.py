"""
orchestrator.py — The real daily cycle runner for MBY-Trading.

Runs the full 5-phase daily cycle (Operating Manual §6) across all
eight agents, in the fixed run order established from the start:

    Atlas -> Vera -> Solomon -> [Nora -> Marcus -> Ada, if triggered] -> Otis -> Clara

    Atlas -> Vera -> Solomon -> [Nora review -> Marcus -> Ada, if triggered]
          -> Otis -> Nora monitor -> Clara

Phase 3/4 only run if Solomon flags action_needed — most days, that's
false, and the cycle goes straight from Solomon to Otis/Clara. This
is the "runs daily, changes the portfolio only when necessary"
principle, expressed structurally rather than just as a prompt rule.

NORA APPEARS TWICE, ON PURPOSE. Her proposal review is gated with the
rest of Phase 3 — with nothing escalated there is nothing to review.
Her portfolio monitoring is NOT gated: it runs every trading day in
Phase 5, after Otis has closed the books, and re-checks every limit
against the book as it actually stands. Risk that only convenes when
the front office wants to trade is not a control.

Scheduling: anchored to America/New_York, not a fixed CET time, so
daylight saving doesn't need manual adjustment twice a year (see the
scheduling notes at the bottom of this file).
"""

import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from core.logging_utils import track_agent_run
from core.market_calendar import is_trading_day
from core.trading_control import get_state as get_trading_control_state
from agents import atlas, vera, solomon, nora, marcus, ada, otis, clara

EASTERN = ZoneInfo("America/New_York")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("orchestrator")


def run_daily_cycle():
    today = datetime.now(EASTERN).date()

    if not is_trading_day(today):
        logger.info(f"{today} is not an NYSE trading day — skipping run.")
        return None

    # State the halt state at the top of every cycle, whether or not it
    # is set. A control you only hear about when it fires is one you
    # forget exists — and a forgotten halt looks exactly like a broken
    # desk, right up until someone checks.
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

    logger.info(f"=== Starting daily cycle for {today} ===")

    # =============================================================
    # EVERY AGENT CALL IS RECORDED, and Phase 5 always runs.
    #
    # Two failure modes this structure exists to prevent:
    #
    #   1. A cycle that dies in Phase 1 used to take Otis and Clara with
    #      it — so on precisely the days something went wrong, the books
    #      never closed and no report was written. The desk went blind
    #      exactly when you needed to see. Phase 5 is therefore in a
    #      `finally`, and each of its three steps is guarded separately
    #      so Otis failing cannot stop Clara.
    #
    #   2. A half-completed cycle left no trace of how far it got. Every
    #      call now writes a row to agent_runs — running on entry,
    #      completed or failed on exit — so "what happened today" is a
    #      query rather than an inference.
    # =============================================================
    results = {
        "date": today,
        "trading_enabled": control.trading_enabled,
        "atlas": None, "vera": None, "solomon": None,
        "nora_review": None, "marcus": None, "ada": None,
        "otis": None, "nora_monitor": None, "clara": None,
        "errors": {},
    }

    def guarded(name: str, phase: int, fn):
        """Run one agent, record it, and return (result, error) rather
        than raising — for the Phase 5 steps that must not stop each
        other."""
        try:
            with track_agent_run(today, name, phase) as run:
                out = fn()
                run.output = out
                return out, None
        except Exception as exc:  # noqa: BLE001
            logger.exception("[Phase %s] %s FAILED: %s", phase, name, exc)
            results["errors"][name] = f"{type(exc).__name__}: {exc}"
            return None, exc

    try:
        # ---- Phase 1: Pre-market scan ----
        logger.info("[Phase 1] Atlas...")
        with track_agent_run(today, "atlas", 1) as run:
            atlas_result = atlas.run(today)
            run.output = atlas_result
        results["atlas"] = atlas_result
        logger.info(f"[Phase 1] Atlas: regime={atlas_result['regime_signal']}, change={atlas_result['change_from_yesterday']}")

        logger.info("[Phase 1] Vera...")
        with track_agent_run(today, "vera", 1) as run:
            vera_result = vera.run(today, atlas_result)
            run.output = vera_result
        results["vera"] = vera_result
        logger.info(f"[Phase 1] Vera: {len(vera_result['candidates'])} new candidate(s), {len(vera_result['monitoring'])} position(s) monitored")

        # ---- Phase 2: Morning-meeting synthesis ----
        logger.info("[Phase 2] Solomon...")
        with track_agent_run(today, "solomon", 2) as run:
            solomon_result = solomon.run(today, atlas_result, vera_result)
            run.output = solomon_result
        results["solomon"] = solomon_result
        logger.info(f"[Phase 2] Solomon: action_needed={solomon_result['action_needed']}")

        # ---- Phase 3 & 4: Risk, sizing, execution — only if triggered ----
        if solomon_result["action_needed"]:
            logger.info("[Phase 3] Nora (proposal review)...")
            # Logged as "nora_review", not "nora": agent_runs is unique on
            # (run_date, agent_name), and her Phase 5 monitoring pass would
            # otherwise overwrite this row.
            with track_agent_run(today, "nora_review", 3) as run:
                nora_result = nora.review_proposals(today)
                run.output = nora_result
            results["nora_review"] = nora_result
            logger.info(
                f"[Phase 3] Nora: portfolio_status={nora_result['portfolio_status']}, "
                f"circuit_breaker={nora_result['circuit_breaker_active']}"
            )

            logger.info("[Phase 3] Marcus...")
            with track_agent_run(today, "marcus", 3) as run:
                marcus_result = marcus.run(today)
                run.output = marcus_result
            results["marcus"] = marcus_result
            logger.info(f"[Phase 3] Marcus: {len(marcus_result['allocations'])} allocation(s)")

            if any(a["target_size_pct"] > 0 for a in marcus_result["allocations"]):
                logger.info("[Phase 4] Ada...")
                with track_agent_run(today, "ada", 4) as run:
                    ada_result = ada.run(today)
                    run.output = ada_result
                results["ada"] = ada_result
                logger.info(f"[Phase 4] Ada: {len(ada_result['orders'])} order(s) placed")
            else:
                logger.info("[Phase 4] Marcus produced no actionable allocations — skipping Ada.")
        else:
            logger.info("Solomon: no action needed today — skipping Phases 3-4.")

    except Exception as exc:  # noqa: BLE001
        # The trading path is done for today, but the desk still has to
        # close its books. Recorded, logged, and carried into Phase 5.
        logger.exception("Trading path (Phases 1-4) aborted: %s", exc)
        results["errors"]["trading_path"] = f"{type(exc).__name__}: {exc}"

    finally:
        # ---- Phase 5: Close & reconciliation (ALWAYS runs) ----
        logger.info("[Phase 5] Otis...")
        otis_result, _ = guarded("otis", 5, lambda: otis.run(today))
        results["otis"] = otis_result
        if otis_result:
            logger.info(
                f"[Phase 5] Otis: reconciled={otis_result['reconciled']}, "
                f"{len(otis_result['discrepancies'])} discrepancy(ies), "
                f"realized={otis_result['realized_pnl_today']}, "
                f"unrealized_change={otis_result['unrealized_change_today']}"
            )
            if otis_result.get("cost_basis_unknown"):
                logger.warning(
                    "[Phase 5] Otis: %d sale(s) had no cost basis on record — "
                    "their realized P&L is NULL, not zero.",
                    otis_result["cost_basis_unknown"],
                )

        # Nora's SECOND job, and the one that runs unconditionally: re-check
        # the book as it stands against every limit, whether or not anybody
        # proposed anything today.
        #
        # This runs AFTER Otis, deliberately — Otis has just rebuilt the
        # positions table and recorded today's NAV, so risk is measured on
        # the closed book rather than on yesterday's. Same order a real desk
        # uses: books close, then risk runs on what closed.
        #
        # It also has to sit outside the action_needed branch above. Risk is
        # not downstream of the front office's appetite to trade: a position
        # drifting from 7% to 11% on price alone has no proposal attached to
        # it, and that is precisely the case daily monitoring exists to
        # catch (Operating Manual §7.4).
        logger.info("[Phase 5] Nora (daily portfolio monitoring)...")
        nora_monitor, _ = guarded("nora_monitor", 5, lambda: nora.monitor_portfolio(today))
        results["nora_monitor"] = nora_monitor
        if nora_monitor:
            logger.info(
                f"[Phase 5] Nora: status={nora_monitor['portfolio_status']}, "
                f"circuit_breaker={nora_monitor['circuit_breaker_active']}, "
                f"{len(nora_monitor['breaches'])} breach(es), "
                f"{nora_monitor['position_count']} position(s)"
            )
            if nora_monitor["circuit_breaker_active"]:
                logger.warning(
                    "[Phase 5] CIRCUIT BREAKER ACTIVE — %s. New positions and adds are "
                    "frozen until drawdown recovers; reductions remain permitted.",
                    nora_monitor["drawdown"]["basis"],
                )

        logger.info("[Phase 5] Clara...")
        clara_result, _ = guarded("clara", 5, lambda: clara.run(today))
        results["clara"] = clara_result
        if clara_result:
            logger.info(f"[Phase 5] Clara: process_check={clara_result['process_check']}")

        report, _ = guarded("clara_report", 5, lambda: clara.compile_daily_report(today))
        if report:
            print("\n" + "=" * 60)
            print(report["full_report_md"])
            print("=" * 60)

    if results["errors"]:
        logger.error(
            "=== Daily cycle for %s finished WITH ERRORS: %s ===",
            today, ", ".join(results["errors"]),
        )
    else:
        logger.info(f"=== Daily cycle complete for {today} ===")

    return results


if __name__ == "__main__":
    # Exit non-zero when anything failed, so a scheduler (cron, Railway)
    # can tell a bad day from a quiet one. The cycle still completes and
    # still closes the books either way — this only reports.
    _result = run_daily_cycle()
    if _result and _result.get("errors"):
        sys.exit(1)


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