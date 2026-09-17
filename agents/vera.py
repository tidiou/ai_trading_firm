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

from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert

import logging

from core.clients import fmp_client
from core.clients.claude_client import run_agent_loop, extract_json
from core.db import session_scope
from core.models import (Thesis, PositionMonitoringLog, NewCandidate, Position,
                         RiskPolicyVersion, PositionPnlHistory, ThesisAssumption)
from core.thesis import AssumptionCheck, compute_verdict

logger = logging.getLogger(__name__)


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

# The conviction at or above which a screened name is SURFACED as a
# new_candidates row. Below it the name is counted in the screening
# summary and discarded.
#
# SET TO MATCH SOLOMON'S new_position BAR DELIBERATELY. He requires
# conviction >= 4 with a named catalyst, so surfacing a 3 would write a
# row that can only ever be rejected downstream — noise in the table
# and in the dashboard, with no decision riding on it.
#
# THE POINT OF THE CONSTANT is that the decision now lives in CODE
# rather than inside the model's response. Vera reports her read on
# every name she screened; this line decides what becomes a row. Before
# this, a name scoring 3 was discarded inside the model's answer and
# left no trace anywhere, so "nothing was close" and "one name nearly
# cleared the bar" were the same observation: an empty table.
SURFACING_THRESHOLD = 4


# =================================================================
# [5] OUTPUT CONTRACTS — one per job, mirroring the Operating
# Manual's two output shapes exactly.
# =================================================================
class AssumptionOut(BaseModel):
    """One claim a thesis rests on, as Vera authors it.

    `falsify_threshold` is prose rather than a number because the
    claims that matter are not all numeric — "management keeps
    repurchasing below intrinsic value" has no numeric threshold, and
    demanding one would either exclude the claim or invite a
    fabricated figure. The requirement is specificity a later reader
    can judge, which the prompt asks for and review enforces.
    """

    claim: str
    metric: Optional[str] = None
    direction: Optional[Literal["up", "down", "stable"]] = None
    falsify_threshold: Optional[str] = None
    is_load_bearing: bool = True


class AssumptionCheckOut(BaseModel):
    """What one claim did since it was last looked at.

    NOTE THE ABSENCE OF A VERDICT FIELD. Vera reports per-claim
    status; core.thesis.compute_verdict derives the thesis-level
    verdict from these. If she were asked for the verdict directly she
    could return "strengthened" while these same notes said two
    load-bearing claims were failing, and nothing would catch the
    contradiction.
    """

    claim: str
    status: Literal["holding", "improving", "strained", "broken", "unchecked"]
    evidence: str = ""


class PositionMonitoring(BaseModel):
    ticker: str
    status: Literal["intact", "at_risk", "broken"]
    trigger: Literal["none", "earnings", "news", "fundamental_drift", "macro_conflict", "profit_target_sustained"]
    reasoning: str
    conviction_score: int  # 1-5, compared against the thesis's original score
    # One entry per open assumption on this thesis. Empty for a thesis
    # that has none yet — which yields NO verdict rather than a
    # flattering default.
    assumption_checks: list[AssumptionCheckOut] = Field(default_factory=list)


class NewCandidateOutput(BaseModel):
    """One screened name — NOT necessarily one that gets surfaced.

    Vera now returns an entry for every ticker she screened, and
    `SURFACING_THRESHOLD` decides which become new_candidates rows. So
    the supporting fields carry defaults: a name she scores 2 needs a
    ticker, a score and a line of reasoning, not a full valuation
    workup she then throws away. A name at or above the bar is required
    to have the full set, and that requirement is enforced in
    `partition_screened()` rather than here — Pydantic cannot express
    "required only when another field is high", and hiding the rule in
    a validator would put a surfacing decision back inside the
    contract, which is exactly what this change moves out of it.
    """

    ticker: str
    thesis: str
    conviction_score: int  # 1-5
    catalyst: str = ""
    key_risks: list[str] = Field(default_factory=list)
    valuation_snapshot: dict = Field(default_factory=dict)


