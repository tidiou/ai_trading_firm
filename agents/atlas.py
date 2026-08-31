"""
Atlas — Macro/Market Intelligence Agent
Operating Manual §7.1

ANATOMY OF THIS AGENT — the same 7 pieces every agent in this
codebase will have. Find each one by its numbered marker below.

  [1] ROLE / MANDATE   -> ATLAS_SYSTEM_PROMPT           (his "job description")
  [2] SKILLS / TOOLS    -> ATLAS_TOOLS                   (what he's allowed to do)
  [3] TOOL DISPATCH     -> execute_tool()                 (tool name -> real code)
  [4] THE AGENTIC LOOP  -> run_agent_loop() call in run()  (Claude drives it, not us)
  [5] OUTPUT CONTRACT   -> AtlasOutput                     (hard schema = guardrail)
  [6] MEMORY            -> get_yesterdays_signal()          (exactly 1 prior fact)
  [7] PERSISTENCE       -> the second `with session_scope()` block in run()

Decision rights = none, by construction: [2] contains no trading or
portfolio tool, so there is nothing to police in a prompt — the
capability simply doesn't exist for him to misuse.

Model/judgment split: [1]-[2] are almost pure LLM judgment; [3] and
the fmp_client calls underneath it are pure deterministic code.
Success metric: not implemented yet — needs real run history,
this becomes Clara's job later.
"""

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.clients import fmp_client
from core.clients.claude_client import run_agent_loop, extract_json
from core.db import session_scope
from core.models import MacroBrief


# =================================================================
# [5] OUTPUT CONTRACT — the schema IS the guardrail. If Claude's
# answer doesn't fit this shape (wrong regime_signal value, missing
# field, malformed JSON), Pydantic raises here, loudly, before
# anything gets written to the database. No silent bad data.
# =================================================================
class AtlasOutput(BaseModel):
    date: date
    regime_signal: Literal["risk-on", "risk-off", "neutral", "transitioning"]
    key_events_today: list[str]
    notable_overnight_moves: list[str]
    change_from_yesterday: Literal["none", "minor", "material"]
    confidence: Literal["low", "medium", "high"]
    narrative: str


