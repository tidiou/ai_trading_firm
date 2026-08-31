"""
Marcus — Portfolio Manager Agent
Operating Manual §7.5

ANATOMY OF THIS AGENT — same 7 pieces, and a genuinely useful
contrast to Nora's design. Read this alongside nora.py: both split
code and judgment, but in different proportions and for different
reasons.

  [1] ROLE / MANDATE   -> MARCUS_SYSTEM_PROMPT
  [2] SKILLS / TOOLS    -> NONE (same reasoning as Solomon/Nora — he
                            reasons over already-computed figures,
                            doesn't fetch anything himself)
  [3] TOOL DISPATCH     -> not needed
  [4] THE AGENTIC LOOP  -> called only if there's an approved
                            proposal to size; skipped on a quiet day
  [5] OUTPUT CONTRACT   -> AllocationAdjustment (Pydantic) — DOES
                            include a size field, unlike Nora's
                            QualitativeNote. See below for why that's
                            safe here but wasn't safe for Nora.
  [6] MEMORY            -> current portfolio positions + each
                            ticker's latest Vera status (for the
                            at-risk guardrail)
  [7] PERSISTENCE        -> allocations table, delete-then-replace
                            per day (same pattern as Solomon's
                            proposals — no natural per-row identity
                            outside "what this run produced")

HOW MARCUS DIFFERS FROM NORA, DELIBERATELY: Nora's decision (approve/
reject, the ceiling itself) can NEVER be touched by an LLM — that's
enforced by giving Claude's output contract no decision field at all.
Marcus is different: the Operating Manual explicitly wants Claude
adjusting the code-computed STARTING size for portfolio-construction
nuance (correlation with existing holdings, splitting limited cash
across competing ideas). So here, Claude's output DOES include a
size. What stays hard, in code, regardless of what Claude says:
  - the final size can never exceed Nora's ceiling (clamped in code)
  - the final size can never be negative (clamped in code)
  - size can never be INCREASED on a position Vera flagged at_risk
    (rejected in code before Claude is even asked)
Claude gets real room to adjust; the walls around that room are code.

DECISION RIGHTS: Marcus can size within Nora's ceiling and prioritize
among competing proposals. He cannot exceed the ceiling, approve
something Nora rejected, or execute — there is no execution tool on
his menu, same pattern as everyone before him.
"""

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.clients.claude_client import run_agent_loop, extract_json
from core.db import session_scope
from core.models import (
    ProposalReview, Proposal, StrategyDecision, NewCandidate,
    PositionMonitoringLog, Position, Allocation,
)

# Marcus-owned constant — a portfolio-construction discipline, NOT
# one of Nora's hard risk limits (those live in risk_policy_versions,
# Nora's domain). Deliberately kept separate: this is about how
# Marcus builds the book, not about what's allowed at all.
MIN_CASH_RESERVE_PCT = 5.0

# Deterministic conviction-to-size tier — the starting point Claude
# adjusts from. Conviction 1-2 shouldn't reach Marcus at all (Solomon
# requires >=4 for new_position), the 3-tier exists as a defensive
# fallback, not an expected path.
CONVICTION_SIZE_TIERS = {5: 1.0, 4: 0.7, 3: 0.4}


# =================================================================
# [5] OUTPUT CONTRACT — DOES include a size, unlike Nora's
# QualitativeNote. See module docstring for why that's the right
# call here.
# =================================================================
class AllocationAdjustment(BaseModel):
    ticker: str
    adjusted_size_pct: float
    rationale: str
    priority: int


# =================================================================
# [1] ROLE / MANDATE
# =================================================================
MARCUS_SYSTEM_PROMPT = """You are Marcus, the Portfolio Manager agent at MBY-Trading.

You are given proposals that have ALREADY cleared risk review, each
with a code-computed STARTING size based on conviction. Your job is
to adjust that starting point for portfolio construction — things a
simple formula can't see:
- Does this create too much overlap/correlation with an existing holding?
- If multiple proposals compete for limited cash, which deserves priority?
- Does anything here warrant sizing below the starting point out of caution?

Hard constraints you must respect (violating these will simply be
overridden by code afterward, so respect them anyway):
- You may adjust a size DOWN from its starting point freely.
- You may adjust a size UP, but never above the ceiling you're shown
  for that ticker — the ceiling is not yours to exceed.
- You cannot invent a new ticker or drop one you weren't given.

If multiple proposals are given, assign a priority (1 = highest) —
this matters when there isn't enough cash-reserve budget for all of
them.

Respond with ONLY a JSON array — your response must start with "["
as its very first character and contain nothing else. Do your
reasoning silently and output only the final result:

[
  {"ticker": "...", "adjusted_size_pct": 0.0, "rationale": "1-2 sentences", "priority": 1}
]
"""