# =================================================================
# [1] ROLE / MANDATE — monitoring job
# =================================================================
ASSUMPTIONS_SYSTEM_PROMPT = """You are Vera, the Equity Research agent at MBY-Trading.

You are being asked to do something you do ONCE per thesis: write down
what it actually rests on.

A thesis written as prose can never be wrong — it just stops being
mentioned. Your job here is to turn one into a short list of CLAIMS
that can be checked, and specifically that can be shown to be FALSE.

For each claim:
- State it as something that is either true or not. "Cloud growth
  stays above 20%" is a claim. "Strong competitive position" is not.
- Name the METRIC that bears on it, if there is one, and which
  DIRECTION supports the claim.
- State what would FALSIFY it. Be specific enough that someone
  reading this in a year can tell whether it happened. "Growth slows"
  is useless; "two consecutive quarters below 15%" is a threshold.
  If the claim is genuinely not numeric, describe the observable
  event instead — vague is the only failure here, not non-numeric.
- Mark whether it is LOAD-BEARING: does the thesis die without it? Be
  honest and be sparing. If everything is load-bearing, nothing is,
  and the first minor disappointment will read as the thesis being
  broken.

Write THREE TO SIX claims. Fewer than three and you have not decomposed
the thesis; more than six and you are listing everything you know about
the company rather than what the investment depends on.

Do not re-argue the thesis, do not rate it, and do not recommend
anything. You are recording its structure.

Respond with ONLY a JSON array — your response must start with "["
as its very first character and contain nothing else:

[
  {
    "claim": "a statement that can be true or false",
    "metric": "what to watch, or null",
    "direction": "up | down | stable, or null",
    "falsify_threshold": "what would settle this against us, specifically",
    "is_load_bearing": true
  }
]
"""


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

THE ASSUMPTIONS. Each position is shown with the numbered claims its
thesis rests on. Return a status for EVERY claim listed, using its
exact wording so it can be matched:

    holding     still true; nothing has moved against it
    improving   evidence moved in its favour beyond what was claimed
    strained    moved against it, but NOT past its stated threshold
    broken      past its stated threshold

    unchecked   no information bearing on it arrived today

`unchecked` is the honest answer most days for most claims, and it is
expected — a quarterly metric does not move on a Tuesday. Do not
report `holding` for a claim you have no new information about:
"nothing arrived" and "I verified it and it holds" are different
statements, and the difference is the point.

`broken` requires the claim to have passed the threshold WRITTEN IN
IT, not your general sense that things look worse. If it moved against
the claim but has not passed the threshold, that is `strained`.

DO NOT return an overall verdict on the thesis. That is computed from
these statuses, deliberately not asked of you.

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
    "assumption_checks": [
      {"claim": "exact wording of the claim as shown to you",
       "status": "holding | improving | strained | broken | unchecked",
       "evidence": "what moved it, or empty if unchecked"}
    ],
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
- REPORT ON EVERY TICKER IN THE LIST, including the ones you think are
  nowhere near worth acting on. You are not deciding what gets acted
  on — you are recording your read. A name you score 2 is a finding,
  not a non-answer, and the firm needs it on record.
- Zero names worth acting on is a completely valid, expected outcome
  on most days — do not inflate a score to have something to report.
  Reporting six 2s is a complete, useful day's work.
- Never use the words "buy" or "sell" — you produce theses and
  conviction scores, not trade instructions. Sizing and execution
  are other agents' jobs entirely.
- Factor in the macro backdrop you're given, but don't let it alone
  drive a stock-specific call — a bullish idea needs its own
  company-specific catalyst even in a risk-on backdrop.
- Score conviction 1-5 honestly: 5 should be rare. Most genuinely
  good ideas are a 3 or 4, and on a quiet day most names are 1-2.
- A score of 4 or 5 means you are asserting this is actionable NOW,
  so those entries MUST carry a genuine, named catalyst — not "looks
  cheap" or "good company". If you cannot articulate why NOW, the
  honest score is 3, not a 4 with a vague catalyst.
- For a name you score 1-3, `thesis` can be a single sentence and
  `catalyst`, `key_risks` and `valuation_snapshot` may be omitted.
  Don't spend a full workup on a name you are not putting forward.

Respond with ONLY a JSON array — your response must start with "["
as its very first character and contain nothing else: no headers, no
"screening notes" sections, no explanation of your reasoning process,
no text before or after the array. Do your reasoning silently and
output only the final result. The array should have ONE ENTRY PER
TICKER you were given:

