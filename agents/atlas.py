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

=====================================================================
THE READ-ACROSS CHANGE, AND WHY THE OLD PROHIBITION IS MOVED RATHER
THAN RELAXED
=====================================================================

Atlas previously could not mention a stock or a sector at all. The
reason was sound: a macro agent that opines on securities becomes a
second stock-picker, and Vera — who receives his brief as context —
stops being an independent opinion. Her screen is only worth having
because nobody handed her the answer first.

But there is a category the old rule swept up wrongly. "TSMC's
monthly revenue is what the silicon complex marks against" is a claim
about where information ENTERS the chain, not a view on whether TSM
is worth owning. In a theme book the links are causally chained, and
information does not arrive at all 27 names at once — it arrives at
one and propagates. A desk that cannot name that cannot attribute its
own P&L.

So the rule is now structural rather than tonal, and it is enforced
by the CONTRACT below, not by asking the prompt nicely:

  - Tickers may appear in ONE place: the structured `read_across`
    field, where every entry is validated. The prose fields
    (`narrative`, `key_events_today`, `notable_overnight_moves`)
    remain ticker-free exactly as before.
  - A read-across entry may report an EVENT or an OBSERVED MOVE.
    Forward-looking or recommendation language in it is a validation
    failure — see core.theme.directional_language.
  - The causal edges are NOT the model's to choose. They come from
    core.theme.BELLWETHERS, and an entry naming a ticker or a link
    outside that map is rejected. Atlas reads the map; he cannot
    extend it.

