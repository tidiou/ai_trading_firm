"""
Nora — Risk Manager Agent
Operating Manual §7.4

ANATOMY OF THIS AGENT — same 7 pieces, but Nora is the sharpest
example yet of the code/judgment split that runs through the whole
system. Read this file with one question in mind: "could Claude's
output ever change whether a trade is approved?" The answer must be
no, and the architecture below is built specifically to guarantee it.

  [1] ROLE / MANDATE   -> NORA_SYSTEM_PROMPT (used ONLY for narrative
                            explanation and qualitative notes — never
                            for the approve/reject decision itself)
  [2] SKILLS / TOOLS    -> NONE (same reasoning as Solomon)
  [3] TOOL DISPATCH     -> not needed
  [4] THE AGENTIC LOOP  -> called ONLY if there's something to review;
                            a quiet day with an empty portfolio and no
                            proposals never touches the API at all
  [5] OUTPUT CONTRACT   -> split in two, deliberately:
                            - RiskDecision (Python dataclass-like dict,
                              computed by check_hard_limits() — this
                              is the AUTHORITATIVE decision)
                            - QualitativeNote (Pydantic, from Claude —
                              has NO decision/max_size_pct field at
                              all, so there is nothing for the LLM
                              layer to override even if it tried)
  [6] MEMORY            -> get_active_risk_policy() (versioned, in
                            risk_policy_versions — auto-seeded from
                            config/risk_policy.py on first run) +
                            current portfolio state
  [7] PERSISTENCE        -> risk_reviews + risk_breaches + proposal_reviews

HOW THE HARD LIMIT ACTUALLY STAYS HARD: check_hard_limits() runs as
plain Python, before Claude is ever called, and its result (decision,
max_size_pct, rules_checked) is what gets persisted and returned.
Claude is handed that ALREADY-DECIDED result and asked only to write
qualitative_notes — a field that has no power to flip an approval.
Even a compromised or confused LLM response cannot change what gets
saved to the database, because the code never reads a decision field
from Claude's output — it doesn't ask for one.

KNOWN V1 LIMITATIONS (flagged honestly, not hidden):
- Sector concentration limits are NOT enforced yet — the positions
  table has no sector column. Revisit once Otis is built and can
  populate sector at buy time.
- The drawdown circuit breaker uses daily_pnl history, which is
  empty until Otis exists — it correctly reports "not breached"
  on an empty book rather than pretending to check something with
  no data.
"""

from datetime import date
from typing import Optional

from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.clients.claude_client import run_agent_loop, extract_json
from core.db import session_scope
from core.models import (
    RiskPolicyVersion, RiskReview, RiskBreach, ProposalReview,
    Proposal, StrategyDecision, Position, DailyPnl,
)
from config.risk_policy import DEFAULT_RISK_POLICY


# =================================================================
# [6] MEMORY — the risk policy itself. Auto-seeds from the config
# default on first use, then lives in the database from then on —
# future tuning happens by inserting a NEW version row (see
# Operating Manual: policy changes are versioned, never overwritten).
# =================================================================
def get_active_risk_policy(session, today: date) -> RiskPolicyVersion:
    policy = (
        session.query(RiskPolicyVersion)
        .filter(RiskPolicyVersion.effective_date <= today)
        .order_by(RiskPolicyVersion.effective_date.desc())
        .first()
    )
    if policy:
        return policy

    # First run ever — seed from the config default.
    policy = RiskPolicyVersion(
        effective_date=today,
        max_position_pct=DEFAULT_RISK_POLICY["max_position_pct"],
        max_sector_pct=DEFAULT_RISK_POLICY["max_sector_pct"],
        drawdown_breaker_pct=DEFAULT_RISK_POLICY["drawdown_breaker_pct"],
        min_position_count=DEFAULT_RISK_POLICY["min_position_count"],
        notes="Auto-seeded from config/risk_policy.py on first run.",
    )
    session.add(policy)
    session.flush()  # populate policy.id without ending the transaction
    return policy


