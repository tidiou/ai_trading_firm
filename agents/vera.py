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

from dataclasses import dataclass
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
from core.theme import THEME_LINKS, THEME_UNIVERSE
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
        "description": "Current price and bid-ask spread for a single stock, from the same feed the desk executes against.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_company_snapshot",
        "description": (
            "Sector and market cap, plus the filed annual series for a single "
            "stock: cash conversion, capex intensity, reinvestment rate, gross "
            "and operating margin, and diluted share count, each with its "
            "multi-year trend. Figures come from the company's own SEC filings."
        ),
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

# One FMP /profile response per ticker per run.
#
# WHY IT IS NEEDED NOW AND WAS NOT BEFORE. The model decides which
# tickers to investigate and may call a tool on the same name more
# than once inside a link's agentic loop. At six names that was
# irrelevant; across seven links and 27 names the ceiling matters,
# because FMP's free tier allows 250 requests a day and Atlas spends
# some of them first. Uncached, a talkative pass can exceed the quota,
# and an exhausted quota raises out of _get — which would fail every
# remaining link.
#
# A profile is static data (sector, market cap, description), so
# caching it within a run costs nothing in freshness. Quotes are NOT
# cached: a price is the one thing here that is supposed to be live.
# Cleared at the top of run(), so the cache never outlives a session.
_profile_cache: dict[str, dict] = {}


# =================================================================
# WHERE THE DATA COMES FROM, AND WHY IT MOVED
#
# Both tools used to be FMP end to end. That was survivable while the
# universe was six mega-caps and became the binding constraint the
# moment it was not: on the free tier, `/quote` and `/key-metrics` are
# restricted to a fixed sample list and return HTTP 402 for anything
# outside it. The restriction is NOT size-correlated — it is a list,
# and the six names in the old universe happened to be on it.
#
# scripts/probe_fmp_coverage.py established the split by asking each
# field of the source that can actually serve it:
#
#   sector, market cap   FMP /profile     serves any ticker, unpaywalled
#   price, spread        Alpaca IEX       every US listing; the same
#                                         feed Ada sets limit prices
#                                         from, so a name priced here
#                                         is a name the desk can trade
#   annual fundamentals  SEC EDGAR        every filer, free, and the
#                                         company's own filed figures
#                                         rather than a vendor's
#                                         derived multiples
#
# WHAT WAS GIVEN UP. FMP's key-metrics carried P/E and price-to-sales.
# Those are gone: they are a vendor's arithmetic over a price this
# module can compute for itself, and keeping them would have kept the
# whole universe hostage to a sample list. What replaces them is
# better suited to the thesis work anyway — a multi-year margin and
# cash-conversion trajectory is what a falsifiable assumption gets
# written against, and a trailing P/E is not.
#
# COVERAGE IS RECORDED FOR ALL THREE. The tracker lives in fmp_client
# and now counts outcomes that have nothing to do with FMP, which is a
# misleading home for it — noted as a rename worth doing. What matters
# is that Solomon's data_coverage gate keeps meaning "how much of what
# she asked for did she get", and that is only true if the two new
# sources report into the same count.
# =================================================================
def _quote(ticker: str) -> dict:
    """Price and spread from Alpaca, recorded as coverage either way."""
    from core.clients import alpaca_client

    try:
        quote = alpaca_client.get_latest_quote(ticker)
    except Exception as exc:  # noqa: BLE001
        fmp_client.coverage.record("alpaca-quote", ticker, ok=False)
        return {"error": f"no quote available: {type(exc).__name__}: {exc}"}
    fmp_client.coverage.record("alpaca-quote", ticker, ok=quote.get("mid") is not None)
    return quote


