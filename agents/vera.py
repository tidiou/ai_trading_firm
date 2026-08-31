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

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.clients import fmp_client
from core.clients.claude_client import run_agent_loop, extract_json
from core.db import session_scope
from core.models import Thesis, PositionMonitoringLog, NewCandidate


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
def execute_tool(tool_name: str, tool_input: dict):
    if tool_name == "get_stock_quote":
        return fmp_client.get_stock_quote(tool_input["ticker"])
    if tool_name == "get_company_snapshot":
        return fmp_client.get_company_snapshot(tool_input["ticker"])
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
FIXED_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    "JPM", "V", "MA", "JNJ", "PG", "HD", "UNH", "DIS", "KO",
]


# =================================================================
# [5] OUTPUT CONTRACTS — one per job, mirroring the Operating
# Manual's two output shapes exactly.
# =================================================================
class PositionMonitoring(BaseModel):
    ticker: str
    status: Literal["intact", "at_risk", "broken"]
    trigger: Literal["none", "earnings", "news", "fundamental_drift", "macro_conflict"]
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

Rules:
- Never suggest a new idea here — that is a separate pass with a
  separate mandate. Stay focused on the positions given to you.
- A status of "at_risk" or "broken" REQUIRES a specific, named
  trigger (an earnings miss, a news event, deteriorating fundamentals,
  or a conflict with today's macro backdrop) — never a vague feeling.
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
    "trigger": "none | earnings | news | fundamental_drift | macro_conflict",
    "reasoning": "1-2 sentences",
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
def get_open_theses(session) -> list[Thesis]:
    return session.query(Thesis).filter(Thesis.closed_date.is_(None)).all()


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
        positions_summary = "\n".join(
            f"- {t.ticker}: original thesis (opened {t.opened_date}, "
            f"conviction {t.original_conviction}/5): {t.thesis_text}"
            for t in open_theses
        )
        monitoring_prompt = f"Today's date: {today}. Positions to review:\n{positions_summary}"

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
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["candidate_date", "ticker"],
                set_={
                    "thesis": stmt.excluded.thesis,
                    "catalyst": stmt.excluded.catalyst,
                    "conviction_score": stmt.excluded.conviction_score,
                    "key_risks": stmt.excluded.key_risks,
                    "valuation_snapshot": stmt.excluded.valuation_snapshot,
                },
            )
            session.execute(stmt)

    return {
        "monitoring": [m.model_dump() for m in monitoring_results],
        "candidates": [c.model_dump() for c in candidate_results],
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