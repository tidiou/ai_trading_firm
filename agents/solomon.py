"""
Solomon — CIO/Strategy Agent
Operating Manual §7.3

ANATOMY OF THIS AGENT — same 7 pieces as Atlas and Vera, but Solomon
is a deliberately different SHAPE of agent worth noticing:

  [1] ROLE / MANDATE   -> SOLOMON_SYSTEM_PROMPT
  [2] SKILLS / TOOLS    -> NONE. Deliberately empty — see below.
  [3] TOOL DISPATCH     -> not needed (no tools to dispatch to)
  [4] THE AGENTIC LOOP  -> still used, via run_agent_loop() with an
                            empty tool list — degenerates to a single
                            reasoning call, but reuses the same harness
                            for consistency (JSON extraction, error
                            handling) rather than writing a bespoke path.
  [5] OUTPUT CONTRACT   -> SolomonOutput (Pydantic)
  [6] MEMORY            -> get_recent_escalation_history() — a
                            SELF-MONITORING memory, different in kind
                            from Atlas's or Vera's: it's not "what did
                            the world do", it's "what have I been
                            doing" (a check on his own behavior)
  [7] PERSISTENCE        -> strategy_decisions + proposals tables,
                            via upsert-then-replace (see note in run())

WHY NO TOOLS: Solomon's whole job is synthesis, not investigation.
He reads Atlas's and Vera's ALREADY-FINISHED outputs — going back to
raw news or raw fundamentals himself would just be redoing their
work with less context than they had. Giving an agent tools it
doesn't need isn't neutral — it invites scope creep (nothing stops
him from starting to "research" a candidate himself instead of
trusting Vera's work) and slower, muddier reasoning.

DECISION RIGHTS: Solomon can only ever ESCALATE or not — he has no
capability to size a position, approve it against risk limits, or
execute. Those boundaries are enforced structurally: nothing in his
output contract has a field for size, and there is no execution tool
on his menu even if he wanted one.
"""

from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.clients.claude_client import run_agent_loop, extract_json
from core.db import session_scope
from core.models import StrategyDecision, Proposal, Position


# =================================================================
# [5] OUTPUT CONTRACT
# =================================================================
class ProposalOutput(BaseModel):
    ticker: str
    action: Literal["exit", "trim", "add", "new_position"]
    linked_trigger: str  # e.g. "vera:thesis_broken", "vera:new_candidate"
    rationale: str
    urgency: Literal["same_day", "this_week"]


class SolomonOutput(BaseModel):
    action_needed: bool
    proposals: list[ProposalOutput]
    watching: list[str]  # tickers noted but not escalated, with why (in narrative)
    narrative: str


# =================================================================
# [1] ROLE / MANDATE — the restraint principle, encoded as explicit,
# checkable rules rather than a vague "use good judgment."
# =================================================================
SOLOMON_SYSTEM_PROMPT = """You are Solomon, the CIO/Strategy agent at MBY-Trading.

Your job is NOT to generate ideas — Vera already did that. Your job
is to be the gatekeeper of ATTENTION: given today's macro backdrop
and Vera's research, decide whether anything actually clears the bar
for escalation to the Risk and Portfolio Management agents. On most
days, the correct answer is that NOTHING clears the bar. Do not
manufacture action to seem useful — a quiet, well-reasoned "no action
needed" is a completely successful outcome.

Hard rules — these are not suggestions:
- A "trim" or "exit" proposal on a held position requires Vera's
  status to be "at_risk" WITH a newly named deteriorating trigger, or
  "broken". A position sitting at "at_risk" with no new information
  since a prior day is NOT sufficient on its own to act.
- A "new_position" proposal requires a candidate with conviction
  score of 4 or 5 AND a specific, named catalyst — not just "looks
  interesting."
- The macro backdrop ALONE, with no company-specific trigger from
  Vera, should never justify action on a specific stock. A "risk-off"
  regime signal can raise your urgency or caution on candidates, but
  it never substitutes for a real, ticker-specific reason.
- Every proposal MUST cite a specific linked_trigger pointing to
  where it came from (e.g. "vera:thesis_broken:AAPL",
  "vera:new_candidate:MSFT") — never a vague justification.
- You will be shown your own escalation history from recent days. If
  you've escalated something every single day recently, treat that
  as a signal to raise your own bar today, not lower it.

Respond with ONLY a JSON object — your response must start with "{"
as its very first character and contain nothing else: no headers, no
explanation of your reasoning process, no text before or after it.
Do your reasoning silently and output only the final result, matching
exactly this shape:

{
  "action_needed": true or false,
  "proposals": [
    {
      "ticker": "...",
      "action": "exit | trim | add | new_position",
      "linked_trigger": "...",
      "rationale": "1-2 sentences",
      "urgency": "same_day | this_week"
    }
  ],
  "watching": ["tickers you noticed but deliberately did not escalate"],
  "narrative": "2-4 sentences synthesizing the day, even if action_needed is false"
}

If nothing clears the bar, "proposals" should be an empty array —
that is the expected, common case, not a failure to find something.
"""