def _fundamentals(ticker: str) -> dict:
    """The filed annual series, from what the sync has stored.

    Returns the rendered description rather than the raw ratio tables.
    The description names the fiscal years it covers and says
    "inputs not reported" where a ratio could not be computed, so the
    model cannot mistake an absent figure for a zero — which is the
    specific way a valuation screen goes quietly wrong.
    """
    from core.fundamentals import fundamentals_for

    try:
        derived = fundamentals_for(ticker)
    except Exception as exc:  # noqa: BLE001
        fmp_client.coverage.record("edgar-fundamentals", ticker, ok=False)
        return {"error": f"{type(exc).__name__}: {exc}"}

    # ok WHENEVER THE LOOKUP ANSWERED, INCLUDING WHEN THE ANSWER IS
    # "NOTHING ON RECORD". Recording an absence as a failure was tried
    # and is wrong twice over:
    #
    #   It would refuse every trade for the first days after this
    #   shipped. Solomon's coverage_gate stands down risk-increasing
    #   proposals below MIN_DATA_COVERAGE_PCT (60). The backfill fills
    #   six names a session by design, so a cold universe would report
    #   22% on day one and 44% on day two — Vera screening 27 names,
    #   surfacing candidates, and Solomon rejecting all of them for a
    #   data problem that is really a rate limit the desk chose.
    #
    #   And it would never clear for a foreign private issuer. A 20-F
    #   filer reports nothing under the us-gaap concepts this desk
    #   tracks, permanently. That is a fact about the filer, not a
    #   failed request, and parking it in degraded_tickers forever
    #   would train the operator to ignore that field.
    #
    # Coverage answers "did the request I made come back". Whether the
    # answer was rich enough to build a thesis on is a different
    # question, and it is reported in `fundamentals_backfill` and
    # visible to the model in this very tool result.
    fmp_client.coverage.record("edgar-fundamentals", ticker, ok=True)
    if not derived.measurable:
        # An honest absence, and a common one: a foreign private issuer
        # files 20-F under IFRS and reports nothing under the us-gaap
        # concepts this desk tracks. Saying so beats an empty dict.
        return {"available": False, "reason": derived.unavailable,
                "summary": derived.describe()}
    return {
        "available": True,
        "summary": derived.describe(),
        "fiscal_years": derived.years,
        "trends_pp": {name: derived.trend(name) for name in
                      ("cash_conversion", "capex_intensity", "reinvestment_rate",
                       "gross_margin", "operating_margin")},
        "diluted_share_change_pct": derived.share_count_change_pct,
    }


def execute_tool(tool_name: str, tool_input: dict):
    if tool_name == "get_stock_quote":
        return _quote(tool_input["ticker"])
    if tool_name == "get_company_snapshot":
        ticker = tool_input["ticker"]
        # FMP profile survives, for the one thing it is still the best
        # source of — and the one thing a risk limit depends on.
        if ticker in _profile_cache:
            profile = _profile_cache[ticker]
        else:
            profile = fmp_client.get_company_profile(ticker)
            # A paywalled or empty response is cached too, deliberately:
            # retrying it inside the same run spends quota to be told
            # the same thing, and quota is the constraint this cache
            # exists for.
            _profile_cache[ticker] = profile
        if isinstance(profile, dict):
            sector = profile.get("sector")
            if isinstance(sector, str) and sector.strip():
                _sector_cache[ticker] = sector.strip()
        return {"profile": profile, "fundamentals": _fundamentals(ticker)}
    raise ValueError(f"Vera has no tool named '{tool_name}'")