WHAT THIS BUYS. Not a timing edge: a read-across is priced downstream
within minutes, long before the next premarket meeting. It buys
correct ATTRIBUTION. If a memory position falls 9% the morning after
the compute bellwether guides down, that is the link repricing rather
than the thesis breaking — and without it Vera's monitoring pass can
write a false `strained` verdict on a move that had nothing to do
with the claims she was checking.
"""

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.clients import fmp_client
from core.clients.claude_client import run_agent_loop, extract_json
from core.db import session_scope
from core.models import MacroBrief
from core.theme import (
    BELLWETHER_TICKERS,
    describe_bellwethers,
    directional_language,
    links_read_across,
    tickers_mentioned,
)


# =================================================================
# [5] OUTPUT CONTRACT — the schema IS the guardrail. If Claude's
# answer doesn't fit this shape (wrong regime_signal value, missing
# field, malformed JSON), Pydantic raises here, loudly, before
# anything gets written to the database. No silent bad data.
# =================================================================
class ReadAcross(BaseModel):
    """One bellwether, one event, and the links that reprice off it.

    Structured rather than prose on purpose. A sentence in a narrative
    is unfalsifiable and unauditable; these fields can be checked by
    Clara, counted, and joined to a position's own move when a verdict
    is computed.
    """

    ticker: str
    # What happened or is scheduled — factual only. "Q3 earnings",
    # "monthly revenue released", "backlog fell 8%".
    event: str
    links_affected: list[str]
    # None means NOT OBSERVED, and must not be 0.0. "We did not look"
    # and "it did not move" are different claims, and a zero here
    # would read as the second while meaning the first.
    observed_move_pct: Optional[float] = None

    @field_validator("ticker")
    @classmethod
    def _must_be_a_known_bellwether(cls, v: str) -> str:
        t = (v or "").strip().upper()
        if t not in BELLWETHER_TICKERS:
            raise ValueError(
                f"'{t}' is not in the bellwether map. Atlas may only name a "
                f"ticker that core.theme.BELLWETHERS already lists — the "
                f"causal edges are code, not a daily judgement call. "
                f"Permitted: {', '.join(BELLWETHER_TICKERS)}")
        return t

    @field_validator("event")
    @classmethod
    def _must_not_forecast(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("a read-across needs an event; an empty one is "
                             "a ticker mentioned for no stated reason")
        if found := directional_language(v):
            raise ValueError(
                f"forward-looking or recommendation language in a read-across "
                f"event: {found}. Report what happened or what is scheduled. "
                f"'Guidance raised' is reporting; 'guidance should support the "
                f"link' is a forecast, and Vera receives it as the answer.")
        return v.strip()

    @model_validator(mode="after")
    def _links_must_come_from_the_map(self):
        declared = links_read_across(self.ticker)
        if not self.links_affected:
            raise ValueError(
                f"{self.ticker} named with no affected link. The claim being "
                f"made is that a LINK reprices; without one there is no claim.")
        if invented := sorted(set(self.links_affected) - set(declared)):
            raise ValueError(
                f"{self.ticker} does not read across to {invented}. "
                f"Its declared links are {declared}. The map is fixed in "
                f"core.theme — a new edge is a code change and a test, not a "
                f"morning opinion.")
        return self


class AtlasOutput(BaseModel):
    date: date
    regime_signal: Literal["risk-on", "risk-off", "neutral", "transitioning"]
    key_events_today: list[str]
    notable_overnight_moves: list[str]
    change_from_yesterday: Literal["none", "minor", "material"]
    confidence: Literal["low", "medium", "high"]
    narrative: str
    # Empty is a perfectly good answer: most days no bellwether reports
    # and none of them moved enough to attribute anything to.
    read_across: list[ReadAcross] = []

    @field_validator("narrative")
    @classmethod
    def _prose_stays_ticker_free(cls, v: str) -> str:
        if found := tickers_mentioned(v):
            raise ValueError(
                f"ticker(s) {found} in the narrative. Tickers belong in "
                f"read_across, where they are validated and auditable — the "
                f"prose prohibition has been moved, not lifted.")
        return v

    @field_validator("key_events_today", "notable_overnight_moves")
    @classmethod
    def _lists_stay_ticker_free(cls, v: list[str]) -> list[str]:
        for item in v:
            if found := tickers_mentioned(item):
                raise ValueError(
                    f"ticker(s) {found} in '{item}'. A bellwether's move goes "
                    f"in read_across with its observed_move_pct, so there is "
                    f"exactly one place to audit it.")
        return v


# =================================================================
# [2] SKILLS / TOOLS — the menu Claude gets to choose FROM. He
# decides which of these to call and in what order (that choice is
# what makes this "agentic" rather than a fixed script). Note what's
# absent: no trading tool, no portfolio-query tool. Atlas is
# architecturally incapable of touching a position — not a rule he
# could break, just not on the menu.
#
# get_bellwether_quotes costs one FMP request per bellwether — seven
# on top of the four indices, of a 250/day free-tier budget. Worth
# knowing before the universe widens and screening starts competing
# for the same quota.
# =================================================================
ATLAS_TOOLS = [
    {
        "name": "get_index_quotes",
        "description": "Current levels and daily % change for major US indices (S&P 500, Dow, Nasdaq) and the VIX volatility index.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_bellwether_quotes",
        "description": (
            "Yesterday's close and daily % change for each name in the fixed "
            "bellwether map, together with the theme links that reprice off "
            "it. Use this to report an OBSERVED move and which links it "
            "propagates to. It does not tell you WHY anything moved — you "
            "have no news feed, so do not infer a cause."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


def get_bellwether_quotes() -> list[dict]:
    """The observed move for each bellwether, joined to its links.

    The join happens HERE, in code, rather than being left to the
    model — handing him the price and the map separately and asking
    him to pair them up is an invitation to pair them wrongly.

    A name whose quote is unavailable is returned with move=None and
    an `unavailable` reason rather than being dropped. A silently
    shortened list would read as "nothing moved", which is the
    codebase's least favourite kind of wrong.
    """
    rows = []
    for ticker in BELLWETHER_TICKERS:
        quote = fmp_client.get_stock_quote(ticker)
        row = {"ticker": ticker, "reprices": links_read_across(ticker)}
        if isinstance(quote, dict) and "error" in quote:
            row.update({"move_pct": None, "price": None,
                        "unavailable": quote["error"]})
        else:
            row.update({
                "move_pct": quote.get("changePercentage",
                                      quote.get("changesPercentage")),
                "price": quote.get("price"),
            })
        rows.append(row)
    return rows


# =================================================================
# [3] TOOL DISPATCH — translates "Claude wants to call X" into
# actually running real code. This function is the entire boundary
# between Claude's reasoning and the outside world for Atlas —
# nothing happens that doesn't pass through here.
# =================================================================
def execute_tool(tool_name: str, tool_input: dict):
    if tool_name == "get_index_quotes":
        return fmp_client.get_index_quotes()
    if tool_name == "get_bellwether_quotes":
        return get_bellwether_quotes()
    raise ValueError(f"Atlas has no tool named '{tool_name}'")


# =================================================================
# [1] ROLE / MANDATE — Atlas's job description. This is what a real
# analyst's manager would tell them on day one: here's your scope,
# here's what's out of bounds, here's exactly how to hand off your
# work when you're done.
# =================================================================
ATLAS_SYSTEM_PROMPT = """You are Atlas, the Macro/Market Intelligence agent at MBY-Trading.

