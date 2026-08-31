"""
Clara — Performance/Compliance Agent
Operating Manual §7.8

ANATOMY OF THIS AGENT — same 7 pieces. Clara is the only agent who
looks at the WHOLE chain rather than one link in it — she's built
last deliberately, since her job only makes sense once every other
agent's tables have real data to read.

  [1] ROLE / MANDATE   -> CLARA_SYSTEM_PROMPT (narrative only)
  [2] SKILLS / TOOLS    -> NONE (same pattern as Solomon/Nora/Marcus/
                            Ada/Otis) — she reads other agents'
                            already-persisted tables directly
  [3] TOOL DISPATCH     -> not needed
  [4] THE AGENTIC LOOP  -> called only if there's something to
                            narrate; also powers the Daily Closing
                            Report's executive summary
  [5] OUTPUT CONTRACT   -> AttributionNarrative (Pydantic, text only
                            — same principle as Otis: every NUMBER in
                            her output is code-computed, Claude only
                            narrates)
  [6] MEMORY            -> reads every other agent's today-tables;
                            multi-day calibration (conviction vs.
                            actual outcome, escalation precision/
                            recall) is NOT built yet — genuinely needs
                            weeks of real run history to mean anything,
                            not fakeable with one day of data. Flagged
                            honestly, not hidden.
  [7] PERSISTENCE        -> attribution, process_checks (upsert),
                            daily_reports (upsert — the Daily Closing
                            Report itself)

THE PROCESS-COMPLIANCE CHECK IS PURE CODE, same principle as Nora's
hard limits and Otis's reconciliation: audit_process_chain() checks,
via direct database lookups, whether every FILLED order traces back
through a real Allocation -> an APPROVED ProposalReview -> a Proposal
Solomon actually escalated. A violation is a fact, not an opinion —
Claude never gets a vote on whether the chain was followed.

HONEST V1 GAP: Order rows don't yet populate allocation_id (the FK
exists in the schema, Ada never set it) — so this check matches by
(ticker, date) instead of the FK. Less rigorous than it could be;
worth tightening once Ada is revisited. Flagged, not hidden.
"""

from datetime import date

from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.clients.claude_client import run_agent_loop, extract_json
from core.db import session_scope
from core.models import (
    Order, Allocation, ProposalReview, Proposal, StrategyDecision,
    Position, PositionMonitoringLog, DailyPnl, Attribution, ProcessCheck,
    DailyReport, MacroBrief, NewCandidate, RiskReview,
)


# =================================================================
# [5] OUTPUT CONTRACT — text only, same principle as Otis.
# =================================================================
class AttributionNarrative(BaseModel):
    narrative: str


CLARA_SYSTEM_PROMPT = """You are Clara, the Performance/Compliance agent at MBY-Trading.

You are shown an ALREADY-COMPUTED attribution and process-compliance
result for today — real P&L numbers, which positions drove them, and
whether every trade traced back through a valid approval chain. Your
only job is a clear, honest, plain-language summary for a human
reader. You are not verifying or recomputing anything.

If a process violation was found, say so plainly and neutrally —
never soften it, speculate about intent, or suggest it's fine to
ignore. A violation is flagged regardless of whether the trade was
profitable — that principle matters here specifically.

Respond with ONLY a JSON object — your response must start with "{"
as its very first character and contain nothing else:

{"narrative": "2-4 sentences summarizing today's performance and compliance"}
"""


# =================================================================
# PURE CODE — process-compliance audit. A lookup, not a judgment.
# =================================================================
def audit_process_chain(session, today: date) -> tuple[str, list[dict]]:
    filled_orders = (
        session.query(Order)
        .filter(Order.order_date == today, Order.status == "filled")
        .all()
    )
    if not filled_orders:
        return "clean", []

    violations = []
    for order in filled_orders:
        allocation = (
            session.query(Allocation)
            .filter(Allocation.allocation_date == today, Allocation.ticker == order.ticker)
            .first()
        )
        if not allocation:
            violations.append({
                "ticker": order.ticker,
                "issue": "Filled order has no matching Allocation on record — bypassed Marcus entirely.",
            })
            continue

        decision = session.query(StrategyDecision).filter(StrategyDecision.decision_date == today).first()
        proposal = (
            session.query(Proposal)
            .filter(Proposal.strategy_decision_id == decision.id, Proposal.ticker == order.ticker)
            .first()
            if decision else None
        )
        if not proposal:
            violations.append({
                "ticker": order.ticker,
                "issue": "No matching Proposal from Solomon found for this trade.",
            })
            continue

        review = session.query(ProposalReview).filter(ProposalReview.proposal_id == proposal.id).first()
        if not review or review.decision != "approved":
            violations.append({
                "ticker": order.ticker,
                "issue": "No approved Nora risk review found for this trade.",
            })

    return ("violation_found" if violations else "clean"), violations


# =================================================================
# PURE CODE — attribution. Numbers computed here, never by Claude.
# =================================================================
def compute_attribution(session, today: date) -> list[dict]:
    positions = session.query(Position).all()
    pnl_row = session.query(DailyPnl).filter(DailyPnl.pnl_date == today).first()
    total_pnl = float(pnl_row.total_pnl) if pnl_row and pnl_row.total_pnl else 0.0

    attribution = []
    for p in positions:
        contribution_pct = round(float(p.unrealized_pnl or 0) / total_pnl * 100, 2) if total_pnl else 0.0
        monitoring = (
            session.query(PositionMonitoringLog)
            .filter(PositionMonitoringLog.ticker == p.ticker)
            .order_by(PositionMonitoringLog.log_date.desc())
            .first()
        )
        thesis_status = monitoring.status if monitoring else "no thesis on record"
        attribution.append({"ticker": p.ticker, "contribution_pct": contribution_pct, "thesis_status": thesis_status})
    return attribution