# =================================================================
# THE UNIVERSE — deterministic, not LLM-chosen (Operating Manual
# §7.2: the universe is pre-filtered by CODE before any LLM reasoning
# happens).
#
# It used to be six US mega-caps: AAPL, MSFT, GOOGL, AMZN, NVDA, JPM.
# That list was not a strategy. It was chosen to survive FMP's
# free-tier sample restriction, and it quietly made a data vendor's
# sample list the desk's research mandate. On 2026-09-23 it had
# shrunk to a three-name screen once holdings were excluded, which is
# not a screen.
#
# It is now the AI data-centre value chain from core/theme.py — 27
# names across 7 causally chained links. The theme module owns which
# names those are and why; this module owns what to do with them.
#
# SCREENED PER LINK, BOTTLENECK-FIRST. Three reasons, in order of
# how much they matter:
#
#   1. THE QUESTION IS DIFFERENT PER LINK. What makes a power name
#      interesting — a contracted interconnection backlog converting
#      on schedule — has nothing to do with what makes a compute name
#      interesting. One undifferentiated pass over 27 tickers asks
#      one question of seven different businesses. Each link's own
#      `depends_on` and `note` go into its prompt, so the screen asks
#      the question that link actually turns on.
#
#   2. ONE FAILED LINK COSTS ONE LINK. A single pass over 27 names is
#      one response that either arrives or does not.
#
#   3. TOKENS. 27 full entries do not fit one 4000-token response,
#      and a truncated JSON array is a failed pass.
#
# THE ORDER IS THE STRATEGY, written down. Power first because power
# is the binding constraint today; hyperscalers last because they are
# the payers, and their capex is everyone else's revenue. Reading the
# chain from the constraint toward the payer is the house view, and a
# later pass sees what the earlier ones surfaced.
# =================================================================
SCREENING_ORDER: list[str] = [
    "power",
    "cooling_infra",
    "foundry_equipment",
    "memory",
    "networking",
    "compute",
    "hyperscaler",
]

FIXED_UNIVERSE = THEME_UNIVERSE


def screening_order() -> list[str]:
    """SCREENING_ORDER, plus any link theme.py has that it doesn't.

    A LINK ADDED TO theme.py AND NOT TO THE ORDER ABOVE IS STILL
    SCREENED — at the end, with a warning. The alternative considered
    was an assert at import, and it was rejected: a link missing from
    this list is a coverage gap in one link, while a failed import of
    agents/vera.py is the whole desk down for the day. Taking the
    worse outcome to punish the smaller mistake is not a trade worth
    making, and the warning still makes the omission visible.

    A link in the order that theme.py no longer has is dropped
    silently — there is nothing to screen and nothing to warn about.
    """
    known = [link for link in SCREENING_ORDER if link in THEME_LINKS]
    extra = [link for link in THEME_LINKS if link not in SCREENING_ORDER]
    if extra:
        logger.warning(
            "core.theme defines link(s) not in SCREENING_ORDER: %s. They will "
            "be screened LAST rather than in their place in the chain — add "
            "them to SCREENING_ORDER in agents/vera.py.", ", ".join(extra))
    return known + extra

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

THE FIRM'S MANDATE. This desk researches one thing: the AI
data-centre value chain, read from its bottleneck outward. The
question is not "is this a good company" but "what does this link of
the chain depend on, and is that dependency holding". You are given
ONE LINK at a time, with what that link turns on. Screen against
THAT, not against a generic quality checklist.

A STRUCTURAL THESIS IS NOT A CATALYST-DRIVEN ONE. Much of this chain
runs on contracted, multi-year commitments — turbine slots booked
into the 2030s, interconnection queues measured in years, backlog
that converts on a schedule. Backlog converting on schedule IS a
named catalyst when you can point to the conversion; "the AI theme
is strong" is not. Do not inflate a score because the theme is
exciting, and do not deflate one to a 3 because the catalyst is a
signed commitment rather than an event on a calendar.

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
def link_batches(excluded: list[str] | set[str] | None = None,
                 order: list[str] | None = None) -> list[tuple[str, list[str]]]:
    """The screening plan: (link, tickers) in bottleneck-first order,
    with held and already-thesised names removed and empty links
    dropped.

    Pure, so the plan can be asserted without a model or a database —
    which is the point of having it as a function rather than a loop
    body. The order it returns is the house view written down; a test
    that pins it is a test that pins the strategy.
    """
    excluded = set(excluded or ())
    # A name listed under two links is screened under the FIRST, which
    # is the same rule THEME_OF applies when it assigns a name to a
    # link for risk aggregation. theme.py's own comment flags AVGO as
    # arguably both compute and networking, so this is one edit away
    # from mattering: without it the name would be screened twice and
    # universe_size would over-count against a THEME_UNIVERSE that
    # de-duplicates.
    seen: set[str] = set()
    batches = []
    for link in (order or screening_order()):
        tickers = [t for t in THEME_LINKS[link]["tickers"]
                   if t not in excluded and t not in seen]
        seen.update(tickers)
        if tickers:
            batches.append((link, tickers))
    return batches


