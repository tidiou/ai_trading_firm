"""
Vera — Equity Research Agent
Operating Manual §7.2

ANATOMY OF THIS AGENT — same 7 pieces as Atlas, tagged the same way.
Vera is a genuine step up in complexity: she has TWO separate jobs
(deliberately kept apart — see Operating Manual §7.2), real
persistent memory of open theses, and a fixed universe to screen.

  [1] ROLE / MANDATE   -> MONITORING_SYSTEM_PROMPT + SCREENING_SYSTEM_PROMPT
  [2] SKILLS / TOOLS    -> VERA_TOOLS                (shared by both jobs)
  [3] TOOL DISPATCH     -> execute_tool()
  [4] THE AGENTIC LOOP  -> two separate run_agent_loop() calls in run()
                            (one per job — see "why two loops" below)
  [5] OUTPUT CONTRACT   -> PositionMonitoring + NewCandidateOutput (Pydantic)
  [6] MEMORY            -> get_open_theses() (real, persistent —
                            unlike Atlas's one-fact memory, this is
                            "everything currently held, and why")
  [7] PERSISTENCE       -> position_monitoring_log + new_candidates tables

WHY TWO LOOPS INSTEAD OF ONE: monitoring is "defend or revise a
prior judgment" and screening is "form a new one" — different
cognitive postures. Running them as separate agentic loops (each
with its own focused system prompt) keeps Vera from blending
"is my existing thesis still good?" with "what's new and exciting?"
into one confused pass. Same principle as Atlas never commenting on
stocks: narrow scope per call = better reasoning per call.

Decision rights = none, same as Atlas: [2] contains no trading tool.
Vera can only ever produce theses and status tags — the system
prompts additionally forbid "buy"/"sell" language as a second,
belt-and-suspenders layer on top of that hard capability limit.
"""

from datetime import date, timedelta
from typing import Literal, Optional

from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.clients import fmp_client
from core.clients.claude_client import run_agent_loop, extract_json
from core.db import session_scope
from core.models import Thesis, PositionMonitoringLog, NewCandidate, Position, RiskPolicyVersion, PositionPnlHistory


# =================================================================
# [2] SKILLS / TOOLS — shared menu for both of Vera's jobs. Claude
# decides which tickers to actually investigate and which tool to
# use for each — we don't script that.
# =================================================================
VERA_TOOLS = [
    {
        "name": "get_stock_quote",
        "description": "Current price, daily change, and volume for a single stock.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_company_snapshot",
        "description": "Company profile (sector, market cap, description) and key valuation metrics (P/E, revenue per share, etc.) for a single stock.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
]


# =================================================================
# [3] TOOL DISPATCH
# =================================================================
# Sector, harvested as a SIDE EFFECT of the profile calls Claude
# already makes during screening. Two things matter about where this
# comes from:
#
#   1. It costs zero extra FMP quota. The profile response is already
#      being fetched; we were simply discarding the sector field.
#   2. It is read from the API response, NOT from Claude's output.
#      Sector is a fact, and facts in this codebase come from code —
#      the same reason Nora's numbers never come from an LLM. Adding
#      a `sector` field to NewCandidateOutput would have been easier
#      and would have made a hard risk limit depend on a model
#      correctly transcribing a string.
#
# Nora's max_sector_pct limit is enforced on whatever lands here.
_sector_cache: dict[str, str] = {}


def execute_tool(tool_name: str, tool_input: dict):
    if tool_name == "get_stock_quote":
        return fmp_client.get_stock_quote(tool_input["ticker"])
    if tool_name == "get_company_snapshot":
        ticker = tool_input["ticker"]
        snapshot = fmp_client.get_company_snapshot(ticker)
        profile = snapshot.get("profile") if isinstance(snapshot, dict) else None
        if isinstance(profile, dict):
            sector = profile.get("sector")
            if isinstance(sector, str) and sector.strip():
                _sector_cache[ticker] = sector.strip()
        return snapshot
    raise ValueError(f"Vera has no tool named '{tool_name}'")


# =================================================================
# FIXED UNIVERSE — deterministic, not LLM-chosen (Operating Manual
# §7.2: the universe is pre-filtered by CODE before any LLM reasoning
# happens). Starting list: well-known, highly liquid US large-caps,
# chosen to maximize the odds of being covered under FMP's free-tier
# sample-symbol restriction. If a ticker 402s at request time,
# get_company_snapshot() degrades gracefully rather than crashing —
# see fmp_client._get(). Revisit/expand this list once on a paid
# FMP tier with full-universe access.
# =================================================================
# Starting list: well-known, highly liquid US large-caps, chosen to
# maximize the odds of being covered under FMP's free-tier
# sample-symbol restriction. Deliberately trimmed from an earlier,
# larger list — each ticker costs 2 FMP calls per screening pass, and
# heavy iterative testing kept exhausting the free tier's daily
# quota. Expand this once on a paid FMP tier, or for a deliberate,
# infrequent "real" run rather than routine testing.
FIXED_UNIVERSE = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "JPM"]