def _get_conviction_for_ticker(session, today: date, ticker: str) -> int:
    """Looks up the conviction score behind a proposal — from
    today's new candidates first (new_position/add), falling back to
    the latest position-monitoring conviction (trim/exit)."""
    candidate = (
        session.query(NewCandidate)
        .filter(NewCandidate.candidate_date == today, NewCandidate.ticker == ticker)
        .first()
    )
    if candidate:
        return candidate.conviction_score

    monitoring = (
        session.query(PositionMonitoringLog)
        .filter(PositionMonitoringLog.ticker == ticker)
        .order_by(PositionMonitoringLog.log_date.desc())
        .first()
    )
    return monitoring.conviction_score if monitoring else 3  # conservative default


def _get_latest_status(session, ticker: str) -> Optional[str]:
    """The at-risk guardrail check needs Vera's most recent verdict on this ticker."""
    monitoring = (
        session.query(PositionMonitoringLog)
        .filter(PositionMonitoringLog.ticker == ticker)
        .order_by(PositionMonitoringLog.log_date.desc())
        .first()
    )
    return monitoring.status if monitoring else None


def run(today: date) -> dict:
    """
    Runs Marcus: reads today's approved proposals from Nora, computes
    a deterministic starting size per ticker, applies the code-level
    guardrails (at-risk block, ceiling clamp), optionally lets Claude
    adjust within those bounds, then persists final allocations.
    """
    with session_scope() as session:
        approved = (
            session.query(ProposalReview, Proposal)
            .join(Proposal, ProposalReview.proposal_id == Proposal.id)
            .join(StrategyDecision, Proposal.strategy_decision_id == StrategyDecision.id)
            .filter(StrategyDecision.decision_date == today, ProposalReview.decision == "approved")
            .all()
        )

        current_positions = {p.ticker: p for p in session.query(Position).all()}
        deployed_pct = sum(float(p.weight_pct or 0) for p in current_positions.values())
        available_pct = max(0.0, 100.0 - MIN_CASH_RESERVE_PCT - deployed_pct)

        # -------------------------------------------------------
        # DETERMINISTIC starting point + hard guardrails, all in
        # code, all computed before Claude is ever involved.
        # -------------------------------------------------------
        base_allocations = []
        for review, proposal in approved:
            if proposal.action in ("exit", "trim"):
                existing_weight = float(current_positions[proposal.ticker].weight_pct) \
                    if proposal.ticker in current_positions else 0.0
                target = 0.0 if proposal.action == "exit" else round(existing_weight * 0.5, 2)
                base_allocations.append({
                    "ticker": proposal.ticker, "action": proposal.action,
                    "base_size_pct": target, "ceiling_pct": None,
                    "conviction": None, "blocked_reason": None,
                })
                continue

            # new_position / add
            status = _get_latest_status(session, proposal.ticker)
            if status == "at_risk":
                # HARD, code-enforced: never increase size on an
                # at_risk position — Claude is never even asked about
                # this ticker, matching how firmly the Operating
                # Manual states this rule.
                base_allocations.append({
                    "ticker": proposal.ticker, "action": proposal.action,
                    "base_size_pct": 0.0, "ceiling_pct": review.max_size_pct,
                    "conviction": None,
                    "blocked_reason": "Blocked: cannot increase size on an at_risk position (code-enforced).",
                })
                continue

            conviction = _get_conviction_for_ticker(session, today, proposal.ticker)
            ceiling = float(review.max_size_pct or 0)
            multiplier = CONVICTION_SIZE_TIERS.get(conviction, 0.0)
            base_size = round(ceiling * multiplier, 2)
            base_allocations.append({
                "ticker": proposal.ticker, "action": proposal.action,
                "base_size_pct": base_size, "ceiling_pct": ceiling,
                "conviction": conviction, "blocked_reason": None,
            })

    # -------------------------------------------------------
    # [4] THE AGENTIC LOOP — only for tickers not already blocked,
    # and only if there's genuinely something to adjust.
    # -------------------------------------------------------
    adjustable = [a for a in base_allocations if a["blocked_reason"] is None and a["action"] in ("new_position", "add")]
    adjustments_by_ticker = {}
    if adjustable:
        portfolio_summary = (
            "\n".join(f"{t}: {p.weight_pct}%" for t, p in current_positions.items())
            or "Portfolio is currently empty."
        )
        proposals_summary = "\n".join(
            f"{a['ticker']}: starting size {a['base_size_pct']}%, ceiling {a['ceiling_pct']}%, conviction {a['conviction']}/5"
            for a in adjustable
        )
        user_prompt = (
            f"Available cash-reserve budget for new allocations today: {available_pct:.1f}%\n"
            f"Current portfolio:\n{portfolio_summary}\n\n"
            f"Proposals to size:\n{proposals_summary}"
        )
        raw = run_agent_loop(
            system_prompt=MARCUS_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            tools=[],
            tool_executor=lambda name, inp: (_ for _ in ()).throw(
                RuntimeError(f"Marcus has no tools, but one was called: {name}")
            ),
            max_tokens=1500,
        )
        adjustments = [AllocationAdjustment.model_validate(item) for item in extract_json(raw)]
        adjustments_by_ticker = {a.ticker: a for a in adjustments}

    # -------------------------------------------------------
    # FINAL CLAMP — code, always applied, regardless of what
    # Claude said. This is the wall around Claude's room to adjust.
    # -------------------------------------------------------
    final_allocations = []
    for a in base_allocations:
        if a["blocked_reason"]:
            final_allocations.append({
                "ticker": a["ticker"], "action": a["action"], "target_size_pct": 0.0,
                "conviction_input": None, "rationale": a["blocked_reason"], "priority": None,
            })
            continue

        if a["action"] in ("exit", "trim"):
            final_allocations.append({
                "ticker": a["ticker"], "action": a["action"], "target_size_pct": a["base_size_pct"],
                "conviction_input": None, "rationale": f"{a['action']} sized by code (v1 simplification).",
                "priority": None,
            })
            continue

        adj = adjustments_by_ticker.get(a["ticker"])
        claude_size = adj.adjusted_size_pct if adj else a["base_size_pct"]
        clamped = max(0.0, min(claude_size, a["ceiling_pct"]))  # THE hard clamp
        final_allocations.append({
            "ticker": a["ticker"], "action": a["action"], "target_size_pct": round(clamped, 2),
            "conviction_input": a["conviction"],
            "rationale": adj.rationale if adj else f"Base conviction-tier size, ceiling {a['ceiling_pct']}%.",
            "priority": adj.priority if adj else 1,
        })

    # =============================================================
    # [7] PERSISTENCE — delete-then-replace today's allocations,
    # same pattern as Solomon's proposals.
    # =============================================================
    with session_scope() as session:
        session.query(Allocation).filter(Allocation.allocation_date == today).delete()
        for a in final_allocations:
            session.add(Allocation(
                allocation_date=today,
                ticker=a["ticker"],
                action=a["action"],
                target_size_pct=a["target_size_pct"],
                conviction_input=a["conviction_input"],
                rationale=a["rationale"],
                priority=a["priority"],
            ))

    return {"date": today, "allocations": final_allocations, "cash_reserve_pct": MIN_CASH_RESERVE_PCT}


if __name__ == "__main__":
    # Manual smoke test: python -m agents.marcus
    from agents import atlas, vera, solomon, nora

    print("Running Atlas...")
    atlas_result = atlas.run(date.today())
    print("Running Vera...")
    vera_result = vera.run(date.today(), atlas_result)
    print("Running Solomon...")
    solomon_result = solomon.run(date.today(), atlas_result, vera_result)
    print("Running Nora...")
    nora_result = nora.run(date.today())
    print("Running Marcus...\n")

    result = run(date.today())
    print(result)