def describe_link(link: str) -> str:
    """The question this link turns on, for its screening prompt. Comes
    from core/theme.py rather than being restated here — one definition
    of the chain, used by the screen, by Atlas's read-across and by
    risk aggregation."""
    spec = THEME_LINKS[link]
    return (f"LINK: {spec['label']}\n"
            f"This link depends on: {spec['depends_on']}.\n"
            f"What to keep in mind: {spec['note']}")


def screen_link(link: str, tickers: list[str], today: date,
                atlas_output: dict) -> list[NewCandidateOutput]:
    """One link's screening pass. Raises on failure — containment is
    the caller's job, because the caller is the one that knows a lost
    link is survivable and a lost day is not."""
    prompt = (
        f"Today's date: {today}.\n"
        f"Macro backdrop from Atlas: regime={atlas_output.get('regime_signal')}, "
        f"confidence={atlas_output.get('confidence')}, "
        f"narrative: {atlas_output.get('narrative')}\n\n"
        f"{describe_link(link)}\n\n"
        f"Tickers to screen (names already held or already carrying an open "
        f"thesis have been removed): {', '.join(tickers)}"
    )
    raw = run_agent_loop(
        system_prompt=SCREENING_SYSTEM_PROMPT,
        user_prompt=prompt,
        tools=VERA_TOOLS,
        tool_executor=execute_tool,
        # Sized for a link, not a universe: the largest links hold five
        # names, and a 4-5 entry carries a full thesis, risks and
        # valuation note. The old single pass had the same budget for
        # up to 27.
        max_tokens=4000,
    )
    return [NewCandidateOutput.model_validate(item) for item in extract_json(raw)]


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
    if summary["universe_size"] == 0:
        # Distinct from the line below on purpose. "Nothing came back"
        # is a failed pass; "there was nothing left to ask about" is a
        # fully-invested book, and reporting the second as the first
        # would send someone looking for an outage that isn't one.
        return ("Nothing left to screen — every name in the universe is "
                "either held or already carries an open thesis.")
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


def get_position_tickers(session) -> list[str]:
    """What the book actually holds, per Otis's positions table.

    Otis rebuilds that table from Alpaca at every close, so it is the
    one record here with an external authority behind it. A thesis is
    something this desk wrote down; a position is something the broker
    confirms. Where the two disagree, the broker is right about what is
    owned and the thesis is right about nothing at all.
    """
    return sorted({p.ticker for p in session.query(Position).all()})


