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
  [5b] VALIDATION LAYER -> validate_proposals(), a deterministic second
                            gate the manual calls for and that did not
                            exist until E5. See its docstring.
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

WHY SOLOMON GETS A VALIDATION LAYER AND ATLAS DOESN'T: he is the only
agent whose output crosses from analysis into instruction. Atlas
describes the weather; Vera describes companies; a defect in either
produces a bad opinion. A defect in Solomon produces an ORDER. The
Pydantic contract already stops him inventing a *field*; it cannot
stop him inventing a *ticker*, because "NVDA" is a perfectly valid
string. That gap is what validate_proposals() closes.
"""

import logging
from datetime import date, timedelta
from typing import Literal, Optional

from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.clients.claude_client import run_agent_loop, extract_json
from core.db import session_scope
from core.models import StrategyDecision, Proposal, Position

logger = logging.getLogger(__name__)


# =================================================================
# [5] OUTPUT CONTRACT
# =================================================================
class ProposalOutput(BaseModel):
    ticker: str
    action: Literal["exit", "trim", "add", "new_position"]
    linked_trigger: str  # e.g. "vera:thesis_broken:AAPL"
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
- An "exit" recommendation from Vera's undocumented-position review
  IS a valid escalation trigger on its own — a position that entered
  the book without ever going through proper research deserves prompt
  attention regardless of whether anything new has "changed" about it,
  since nothing was ever documented to change FROM.
- A "profit_target_sustained" trigger IS a valid TRIM escalation on
  its own — Vera only assigns this trigger when the trailing week's
  data shows a genuinely sustained gain, not a single-day spike, so
  it doesn't need a separate catalyst the way a new position would.
  It does NOT on its own justify a full exit.
- A "new_position" proposal requires a candidate with conviction
  score of 4 or 5 AND a specific, named catalyst — not just "looks
  interesting."
- An "add" proposal requires a position you already hold, which Vera
  monitored today, and whose thesis she did NOT mark at_risk or
  broken. You do not add to a name that is deteriorating.
- The macro backdrop ALONE, with no company-specific trigger from
  Vera, should never justify action on a specific stock. A "risk-off"
  regime signal can raise your urgency or caution on candidates, but
  it never substitutes for a real, ticker-specific reason.
- Every proposal MUST cite a specific linked_trigger pointing to
  where it came from (e.g. "vera:thesis_broken:AAPL",
  "vera:new_candidate:MSFT") — never a vague justification. The
  trigger must name the ticker it belongs to.
- You may only propose a ticker that appears in the material below.
  You do not have a universe of your own. If a name is not in Vera's
  monitoring, her candidates, or her undocumented-position reviews,
  it does not exist for the purposes of today's decision.
- A DATA COVERAGE figure appears below. It is the share of the names
  Vera looked at for which she actually received complete data. A low
  number means she was reasoning from a fragment, and is a reason to
  raise your bar, not lower it. Below the floor, new positions and
  adds are refused in code — trims and exits are not, because a broken
  feed is a reason to stop buying and never a reason to stop
  de-risking.
- You will be shown your own escalation history from recent days. If
  you've escalated something every single day recently, treat that
  as a signal to raise your own bar today, not lower it.

EVERY RULE ABOVE IS ALSO CHECKED IN CODE AFTER YOU ANSWER. A proposal
naming a ticker Vera did not write about today, or one whose stated
justification does not match her recorded status, is dropped before it
reaches Risk — and the drop is recorded against your name in the run
ledger. Proposing loosely does not get you a trade; it gets you an
audit trail showing you proposed something unfounded. Propose only
what you can point at.

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
# [5b] THE VALIDATION LAYER  (E5)
#
# The Operating Manual specifies a second, deterministic layer between
# Solomon and Risk. It was never built, and the gap it left was not
# theoretical: Nora checks SIZE, not PROVENANCE. She will happily
# approve an 8%-headroom position in a ticker that exists nowhere in
# Vera's research, and Ada will place it. A hallucinated string is a
# valid string.
#
# So these functions re-derive, from Vera's actual output, whether each
# proposal could legitimately have come from anywhere. They are pure —
# no session, no network, no model — which is what makes them testable
# and what makes them trustworthy: a check that itself depends on an
# LLM is not a check.
#
# A rejected proposal is DROPPED but never silently. It is returned in
# `rejected_proposals` with a reason, which the orchestrator stores in
# agent_runs.raw_output — so "Solomon proposed something unfounded" is
# a fact on the record rather than an absence. That matters more than
# it sounds: the failure mode this guards against is a model that has
# quietly started reasoning badly, and you only see that in the
# pattern over days.
# =================================================================
MIN_NEW_POSITION_CONVICTION = 4

# Vera's monitoring trigger vocabulary; "none" means she recorded no
# new deteriorating information, which is precisely the case the first
# hard rule says is not sufficient to act on.
NO_NEW_INFORMATION = "none"
SUSTAINED_GAIN = "profit_target_sustained"


def _norm(ticker) -> str:
    return str(ticker or "").strip().upper()


def build_vera_index(vera_output: dict) -> dict[str, dict]:
    """
    Everything Vera actually said today, keyed by ticker.

    This is the whole universe Solomon is permitted to draw from. Built
    from her output rather than from the database on purpose: the
    question is not "is this a real company", it is "did our research
    agent write about this today".
    """
    index: dict[str, dict] = {}

    for m in vera_output.get("monitoring") or []:
        t = _norm(m.get("ticker"))
        if t:
            index.setdefault(t, {})["monitoring"] = m

    for c in vera_output.get("candidates") or []:
        t = _norm(c.get("ticker"))
        if t:
            index.setdefault(t, {})["candidate"] = c

    for o in vera_output.get("orphan_reviews") or []:
        t = _norm(o.get("ticker"))
        if t:
            index.setdefault(t, {})["orphan"] = o

    return index


def _reject_reason(proposal: ProposalOutput,
                   vera_index: dict[str, dict],
                   held: set[str]) -> Optional[str]:
    """
    The reason this proposal may not proceed, or None if it stands.

    Ordered cheapest-and-most-fundamental first, so the reason returned
    is the most informative one rather than whichever check happened to
    run first.
    """
    ticker = _norm(proposal.ticker)
    action = proposal.action
    trigger = (proposal.linked_trigger or "").strip()

    if not ticker:
        return "proposal has no ticker"

    # --- provenance: did Vera write about this at all today? --------
    entry = vera_index.get(ticker)
    if entry is None:
        return (f"{ticker} appears nowhere in Vera's output today — not in "
                f"monitoring, candidates, or undocumented-position reviews. "
                f"Solomon has no research universe of his own.")

    # --- the linked_trigger must actually point somewhere -----------
    if not trigger:
        return f"{ticker}: no linked_trigger given"
    if ticker not in trigger.upper():
        return (f"{ticker}: linked_trigger {trigger!r} does not name the ticker "
                f"it belongs to, so it cannot be traced back to a source")

    monitoring = entry.get("monitoring")
    candidate = entry.get("candidate")
    orphan = entry.get("orphan")

    # --- new_position ------------------------------------------------
    if action == "new_position":
        if ticker in held:
            return (f"{ticker} is already held — a change to an existing "
                    f"position is 'add' or 'trim', not 'new_position'")
        if candidate is None:
            return (f"{ticker} was not among Vera's candidates today, so there "
                    f"is no conviction score or catalyst behind it")
        conviction = candidate.get("conviction_score")
        if conviction is None or int(conviction) < MIN_NEW_POSITION_CONVICTION:
            return (f"{ticker} conviction {conviction} is below the "
                    f"{MIN_NEW_POSITION_CONVICTION} bar for a new position")
        if not str(candidate.get("catalyst") or "").strip():
            return f"{ticker} has no named catalyst — 'looks interesting' is not a trigger"
        return None

    # --- everything else acts on something already on the book -------
    if ticker not in held:
        return (f"{ticker} is not currently held, so '{action}' has nothing "
                f"to act on")

    # --- add ---------------------------------------------------------
    # The manual is silent on 'add' specifically. This mirrors Marcus's
    # existing guardrail (never increase size on an at_risk name) one
    # step earlier in the chain, so the desk refuses at the point of
    # DECISION rather than at the point of sizing. Flagged as a
    # deliberate extension rather than a transcribed rule.
    if action == "add":
        if monitoring is None:
            return (f"{ticker} is held but Vera did not monitor it today — "
                    f"nothing supports adding to it")
        status = monitoring.get("status")
        if status in ("at_risk", "broken"):
            return (f"{ticker} is '{status}' in Vera's monitoring — the desk "
                    f"does not add to a deteriorating thesis")
        return None

    # --- trim / exit -------------------------------------------------
    if action in ("trim", "exit"):
        # An undocumented position Vera recommends exiting is a valid
        # trigger on its own — nothing was ever documented to change from.
        if orphan is not None and orphan.get("recommendation") == "exit":
            return None

        if monitoring is None:
            return (f"{ticker} is held but has no monitoring entry and no "
                    f"undocumented-position review — nothing supports "
                    f"'{action}'")

        status = monitoring.get("status")
        mon_trigger = monitoring.get("trigger")

        if status == "broken":
            return None

        # A sustained gain justifies taking something off the table. It
        # does not justify closing the whole position, which is why this
        # is checked before the at_risk branch rather than folded into it.
        if mon_trigger == SUSTAINED_GAIN:
            if action == "trim":
                return None
            return (f"{ticker}: a sustained gain justifies a trim, not a full "
                    f"exit — Vera's status is '{status}'")

        if status == "at_risk":
            if mon_trigger in (None, "", NO_NEW_INFORMATION):
                return (f"{ticker} is at_risk with no newly named deteriorating "
                        f"trigger — not sufficient on its own to act")
            return None

        return (f"{ticker} is '{status}' in Vera's monitoring — '{action}' "
                f"requires 'broken', or 'at_risk' with a new trigger")

    return f"{ticker}: unrecognised action {action!r}"


def validate_proposals(proposals: list[ProposalOutput],
                       vera_output: dict,
                       held: set[str]) -> tuple[list[ProposalOutput], list[dict]]:
    """
    Split Solomon's proposals into those that survive the deterministic
    check and those that do not.

    Pure: give it three plain values and it answers the same way every
    time. Returns (kept, rejected) where each rejected entry is the
    original proposal plus a human-readable `rejection_reason`.
    """
    vera_index = build_vera_index(vera_output)
    held = {_norm(t) for t in held}

    kept: list[ProposalOutput] = []
    rejected: list[dict] = []

    for p in proposals:
        reason = _reject_reason(p, vera_index, held)
        if reason is None:
            kept.append(p)
        else:
            entry = p.model_dump()
            entry["rejection_reason"] = reason
            rejected.append(entry)
            logger.warning("Solomon proposal rejected — %s", reason)

    return kept, rejected


# =================================================================
# THE DEGRADED-DATA GATE  (D7)
#
# Vera reports how much of what she asked FMP for she actually got.
# Below a floor, the desk stops opening or increasing risk.
#
# THE ASYMMETRY IS THE WHOLE POINT, and it is the same one the
# drawdown breaker makes rather than the one the kill switch makes.
# The kill switch stops everything including exits, deliberately: you
# reach for it when you no longer trust the system. A broken data feed
# is a different problem. It means we cannot see well enough to pick
# among names — so we stop BUYING. It is not a reason to stop selling.
# A gate that froze de-risking because the fundamentals feed was
# paywalled would leave the desk holding a broken thesis it could see
# perfectly well was broken.
#
# So: new_position and add are refused below the floor. trim and exit
# pass through untouched.
#
# The floor is a dial, not a discovered truth. 60% says: if we could
# not get a complete picture for more than two names in five, we are
# choosing among a fragment and should not pretend otherwise.
# =================================================================
MIN_DATA_COVERAGE_PCT = 60.0
RISK_INCREASING = {"new_position", "add"}


def coverage_gate(proposals: list[ProposalOutput],
                  data_coverage: Optional[dict]) -> tuple[list[ProposalOutput], list[dict]]:
    """
    Refuse risk-increasing proposals when Vera's data was too thin to
    support choosing among names. Pure, like validate_proposals().

    Two cases deliberately pass everything through:
      - no coverage report at all (an older Vera, or a stubbed one) —
        absence of a measurement is not evidence of a bad one;
      - coverage_pct of None, which means nothing was requested. A day
        holding no positions and screening nothing is not a degraded
        day, and scoring it 0% would stand the desk down for being idle.
    """
    if not data_coverage:
        return list(proposals), []

    pct = data_coverage.get("coverage_pct")
    if pct is None or pct >= MIN_DATA_COVERAGE_PCT:
        return list(proposals), []

    attempted = data_coverage.get("tickers_attempted", 0)
    complete = data_coverage.get("tickers_complete", 0)
    degraded = sorted((data_coverage.get("degraded_tickers") or {}).keys())

    kept: list[ProposalOutput] = []
    rejected: list[dict] = []
    for p in proposals:
        if p.action in RISK_INCREASING:
            entry = p.model_dump()
            entry["rejection_reason"] = (
                f"data coverage {pct}% is below the {MIN_DATA_COVERAGE_PCT}% floor "
                f"({complete} of {attempted} tickers came back complete; degraded: "
                f"{', '.join(degraded) or 'none named'}). The desk does not open or "
                f"increase risk on a fragment. De-risking is unaffected."
            )
            rejected.append(entry)
            logger.warning("Solomon proposal refused on data coverage — %s",
                           entry["rejection_reason"])
        else:
            kept.append(p)

    return kept, rejected


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


def get_held_tickers(session) -> set[str]:
    """What the desk actually owns, for the validation layer. Read at
    the same moment as the portfolio summary Solomon is shown, so the
    check and the prompt cannot disagree about the book."""
    return {_norm(p.ticker) for p in session.query(Position).all()}


def run(today: date, atlas_output: dict, vera_output: dict) -> dict:
    """
    Runs Solomon: gathers his self-monitoring memory and current
    portfolio state, then a single reasoning pass (via the shared
    agentic-loop harness, with an empty tool list) over Atlas's and
    Vera's already-finished outputs — then validates what comes back
    against Vera's actual output before anything is persisted.
    """
    with session_scope() as session:
        escalation_history = get_recent_escalation_history(session, today)
        portfolio_summary = get_current_portfolio_summary(session)
        held = get_held_tickers(session)

    data_coverage = vera_output.get("data_coverage")
    if not data_coverage:
        coverage_line = "not reported by this run of Vera."
    elif data_coverage.get("coverage_pct") is None:
        coverage_line = "no data was requested today — nothing held, nothing screened."
    else:
        coverage_line = (
            f"{data_coverage['coverage_pct']}% "
            f"({data_coverage['tickers_complete']} of "
            f"{data_coverage['tickers_attempted']} tickers complete). "
            f"Incomplete: {', '.join(sorted((data_coverage.get('degraded_tickers') or {}).keys())) or 'none'}. "
            f"Floor for opening or increasing risk: {MIN_DATA_COVERAGE_PCT}%."
        )

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

UNDOCUMENTED POSITION REVIEWS (from Vera — positions held with no
prior thesis, freshly assessed today; an "exit" recommendation here
is a valid escalation trigger even though it didn't go through the
normal candidate pipeline; "adopt" recommendations are already
documented and need no action from you):
{vera_output.get('orphan_reviews') or 'None — no undocumented positions currently held.'}

DATA COVERAGE (how much of what Vera asked for she actually got):
{coverage_line}

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
    # [5b] VALIDATION — between the model and the database, which is
    # the only place it can go. Once a proposal row exists, Nora has
    # something to approve and the provenance question is closed.
    # =============================================================
    proposed = list(output.proposals)

    # A model that says "no action" and then lists proposals has
    # contradicted itself. Resolve it in the restraint-preserving
    # direction: his stated verdict wins, the proposals are dropped.
    # The reverse (action with no proposals) needs no special case —
    # the recomputation below handles it.
    if not output.action_needed and proposed:
        rejected = [{**p.model_dump(),
                     "rejection_reason": "action_needed was false — a verdict of "
                                         "no action cannot carry proposals"}
                    for p in proposed]
        kept: list[ProposalOutput] = []
        logger.warning("Solomon returned %d proposal(s) with action_needed=false; "
                       "dropping them and standing by the verdict.", len(proposed))
    else:
        kept, rejected = validate_proposals(proposed, vera_output, held)

    # Provenance first, then coverage — so a hallucinated ticker on a
    # degraded day is reported as a hallucination, which is the more
    # serious of the two facts about it.
    kept, coverage_rejected = coverage_gate(kept, data_coverage)
    rejected = rejected + coverage_rejected

    # action_needed is DERIVED from what survived, not taken on trust.
    # Otherwise a day where every proposal was unfounded still opens
    # Phases 3 and 4 in the orchestrator, which gates on this flag.
    action_needed = bool(kept)

    if rejected:
        logger.warning("Solomon: %d of %d proposal(s) failed validation and will "
                       "not reach Risk.", len(rejected), len(proposed))

    # =============================================================
    # [7] PERSISTENCE — upsert the day's decision, then REPLACE its
    # proposals wholesale (delete + reinsert) rather than trying to
    # upsert individual proposal rows. This is a different, equally
    # valid idempotency pattern from Atlas/Vera's per-row upsert:
    # proposals don't have a stable identity of their own outside
    # "the set Solomon produced this run" — so on a same-day rerun,
    # the cleanest semantics are "this run's proposals fully replace
    # the previous run's," not a field-by-field merge.
    #
    # Only VALIDATED proposals are written. A rejected one is not a
    # weaker proposal, it is one with no basis — it does not belong in
    # a table Nora reads as a work queue.
    # =============================================================
    with session_scope() as session:
        stmt = pg_insert(StrategyDecision).values(
            decision_date=today,
            action_needed=action_needed,
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
        for p in kept:
            session.add(Proposal(
                strategy_decision_id=decision_id,
                ticker=_norm(p.ticker),
                action=p.action,
                linked_trigger=p.linked_trigger,
                rationale=p.rationale,
                urgency=p.urgency,
            ))

    # `rejected_proposals` rides back to the orchestrator, which stores
    # the returned dict in agent_runs.raw_output — so a rejection is
    # durable and greppable without a schema change.
    return {
        "action_needed": action_needed,
        "proposals": [p.model_dump() for p in kept],
        "watching": output.watching,
        "narrative": output.narrative,
        "rejected_proposals": rejected,
        "data_coverage": data_coverage,
        "validation": {
            "proposed": len(proposed),
            "kept": len(kept),
            "rejected": len(rejected),
            "model_said_action_needed": output.action_needed,
        },
    }


if __name__ == "__main__":
    # Manual smoke test: python -m agents.solomon
    # Reuses today's Atlas/Vera output if it already exists, instead
    # of re-burning FMP calls testing a downstream agent — see
    # core/dev_helpers.py for why. The real orchestrator.py always
    # runs Atlas/Vera fresh; this shortcut is testing-only.
    from core.dev_helpers import get_or_run_atlas, get_or_run_vera

    atlas_result = get_or_run_atlas(date.today())
    vera_result = get_or_run_vera(date.today(), atlas_result)
    print("Running Solomon...\n")

    result = run(date.today(), atlas_result, vera_result)
    print(result)
    if result["rejected_proposals"]:
        print(f"\n{len(result['rejected_proposals'])} proposal(s) failed validation:")
        for r in result["rejected_proposals"]:
            print(f"  - {r['rejection_reason']}")