[
  {
    "ticker": "...",
    "thesis": "2-3 sentences if you score this 4-5; one line if 1-3",
    "conviction_score": 1-5,
    "catalyst": "the specific, named reason this matters now — REQUIRED at 4-5, omit at 1-3",
    "key_risks": ["...", "..."],
    "valuation_snapshot": {"note": "key numbers that informed this view"}
  }
]
"""


# =================================================================
# SCREENING SUMMARY — what the pass did, including when it did
# nothing
#
# WHY THIS EXISTS. The monitoring pass writes a row per held name
# every day, so it always speaks. The screening pass did not: when it
# found nothing it wrote zero new_candidates rows and nothing else, so
# three different situations were one observation —
#
#     screened everything, nothing came close
#     screened everything, one name nearly cleared the bar
#     screening errored and the failure was swallowed upstream
#
# — all of which are an empty table. That is the same
# absence-versus-expectation problem that `orchestrator.cycle_status`
# exists for, and the same principle as the dashboard's rule that a
# metric with no table behind it shows its reason rather than nothing.
#
# WHY IT IS COMPUTED, NOT NARRATED. The temptation is to ask the model
# to explain why it surfaced nothing. Two problems: it will produce a
# fluent explanation whether or not that was the reason, and asking an
# agent to justify an empty result puts pressure on it to find
# something to say — one short step from finding something to surface,
# which is the exact pressure the restraint principle exists to
# resist. So every field below is derived in code from the scores she
# returned. There is no narrative field, and that is deliberate.
# =================================================================
def partition_screened(
    screened: list[NewCandidateOutput],
    threshold: int = SURFACING_THRESHOLD,
) -> tuple[list[NewCandidateOutput], list[NewCandidateOutput]]:
    """Split screened names into (surfaced, held_back).

    Surfacing requires BOTH a score at or above the threshold AND a
    non-empty catalyst. The second half is not redundant: the prompt
    says a 4 must name its catalyst, and a 4 that doesn't has failed
    its own stated standard. Writing it as a candidate row anyway
    would hand Solomon a proposal whose `linked_trigger` cannot name
    anything — so it is held back, and counted, rather than surfaced.
    """
    surfaced, held_back = [], []
    for item in screened:
        if item.conviction_score >= threshold and item.catalyst.strip():
            surfaced.append(item)
        else:
            held_back.append(item)
    return surfaced, held_back


def screening_summary(
    universe: list[str],
    screened: list[NewCandidateOutput],
    surfaced: list[NewCandidateOutput],
    held_tickers: list[str],
    threshold: int = SURFACING_THRESHOLD,
) -> dict:
    """A statement about the screening pass, whether or not it surfaced
    anything. Every number here is computed, or None with the reason
    implied by the shape — never a placeholder.

    `best_conviction` is the field this was built for. Over weeks it
    separates two hypotheses that look identical from outside:

        consistently 3-4  the bar is roughly right and the universe is
                          producing near-misses — a calibration question
        consistently 1-2  the universe has nothing to offer, and no
                          threshold change will help
    """
    returned = [s.ticker for s in screened]
    best = max(screened, key=lambda s: s.conviction_score, default=None)

    # A shortfall between what was asked for and what came back makes
    # best_conviction unreliable, so it is reported rather than
    # smoothed over. Names are listed, not just counted: which ticker
    # went missing is the actionable half.
    not_returned = [t for t in universe if t not in returned]

    return {
        "universe_size": len(universe),
        "screened_count": len(screened),
        "not_returned": not_returned,
        "excluded_as_held": sorted(held_tickers),
        "threshold": threshold,
        "surfaced_count": len(surfaced),
        "surfaced_tickers": [s.ticker for s in surfaced],
        "best_conviction": best.conviction_score if best else None,
        "best_conviction_ticker": best.ticker if best else None,
        "conviction_distribution": {
            str(score): sum(1 for s in screened if s.conviction_score == score)
            for score in range(1, 6)
            if any(s.conviction_score == score for s in screened)
        },
        "held_back_count": len(screened) - len(surfaced),
    }


def describe_screening(summary: dict) -> str:
    """One line for the daily report and the dashboard. Reads the
    computed summary only — it never reaches for the model."""
    if summary["screened_count"] == 0:
        return ("Screening returned nothing at all — not "
                "'no ideas', but no read on any name. Treat as a failed pass.")

    parts = [f"{summary['screened_count']} of {summary['universe_size']} screened"]
    if summary["excluded_as_held"]:
        parts.append(f"{', '.join(summary['excluded_as_held'])} held")
    if summary["not_returned"]:
        parts.append(f"no read on {', '.join(summary['not_returned'])}")

    head = " · ".join(parts) + "."

    if summary["surfaced_count"]:
        tail = (f" Surfaced {summary['surfaced_count']}: "
                f"{', '.join(summary['surfaced_tickers'])}.")
    else:
        tail = (f" Nothing surfaced. Best conviction "
                f"{summary['best_conviction']} "
                f"({summary['best_conviction_ticker']}) against a bar of "
                f"{summary['threshold']}.")
    return head + tail


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


# =================================================================
# ASSUMPTIONS — authored once per thesis, checked every day
#
# WHY THE AUTHORING IS SEPARATE FROM THE CHECKING. They have different
# cadences and different mandates. Writing down what a thesis rests on
# is a one-time act of decomposition; checking those claims is a daily
# comparison. Folding both into the monitoring prompt would make it
# conditional on whether the thesis happened to have assumptions yet,
# and a prompt that does two jobs depending on database state is how
# you get a model doing neither well.
#
# It also means the cost is where it belongs. Authoring is a handful
# of calls, ever. Checking is the daily work.
# =================================================================
def open_assumptions(session, thesis_id: int) -> list[ThesisAssumption]:
    return (
        session.query(ThesisAssumption)
        .filter(ThesisAssumption.thesis_id == thesis_id,
                ThesisAssumption.retired_date.is_(None))
        .order_by(ThesisAssumption.id)
        .all()
    )


def author_assumptions(today: date, thesis: Thesis) -> list[dict]:
    """Decompose a thesis into checkable claims. Called ONCE per
    thesis — the first monitoring pass after it opens.

    This is also what retro-fits the theses that predate migration
    011. They were adopted as orphans with no structure behind them,
    and until they have claims they report no verdict at all, which is
    honest but useless. The first run after this ships gives them one.
    """
    prompt = (
        f"Ticker: {thesis.ticker}\n"
        f"Opened: {thesis.opened_date}\n"
        f"Conviction at open: {thesis.original_conviction}\n"
        f"Catalyst: {thesis.catalyst or 'none recorded'}\n\n"
        f"The thesis, as written:\n{thesis.thesis_text}"
    )
    if thesis.key_risks:
        prompt += f"\n\nRisks recorded at open: {thesis.key_risks}"

    raw = run_agent_loop(
        system_prompt=ASSUMPTIONS_SYSTEM_PROMPT,
        user_prompt=prompt,
        tools=VERA_TOOLS,
        tool_executor=execute_tool,
        max_tokens=2000,
    )
    authored = [AssumptionOut.model_validate(item) for item in extract_json(raw)]

    with session_scope() as session:
        existing = {a.claim for a in open_assumptions(session, thesis.id)}
        for a in authored:
            if a.claim in existing:
                continue  # UNIQUE (thesis_id, claim) — a rerun must not duplicate
            session.add(ThesisAssumption(
                thesis_id=thesis.id,
                claim=a.claim,
                metric=a.metric,
                direction=a.direction,
                falsify_threshold=a.falsify_threshold,
                is_load_bearing=a.is_load_bearing,
                status="holding",
                opened_date=today,
            ))
    logger.info("Authored %s assumption(s) for %s (thesis %s).",
                len(authored), thesis.ticker, thesis.id)
    return [a.model_dump() for a in authored]


def _format_assumptions(assumptions: list[ThesisAssumption]) -> str:
    """The claims, numbered, for the monitoring prompt. Exact wording
    is echoed back by the model so checks can be matched to rows."""
    if not assumptions:
        return "  (no assumptions on record for this thesis yet)"
    lines = []
    for i, a in enumerate(assumptions, 1):
        bits = [f"  {i}. {a.claim}"]
        if a.metric:
            bits.append(f"     metric: {a.metric}"
                        + (f" ({a.direction} supports it)" if a.direction else ""))
        if a.falsify_threshold:
            bits.append(f"     falsified if: {a.falsify_threshold}")
        bits.append(f"     load-bearing: {'yes' if a.is_load_bearing else 'no'}"
                    f" | last status: {a.status}")
        lines.append("\n".join(bits))
    return "\n".join(lines)


def _match_checks(assumptions: list[ThesisAssumption],
                  reported: list) -> tuple[list[AssumptionCheck], list[str]]:
    """Pair the model's reported statuses to the stored claims.

    Matching is on exact claim text, which is why the prompt asks for
    it verbatim. A claim the model did not report becomes `unchecked`
    rather than being dropped: silently omitting a claim would shrink
    the denominator and make a thesis look better verified than it was.

    Returns (checks, unmatched) — `unmatched` is anything the model
    reported that does not correspond to a stored claim, which is
    reported rather than ignored because it usually means the wording
    drifted and the next day's match will fail the same way.
    """
    by_claim = {r.claim: r for r in reported}
    checks, used = [], set()
    for a in assumptions:
        r = by_claim.get(a.claim)
        if r is not None:
            used.add(a.claim)
            checks.append(AssumptionCheck(
                claim=a.claim, status=r.status,
                is_load_bearing=a.is_load_bearing,
                evidence=r.evidence, assumption_id=a.id))
        else:
            checks.append(AssumptionCheck(
                claim=a.claim, status="unchecked",
                is_load_bearing=a.is_load_bearing,
                evidence="", assumption_id=a.id))
    unmatched = [c for c in by_claim if c not in used]
    return checks, unmatched


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
    # D7 — start the data-coverage count clean. Atlas runs before Vera
    # in the cycle and shares this client, so without a reset his four
    # index calls would be pooled into her ticker coverage and dilute
    # the number Solomon gates on.
    fmp_client.coverage.reset()

    # Populated by the monitoring pass; empty on a day with nothing held.
    authored_assumptions: dict = {}
    verdicts: dict = {}
    assumptions_by_thesis: dict = {}

    with session_scope() as session:
        open_theses = get_open_theses(session)
        held_tickers = [t.ticker for t in open_theses]

    # -------------------------------------------------------------
    # JOB 1: MONITORING — [4] first agentic loop
    # -------------------------------------------------------------
    if open_theses:
        # ---------------------------------------------------------
        # Any open thesis with no claims on record gets them now.
        # One-time per thesis, and the mechanism that retro-fits the
        # theses adopted as orphans before migration 011 — until they
        # have claims they report no verdict at all.
        # ---------------------------------------------------------
        for t in open_theses:
            with session_scope() as session:
                has_claims = bool(open_assumptions(session, t.id))
            if not has_claims:
                try:
                    authored_assumptions[t.ticker] = author_assumptions(today, t)
                except Exception as exc:  # noqa: BLE001
                    # A thesis without claims still gets monitored the
                    # old way and simply reports no verdict. Failing
                    # the whole monitoring pass because one
                    # decomposition call failed would be a much worse
                    # trade than losing one thesis's structure today.
                    logger.warning("Could not author assumptions for %s: %s. "
                                   "It will be monitored without a verdict.",
                                   t.ticker, exc)

        with session_scope() as session:
            policy = get_active_risk_policy_for_review(session, today)
            positions_by_ticker = {p.ticker: p for p in session.query(Position).all()}
            trailing_history = _get_trailing_pnl_history(session, today, [t.ticker for t in open_theses])
            # Read here and used after the scope closes, which is safe
            # because SessionLocal is built with expire_on_commit=False
            # — the column values stay loaded on the instances. Anyone
            # changing that setting in core/db.py breaks this.
            assumptions_by_thesis = {t.id: open_assumptions(session, t.id) for t in open_theses}

        positions_summary = "\n\n".join(
            _format_position_block(t, positions_by_ticker.get(t.ticker), trailing_history.get(t.ticker, []), policy)
            + "\n  Assumptions this thesis rests on:\n"
            + _format_assumptions(assumptions_by_thesis.get(t.id, []))
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
    screened_results = [
        NewCandidateOutput.model_validate(item)
        for item in extract_json(screening_raw)
    ]

    # The surfacing decision, in code. `candidate_results` keeps its
    # old meaning — the names that become new_candidates rows and that
    # Solomon may act on — so nothing downstream changes. What is new
    # is that the names which did NOT clear the bar still exist here
    # long enough to be counted.
    candidate_results, held_back = partition_screened(screened_results)
    screening = screening_summary(
        universe=screening_universe,
        screened=screened_results,
        surfaced=candidate_results,
        held_tickers=held_tickers,
    )
    # The rendered sentence is stored ALONGSIDE the numbers it comes
    # from, deliberately. It costs a little denormalisation and buys
    # two things: no consumer needs to import a formatter from an agent
    # module just to display a line, and the ledger keeps what was
    # actually said on the day even if the wording is changed later.
    screening["statement"] = describe_screening(screening)
    logger.info("Vera screening — %s", screening["statement"])
    if held_back:
        logger.info(
            "Held back below the bar: %s",
            ", ".join(f"{h.ticker} ({h.conviction_score})" for h in held_back),
        )

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
            thesis_id = thesis_by_ticker[m.ticker]
            stored = assumptions_by_thesis.get(thesis_id, [])

            # THE VERDICT, COMPUTED. Vera reported a status per claim;
            # this derives what that means for the thesis. She is never
            # asked for the verdict itself, so it cannot contradict its
            # own evidence.
            checks, unmatched = _match_checks(stored, m.assumption_checks)
            if unmatched:
                # Usually the wording drifted, and tomorrow's match
                # will fail the same way — so it is reported rather
                # than swallowed.
                logger.warning(
                    "%s: %s reported check(s) matched no stored claim: %s. "
                    "The claim text must be echoed verbatim for matching.",
                    m.ticker, len(unmatched), "; ".join(unmatched))
            v = compute_verdict(checks)
            verdicts[m.ticker] = {
                "verdict": v.verdict, "reason": v.reason,
                "checked": v.checked, "total": v.total,
                "broken": v.broken_claims, "strained": v.strained_claims,
                "improved": v.improved_claims,
                "unmatched": unmatched,
            }
            logger.info("%s — %s", m.ticker, v.describe())

            checks_json = [
                {"claim": c.claim, "status": c.status,
                 "is_load_bearing": c.is_load_bearing, "evidence": c.evidence}
                for c in checks
            ] or None

            stmt = pg_insert(PositionMonitoringLog).values(
                log_date=today,
                thesis_id=thesis_id,
                ticker=m.ticker,
                status=m.status,
                trigger=m.trigger,
                reasoning=m.reasoning,
                conviction_score=m.conviction_score,
                verdict=v.verdict,
                assumption_checks=checks_json,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["log_date", "thesis_id"],
                set_={
                    "status": stmt.excluded.status,
                    "trigger": stmt.excluded.trigger,
                    "reasoning": stmt.excluded.reasoning,
                    "conviction_score": stmt.excluded.conviction_score,
                    "verdict": stmt.excluded.verdict,
                    "assumption_checks": stmt.excluded.assumption_checks,
                },
            )
            session.execute(stmt)

            # Carry each claim's status forward. The daily observation
            # lives on the log; this is the CURRENT state, which is
            # what tomorrow's prompt shows as "last status".
            for c in checks:
                if c.assumption_id is None or c.status == "unchecked":
                    # An unchecked claim keeps the status it had. Writing
                    # `unchecked` here would erase the last real
                    # observation and make every quiet day look like a
                    # gap in the record.
                    continue
                (
                    session.query(ThesisAssumption)
                    .filter(ThesisAssumption.id == c.assumption_id)
                    .update({"status": c.status, "evidence": c.evidence,
                             "last_checked": today},
                            synchronize_session=False)
                )

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

    # D7 — how much of what she asked for she actually got. Carried in
    # her output rather than logged, because the agent that has to act
    # on it is Solomon, and a number he cannot see is a number he
    # cannot weigh.
    data_coverage = fmp_client.coverage.summary()
    if data_coverage["requests_degraded"]:
        logger.warning(
            "Vera ran degraded: %s of %s tickers complete (%s%%). Degraded: %s",
            data_coverage["tickers_complete"], data_coverage["tickers_attempted"],
            data_coverage["coverage_pct"], data_coverage["degraded_tickers"],
        )

    return {
        "monitoring": [m.model_dump() for m in monitoring_results],
        "candidates": [c.model_dump() for c in candidate_results],
        "orphan_reviews": orphan_reviews,
        "data_coverage": data_coverage,
        # What the screening pass did, including on the days it did
        # nothing. Rides in the return dict rather than a new table:
        # the orchestrator already persists this to
        # agent_runs.raw_output, which is JSONB and therefore
        # queryable later for the best_conviction trend. No migration.
        "screening": screening,
        # What today did to each open thesis, computed from the
        # per-claim checks rather than asked of the model.
        "verdicts": verdicts,
        # Claims written for a thesis that had none — one-time per
        # thesis, so this is empty on almost every day.
        "authored_assumptions": authored_assumptions,
        # Her read on the names that did not clear the bar. Kept in the
        # run ledger only, never written to new_candidates — a row
        # there is a name the firm is putting forward, and these are
        # not. Scores are trimmed of the full workup they never had.
        "held_back": [
            {"ticker": h.ticker, "conviction_score": h.conviction_score,
             "thesis": h.thesis}
            for h in held_back
        ],
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