# =================================================================
# BOOK RECONCILIATION — positions against open theses.
#
# THE DEFECT THIS EXISTS TO PREVENT. Until today the monitoring pass
# took its list of "held" names from the OPEN THESES table, and the
# screening pass excluded that same list from the universe. Nothing
# ever compared it to what the broker said was owned. On 2026-09-23
# the book held AAPL and NVDA while Vera monitored AAPL and GOOGL:
#
#   GOOGL  a thesis Marcus opened at ALLOCATION on 17 Sept. Ada then
#          refused the order on a stale ledger, so the order never
#          filled — and the thesis outlived it. Vera checked its
#          assumptions daily against a position that did not exist,
#          and Clara published "AAPL and GOOGL holdings are intact".
#   NVDA   84% of the invested book, monitored by nobody, because the
#          orphan-adoption pass that documents it ran AFTER the
#          monitoring pass had already chosen its list.
#
# WHY IT SURVIVED SIX DAYS. Both counts were 2. Nora reported "2
# positions" from the positions table and Vera reported "2 positions
# monitored" from the theses table, the numbers agreed, and nothing
# printed the names side by side. A mismatch that is only visible in
# the names is invisible in a count, so this names them.
#
# THE TWO DIFFERENCES ARE NAMED SEPARATELY AND SYMMETRICALLY,
# because they are opposite failures with opposite remedies:
#
#   positions − theses = ORPHAN     held, undocumented. Already
#                                   handled: review_orphan_positions
#                                   adopts or recommends exit.
#   theses − positions = PHANTOM    documented, not held. NOT handled,
#                                   deliberately — see run().
# =================================================================
@dataclass
class BookReconciliation:
    positions: list[str]     # what the broker says is owned
    theses: list[str]        # what has an open thesis on record
    monitored: list[str]     # both — a position whose claims get checked
    orphans: list[str]       # held with no thesis
    phantoms: list[str]      # a thesis with no position

    @property
    def is_reconciled(self) -> bool:
        return not self.orphans and not self.phantoms

    def describe(self) -> str:
        """One line, always naming tickers rather than counting them.
        This is the string the 17–23 Sept inversion would have failed
        loudly against on day one."""
        if self.is_reconciled:
            names = ", ".join(self.monitored) or "none"
            return (f"Book reconciled — {len(self.monitored)} position(s) "
                    f"monitored: {names}.")
        parts = [f"{len(self.monitored)} monitored "
                 f"({', '.join(self.monitored) or 'none'})"]
        if self.orphans:
            parts.append("orphan position(s) with no thesis: "
                         + ", ".join(self.orphans))
        if self.phantoms:
            parts.append("phantom thesis(es) with no position: "
                         + ", ".join(self.phantoms))
        return "Book NOT reconciled — " + "; ".join(parts) + "."