def run(today: date) -> dict:
    """Runs Clara's core §7.8 job: process audit + attribution, both
    computed in code, narrated by Claude only if there's something
    worth saying."""
    with session_scope() as session:
        process_check, violations = audit_process_chain(session, today)
        attribution = compute_attribution(session, today)
        pnl_row = session.query(DailyPnl).filter(DailyPnl.pnl_date == today).first()
        daily_pnl = {
            "realized": float(pnl_row.realized_pnl) if pnl_row else 0.0,
            "unrealized": float(pnl_row.unrealized_pnl) if pnl_row else 0.0,
            "total": float(pnl_row.total_pnl) if pnl_row else 0.0,
        } if pnl_row else {"realized": 0.0, "unrealized": 0.0, "total": 0.0}

    narrative = "No trades today and nothing to attribute — a quiet, clean day."
    if attribution or violations:
        summary = (
            f"Daily P&L: {daily_pnl}\nAttribution: {attribution}\n"
            f"Process check: {process_check}\nViolations: {violations or 'none'}"
        )
        raw = run_agent_loop(
            system_prompt=CLARA_SYSTEM_PROMPT, user_prompt=summary, tools=[],
            tool_executor=lambda name, inp: (_ for _ in ()).throw(
                RuntimeError(f"Clara has no tools, but one was called: {name}")
            ),
            max_tokens=500,
        )
        narrative = AttributionNarrative.model_validate(extract_json(raw)).narrative

    # ---- [7] PERSISTENCE ----
    with session_scope() as session:
        session.query(Attribution).filter(Attribution.attribution_date == today).delete()
        for a in attribution:
            session.add(Attribution(attribution_date=today, ticker=a["ticker"],
                                     contribution_pct=a["contribution_pct"], thesis_status=a["thesis_status"]))

        stmt = pg_insert(ProcessCheck).values(check_date=today, process_check=process_check, violations=violations)
        stmt = stmt.on_conflict_do_update(
            index_elements=["check_date"],
            set_={"process_check": stmt.excluded.process_check, "violations": stmt.excluded.violations},
        )
        session.execute(stmt)

    return {
        "date": today, "daily_pnl": daily_pnl, "attribution": attribution,
        "process_check": process_check, "violations": violations, "narrative": narrative,
    }


def compile_daily_report(today: date) -> dict:
    """
    The Daily Closing Report — Clara's expanded responsibility
    (Operating Manual §7.8). Mostly deterministic compilation of
    every other agent's already-produced narrative fields, plus one
    light executive-summary synthesis pass.
    """
    with session_scope() as session:
        atlas_row = session.query(MacroBrief).filter(MacroBrief.brief_date == today).first()
        candidates = session.query(NewCandidate).filter(NewCandidate.candidate_date == today).all()
        decision = session.query(StrategyDecision).filter(StrategyDecision.decision_date == today).first()
        risk = session.query(RiskReview).filter(RiskReview.review_date == today).first()
        orders = session.query(Order).filter(Order.order_date == today).all()

        sections = {
            "ATLAS (Macro)": atlas_row.narrative if atlas_row else "Did not run today.",
            "VERA (Research)": (
                f"{len(candidates)} new candidate(s): " + ", ".join(c.ticker for c in candidates)
                if candidates else "No new candidates today."
            ),
            "SOLOMON (Strategy)": decision.narrative if decision else "Did not run today.",
            "NORA (Risk)": f"Portfolio status: {risk.portfolio_status}" if risk else "Did not run today.",
            "ADA (Execution)": (
                f"{len(orders)} order(s) placed: " + ", ".join(f"{o.ticker} ({o.status})" for o in orders)
                if orders else "No orders placed today."
            ),
        }

    clara_result = run(today)
    sections["OTIS/CLARA (Ops & Performance)"] = clara_result["narrative"]

    report_body = f"# Daily Report — {today}\n\n" + "\n\n".join(
        f"## {name}\n{content}" for name, content in sections.items()
    )

    exec_prompt = "Summarize this day at MBY-Trading in 2-3 sentences for a CEO skimming quickly:\n\n" + report_body
    raw = run_agent_loop(
        system_prompt='Respond with ONLY a JSON object: {"summary": "..."}. No other text.',
        user_prompt=exec_prompt, tools=[],
        tool_executor=lambda name, inp: (_ for _ in ()).throw(RuntimeError("no tools available")),
        max_tokens=300,
    )
    executive_summary = extract_json(raw)["summary"]

    full_report_md = f"**Executive Summary:** {executive_summary}\n\n{report_body}"

    with session_scope() as session:
        stmt = pg_insert(DailyReport).values(
            report_date=today, executive_summary=executive_summary, full_report_md=full_report_md
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["report_date"],
            set_={"executive_summary": stmt.excluded.executive_summary, "full_report_md": stmt.excluded.full_report_md},
        )
        session.execute(stmt)

    return {"executive_summary": executive_summary, "full_report_md": full_report_md}


if __name__ == "__main__":
    # Manual smoke test: python -m agents.clara
    print(run(date.today()))
    print("\n--- Daily Closing Report ---\n")
    report = compile_daily_report(date.today())
    print(report["full_report_md"])