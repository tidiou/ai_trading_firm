"""
Testing-only helpers. NEVER used by orchestrator.py.

WHAT THESE ARE FOR: Solomon, Nora and Marcus each have a `__main__`
smoke test that needs Atlas's and Vera's output as input. Running the
real chain every time means Vera re-screens the universe, and at 2 FMP
calls per ticker against a 250-calls/day free tier, a few iterations of
"run it and see" exhausts the day's quota — which is how testing kept
stalling during the build.

So these read today's ALREADY-PERSISTED Atlas/Vera output and rebuild
the same dict shape their run() functions return, falling back to
actually running the agent when there is nothing on record yet.

WHY THIS FILE IS SEPARATE, AND WHY THE ORCHESTRATOR MUST NOT IMPORT IT:
reusing a cached morning read is correct for a smoke test and wrong for
a real cycle. The desk's whole premise is a fresh look at the market
each day; orchestrator.py therefore always calls atlas.run() and
vera.run() directly, and nothing here is on that path.

HONEST LIMITATION — orphan_reviews. Vera's undocumented-position review
only persists its "adopt" outcomes (as a Thesis); "exit" recommendations
live in the returned dict and nowhere else. So a cached Vera output
returns an empty orphan_reviews list rather than pretending to
reconstruct it. Downstream, Solomon reads that as "no undocumented
positions", which is a real difference from a live run — if you are
specifically testing orphan handling, call vera.run() directly.

The deeper fix for the "did this agent already run today?" question is
the agent_runs table, which nothing currently writes (see the control
review, D8). Until it does, this file infers from the artefacts each
agent leaves behind, which is workable but is inference rather than a
record.
"""

from datetime import date

from core.db import session_scope
from core.models import MacroBrief, NewCandidate, PositionMonitoringLog, Thesis


def get_or_run_atlas(today: date) -> dict:
    """
    Today's macro brief, from the database if Atlas already ran.
    Returns the same shape as atlas.run() — i.e. AtlasOutput.model_dump().
    """
    with session_scope() as session:
        brief = (
            session.query(MacroBrief)
            .filter(MacroBrief.brief_date == today)
            .first()
        )
        if brief is not None:
            print(f"Reusing today's Atlas brief from the database (regime={brief.regime_signal}).")
            return {
                "date": brief.brief_date,
                "regime_signal": brief.regime_signal,
                "key_events_today": brief.key_events or [],
                "notable_overnight_moves": brief.notable_moves or [],
                "change_from_yesterday": brief.change_from_yesterday,
                "confidence": brief.confidence,
                "narrative": brief.narrative or "",
            }

    print("No Atlas brief for today — running Atlas.")
    from agents import atlas
    return atlas.run(today)


def get_or_run_vera(today: date, atlas_output: dict) -> dict:
    """
    Today's Vera output, from the database if she already ran.
    Returns the same shape as vera.run().

    Deciding whether she ran is inference, not a record (see the module
    docstring). The rule: if the firm holds documented positions, a
    completed monitoring pass must have left one log row per open thesis
    today. If it holds none, monitoring is legitimately empty and
    today's candidate rows are the only evidence. When neither test can
    speak, we re-run rather than hand back a confidently empty result —
    spending FMP quota is the cheaper mistake.
    """
    with session_scope() as session:
        open_theses = session.query(Thesis).filter(Thesis.closed_date.is_(None)).count()

        monitoring_rows = (
            session.query(PositionMonitoringLog)
            .filter(PositionMonitoringLog.log_date == today)
            .all()
        )
        candidate_rows = (
            session.query(NewCandidate)
            .filter(NewCandidate.candidate_date == today)
            .all()
        )

        ran_today = (
            (open_theses > 0 and len(monitoring_rows) > 0)
            or (open_theses == 0 and len(candidate_rows) > 0)
        )

        if ran_today:
            print(
                f"Reusing today's Vera output from the database "
                f"({len(monitoring_rows)} monitored, {len(candidate_rows)} candidate(s))."
            )
            return {
                "monitoring": [
                    {
                        "ticker": m.ticker,
                        "status": m.status,
                        "trigger": m.trigger,
                        "reasoning": m.reasoning,
                        "conviction_score": m.conviction_score,
                    }
                    for m in monitoring_rows
                ],
                "candidates": [
                    {
                        "ticker": c.ticker,
                        "thesis": c.thesis,
                        "catalyst": c.catalyst,
                        "conviction_score": c.conviction_score,
                        "key_risks": c.key_risks or [],
                        "valuation_snapshot": c.valuation_snapshot or {},
                    }
                    for c in candidate_rows
                ],
                # Not reconstructible — see the module docstring.
                "orphan_reviews": [],
            }

    print("No Vera output for today — running Vera (this WILL spend FMP quota).")
    from agents import vera
    return vera.run(today, atlas_output)