# =================================================================
# [5] OUTPUT CONTRACTS — one per job, mirroring the Operating
# Manual's two output shapes exactly.
# =================================================================
class PositionMonitoring(BaseModel):
    ticker: str
    status: Literal["intact", "at_risk", "broken"]
    trigger: Literal["none", "earnings", "news", "fundamental_drift", "macro_conflict", "profit_target_sustained"]
    reasoning: str
    conviction_score: int  # 1-5, compared against the thesis's original score


class NewCandidateOutput(BaseModel):
    ticker: str
    thesis: str
    catalyst: str
    conviction_score: int  # 1-5
    key_risks: list[str]
    valuation_snapshot: dict


# =================================================================
# [1] ROLE / MANDATE — monitoring job
# =================================================================
MONITORING_SYSTEM_PROMPT = """You are Vera, the Equity Research agent at MBY-Trading.

This is your MONITORING pass. You are reviewing positions the firm
ALREADY HOLDS, each with an existing thesis. Your only question for
each one: has anything happened that changes whether the original
thesis still holds?

You will be shown, for each position: today's unrealized P&L%, the
firm's review thresholds (a loss beyond the threshold, or a gain
beyond the threshold), the trailing week's daily P&L% history, and
today's macro backdrop from Atlas.

IMPORTANT — how to use the review thresholds: crossing a loss or
gain threshold is NOT a signal to act. It is a signal to INVESTIGATE
properly, using your tools, before concluding anything:

- On a meaningful LOSS: compare this position's decline against
  Atlas's regime signal and the broad market's move. If the whole
  market is down and this position moved roughly in line with it,
  that's very likely just correlation/normal volatility — say so,
  and don't over-react to it. If this position fell meaningfully more
  than the broad market while the regime is otherwise calm, that is
  a real signal worth investigating with your tools (check current
  fundamentals/news) — is something company-specific actually wrong,
  or is this still just noise? Only flag "at_risk"/"broken" if your
  investigation actually turns up a specific, real reason — being
  down alone is never sufficient justification.

- On a meaningful GAIN: look at the trailing week of daily P&L%
  history you're given. A gain that has been building steadily over
  several days is a real, sustained trend worth taking seriously as a
  profit-taking candidate. A gain driven by a single recent day's
  spike, with little support in the preceding days, is more likely
  short-term hype — the firm plays a long-term game and should not
  react to a one-day pop. Only flag a position for profit-taking
  consideration if the trend genuinely looks sustained, and say so
  explicitly in your reasoning either way.

Other rules:
- Never suggest a new idea here — that is a separate pass with a
  separate mandate. Stay focused on the positions given to you.
- A status of "at_risk" or "broken" REQUIRES a specific, named
  trigger — never a vague feeling, and never "it went down" alone.
- Never use the words "buy" or "sell" — you produce assessments, not
  trade instructions.
- If nothing has changed for a position, say so plainly — "status
  intact, no new information" is a completely valid, expected answer.

Once you've reviewed every position given to you, respond with ONLY
a JSON array — your response must start with "[" as its very first
character and contain nothing else: no headers, no "notes" sections,
no explanation of your reasoning process, no text before or after
the array. Do your reasoning silently and output only the final
result, matching exactly this shape:

[
  {
    "ticker": "...",
    "status": "intact | at_risk | broken",
    "trigger": "none | earnings | news | fundamental_drift | macro_conflict | profit_target_sustained",
    "reasoning": "1-3 sentences — explain WHY, referencing the market comparison or trend data you were given",
    "conviction_score": 1-5
  }
]

If you are given zero positions to review, respond with an empty
array: []
"""


