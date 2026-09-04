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

=================================================================
NORA HAS TWO JOBS, AND THEY RUN ON DIFFERENT SCHEDULES
=================================================================

This is the single most important structural point in this file.

  review_proposals(today)   Phase 3. Gated on Solomon escalating.
                            Reviews each proposal against the limits.
                            If there is nothing to review, there is
                            genuinely nothing to do.

  monitor_portfolio(today)  Phase 5. Runs EVERY trading day, always,
                            regardless of whether anyone proposed
                            anything. Re-checks the book as it stands
                            against every limit, and evaluates the
                            drawdown circuit breaker.

They used to be one function gated behind Solomon's action_needed,
which meant that on a quiet day — most days, by design — no limit was
checked at all. That is backwards: risk is not downstream of the
front office's appetite to trade. On a real desk, market risk runs at
the close every single day on the book as it stands, whether or not
anybody wanted to do anything, and that independence IS the control.

It also has a concrete consequence. A position that drifts from 7% to
11% purely on price appreciation was never caught, because no proposal
was ever made about it — which is exactly the scenario §7.4 names as
the reason daily monitoring exists ("price moves alone can breach
limits").

=================================================================
HOW A LIMIT CHECK IS SHAPED — POST-TRADE, NOT PRE-TRADE
=================================================================

check_hard_limits() answers "what would the book look like AFTER this
trade, and is that state allowed?" — never "is the book allowed right
now?" The latter is a question nobody asked, and answering it was how
an `add` to a 7% position could legally take it to ~12.6%: the
ceiling handed downstream was the full 8% limit rather than the 1%
of headroom actually remaining.

So max_size_pct is now HEADROOM, not the limit. It is the additional
weight this trade may add, already net of what is held. Marcus sizes
within it and Ada converges on it; nobody has to remember to subtract.
"""

from datetime import date
from typing import Optional

from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.clients.claude_client import run_agent_loop, extract_json
from core.db import session_scope
from core.models import (
    RiskPolicyVersion, RiskReview, RiskBreach, ProposalReview,
    Proposal, StrategyDecision, Position, DailyPnl, Thesis, NewCandidate,
)
from config.risk_policy import DEFAULT_RISK_POLICY

# A position whose sector we don't know is NOT quietly pooled with the
# other unknowns — that would let genuine concentration hide inside a
# bucket labelled "unknown". Each unknown-sector name is treated as its
# own single-name sector, which is the conservative reading: it can
# never mask a build-up, only ever flag one.
UNKNOWN_SECTOR_PREFIX = "__unknown__:"

# Breach `source` values — risk_breaches.source is NOT NULL, and these
# are the two legitimate origins. Portfolio breaches come from
# monitor_portfolio(); proposal breaches from review_proposals().
SOURCE_PORTFOLIO = "existing_position"
SOURCE_PROPOSAL = "proposal"


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
        loss_review_pct=DEFAULT_RISK_POLICY["loss_review_pct"],
        profit_review_pct=DEFAULT_RISK_POLICY["profit_review_pct"],
        notes="Auto-seeded from config/risk_policy.py on first run.",
    )
    session.add(policy)
    session.flush()  # populate policy.id without ending the transaction
    return policy


# =================================================================
# SECTOR RESOLUTION
#
# Sector is stored on the position at reconciliation time (Otis
# carries it across from Vera's research). Vera gets it for free from
# the FMP profile call she already makes, so this costs no extra
# quota — which matters, because the free tier is 250 calls/day and
# re-fetching a sector that changes maybe once a decade would be a
# poor way to spend it.
# =================================================================
def _sector_key(ticker: str, sector: Optional[str]) -> str:
    """Sector bucket for a ticker. Unknown sector -> its own bucket."""
    if sector and sector.strip():
        return sector.strip()
    return f"{UNKNOWN_SECTOR_PREFIX}{ticker}"


def resolve_proposed_sector(session, ticker: str) -> Optional[str]:
    """
    Sector for a ticker that may not be held yet. Held position first
    (Otis keeps it current), then the open thesis, then today's
    candidate row. Returns None if genuinely unknown — callers must
    handle that rather than guessing.
    """
    position = session.query(Position).filter(Position.ticker == ticker).first()
    if position is not None and getattr(position, "sector", None):
        return position.sector

    thesis = (
        session.query(Thesis)
        .filter(Thesis.ticker == ticker, Thesis.closed_date.is_(None))
        .order_by(Thesis.opened_date.desc())
        .first()
    )
    if thesis is not None and getattr(thesis, "sector", None):
        return thesis.sector

    candidate = (
        session.query(NewCandidate)
        .filter(NewCandidate.ticker == ticker)
        .order_by(NewCandidate.candidate_date.desc())
        .first()
    )
    if candidate is not None and getattr(candidate, "sector", None):
        return candidate.sector

    return None


def sector_weights(current_positions: list[Position]) -> dict[str, float]:
    """Current portfolio weight aggregated by sector bucket."""
    weights: dict[str, float] = {}
    for p in current_positions:
        key = _sector_key(p.ticker, getattr(p, "sector", None))
        weights[key] = weights.get(key, 0.0) + float(p.weight_pct or 0)
    return weights


# =================================================================
# DETERMINISTIC HARD-LIMIT CHECKS — plain Python, no LLM involved.
# This is the actual wall. Everything above it can be wrong; this
# cannot be talked out of its answer.
# =================================================================
def check_hard_limits(ticker: str, action: str, current_positions: list[Position],
                      policy: RiskPolicyVersion,
                      proposed_sector: Optional[str] = None,
                      circuit_breaker_active: bool = False) -> dict:
    """
    Returns {"decision": "approved"|"rejected", "max_size_pct": float|None,
    "rules_checked": [...]}. This function's output is FINAL — nothing
    downstream (including Claude) can change it.

    max_size_pct is HEADROOM: the additional portfolio weight this
    trade may take, already net of what is currently held. It is the
    minimum of the position headroom and the sector headroom, so a
    name can be blocked by its sector even when it has room of its own.
    """
    rules_checked = []

    if action in ("exit", "trim"):
        # Reducing risk is always allowed from a limits perspective —
        # including while the circuit breaker is active. A breaker that
        # stopped you de-risking would be worse than no breaker at all.
        rules_checked.append("reduction (exit/trim) — no position-limit check needed")
        return {"decision": "approved", "max_size_pct": None, "rules_checked": rules_checked}

    # ---- action in ("new_position", "add") -----------------------

    # The circuit breaker freezes NEW RISK, and it does so here, at the
    # authoritative decision point, rather than only as a flag on a
    # dashboard. Marcus and Ada check it too — defence in depth — but
    # this is the check that makes it real.
    if circuit_breaker_active:
        return {
            "decision": "rejected",
            "max_size_pct": 0.0,
            "rules_checked": rules_checked + [
                "REJECTED: portfolio drawdown circuit breaker is ACTIVE — "
                "no new positions or adds while frozen (reductions still permitted)"
            ],
        }

    existing = next((p for p in current_positions if p.ticker == ticker), None)
    existing_weight = float(existing.weight_pct) if existing and existing.weight_pct else 0.0
    max_pct = float(policy.max_position_pct)

    # ---- limit 1: single-position weight, checked POST-trade ----
    position_headroom = round(max_pct - existing_weight, 4)
    rules_checked.append(
        f"max_position_pct: {ticker} holds {existing_weight}%, limit {max_pct}%, "
        f"headroom {max(position_headroom, 0.0)}%"
    )
    if position_headroom <= 0:
        return {
            "decision": "rejected",
            "max_size_pct": 0.0,
            "rules_checked": rules_checked + [
                f"REJECTED: {ticker} already at or above the {max_pct}% position limit"
            ],
        }

    # ---- limit 2: sector weight, also POST-trade ----
    sector = proposed_sector if proposed_sector else (
        getattr(existing, "sector", None) if existing is not None else None
    )
    key = _sector_key(ticker, sector)
    max_sector = float(policy.max_sector_pct)
    weights = sector_weights(current_positions)
    sector_current = weights.get(key, 0.0)
    sector_headroom = round(max_sector - sector_current, 4)

    label = ticker + " (sector unknown — treated as its own bucket)" \
        if key.startswith(UNKNOWN_SECTOR_PREFIX) else key
    rules_checked.append(
        f"max_sector_pct: {label} at {round(sector_current, 4)}%, "
        f"limit {max_sector}%, headroom {max(sector_headroom, 0.0)}%"
    )
    if sector_headroom <= 0:
        return {
            "decision": "rejected",
            "max_size_pct": 0.0,
            "rules_checked": rules_checked + [
                f"REJECTED: sector {label} already at or above the {max_sector}% limit"
            ],
        }

    # The binding constraint is whichever is tighter. Note this can be
    # the sector even when the name itself has plenty of room — which
    # is the entire point of having a sector limit.
    headroom = round(min(position_headroom, sector_headroom), 4)
    binding = "position" if position_headroom <= sector_headroom else "sector"
    rules_checked.append(f"binding constraint: {binding} — ceiling set to {headroom}% of NAV")

    return {"decision": "approved", "max_size_pct": headroom, "rules_checked": rules_checked}


def check_position_weight_drift(current_positions: list[Position],
                                policy: RiskPolicyVersion) -> list[dict]:
    """
    Positions that breach the single-name limit on price movement
    alone, with no trade involved. This is the check that only exists
    if Nora runs daily — nobody proposes anything about a position
    that is quietly appreciating past its limit.
    """
    max_pct = float(policy.max_position_pct)
    breaches = []
    for p in current_positions:
        weight = float(p.weight_pct or 0)
        if weight > max_pct:
            breaches.append({
                "ticker": p.ticker,
                "rule_violated": "max_position_pct",
                "current_value": round(weight, 2),
                "limit_value": max_pct,
                "source": SOURCE_PORTFOLIO,
            })
    return breaches


def check_sector_concentration(current_positions: list[Position],
                               policy: RiskPolicyVersion) -> list[dict]:
    """Sector buckets over the limit, again from drift alone."""
    max_sector = float(policy.max_sector_pct)
    breaches = []
    for key, weight in sector_weights(current_positions).items():
        if weight > max_sector:
            label = key
            if key.startswith(UNKNOWN_SECTOR_PREFIX):
                label = f"{key[len(UNKNOWN_SECTOR_PREFIX):]} (sector unknown)"
            breaches.append({
                "ticker": None,
                "rule_violated": f"max_sector_pct:{label}",
                "current_value": round(weight, 2),
                "limit_value": max_sector,
                "source": SOURCE_PORTFOLIO,
            })
    return breaches


def check_position_count(current_positions: list[Position],
                         policy: RiskPolicyVersion) -> list[dict]:
    """
    Minimum diversification (§7.4: "minimum position count once fully
    deployed: 12-15 names").

    WARN ONLY, deliberately. This constrains the shape of a finished
    book, not any individual trade — and enforcing it as a rejection
    would be self-defeating, since the only way out of a 4-name book
    is to open more names. A book still ramping from zero is
    legitimately below the minimum, so what's wanted here is
    visibility, not a blocker.

    It is recorded as a breach row (so it surfaces on the dashboard
    and in Clara's audit) but never blocks anything, and portfolio
    status treats it as a warning rather than a hard breach.
    """
    minimum = int(policy.min_position_count)
    count = len(current_positions)
    if count == 0 or count >= minimum:
        return []
    return [{
        "ticker": None,
        "rule_violated": "min_position_count",
        "current_value": count,
        "limit_value": minimum,
        "source": SOURCE_PORTFOLIO,
    }]


def check_drawdown_circuit_breaker(session, policy: RiskPolicyVersion) -> dict:
    """
    Portfolio drawdown from peak NAV. Returns
    {"breached": bool, "current_drawdown_pct": float, "basis": str}.

    MEASURED ON NAV, NOT ON P&L. This previously took the peak of
    daily_pnl.total_pnl and compared today against it — but total_pnl
    is a single day's figure, not an equity curve, so "peak" meant
    "your best single day". A day that made 100 followed by an
    ordinary flat day read as (0-100)/100 = -100% and tripped a -10%
    breaker on a day nothing happened; and when peak P&L was negative,
    abs(peak) inverted the sign so the breaker stopped firing exactly
    when the book was losing money.

    Drawdown is now (nav - peak_nav) / peak_nav, the standard
    definition, which never divides by a signed quantity.

    Rows written before the nav column existed have nav = NULL. Those
    are skipped rather than back-filled with a guess; if fewer than
    two usable points remain, this honestly reports "not breached,
    insufficient history" rather than inventing a number.
    """
    history = (
        session.query(DailyPnl)
        .filter(DailyPnl.nav.isnot(None))
        .order_by(DailyPnl.pnl_date)
        .all()
    )
    navs = [float(r.nav) for r in history if r.nav is not None and float(r.nav) > 0]

    if len(navs) < 2:
        return {
            "breached": False,
            "current_drawdown_pct": 0.0,
            "basis": f"insufficient NAV history ({len(navs)} usable point(s)) — not evaluated",
        }

    peak_nav = max(navs)
    current_nav = navs[-1]
    drawdown_pct = (current_nav - peak_nav) / peak_nav * 100
    breached = drawdown_pct <= float(policy.drawdown_breaker_pct)
    return {
        "breached": breached,
        "current_drawdown_pct": round(drawdown_pct, 2),
        "basis": f"NAV {current_nav:,.2f} vs peak {peak_nav:,.2f} over {len(navs)} sessions",
    }


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


# =================================================================
# [7] PERSISTENCE HELPERS
#
# Both of Nora's jobs write to the same risk_reviews row for the day
# (review_date is unique). They must not clobber each other, so each
# updates only the fields it owns and replaces only the breaches it
# produced, scoped by `source`.
# =================================================================
def _upsert_risk_review(session, today: date,
                        portfolio_status: Optional[str] = None,
                        circuit_breaker_active: Optional[bool] = None) -> int:
    """Upsert today's risk_reviews row, updating only supplied fields."""
    existing = session.query(RiskReview).filter(RiskReview.review_date == today).first()

    status = portfolio_status if portfolio_status is not None else (
        existing.portfolio_status if existing else "within_limits"
    )
    breaker = circuit_breaker_active if circuit_breaker_active is not None else (
        existing.circuit_breaker_active if existing else False
    )

    stmt = pg_insert(RiskReview).values(
        review_date=today,
        portfolio_status=status,
        circuit_breaker_active=breaker,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["review_date"],
        set_={
            "portfolio_status": stmt.excluded.portfolio_status,
            "circuit_breaker_active": stmt.excluded.circuit_breaker_active,
        },
    )
    return session.execute(stmt.returning(RiskReview.id)).scalar_one()


def _replace_breaches(session, review_id: int, source: str, breaches: list[dict]) -> None:
    """Replace only this source's breaches, leaving the other source's rows intact."""
    (
        session.query(RiskBreach)
        .filter(RiskBreach.risk_review_id == review_id, RiskBreach.source == source)
        .delete(synchronize_session=False)
    )
    for b in breaches:
        session.add(RiskBreach(risk_review_id=review_id, **b))


def _escalate_status(current: str, candidate: str) -> str:
    """Never downgrade a status that another pass already raised."""
    rank = {"within_limits": 0, "breach_warning": 1, "breach_hard": 2}
    return current if rank.get(current, 0) >= rank.get(candidate, 0) else candidate


# =================================================================
# JOB 1 — PROPOSAL REVIEW. Phase 3, gated on Solomon escalating.
# =================================================================
def review_proposals(today: date) -> dict:
    """
    Reviews today's proposals against the hard limits. Gated by the
    orchestrator on Solomon's action_needed — if nothing was escalated
    there is genuinely nothing here to do, and this never touches the
    API.
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

        # The breaker is evaluated here too, rather than read from
        # today's review row: monitor_portfolio() runs later in the
        # cycle (Phase 5), so at proposal time the authoritative
        # answer is the one computed from the NAV history to date.
        drawdown = check_drawdown_circuit_breaker(session, policy)
        breaker_active = drawdown["breached"]

        # [4] Hard limits — plain Python, computed before any LLM call.
        reviews = []
        proposal_breaches = []
        for p in proposals:
            proposed_sector = resolve_proposed_sector(session, p.ticker)
            hard_result = check_hard_limits(
                p.ticker, p.action, current_positions, policy,
                proposed_sector=proposed_sector,
                circuit_breaker_active=breaker_active,
            )
            reviews.append({"proposal_id": p.id, "ticker": p.ticker, **hard_result})

            if hard_result["decision"] == "rejected":
                rule = "circuit_breaker_frozen" if breaker_active else "max_position_or_sector_pct"
                proposal_breaches.append({
                    "ticker": p.ticker,
                    "rule_violated": rule,
                    "current_value": None,
                    "limit_value": None,
                    "source": SOURCE_PROPOSAL,
                })

        portfolio_snapshot = (
            "\n".join(
                f"{p.ticker}: {p.weight_pct}% ({getattr(p, 'sector', None) or 'sector unknown'})"
                for p in current_positions
            )
            or "Portfolio is currently empty."
        )

    # Only call Claude if there's an APPROVED proposal worth a
    # qualitative pass — never spend a call on a quiet day.
    approved_tickers = [r["ticker"] for r in reviews if r["decision"] == "approved"]
    qualitative_notes_by_ticker = {}
    if approved_tickers:
        user_prompt = (
            f"Approved proposals to review qualitatively: {approved_tickers}\n"
            f"Current portfolio:\n{portfolio_snapshot}"
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

    any_rejected = any(r["decision"] == "rejected" for r in reviews)
    candidate_status = "breach_warning" if any_rejected else "within_limits"

    with session_scope() as session:
        existing = session.query(RiskReview).filter(RiskReview.review_date == today).first()
        status = _escalate_status(
            existing.portfolio_status if existing else "within_limits",
            candidate_status,
        )
        review_id = _upsert_risk_review(session, today, portfolio_status=status)
        _replace_breaches(session, review_id, SOURCE_PROPOSAL, proposal_breaches)

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
        "portfolio_status": status,
        "proposal_reviews": reviews,
        "circuit_breaker_active": breaker_active,
        "drawdown": drawdown,
    }


# =================================================================
# JOB 2 — DAILY PORTFOLIO MONITORING. Phase 5, ALWAYS runs.
# =================================================================
def monitor_portfolio(today: date) -> dict:
    """
    Re-checks the book as it stands against every limit, every trading
    day, whether or not anything was proposed. Pure code — no LLM call
    at all, because there is no judgment here: a weight either exceeds
    a limit or it doesn't.

    This is where the drawdown circuit breaker is evaluated and
    recorded as the authoritative state for the day.
    """
    with session_scope() as session:
        policy = get_active_risk_policy(session, today)
        current_positions = session.query(Position).all()

        drawdown = check_drawdown_circuit_breaker(session, policy)

        breaches: list[dict] = []
        if drawdown["breached"]:
            breaches.append({
                "ticker": None,
                "rule_violated": "portfolio_drawdown_circuit_breaker",
                "current_value": drawdown["current_drawdown_pct"],
                "limit_value": float(policy.drawdown_breaker_pct),
                "source": SOURCE_PORTFOLIO,
            })

        breaches.extend(check_position_weight_drift(current_positions, policy))
        breaches.extend(check_sector_concentration(current_positions, policy))

        # Warn-only: recorded, surfaced, but never escalates status to
        # breach_hard and never blocks a trade.
        count_warnings = check_position_count(current_positions, policy)
        breaches.extend(count_warnings)

        hard_breaches = [b for b in breaches if b["rule_violated"] != "min_position_count"]
        candidate_status = (
            "breach_hard" if hard_breaches
            else ("breach_warning" if count_warnings else "within_limits")
        )

        existing = session.query(RiskReview).filter(RiskReview.review_date == today).first()
        status = _escalate_status(
            existing.portfolio_status if existing else "within_limits",
            candidate_status,
        )

        review_id = _upsert_risk_review(
            session, today,
            portfolio_status=status,
            circuit_breaker_active=drawdown["breached"],
        )
        _replace_breaches(session, review_id, SOURCE_PORTFOLIO, breaches)

        position_count = len(current_positions)
        sectors = {k: round(v, 2) for k, v in sector_weights(current_positions).items()}

    return {
        "date": today,
        "portfolio_status": status,
        "circuit_breaker_active": drawdown["breached"],
        "drawdown": drawdown,
        "breaches": breaches,
        "position_count": position_count,
        "sector_weights": sectors,
    }


def is_circuit_breaker_active(today: date) -> bool:
    """
    Cheap read for downstream agents (Marcus, Ada) that must not act
    while the book is frozen. Reads today's recorded state if
    monitor_portfolio() has already run; otherwise recomputes from NAV
    history so the answer is never stale-by-omission.
    """
    with session_scope() as session:
        review = session.query(RiskReview).filter(RiskReview.review_date == today).first()
        if review is not None:
            return bool(review.circuit_breaker_active)
        policy = get_active_risk_policy(session, today)
        return check_drawdown_circuit_breaker(session, policy)["breached"]


def run(today: date) -> dict:
    """
    Backwards-compatible entry point: proposal review only.

    Kept so existing callers and smoke tests keep working, but the
    orchestrator now calls review_proposals() and monitor_portfolio()
    explicitly at their own phases — see the module docstring for why
    they must not share a schedule.
    """
    return review_proposals(today)


if __name__ == "__main__":
    # Manual smoke test: python -m agents.nora
    # Reuses today's Atlas/Vera/Solomon output where possible — see
    # core/dev_helpers.py. Solomon still re-runs each time (he's not
    # FMP-dependent, so it's cheap), but Atlas/Vera won't re-fetch if
    # today's data already exists.
    from core.dev_helpers import get_or_run_atlas, get_or_run_vera
    from agents import solomon

    atlas_result = get_or_run_atlas(date.today())
    vera_result = get_or_run_vera(date.today(), atlas_result)
    print("Running Solomon...")
    solomon_result = solomon.run(date.today(), atlas_result, vera_result)

    print("\nRunning Nora — proposal review (Phase 3)...\n")
    review_result = review_proposals(date.today())
    print("PROPOSAL REVIEW:", review_result)

    print("\nRunning Nora — daily portfolio monitoring (Phase 5)...\n")
    monitor_result = monitor_portfolio(date.today())
    print("PORTFOLIO MONITOR:", monitor_result)

    if not review_result["proposal_reviews"]:
        print("\n--- No real proposals today. Running a SYNTHETIC test of ---")
        print("--- check_hard_limits() directly, bypassing the DB, just  ---")
        print("--- to demonstrate the hard-limit logic in isolation.     ---\n")
        with session_scope() as session:
            test_policy = get_active_risk_policy(session, date.today())
            test_positions = session.query(Position).all()
            test_sector = resolve_proposed_sector(session, "AAPL")
        test_result = check_hard_limits(
            "AAPL", "new_position", test_positions, test_policy,
            proposed_sector=test_sector,
        )
        print("SYNTHETIC test — proposing AAPL as a new_position:", test_result)