# =================================================================
# DETERMINISTIC HARD-LIMIT CHECKS — plain Python, no LLM involved.
# This is the actual wall. Everything above it can be wrong; this
# cannot be talked out of its answer.
# =================================================================
def check_hard_limits(ticker: str, action: str, current_positions: list[Position],
                       policy: RiskPolicyVersion) -> dict:
    """
    Returns {"decision": "approved"|"rejected", "max_size_pct": float|None,
    "rules_checked": [...]}. This function's output is FINAL — nothing
    downstream (including Claude) can change it.
    """
    rules_checked = []

    if action in ("exit", "trim"):
        # Reducing risk is always allowed from a limits perspective.
        rules_checked.append("reduction (exit/trim) — no position-limit check needed")
        return {"decision": "approved", "max_size_pct": None, "rules_checked": rules_checked}

    # action in ("new_position", "add")
    existing = next((p for p in current_positions if p.ticker == ticker), None)
    existing_weight = float(existing.weight_pct) if existing and existing.weight_pct else 0.0
    max_pct = float(policy.max_position_pct)

    rules_checked.append(f"max_position_pct: existing {existing_weight}% vs limit {max_pct}%")
    if existing_weight >= max_pct:
        return {
            "decision": "rejected",
            "max_size_pct": 0.0,
            "rules_checked": rules_checked + [f"REJECTED: {ticker} already at or above {max_pct}% limit"],
        }

    return {"decision": "approved", "max_size_pct": max_pct, "rules_checked": rules_checked}


def check_drawdown_circuit_breaker(session, policy: RiskPolicyVersion) -> dict:
    """
    Checks portfolio-wide drawdown from peak. Returns
    {"breached": bool, "current_drawdown_pct": float}.
    Correctly reports "not breached" on an empty book (no data yet)
    rather than fabricating a number — see module docstring.
    """
    history = session.query(DailyPnl).order_by(DailyPnl.pnl_date).all()
    if not history:
        return {"breached": False, "current_drawdown_pct": 0.0}

    peak = max(float(r.total_pnl) for r in history)
    current = float(history[-1].total_pnl)
    if peak == 0:
        return {"breached": False, "current_drawdown_pct": 0.0}

    drawdown_pct = (current - peak) / abs(peak) * 100
    breached = drawdown_pct <= float(policy.drawdown_breaker_pct)
    return {"breached": breached, "current_drawdown_pct": round(drawdown_pct, 2)}


# =================================================================
# [5] OUTPUT CONTRACT — Claude's side. Deliberately has NO decision
# or max_size_pct field — there is nothing here for the LLM to
# override even if it wanted to.
# =================================================================
class QualitativeNote(BaseModel):
    ticker: str
    qualitative_notes: str  # correlation risk, thematic overlap, anything a hard rule wouldn't catch


# =================================================================
# [1] ROLE / MANDATE — used only to generate qualitative_notes on
# top of already-final decisions.
# =================================================================
NORA_SYSTEM_PROMPT = """You are Nora, the Risk Manager agent at MBY-Trading.

You are being shown proposals that have ALREADY been approved or
rejected by deterministic risk-limit checks — that decision is final
and you cannot change it. Your job is narrower: for each APPROVED
proposal, add any qualitative or correlation risk you notice that a
simple percentage-limit rule wouldn't catch — for example, thematic
overlap with an existing holding, or a sector-level concentration
building up even though no single hard limit is breached yet.

If you see no meaningful qualitative concern for a given ticker, say
so plainly and briefly — that is a valid, common answer.

Respond with ONLY a JSON array — your response must start with "["
as its very first character and contain nothing else. Do your
reasoning silently and output only the final result, matching this
shape:

[
  {"ticker": "...", "qualitative_notes": "1-2 sentences, or 'No additional concerns noted.'"}
]
"""