# =================================================================
# [1] ROLE / MANDATE — screening job
# =================================================================
SCREENING_SYSTEM_PROMPT = """You are Vera, the Equity Research agent at MBY-Trading.

This is your SCREENING pass. You are looking for NEW investment
candidates from a fixed list of tickers, for a discretionary,
weeks-to-months holding horizon.

Rules:
- Only investigate tickers from the list you're given — this is a
  pre-filtered universe, not an invitation to consider anything else.
- Zero good candidates is a completely valid, expected outcome on
  most days — do not force a mediocre idea into existence just to
  have something to report.
- Every candidate needs a genuine, named catalyst — not just "looks
  cheap" or "good company." If you can't articulate why NOW, it's
  not ready to surface.
- Never use the words "buy" or "sell" — you produce theses and
  conviction scores, not trade instructions. Sizing and execution
  are other agents' jobs entirely.
- Factor in the macro backdrop you're given, but don't let it alone
  drive a stock-specific call — a bullish idea needs its own
  company-specific catalyst even in a risk-on backdrop.
- Score conviction 1-5 honestly: 5 should be rare. Most genuinely
  good ideas are a 3 or 4.

Respond with ONLY a JSON array — your response must start with "["
as its very first character and contain nothing else: no headers, no
"screening notes" sections, no explanation of your reasoning process,
no text before or after the array. Do your reasoning silently and
output only the final result. Most days this array should be short
or empty:

[
  {
    "ticker": "...",
    "thesis": "2-3 sentences on why this company, why now",
    "catalyst": "the specific, named reason this matters now",
    "conviction_score": 1-5,
    "key_risks": ["...", "..."],
    "valuation_snapshot": {"note": "key numbers that informed this view"}
  }
]
"""


# =================================================================
# [6] MEMORY — real, persistent memory: every currently open
# thesis. This is what lets the monitoring pass ask "has anything
# changed" instead of forming an opinion fresh every day.
# =================================================================
def get_active_risk_policy_for_review(session, today: date):
    """
    Read-only lookup of Nora's risk policy, specifically for the
    loss_review_pct/profit_review_pct thresholds. Deliberately does
    NOT auto-seed (that's Nora's job, in nora.py) — if no policy
    exists yet (e.g. Nora hasn't run today), falls back to the config
    default so Vera's monitoring can still run sensibly.
    """
    policy = (
        session.query(RiskPolicyVersion)
        .filter(RiskPolicyVersion.effective_date <= today)
        .order_by(RiskPolicyVersion.effective_date.desc())
        .first()
    )
    if policy:
        return policy
    from types import SimpleNamespace
    from config.risk_policy import DEFAULT_RISK_POLICY
    return SimpleNamespace(
        loss_review_pct=DEFAULT_RISK_POLICY["loss_review_pct"],
        profit_review_pct=DEFAULT_RISK_POLICY["profit_review_pct"],
    )


def _get_trailing_pnl_history(session, today: date, tickers: list[str], days: int = 7) -> dict:
    """Trailing week of daily P&L% per ticker, from Otis's append-only
    history — this is what lets Vera distinguish a sustained trend
    from a single-day spike."""
    cutoff = today - timedelta(days=days)
    rows = (
        session.query(PositionPnlHistory)
        .filter(PositionPnlHistory.ticker.in_(tickers), PositionPnlHistory.snapshot_date >= cutoff)
        .order_by(PositionPnlHistory.snapshot_date)
        .all()
    )
    history = {}
    for row in rows:
        history.setdefault(row.ticker, []).append(
            {"date": str(row.snapshot_date), "pnl_pct": float(row.unrealized_pnl_pct)}
        )
    return history