# =================================================================
# [6] MEMORY — self-monitoring, not world-monitoring. This is a
# genuinely different KIND of memory than Atlas's or Vera's: it's
# not about tracking the world, it's about Solomon watching his own
# pattern of behavior for signs of drift (escalating too readily).
# =================================================================
def get_recent_escalation_history(session, today: date, lookback_days: int = 5) -> str:
    cutoff = today - timedelta(days=lookback_days * 3)  # generous window in case of gaps
    recent = (
        session.query(StrategyDecision)
        .filter(StrategyDecision.decision_date < today, StrategyDecision.decision_date >= cutoff)
        .order_by(StrategyDecision.decision_date.desc())
        .limit(lookback_days)
        .all()
    )
    if not recent:
        return "No prior decisions on record — this is the first run."
    lines = [
        f"{d.decision_date}: {'escalated action' if d.action_needed else 'no action needed'}"
        for d in recent
    ]
    return "\n".join(lines)


def get_current_portfolio_summary(session) -> str:
    """
    Not really 'memory' in Solomon's own right — this is Otis's future
    job (single source of truth). Until Otis exists, Solomon reads the
    positions table directly. Worth revisiting once Otis is built.
    """
    positions = session.query(Position).all()
    if not positions:
        return "Portfolio is currently empty — no open positions."
    return "\n".join(f"{p.ticker}: {p.weight_pct}% of portfolio" for p in positions)


def run(today: date, atlas_output: dict, vera_output: dict) -> dict:
    """
    Runs Solomon: gathers his self-monitoring memory and current
    portfolio state, then a single reasoning pass (via the shared
    agentic-loop harness, with an empty tool list) over Atlas's and
    Vera's already-finished outputs.
    """
    with session_scope() as session:
        escalation_history = get_recent_escalation_history(session, today)
        portfolio_summary = get_current_portfolio_summary(session)

    user_prompt = f"""Today's date: {today}

MACRO BACKDROP (from Atlas):
regime_signal: {atlas_output.get('regime_signal')}
change_from_yesterday: {atlas_output.get('change_from_yesterday')}
confidence: {atlas_output.get('confidence')}
narrative: {atlas_output.get('narrative')}

POSITION MONITORING (from Vera):
{vera_output.get('monitoring') or 'No open positions to monitor.'}

NEW CANDIDATES (from Vera):
{vera_output.get('candidates') or 'No new candidates surfaced today.'}

CURRENT PORTFOLIO:
{portfolio_summary}

YOUR RECENT ESCALATION HISTORY (self-monitoring):
{escalation_history}

Decide whether anything clears the bar for escalation today."""

    # =============================================================
    # [4] THE AGENTIC LOOP — reused even with zero tools, so a
    # malformed/empty response gets the same robust JSON extraction
    # and error handling every other agent gets, rather than a
    # separate bespoke code path just for Solomon.
    # =============================================================
    raw_response = run_agent_loop(
        system_prompt=SOLOMON_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        tools=[],  # deliberately empty — see module docstring
        tool_executor=lambda name, inp: (_ for _ in ()).throw(
            RuntimeError(f"Solomon has no tools, but one was called: {name}")
        ),
        max_tokens=2000,
    )

    parsed = extract_json(raw_response)
    output = SolomonOutput.model_validate(parsed)

    # =============================================================
    # [7] PERSISTENCE — upsert the day's decision, then REPLACE its
    # proposals wholesale (delete + reinsert) rather than trying to
    # upsert individual proposal rows. This is a different, equally
    # valid idempotency pattern from Atlas/Vera's per-row upsert:
    # proposals don't have a stable identity of their own outside
    # "the set Solomon produced this run" — so on a same-day rerun,
    # the cleanest semantics are "this run's proposals fully replace
    # the previous run's," not a field-by-field merge.
    # =============================================================
    with session_scope() as session:
        stmt = pg_insert(StrategyDecision).values(
            decision_date=today,
            action_needed=output.action_needed,
            narrative=output.narrative,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["decision_date"],
            set_={
                "action_needed": stmt.excluded.action_needed,
                "narrative": stmt.excluded.narrative,
            },
        )
        result = session.execute(stmt.returning(StrategyDecision.id))
        decision_id = result.scalar_one()

        session.query(Proposal).filter(Proposal.strategy_decision_id == decision_id).delete()
        for p in output.proposals:
            session.add(Proposal(
                strategy_decision_id=decision_id,
                ticker=p.ticker,
                action=p.action,
                linked_trigger=p.linked_trigger,
                rationale=p.rationale,
                urgency=p.urgency,
            ))

    return output.model_dump()


if __name__ == "__main__":
    # Manual smoke test: python -m agents.solomon
    # Chains Atlas -> Vera -> Solomon, mirroring the real daily cycle.
    from agents import atlas, vera

    print("Running Atlas...")
    atlas_result = atlas.run(date.today())
    print("Running Vera...")
    vera_result = vera.run(date.today(), atlas_result)
    print("Running Solomon...\n")

    result = run(date.today(), atlas_result, vera_result)
    print(result)