# =================================================================
# [2] SKILLS / TOOLS — the menu Claude gets to choose FROM. He
# decides which of these to call and in what order (that choice is
# what makes this "agentic" rather than a fixed script). Note what's
# absent: no trading tool, no portfolio-query tool. Atlas is
# architecturally incapable of touching a position — not a rule he
# could break, just not on the menu.
# =================================================================
ATLAS_TOOLS = [
    {
        "name": "get_index_quotes",
        "description": "Current levels and daily % change for major US indices (S&P 500, Dow, Nasdaq) and the VIX volatility index.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


# =================================================================
# [3] TOOL DISPATCH — translates "Claude wants to call X" into
# actually running real code. This function is the entire boundary
# between Claude's reasoning and the outside world for Atlas —
# nothing happens that doesn't pass through here.
# =================================================================
def execute_tool(tool_name: str, tool_input: dict):
    if tool_name == "get_index_quotes":
        return fmp_client.get_index_quotes()
    raise ValueError(f"Atlas has no tool named '{tool_name}'")


# =================================================================
# [1] ROLE / MANDATE — Atlas's job description. This is what a real
# analyst's manager would tell them on day one: here's your scope,
# here's what's out of bounds, here's exactly how to hand off your
# work when you're done.
# =================================================================
ATLAS_SYSTEM_PROMPT = """You are Atlas, the Macro/Market Intelligence agent at MBY-Trading.

Your ONLY job: characterize today's macro backdrop and determine whether
it has materially changed since yesterday. You do not comment on
individual stocks, sectors, or companies under any circumstances —
that is a different agent's job (Vera's), and mixing the two is a
mistake.

You have a tool to check current index and VIX levels. You do not
currently have access to a news feed or economic calendar, so base
your read on price action and volatility alone — do not invent or
assume news events you have no way of confirming. If your confidence
is lower because of this limited input, say so honestly in the
confidence field rather than overstating certainty.

Rules you must follow:
- If you report change_from_yesterday as "material", you MUST name
  the specific event that drove it in your narrative. Never use vague
  language like "sentiment shifted."
- Never mention a specific ticker or company.
- Your confidence field should honestly reflect how clear-cut today's
  read is — most days are "medium", reserve "high" for genuinely
  unambiguous situations and "low" for real uncertainty.

Once you have gathered what you need, respond with ONLY a single JSON
object — no markdown fences, no other text before or after it —
matching exactly this shape:

{
  "date": "YYYY-MM-DD",
  "regime_signal": "risk-on | risk-off | neutral | transitioning",
  "key_events_today": ["..."],
  "notable_overnight_moves": ["..."],
  "change_from_yesterday": "none | minor | material",
  "confidence": "low | medium | high",
  "narrative": "2-3 sentence plain-English summary"
}
"""


def get_yesterdays_signal(session, today: date) -> str:
    """
    [6] MEMORY — Atlas's entire memory: yesterday's regime_signal,
    nothing more. Deliberately lean — see Operating Manual §7.1.
    Compare to Vera later, who has to remember a full thesis per
    position; Atlas only ever needs one day of lookback.
    """
    prior = (
        session.query(MacroBrief)
        .filter(MacroBrief.brief_date < today)
        .order_by(MacroBrief.brief_date.desc())
        .first()
    )
    return prior.regime_signal if prior else "unknown (no prior brief — first run)"


def run(today: date) -> dict:
    """
    Runs Atlas end-to-end, in order: [6] read memory -> [4] the
    agentic loop -> [5] validate against the contract -> [7] persist.
    Returns the validated dict so the orchestrator/other agents can
    use it immediately without a re-read.
    """
    with session_scope() as session:
        yesterdays_signal = get_yesterdays_signal(session, today)  # [6] MEMORY

    user_prompt = (
        f"Today's date: {today}. "
        f"Yesterday's regime signal was: '{yesterdays_signal}'. "
        f"Gather what you need and produce today's macro brief."
    )

    # =============================================================
    # [4] THE AGENTIC LOOP — this single call is where "agent" stops
    # being a metaphor. run_agent_loop() (defined in claude_client.py,
    # shared by every agent) hands Claude the [1] system prompt and
    # [2] tool menu, then lets CLAUDE decide which tools to call and
    # when it has enough to answer — we do not script that sequence
    # ourselves. Every tool call Claude makes gets routed through
    # [3] execute_tool() before coming back to him.
    # =============================================================
    raw_response = run_agent_loop(
        system_prompt=ATLAS_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        tools=ATLAS_TOOLS,
        tool_executor=execute_tool,
    )

    # [5] OUTPUT CONTRACT enforced here: a malformed or off-contract
    # response raises BEFORE anything touches the database — never
    # silently store something that doesn't match what the rest of
    # the system expects to read back later.
    parsed = extract_json(raw_response)
    output = AtlasOutput.model_validate(parsed)

    # =============================================================
    # [7] PERSISTENCE — the one durable trace of this entire run.
    # This is what get_yesterdays_signal() will read tomorrow, and
    # what Solomon will eventually read every day.
    # =============================================================
    with session_scope() as session:
        # Idempotent write: if today's brief already exists (e.g. this
        # is a manual rerun during testing), overwrite it rather than
        # crashing on the brief_date unique constraint. A second run
        # on the same day should refresh today's brief, not duplicate
        # or fail — this is what makes reruns safe.
        stmt = pg_insert(MacroBrief).values(
            brief_date=output.date,
            regime_signal=output.regime_signal,
            change_from_yesterday=output.change_from_yesterday,
            confidence=output.confidence,
            key_events=output.key_events_today,
            notable_moves=output.notable_overnight_moves,
            narrative=output.narrative,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["brief_date"],
            set_={
                "regime_signal": stmt.excluded.regime_signal,
                "change_from_yesterday": stmt.excluded.change_from_yesterday,
                "confidence": stmt.excluded.confidence,
                "key_events": stmt.excluded.key_events,
                "notable_moves": stmt.excluded.notable_moves,
                "narrative": stmt.excluded.narrative,
            },
        )
        session.execute(stmt)

    return output.model_dump()


if __name__ == "__main__":
    # Manual smoke test: python agents/atlas.py
    result = run(date.today())
    print(result)