def _format_position_block(thesis: Thesis, position, history: list[dict], policy) -> str:
    """Builds one position's block of context for the monitoring
    prompt — current P&L, trailing trend, and the review thresholds."""
    if position and position.market_value and (position.market_value - position.unrealized_pnl) != 0:
        current_pnl_pct = float(position.unrealized_pnl) / float(position.market_value - position.unrealized_pnl) * 100
        pnl_line = f"{current_pnl_pct:.2f}%"
    else:
        pnl_line = "unavailable"

    history_str = ", ".join(f"{h['date']}: {h['pnl_pct']}%" for h in history) or "no history yet"

    return (
        f"- {thesis.ticker}: original thesis (opened {thesis.opened_date}, "
        f"conviction {thesis.original_conviction}/5): {thesis.thesis_text}\n"
        f"  Current unrealized P&L: {pnl_line}\n"
        f"  Trailing week P&L history: {history_str}\n"
        f"  Review thresholds: investigate on loss beyond {policy.loss_review_pct}%, "
        f"investigate profit-taking on gain beyond {policy.profit_review_pct}%"
    )


def get_open_theses(session) -> list[Thesis]:
    return session.query(Thesis).filter(Thesis.closed_date.is_(None)).all()


# =================================================================
# JOB 3 (added later): orphan position review. Closes a real gap
# found when a test trade (AAPL) entered the book without ever going
# through Vera's normal pipeline — she had no way to even SEE it,
# since monitor_positions() only looks at tickers with an open
# Thesis. This gives every held position a chance to be evaluated,
# even ones that slipped in outside the normal flow.
# =================================================================
class OrphanReview(BaseModel):
    ticker: str
    recommendation: Literal["adopt", "exit"]
    reasoning: str
    thesis: Optional[str] = None       # only if recommendation == "adopt"
    catalyst: Optional[str] = None     # only if recommendation == "adopt"
    conviction_score: Optional[int] = None  # only if recommendation == "adopt"


ORPHAN_SYSTEM_PROMPT = """You are Vera, the Equity Research agent at MBY-Trading.

This is a special pass: you're reviewing positions the firm currently
HOLDS but that have no documented thesis on record — they entered the
book without going through normal research and approval. Your job is
to make an honest, fresh assessment of each one and recommend either:

- "adopt": this is genuinely a name worth holding — write a real
  thesis, catalyst, and conviction score for it, as if proposing it
  fresh today. Adopting only documents the rationale; it does not
  add to the position.
- "exit": this doesn't hold up under fresh scrutiny and shouldn't
  stay in the portfolio without ever having been properly evaluated.

Do not let the fact that it's already held bias you toward keeping
it — evaluate it exactly as skeptically as you would a brand-new
candidate.

Respond with ONLY a JSON array — your response must start with "["
as its very first character and contain nothing else:

[
  {
    "ticker": "...", "recommendation": "adopt | exit", "reasoning": "...",
    "thesis": "... (only if adopt)", "catalyst": "... (only if adopt)",
    "conviction_score": "1-5 (only if adopt)"
  }
]
"""


def get_orphan_positions(session) -> list[str]:
    """Tickers currently held (per Otis's positions table) with no
    open Thesis on record — the gap that let AAPL go completely
    unexamined by the research/approval pipeline."""
    held = {p.ticker for p in session.query(Position).all()}
    documented = {t.ticker for t in session.query(Thesis).filter(Thesis.closed_date.is_(None)).all()}
    return sorted(held - documented)