def reconcile_book(position_tickers, thesis_tickers) -> BookReconciliation:
    """Pure set arithmetic over two ticker lists. Takes tickers rather
    than a session so the invariant can be tested without a database —
    which is the whole reason it is a function and not four lines
    inlined in run()."""
    held = {t for t in position_tickers}
    documented = {t for t in thesis_tickers}
    return BookReconciliation(
        positions=sorted(held),
        theses=sorted(documented),
        monitored=sorted(held & documented),
        orphans=sorted(held - documented),
        phantoms=sorted(documented - held),
    )


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
    unexamined by the research/approval pipeline.

    Now one half of reconcile_book(), so the two directions of
    disagreement cannot drift apart. This function was already reading
    the right table; the monitoring pass simply never used it."""
    return reconcile_book(
        get_position_tickers(session),
        [t.ticker for t in get_open_theses(session)],
    ).orphans


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
    _profile_cache.clear()

    # -------------------------------------------------------------
    # The valuation half of get_company_snapshot reads filed figures
    # out of company_facts, so a name with nothing stored gets screened
    # on price and sector alone. This fills the gaps — bounded, so a
    # cold universe costs several sessions rather than one long one,
    # and contained, because a screen on partial data beats no screen.
    #
    # NOT the refresh mechanism. New filings are picked up by
    # `python -m core.fundamentals sync-universe`, run periodically.
    # This only answers "is there anything here at all".
    # -------------------------------------------------------------
    fundamentals_backfill: dict = {}
    try:
        from core.fundamentals import backfill_missing

        fundamentals_backfill = backfill_missing(FIXED_UNIVERSE)
        if fundamentals_backfill.get("missing_total"):
            # The empty names are called out separately because they
            # are NOT a backlog. A filer reporting nothing under the
            # us-gaap concepts this desk tracks will report nothing
            # tomorrow either, so it stays in `missing` forever — and
            # a daily warning telling the operator to run a command
            # that cannot fix it is a warning that trains them to stop
            # reading warnings.
            empty = fundamentals_backfill.get("empty") or []
            logger.warning(
                "Fundamentals missing for %s theme name(s); synced %s this run, "
                "%s deferred to a later session.%s Run `python -m "
                "core.fundamentals sync-universe` to fill the rest at once.",
                fundamentals_backfill["missing_total"],
                len(fundamentals_backfill["synced"]),
                len(fundamentals_backfill["deferred"]),
                (f" {', '.join(empty)} reported nothing under us-gaap and will "
                 f"not fill — expected for a foreign private issuer filing "
                 f"20-F under IFRS." if empty else ""))
    except Exception as exc:  # noqa: BLE001
        fundamentals_backfill = {"error": f"{type(exc).__name__}: {exc}"}
        logger.warning("Fundamentals backfill failed (%s). Screening continues "
                       "on whatever is already stored.", exc)

    # Populated by the monitoring pass; empty on a day with nothing held.
    authored_assumptions: dict = {}
    verdicts: dict = {}
    assumptions_by_thesis: dict = {}

    # -------------------------------------------------------------
    # JOB 3 RUNS FIRST — it used to run last, and that ordering cost
    # NVDA a day of monitoring. Adoption writes a Thesis row; reading
    # the theses afterwards means a name adopted this morning is
    # monitored this morning instead of tomorrow.
    #
    # Wrapped, because the move changed what a failure here costs.
    # Last it was the final step and a failure lost only itself; first,
    # an unguarded failure would take the whole monitoring and
    # screening pass with it. This is the same trade author_assumptions
    # makes a few lines below and for the same reason: the orphan pass
    # is documentation, and no documentation gap is worth a day of
    # monitoring. The error is returned rather than only logged, so a
    # silent skip cannot look like "no orphans".
    # -------------------------------------------------------------
    orphan_reviews: list[dict] = []
    orphan_review_error: Optional[str] = None
    try:
        orphan_reviews = review_orphan_positions(today)
    except Exception as exc:  # noqa: BLE001
        orphan_review_error = f"{type(exc).__name__}: {exc}"
        logger.warning("Orphan review failed (%s). Any undocumented "
                       "position stays undocumented today and will be "
                       "reported as an orphan below.", orphan_review_error)

    with session_scope() as session:
        open_theses = get_open_theses(session)
        book = reconcile_book(get_position_tickers(session),
                              [t.ticker for t in open_theses])

    # THE MONITORED SET IS positions ∩ open theses. A position with a
    # thesis is the only thing whose assumptions can be checked: a
    # phantom has no price action to check them against, and an orphan
    # has no claims to check.
    monitored = set(book.monitored)
    monitored_theses = [t for t in open_theses if t.ticker in monitored]

    # THE INVARIANT NOBODY WAS CHECKING. After the orphan pass above,
    # a clean book reconciles exactly; anything left is a real
    # disagreement and is logged at WARNING with names, not counts.
    # An orphan surviving this line means Vera reviewed it and said
    # exit, or the review failed.
    if book.is_reconciled:
        logger.info("Vera — %s", book.describe())
    else:
        logger.warning("Vera — %s", book.describe())

    # The broker is the authority on what is held, so this is what
    # "held" means everywhere below it.
    held_tickers = book.positions
    # Screening skips both: a name already owned, and a name that
    # already has an open thesis. The second is not a claim that a
    # phantom is held — it is refusing to propose a second thesis for
    # a name that already has one open. The phantom itself is
    # surfaced, loudly, above and in the return dict.
    excluded_from_screening = sorted(set(book.positions) | set(book.theses))

    # -------------------------------------------------------------
    # JOB 1: MONITORING — [4] first agentic loop
    # -------------------------------------------------------------
    if monitored_theses:
        # ---------------------------------------------------------
        # Any open thesis with no claims on record gets them now.
        # One-time per thesis, and the mechanism that retro-fits the
        # theses adopted as orphans before migration 011 — until they
        # have claims they report no verdict at all.
        # ---------------------------------------------------------
        for t in monitored_theses:
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
            trailing_history = _get_trailing_pnl_history(session, today, [t.ticker for t in monitored_theses])
            # Read here and used after the scope closes, which is safe
            # because SessionLocal is built with expire_on_commit=False
            # — the column values stay loaded on the instances. Anyone
            # changing that setting in core/db.py breaks this.
            assumptions_by_thesis = {t.id: open_assumptions(session, t.id) for t in monitored_theses}

        positions_summary = "\n\n".join(
            _format_position_block(t, positions_by_ticker.get(t.ticker), trailing_history.get(t.ticker, []), policy)
            + "\n  Assumptions this thesis rests on:\n"
            + _format_assumptions(assumptions_by_thesis.get(t.id, []))
            for t in monitored_theses
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
    batches = link_batches(excluded_from_screening)
    screening_universe = [t for _, tickers in batches for t in tickers]

    screened_results: list[NewCandidateOutput] = []
    link_failures: dict[str, str] = {}
    for link, tickers in batches:
        try:
            screened_results.extend(screen_link(link, tickers, today, atlas_output))
        except Exception as exc:  # noqa: BLE001
            # CONTAINED ON PURPOSE. Seven passes instead of one is only
            # an improvement if a failed pass costs a link rather than
            # the day. The names in a failed link then show up in the
            # summary's `not_returned` — the field that already exists
            # for exactly this, "asked for a read and did not get one"
            # — so nothing goes missing silently.
            link_failures[link] = f"{type(exc).__name__}: {exc}"
            logger.warning("Screening failed for link %s (%s): %s. "
                           "The other links continue.",
                           link, ", ".join(tickers), link_failures[link])

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
    # Which links were actually asked, and which fell over. A link that
    # failed leaves its names in `not_returned`, but only this says
    # WHY they are not there — a distinction that matters when
    # `best_conviction` is read as a trend over weeks.
    screening["links_screened"] = [link for link, _ in batches]
    screening["link_failures"] = link_failures
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
        thesis_by_ticker = {t.ticker: t.id for t in monitored_theses}
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

        # A same-day rerun should REPLACE today's screening results
        # rather than accumulate tickers across runs (the earlier
        # per-row upsert let a stale candidate linger even if a later
        # rerun didn't surface it again).
        #
        # SCOPED TO THE NAMES THIS RUN ACTUALLY GOT A READ ON, which
        # this file learned the hard way. It used to delete every row
        # for today unconditionally, and that was safe only because a
        # screening failure RAISED out of run() before reaching here.
        # Per-link screening contains those failures, so a pass in
        # which every link failed would now arrive with an empty
        # candidate list and quietly delete a successful earlier run's
        # work. Deleting only what was re-screened makes the blast
        # radius the same as the read: a failed link's rows survive,
        # an untouched name's row survives, and a name screened today
        # that fell below the bar loses its row exactly as intended.
        screened_today = {c.ticker for c in screened_results}
        if screened_today:
            (
                session.query(NewCandidate)
                .filter(NewCandidate.candidate_date == today,
                        NewCandidate.ticker.in_(screened_today))
                .delete(synchronize_session=False)
            )
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

    # (The orphan review used to run here. It now runs at the top of
    # run(), so that a name adopted this morning is monitored this
    # morning.)

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
        # Non-None means the orphan pass threw and was contained. An
        # empty orphan_reviews list with this set is NOT "no orphans".
        "orphan_review_error": orphan_review_error,
        # What the broker holds, what has a thesis, and the two ways
        # they disagree — named, not counted. `phantoms` is the field
        # that would have caught the GOOGL inversion on 17 Sept.
        #
        # A PHANTOM IS NOT AUTO-CLOSED, deliberately. Closing it here
        # would resolve GOOGL silently and discard real research on a
        # guess about why the position is missing — the order never
        # filled, it was sold outside the desk, or Otis's sync is
        # behind. Those want three different answers. This module's
        # instinct everywhere else is to refuse and surface rather
        # than guess, and a phantom thesis is a decision for a human,
        # not a cleanup for the desk.
        "book": {
            "positions": book.positions,
            "theses": book.theses,
            "monitored": book.monitored,
            "orphans": book.orphans,
            "phantoms": book.phantoms,
            "reconciled": book.is_reconciled,
            "statement": book.describe(),
        },
        # What the bounded self-heal did, if anything. Empty dict on
        # the ordinary day where every theme name already has facts.
        "fundamentals_backfill": fundamentals_backfill,
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