def run(today: date) -> dict:
    """
    Runs Nora: reads today's Solomon decision (if any), the current
    portfolio, and the active risk policy — then applies hard limits
    in code first, and only calls Claude if there's something
    approved worth a qualitative pass. On a quiet day with nothing
    to review, this never touches the API at all.
    """
    with session_scope() as session:
        policy = get_active_risk_policy(session, today)
        current_positions = session.query(Position).all()

        decision = (
            session.query(StrategyDecision)
            .filter(StrategyDecision.decision_date == today)
            .first()
        )
        proposals = (
            session.query(Proposal).filter(Proposal.strategy_decision_id == decision.id).all()
            if decision else []
        )

        drawdown = check_drawdown_circuit_breaker(session, policy)
        existing_breaches = []
        if drawdown["breached"]:
            existing_breaches.append({
                "ticker": None,
                "rule_violated": "portfolio_drawdown_circuit_breaker",
                "current_value": drawdown["current_drawdown_pct"],
                "limit_value": float(policy.drawdown_breaker_pct),
            })

        # [4] Hard limits — plain Python, computed before any LLM call.
        reviews = []
        for p in proposals:
            hard_result = check_hard_limits(p.ticker, p.action, current_positions, policy)
            reviews.append({"proposal_id": p.id, "ticker": p.ticker, **hard_result})

    # Only call Claude if there's an APPROVED proposal worth a
    # qualitative pass — never spend a call on a quiet day.
    approved_tickers = [r["ticker"] for r in reviews if r["decision"] == "approved"]
    qualitative_notes_by_ticker = {}
    if approved_tickers:
        portfolio_summary = (
            "\n".join(f"{p.ticker}: {p.weight_pct}%" for p in current_positions)
            or "Portfolio is currently empty."
        )
        user_prompt = (
            f"Approved proposals to review qualitatively: {approved_tickers}\n"
            f"Current portfolio:\n{portfolio_summary}"
        )
        raw = run_agent_loop(
            system_prompt=NORA_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            tools=[],
            tool_executor=lambda name, inp: (_ for _ in ()).throw(
                RuntimeError(f"Nora has no tools, but one was called: {name}")
            ),
            max_tokens=1500,
        )
        notes = [QualitativeNote.model_validate(item) for item in extract_json(raw)]
        qualitative_notes_by_ticker = {n.ticker: n.qualitative_notes for n in notes}

    for r in reviews:
        code_summary = "; ".join(r["rules_checked"])
        qual = qualitative_notes_by_ticker.get(r["ticker"], "")
        r["reasoning"] = f"{code_summary}" + (f" | {qual}" if qual else "")

    circuit_breaker_active = drawdown["breached"]
    portfolio_status = (
        "breach_hard" if existing_breaches
        else ("breach_warning" if any(r["decision"] == "rejected" for r in reviews)
              else "within_limits")
    )

    # =============================================================
    # [7] PERSISTENCE
    # =============================================================
    with session_scope() as session:
        stmt = pg_insert(RiskReview).values(
            review_date=today,
            portfolio_status=portfolio_status,
            circuit_breaker_active=circuit_breaker_active,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["review_date"],
            set_={
                "portfolio_status": stmt.excluded.portfolio_status,
                "circuit_breaker_active": stmt.excluded.circuit_breaker_active,
            },
        )
        result = session.execute(stmt.returning(RiskReview.id))
        review_id = result.scalar_one()

        # Replace this run's breaches/reviews wholesale — same pattern
        # as Solomon's proposals: these rows only have identity as
        # "the set produced by today's review."
        session.query(RiskBreach).filter(RiskBreach.risk_review_id == review_id).delete()
        for b in existing_breaches:
            session.add(RiskBreach(risk_review_id=review_id, **b))

        for r in reviews:
            session.query(ProposalReview).filter(ProposalReview.proposal_id == r["proposal_id"]).delete()
            session.add(ProposalReview(
                proposal_id=r["proposal_id"],
                decision=r["decision"],
                max_size_pct=r["max_size_pct"],
                rules_checked=r["rules_checked"],
                reasoning=r["reasoning"],
            ))

    return {
        "date": today,
        "portfolio_status": portfolio_status,
        "existing_breaches": existing_breaches,
        "proposal_reviews": reviews,
        "circuit_breaker_active": circuit_breaker_active,
    }


if __name__ == "__main__":
    # Manual smoke test: python -m agents.nora
    # Chains the full pipeline so far. Since the portfolio is likely
    # empty and Solomon may find nothing today, this also runs a
    # clearly-labeled SYNTHETIC test proposal so you can actually see
    # the hard-limit logic fire at least once.
    from agents import atlas, vera, solomon

    print("Running Atlas...")
    atlas_result = atlas.run(date.today())
    print("Running Vera...")
    vera_result = vera.run(date.today(), atlas_result)
    print("Running Solomon...")
    solomon_result = solomon.run(date.today(), atlas_result, vera_result)
    print("Running Nora (real proposals, if any)...\n")

    result = run(date.today())
    print("REAL RESULT:", result)

    if not result["proposal_reviews"]:
        print("\n--- No real proposals today. Running a SYNTHETIC test of ---")
        print("--- check_hard_limits() directly, bypassing the DB, just  ---")
        print("--- to demonstrate the hard-limit logic in isolation.     ---\n")
        with session_scope() as session:
            test_policy = get_active_risk_policy(session, date.today())
            test_positions = session.query(Position).all()
        test_result = check_hard_limits("AAPL", "new_position", test_positions, test_policy)
        print("SYNTHETIC test — proposing AAPL as a new_position:", test_result)