def review_orphan_positions(today: date) -> list[dict]:
    with session_scope() as session:
        orphans = get_orphan_positions(session)
        if orphans:
            positions_by_ticker = {p.ticker: p for p in session.query(Position).all() if p.ticker in orphans}
    if not orphans:
        return []

    pnl_lines = []
    for ticker in orphans:
        pos = positions_by_ticker.get(ticker)
        if pos and pos.market_value and (pos.market_value - pos.unrealized_pnl) != 0:
            pnl_pct = float(pos.unrealized_pnl) / float(pos.market_value - pos.unrealized_pnl) * 100
            pnl_lines.append(f"{ticker}: {pnl_pct:.2f}% unrealized")
        else:
            pnl_lines.append(f"{ticker}: P&L unavailable")

    user_prompt = f"Undocumented positions currently held:\n" + "\n".join(pnl_lines)
    raw = run_agent_loop(
        system_prompt=ORPHAN_SYSTEM_PROMPT, user_prompt=user_prompt,
        tools=VERA_TOOLS, tool_executor=execute_tool, max_tokens=2000,
    )
    reviews = [OrphanReview.model_validate(item) for item in extract_json(raw)]

    with session_scope() as session:
        for r in reviews:
            if r.recommendation == "adopt":
                # Documentation only — no capital moves, so this doesn't
                # need Nora/Marcus approval, just an honest thesis on record.
                session.add(Thesis(
                    ticker=r.ticker, opened_date=today, thesis_text=r.thesis,
                    catalyst=r.catalyst, original_conviction=r.conviction_score or 3,
                ))
    return [r.model_dump() for r in reviews]