Your job has two parts:

1. Characterize today's macro backdrop and whether it has materially
   changed since yesterday.
2. Report the READ-ACROSS: which links of the AI data-centre theme
   reprice off which bellwether today, and any move you actually
   observed.

WHAT YOU MAY AND MAY NOT SAY ABOUT COMPANIES

You do not express a view on any security, ever. You do not say
anything is cheap, attractive, likely to rise, poised to benefit, or
worth owning. That is Vera's job, and the only reason her screen is
worth anything is that nobody handed her the answer first.

What you MAY do is name a bellwether as the SOURCE of an event or an
observed move, because that is a statement about where information
enters the chain rather than a view on a stock.

  Correct:   ticker NVDA, event "Q3 earnings scheduled Wednesday",
             links_affected ["compute", "memory"]
  Correct:   ticker TSM, event "monthly revenue released", move -4.1
  REJECTED:  event "guidance looks strong, should support memory"
  REJECTED:  event "cheap here ahead of the print"

The second pair are forecasts, and the output contract will reject
them outright rather than pass them to Vera.

Two hard structural rules, both enforced by the schema:

- Tickers appear ONLY in read_across. Your narrative,
  key_events_today and notable_overnight_moves must contain no ticker
  at all. Describe index and volatility behaviour there.
- You may only name a ticker from the bellwether map below, and only
  with the links that map already assigns to it. You cannot add a
  name or an edge. If something outside the map seems to be steering
  the theme, say so in the narrative WITHOUT naming it, and the map
  gets reviewed as a code change.

{bellwethers}
YOUR INPUTS, AND THEIR LIMITS

You have a tool for index and VIX levels, and a tool for bellwether
quotes with their links. You do NOT have a news feed or an economic
calendar. So:

- You can report that a bellwether MOVED. You cannot report WHY. Do
  not infer a cause you have no way of confirming, and do not present
  a guess as an event.
- An earnings date you are confident of may be reported as a
  scheduled event. If you are not confident of the date, leave it out
  rather than guessing — a wrong date is worse than a missing one.
- Let your confidence field reflect this honestly. Most days are
  "medium".

Other rules:
- If you report change_from_yesterday as "material", you MUST name
  the specific driver in your narrative. Never "sentiment shifted."
- read_across may be an empty list. Most days no bellwether reports
  and nothing moved enough to attribute. An empty list is a real
  answer; an invented entry is not.

Once you have gathered what you need, respond with ONLY a single JSON
object — no markdown fences, no other text before or after it —
matching exactly this shape:

{{
  "date": "YYYY-MM-DD",
  "regime_signal": "risk-on | risk-off | neutral | transitioning",
  "key_events_today": ["..."],
  "notable_overnight_moves": ["..."],
  "change_from_yesterday": "none | minor | material",
  "confidence": "low | medium | high",
  "narrative": "2-3 sentence plain-English summary, no tickers",
  "read_across": [
    {{
      "ticker": "NVDA",
      "event": "Q3 earnings scheduled Wednesday",
      "links_affected": ["compute", "memory"],
      "observed_move_pct": null
    }}
  ]
}}
"""


def build_system_prompt() -> str:
    """The bellwether map is injected rather than hard-coded in the
    prompt string, so the prompt and core.theme can never disagree
    about which names are permitted — the validator and the
    instructions read from one source."""
    return ATLAS_SYSTEM_PROMPT.format(bellwethers=describe_bellwethers())


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
        f"Gather what you need and produce today's macro brief, "
        f"including the read-across."
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
        system_prompt=build_system_prompt(),
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
            read_across=[r.model_dump() for r in output.read_across],
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
                "read_across": stmt.excluded.read_across,
            },
        )
        session.execute(stmt)

    return output.model_dump()


if __name__ == "__main__":
    # Manual smoke test: python agents/atlas.py
    result = run(date.today())
    print(result)
