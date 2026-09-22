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
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.clients.claude_client import run_agent_loop, extract_json
from core.benchmark import BENCHMARK_TICKER, performance_since_inception
from core.db import session_scope
from core.logging_utils import load_agent_output
from core.market_calendar import trading_days_between
from core.models import (
    Order, Allocation, ProposalReview, Proposal, StrategyDecision,
    Position, PositionMonitoringLog, PositionPnlHistory, DailyPnl,
    Attribution, ProcessCheck, DailyReport, MacroBrief, NewCandidate,
    RiskReview,
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
def compute_performance(today: date) -> dict:
    """
    The desk against the index (D4). Its own function so the headline
    figure is computed in code and merely narrated by Claude, like
    every other number Clara reports.

    WHY THIS IS THE HEADLINE AND ABSOLUTE P&L IS NOT. A desk that
    returned 4% in a month the index returned 6% lost money in the only
    sense that matters. Reporting the 4% as a good month is not a
    rounding error in the reporting, it is the wrong question answered
    confidently — and it was what this agent did until now.

    Returns a dict rather than the dataclass so it serialises straight
    into agent_runs.raw_output alongside everything else.
    """
    result = performance_since_inception(today)
    return {
        "measurable": result.measurable,
        "unavailable": result.unavailable,
        "summary": result.describe(),
        "sessions": result.sessions,
        "desk_return_pct": result.desk_return_pct,
        "benchmark": BENCHMARK_TICKER,
        "benchmark_return_pct": result.benchmark_return_pct,
        "active_return_pct": result.active_return_pct,
        "selection_pct": result.selection_pct,
        "cash_drag_pct": result.cash_drag_pct,
        "avg_invested_pct": result.avg_invested_pct,
        "tracking_error_pct": result.tracking_error_pct,
    }


# =================================================================
# ATTRIBUTION — A DAILY FLOW OVER A DAILY FLOW
#
# THE BUG THIS REPLACES, because it is worth naming precisely. The
# numerator used to be `positions.unrealized_pnl`, which is the
# position's LIFETIME open gain — a stock. The denominator was
# `daily_pnl.total_pnl`, today's move — a flow. Dividing one by the
# other produces a number with no meaning that nonetheless looks like
# a percentage: on 2026-09-17 it reported AAPL at +31.08% and NVDA at
# -188.48% of a day's P&L, which sum to -157% rather than 100%.
#
# `agents/otis.py` fixed this exact confusion on its own side and its
# docstring says so — "subtracting a cumulative stock from a daily
# flow ... and it fed Clara's attribution". The upstream half was
# repaired; the sentence named the downstream consumer and nobody
# followed through. This is that follow-through.
#
# THE NUMERATOR IS NOW THE POSITION'S CHANGE SINCE THE PREVIOUS
# SNAPSHOT, derived from position_pnl_history. And it REFUSES rather
# than approximating in three cases, because each of them would
# reproduce the same class of error in a new disguise:
#
#   1. The prior snapshot is not the previous session. A twelve-day
#      change over a one-day denominator is the original bug wearing
#      a different hat — and it is exactly the state the desk was in
#      last night, with snapshots on the 5th and the 17th.
#   2. A position has no prior snapshot at all (opened today). Its
#      day's move is real but is not a change in an open position,
#      and treating absence as zero would credit it with nothing.
#   3. Today's P&L is zero. A share of zero is undefined, not 0%.
#
# The dollar figure is reported alongside the percentage and is
# available in every case where the change itself is computable,
# because a reader wants "NVDA lost $188 today" more than a ratio.
# It is deliberately NOT persisted: the attribution table has no
# column for it, and adding one is a migration rather than part of
# an arithmetic fix.
# =================================================================
def open_unrealized_usd(market_value, unrealized_pnl_pct):
    """PURE. A position's absolute open gain, from the two figures
    position_pnl_history actually stores.

    If pct = (mv - cost) / cost, then cost = mv / (1 + pct/100) and the
    open gain is mv - cost. Reconstructed rather than stored because
    storing it would be a migration; exact given the inputs.

    Returns None when it cannot be derived — including a position down
    exactly 100%, where cost would divide by zero.
    """
    if market_value is None or unrealized_pnl_pct is None:
        return None
    mv, pct = float(market_value), float(unrealized_pnl_pct)
    denom = 1.0 + pct / 100.0
    if denom == 0:
        return None
    return mv - (mv / denom)


def compute_contributions(today_rows: list[dict], prior_rows: list[dict],
                          total_pnl: float,
                          sessions_since_prior) -> list[dict]:
    """PURE. Each position's share of today's P&L, or None with a
    stated reason.

    `today_rows` and `prior_rows` are dicts with ticker, market_value
    and unrealized_pnl_pct. `sessions_since_prior` is NYSE sessions
    between the two snapshot dates — 1 is adjacent, anything else is
    a gap, and None means there is no prior snapshot at all.
    """
    prior = {r["ticker"]: r for r in prior_rows}
    out = []

    for row in sorted(today_rows, key=lambda r: r["ticker"]):
        ticker = row["ticker"]
        entry = {"ticker": ticker, "contribution_usd": None,
                 "contribution_pct": None, "basis": ""}

        if sessions_since_prior is None:
            entry["basis"] = ("no earlier snapshot on record — first close "
                              "for this book")
            out.append(entry)
            continue
        if sessions_since_prior != 1:
            entry["basis"] = (
                f"previous snapshot is {sessions_since_prior} session(s) back, "
                f"not one — a multi-session change over a one-day P&L figure "
                f"would be meaningless, so it is not reported")
            out.append(entry)
            continue
        if ticker not in prior:
            entry["basis"] = ("no prior snapshot for this position — opened "
                              "since the last close")
            out.append(entry)
            continue

        now_abs = open_unrealized_usd(row.get("market_value"),
                                      row.get("unrealized_pnl_pct"))
        was_abs = open_unrealized_usd(prior[ticker].get("market_value"),
                                      prior[ticker].get("unrealized_pnl_pct"))
        if now_abs is None or was_abs is None:
            entry["basis"] = "market value or P&L percentage missing on one of the two snapshots"
            out.append(entry)
            continue

        change = now_abs - was_abs
        entry["contribution_usd"] = round(change, 2)
        if not total_pnl:
            entry["basis"] = ("today's total P&L is zero — a share of zero is "
                              "undefined, so only the dollar change is given")
        else:
            entry["contribution_pct"] = round(change / total_pnl * 100, 2)
            entry["basis"] = "change since the previous session, over today's total P&L"
        out.append(entry)

    return out


def compute_attribution(session, today: date) -> list[dict]:
    """DATABASE. Reads the two snapshots and the day's P&L, then defers
    to the pure function above for every judgement."""
    pnl_row = session.query(DailyPnl).filter(DailyPnl.pnl_date == today).first()
    total_pnl = float(pnl_row.total_pnl) if pnl_row and pnl_row.total_pnl else 0.0

    prior_date = (
        session.query(func.max(PositionPnlHistory.snapshot_date))
        .filter(PositionPnlHistory.snapshot_date < today)
        .scalar()
    )
    sessions_since_prior = (trading_days_between(prior_date, today)
                            if prior_date else None)

    def snaps(on_date):
        if on_date is None:
            return []
        return [{"ticker": r.ticker, "market_value": r.market_value,
                 "unrealized_pnl_pct": r.unrealized_pnl_pct}
                for r in session.query(PositionPnlHistory)
                .filter(PositionPnlHistory.snapshot_date == on_date).all()]

    contributions = compute_contributions(
        snaps(today), snaps(prior_date), total_pnl, sessions_since_prior)

    # Thesis status is a separate question from arithmetic, and the
    # "no thesis on record" case is a compliance finding rather than a
    # missing number — it is what surfaced NVDA on 2026-09-17.
    for entry in contributions:
        monitoring = (
            session.query(PositionMonitoringLog)
            .filter(PositionMonitoringLog.ticker == entry["ticker"])
            .order_by(PositionMonitoringLog.log_date.desc())
            .first()
        )
        entry["thesis_status"] = monitoring.status if monitoring else "no thesis on record"
    return contributions


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

    # Outside the session above: it opens its own, and holding two
    # would nest them for no reason.
    performance = compute_performance(today)

    narrative = "No trades today and nothing to attribute — a quiet, clean day."
    if attribution or violations:
        summary = (
            f"Performance vs {performance['benchmark']}: {performance['summary']}\n"
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
        "date": today,
        # First in the dict on purpose — it is the headline, and
        # absolute P&L below it is the secondary figure.
        "performance": performance,
        "daily_pnl": daily_pnl, "attribution": attribution,
        "process_check": process_check, "violations": violations, "narrative": narrative,
    }


def _vera_section(today: date, candidates: list) -> str:
    """Vera's line in the daily report.

    "No new candidates today." was true and useless: it is the same
    sentence whether nothing came close, one name missed the bar by a
    point, or the screening pass failed and the failure was swallowed.
    Vera now records a computed statement about the pass, so prefer it.

    READ FROM THE RUN LEDGER, NOT RECOMPUTED. The statement is a fact
    about what happened in that run, and re-deriving it here from
    today's tables would silently change it if a later rerun screened a
    different universe. Falls back to the candidate count when the
    statement is absent — a run predating this change, or a day Vera
    did not run at all, and those must not look identical either.
    """
    vera_output = load_agent_output(today, "vera")
    if vera_output is None:
        return "Did not run today."

    screening = vera_output.get("screening") or {}
    statement = screening.get("statement")
    if not statement:
        # Pre-change run: say only what such a row can support.
        return (
            f"{len(candidates)} new candidate(s): "
            + ", ".join(c.ticker for c in candidates)
            if candidates else
            "No new candidates today (run predates screening summaries, "
            "so how close anything came is not on record)."
        )

    held_back = vera_output.get("held_back") or []
    if held_back and not screening.get("surfaced_count"):
        # The near-misses are the interesting half on a quiet day, and
        # they are the whole reason this section changed.
        scores = ", ".join(
            f"{h['ticker']} {h['conviction_score']}"
            for h in sorted(held_back,
                            key=lambda h: -h.get("conviction_score", 0))
        )
        return f"{statement}\n\nScored: {scores}."
    return statement


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
            "VERA (Research)": _vera_section(today, candidates),
            "SOLOMON (Strategy)": decision.narrative if decision else "Did not run today.",
            "NORA (Risk)": f"Portfolio status: {risk.portfolio_status}" if risk else "Did not run today.",
            "ADA (Execution)": (
                f"{len(orders)} order(s) placed: " + ", ".join(f"{o.ticker} ({o.status})" for o in orders)
                if orders else "No orders placed today."
            ),
        }

    clara_result = run(today)
    sections["OTIS/CLARA (Ops & Performance)"] = clara_result["narrative"]

    # The comparison leads the report, above every agent's narrative
    # (D4). A closing report whose first number is absolute P&L trains
    # its reader to ask the wrong question — the desk's return only
    # means something next to the return of doing nothing instead.
    report_body = (
        f"# Daily Report — {today}\n\n"
        f"## PERFORMANCE vs {clara_result['performance']['benchmark']}\n"
        f"{clara_result['performance']['summary']}\n\n"
        + "\n\n".join(f"## {name}\n{content}" for name, content in sections.items())
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