def run(today: date, atlas_output: dict) -> dict:
    """
    Runs both of Vera's jobs and persists both. Takes Atlas's output
    as context for the screening pass (per your request to combine
    his macro read with her stock-level work) — note this is context,
    not a directive; the screening prompt explicitly says macro alone
    shouldn't drive a stock-specific call.
    """
    with session_scope() as session:
        open_theses = get_open_theses(session)
        held_tickers = [t.ticker for t in open_theses]

    # -------------------------------------------------------------
    # JOB 1: MONITORING — [4] first agentic loop
    # -------------------------------------------------------------
    if open_theses:
        with session_scope() as session:
            policy = get_active_risk_policy_for_review(session, today)
            positions_by_ticker = {p.ticker: p for p in session.query(Position).all()}
            trailing_history = _get_trailing_pnl_history(session, today, [t.ticker for t in open_theses])

        positions_summary = "\n".join(
            _format_position_block(t, positions_by_ticker.get(t.ticker), trailing_history.get(t.ticker, []), policy)
            for t in open_theses
        )

        monitoring_prompt = (
            f"Today's date: {today}\n"
            f"Macro backdrop (Atlas): regime={atlas_output.get('regime_signal')}, "
            f"confidence={atlas_output.get('confidence')}, narrative: {atlas_output.get('narrative')}\n\n"
            f"Positions to review:\n{positions_summary}"
        )

        monitoring_raw = run_agent_loop(
            system_prompt=MONITORING_SYSTEM_PROMPT,
            user_prompt=monitoring_prompt,
            tools=VERA_TOOLS,
            tool_executor=execute_tool,
            max_tokens=4000,  # larger than Atlas's default — up to N positions, each with its own object
        )
        monitoring_results = [
            PositionMonitoring.model_validate(item)
            for item in extract_json(monitoring_raw)
        ]
    else:
        monitoring_results = []  # nothing held yet — a valid, expected state on day one

    # -------------------------------------------------------------
    # JOB 2: SCREENING — [4] second agentic loop
    # -------------------------------------------------------------
    screening_universe = [t for t in FIXED_UNIVERSE if t not in held_tickers]
    screening_prompt = (
        f"Today's date: {today}.\n"
        f"Macro backdrop from Atlas: regime={atlas_output.get('regime_signal')}, "
        f"confidence={atlas_output.get('confidence')}, "
        f"narrative: {atlas_output.get('narrative')}\n"
        f"Universe to screen (already-held tickers excluded): {', '.join(screening_universe)}"
    )

    screening_raw = run_agent_loop(
        system_prompt=SCREENING_SYSTEM_PROMPT,
        user_prompt=screening_prompt,
        tools=VERA_TOOLS,
        tool_executor=execute_tool,
        max_tokens=4000,  # up to 16 tickers screened, each candidate needs a full thesis/risks/valuation
    )
    candidate_results = [
        NewCandidateOutput.model_validate(item)
        for item in extract_json(screening_raw)
    ]

    # -------------------------------------------------------------
    # [7] PERSISTENCE — both jobs' results, as upserts. Same
    # reasoning as Atlas: a same-day rerun (common during testing,
    # and possible in production if the orchestrator is ever
    # manually re-triggered) should refresh today's rows, not
    # duplicate or crash on the unique constraints below.
    # -------------------------------------------------------------
    with session_scope() as session:
        thesis_by_ticker = {t.ticker: t.id for t in open_theses}
        for m in monitoring_results:
            stmt = pg_insert(PositionMonitoringLog).values(
                log_date=today,
                thesis_id=thesis_by_ticker[m.ticker],
                ticker=m.ticker,
                status=m.status,
                trigger=m.trigger,
                reasoning=m.reasoning,
                conviction_score=m.conviction_score,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["log_date", "thesis_id"],
                set_={
                    "status": stmt.excluded.status,
                    "trigger": stmt.excluded.trigger,
                    "reasoning": stmt.excluded.reasoning,
                    "conviction_score": stmt.excluded.conviction_score,
                },
            )
            session.execute(stmt)

        # Delete today's prior candidates before reinserting — a
        # same-day rerun should REPLACE today's screening results,
        # not accumulate tickers across multiple runs (the earlier
        # per-row upsert let stale candidates from an earlier run
        # linger even if a later rerun didn't surface them again).
        session.query(NewCandidate).filter(NewCandidate.candidate_date == today).delete()
        for c in candidate_results:
            stmt = pg_insert(NewCandidate).values(
                candidate_date=today,
                ticker=c.ticker,
                thesis=c.thesis,
                catalyst=c.catalyst,
                conviction_score=c.conviction_score,
                key_risks=c.key_risks,
                valuation_snapshot=c.valuation_snapshot,
                sector=_sector_cache.get(c.ticker),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["candidate_date", "ticker"],
                set_={
                    "thesis": stmt.excluded.thesis,
                    "catalyst": stmt.excluded.catalyst,
                    "conviction_score": stmt.excluded.conviction_score,
                    "key_risks": stmt.excluded.key_risks,
                    "valuation_snapshot": stmt.excluded.valuation_snapshot,
                    # Keep a sector already on record if this run's
                    # profile call was paywalled — never blank it.
                    "sector": func.coalesce(stmt.excluded.sector, NewCandidate.__table__.c.sector),
                },
            )
            session.execute(stmt)

        # Back-fill sector onto open theses whose rows predate the
        # column (or whose profile call was paywalled at open time).
        # Cheap, idempotent, and it means a long-held position still
        # counts toward its sector limit rather than sitting in an
        # unknown bucket forever.
        for ticker, sector in _sector_cache.items():
            (
                session.query(Thesis)
                .filter(Thesis.ticker == ticker,
                        Thesis.closed_date.is_(None),
                        Thesis.sector.is_(None))
                .update({"sector": sector}, synchronize_session=False)
            )

    orphan_reviews = review_orphan_positions(today)

    return {
        "monitoring": [m.model_dump() for m in monitoring_results],
        "candidates": [c.model_dump() for c in candidate_results],
        "orphan_reviews": orphan_reviews,
    }


if __name__ == "__main__":
    # Manual smoke test: python -m agents.vera
    # Runs Atlas fresh first, since Vera's screening pass wants his
    # macro context — mirrors how the real daily cycle will chain them.
    from agents import atlas

    print("Running Atlas first (Vera's screening pass uses his output)...")
    atlas_result = atlas.run(date.today())
    print("Atlas done. Running Vera...\n")

    result = run(date.today(), atlas_result)
    print(result)