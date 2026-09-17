"""
The AI data-centre theme — the universe, and the one number that governs it.

=====================================================================
WHY A THEME MODULE AT ALL
=====================================================================

The desk's universe was six mega-caps in a flat list, five of which
happened to be AI-adjacent. That is a concentration nobody declared
and nothing could measure.

Naming the theme changes two things beyond breadth:

  THESES START SHARING ASSUMPTIONS. "Hyperscaler capex keeps growing"
  is load-bearing for compute, memory, networking, cooling AND power
  at the same time. One earnings call updates six theses. With
  unrelated names, every thesis is an island and nothing accumulates.

  THE CONCENTRATION BECOMES VISIBLE TO RISK. Nora's max_sector_pct is
  blind here: GICS files NVIDIA under Information Technology,
  Constellation under Utilities and Vertiv under Industrials. Three
  buckets, one bet, and a 25% sector cap satisfied on paper at 100%
  thematic exposure. THEME_OF gives risk something to count.

=====================================================================
THE GOVERNING NUMBER: CAPEX AGAINST OPERATING CASH FLOW
=====================================================================

Every link in this chain is a derivative of hyperscaler capex. So the
one measurement that matters more than any other is whether that capex
is still being paid for out of operating cash flow, or out of
borrowing.

The distinction is not academic. Self-funded capex can be cut without
consequence. Debt-funded capex has to earn its coupon, and that is the
mechanism by which every previous infrastructure build-out — railways,
fibre — destroyed its investors while the infrastructure itself went
on to change the world. The technology thesis was right both times.
The equity was not.

Third-party research put the aggregate crossover around Q3 2026. This
module exists so the desk does not have to take anyone's word for it:
both inputs are already in company_facts, so the crossover is
computable from primary filings, per company, every quarter.

=====================================================================
TWO TRAPS IN QUARTERLY XBRL, BOTH HANDLED HERE
=====================================================================

1. CASH-FLOW FACTS ARE USUALLY CUMULATIVE. In a Q3 10-Q,
   NetCashProvidedByUsedInOperatingActivities normally covers the
   NINE MONTHS to date, not the quarter. Reading it as a quarter
   understates nothing and overstates everything by up to 3x. The fix
   is duration: every flow fact carries start and end, so a ~90-day
   fact is a discrete quarter and a ~270-day fact is year-to-date.
   Discrete facts are preferred; where a filer reports only
   cumulative, consecutive periods are differenced.

   The differencing groups by PERIOD_START, not by the fact's
   fiscal_year. EDGAR's `fy`/`fp` describe the FILING a fact appeared
   in, not the period the fact covers, so a 10-Q's prior-year
   comparative carries this year's fiscal_year — and differencing
   across that boundary produces nonsense. Every cumulative fact
   within one fiscal year shares one start date, the fiscal year's
   first day, which identifies the ladder exactly and needs no trust
   in a metadata field.

2. FISCAL CALENDARS DO NOT LINE UP. Microsoft's year ends in June,
   Oracle's in May, Amazon/Alphabet/Meta are calendar-year. Summing
   "fiscal Q3" across them adds up different three-month windows and
   produces a number that means nothing. Aggregation is therefore by
   CALENDAR quarter, derived from period_end.

Both of these would produce confident, wrong figures rather than
errors, which is the failure mode this codebase treats as most
serious.
"""

import logging
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from core.fundamentals import load_facts

logger = logging.getLogger(__name__)

# =================================================================
# THE UNIVERSE, BY LINK IN THE CHAIN
#
# A RESEARCH UNIVERSE, NOT A BUY LIST. Inclusion here means "worth
# Vera's attention", nothing more — no view on price, valuation or
# whether any of it should be owned. That is the desk's job and the
# operator's, not this file's.
#
# Ordered roughly by position in the chain: the hyperscalers spend,
# everyone below them receives. Note that the hyperscalers are the
# PAYERS — their capex is the rest of the chain's revenue, and they
# carry the depreciation and, increasingly, the debt. Holding them is
# a different trade from holding the build-out, which is why they are
# their own link rather than folded in.
#
# US-LISTED ONLY, because Ada executes through Alpaca. That constraint
# has a consequence worth stating plainly: the memory oligopoly is
# Samsung, SK Hynix and Micron, and only Micron is reachable. The
# memory link is therefore effectively a single name, not a
# diversified exposure — do not mistake one ticker for a sector.
# Siemens Energy (ENR.DE) is likewise absent from the power link for
# the same reason.
# =================================================================
THEME_LINKS: dict[str, dict] = {
    "hyperscaler": {
        "label": "Hyperscalers (the payers)",
        "tickers": ["MSFT", "GOOGL", "AMZN", "META", "ORCL"],
        "depends_on": "their own cloud/AI revenue converting fast enough to "
                      "justify the capex they have already guided to",
        "note": "The demand signal for every link below. Their capex guidance "
                "is the leading indicator for the whole theme.",
    },
    "compute": {
        "label": "Compute & accelerators",
        "tickers": ["NVDA", "AMD", "AVGO", "MRVL"],
        "depends_on": "holding share as the workload mix shifts from training "
                      "toward inference, where custom silicon competes hardest",
        "note": "Inference is now roughly two-thirds of compute cycles. The "
                "incumbent's >90% share is training-weighted, which is where "
                "its moat actually sits.",
    },
    "memory": {
        "label": "Memory & storage",
        "tickers": ["MU"],
        "depends_on": "capacity discipline holding while HBM demand grows",
        "note": "EFFECTIVELY ONE ACCESSIBLE NAME — see the module comment. "
                "Also the most violently cyclical link: this company posted a "
                "NEGATIVE 9.1% gross margin in FY2023, two years after a "
                "45.2% one.",
    },
    "networking": {
        "label": "Networking & interconnect",
        "tickers": ["ANET", "CIEN", "COHR", "CRDO"],
        "depends_on": "scale-up and scale-out topologies continuing to grow "
                      "port and optics content per rack",
        "note": "Benefits from the inference shift roughly as much as from "
                "training, since both move data.",
    },
    "foundry_equipment": {
        "label": "Foundry & equipment",
        "tickers": ["TSM", "ASML", "AMAT", "LRCX", "KLAC"],
        "depends_on": "leading-edge capacity remaining the binding constraint "
                      "rather than becoming oversupplied",
        "note": "The constraint behind every other constraint — every "
                "accelerator and custom ASIC passes through the same few "
                "leading-edge nodes.",
    },
    "power": {
        "label": "Power generation & grid",
        "tickers": ["GEV", "CEG", "VST", "ETN", "PWR"],
        "depends_on": "the interconnection and turbine backlog converting to "
                      "revenue on schedule rather than being cancelled",
        "note": "The current bottleneck, and the link with the HARDEST "
                "visibility: turbine slots contracted into 2031 and "
                "interconnection queues of 5-6 years are signed commitments "
                "rather than forecasts.",
    },
    "cooling_infra": {
        "label": "Cooling & data-centre infrastructure",
        "tickers": ["VRT", "MOD", "HUBB"],
        "depends_on": "the transition to liquid cooling raising content per "
                      "megawatt, and backlog converting",
        "note": "Watch GAAP net income here with care — a one-off charge can "
                "produce a loss in a quarter the business grew 30%.",
    },
}

# ticker -> link, for risk aggregation. Built once; a ticker in two
# links (AVGO is arguably compute and networking) is assigned to the
# first, deliberately: a name must count once, or a theme cap
# double-counts it and reads as less concentrated than it is.
THEME_OF: dict[str, str] = {}
for _link, _spec in THEME_LINKS.items():
    for _t in _spec["tickers"]:
        THEME_OF.setdefault(_t, _link)

THEME_UNIVERSE: list[str] = sorted(THEME_OF)

# The payers, whose capex is everyone else's revenue.
HYPERSCALERS: list[str] = list(THEME_LINKS["hyperscaler"]["tickers"])


# =================================================================
# BELLWETHERS — where information ENTERS the chain
#
# A theme's links are causally chained, not merely correlated, and
# information does not arrive at all 27 names at once. It arrives at
# one name and propagates. Naming that is a statement about
# information flow, NOT a view on a security: "TSMC's monthly revenue
# is what the silicon complex marks against" says nothing about
# whether TSM is worth owning.
#
# WHY THIS LIVES IN CODE RATHER THAN IN A PROMPT. The causal edges
# below are structural facts about the chain, and the whole value of
# the map is that they are fixed, reviewable and the same every
# morning. A model asked to nominate today's bellwethers would
# nominate whatever it had just been thinking about, and the edge list
# would drift until it meant nothing. Atlas READS this; he cannot add
# to it. Same principle as the verdict being computed rather than
# asked for.
#
# WHAT THIS BUYS, STATED HONESTLY. Not a timing edge — a read-across
# is the most crowded inference in the market and is priced downstream
# within minutes, long before the next premarket meeting. What it buys
# is correct ATTRIBUTION. If MU falls 9% the morning after NVDA guides
# down, that is the link repricing, not MU's thesis breaking. Without
# this map, Vera's monitoring pass can mark an assumption `strained`
# on a move that had nothing to do with the claims she is checking —
# a false verdict, written to the record and graded later.
#
# `reads_across` names LINKS, never tickers, on purpose: the claim is
# that a link reprices, and which name inside it to look at is Vera's
# decision, not Atlas's.
# =================================================================
BELLWETHERS: dict[str, dict] = {
    "NVDA": {
        "reads_across": ["compute", "memory", "networking",
                         "cooling_infra", "foundry_equipment"],
        "cadence": "quarterly earnings",
        "why": "The largest single revenue signal in the chain. Its "
               "data-centre guidance is the public read on HBM demand, "
               "optics content per rack and liquid-cooling attach rates "
               "at the same time.",
    },
    "TSM": {
        "reads_across": ["compute", "foundry_equipment"],
        "cadence": "MONTHLY revenue, plus quarterly earnings",
        "why": "The highest-frequency hard datapoint anywhere in the "
               "chain — monthly rather than quarterly, which makes it "
               "the only bellwether that can update a thesis between "
               "earnings seasons.",
    },
    "ASML": {
        "reads_across": ["foundry_equipment", "compute"],
        "cadence": "quarterly bookings",
        "why": "Bookings lead installed leading-edge capacity by "
               "several quarters, so this is the earliest read on "
               "whether the constraint behind every other constraint "
               "is being relieved.",
    },
    "AVGO": {
        "reads_across": ["compute", "networking"],
        "cadence": "quarterly earnings",
        "why": "The custom-silicon counterweight to NVDA. Read the two "
               "together or the compute link looks like one trend when "
               "it is two.",
    },
    "MU": {
        "reads_across": ["memory"],
        "cadence": "quarterly earnings and pricing commentary",
        "why": "The only listed read on DRAM and HBM pricing reachable "
               "from a US brokerage account — see the memory link's "
               "note about being one accessible name.",
    },
    "GEV": {
        "reads_across": ["power", "cooling_infra"],
        "cadence": "quarterly order intake and backlog",
        "why": "Turbine order intake and backlog conversion is the "
               "build schedule for data-centre power, and a "
               "cancellation shows up here before it shows up in a "
               "hyperscaler's capex line.",
    },
    "MSFT": {
        "reads_across": ["hyperscaler", "compute", "memory", "networking",
                         "foundry_equipment", "power", "cooling_infra"],
        "cadence": "quarterly earnings — JUNE fiscal year end",
        "why": "All five payers' capex lines read across to everything "
               "below them; this one is listed because its June fiscal "
               "year makes it structurally FIRST — it reports on "
               "windows no calendar-year filer has covered yet.",
    },
}

BELLWETHER_TICKERS: list[str] = list(BELLWETHERS)

# link -> the bellwethers that steer it. Built by inversion so the two
# directions can never disagree.
STEERED_BY: dict[str, list[str]] = {link: [] for link in THEME_LINKS}
for _b, _spec in BELLWETHERS.items():
    for _link in _spec["reads_across"]:
        STEERED_BY[_link].append(_b)


def links_read_across(ticker: str) -> list[str]:
    """Which links reprice off this name. Empty for a non-bellwether —
    which is the answer, not a failure."""
    spec = BELLWETHERS.get(ticker.upper())
    return list(spec["reads_across"]) if spec else []


def bellwethers_for(link: str) -> list[str]:
    """Which names steer this link. The question Vera asks when a
    position moves and she needs to know whether the mover was the link
    or the name."""
    return list(STEERED_BY.get(link, []))


def describe_bellwethers() -> str:
    """Rendered for Atlas's prompt. Deliberately states the cadence and
    the reason beside each name: a bellwether with no stated reason is
    indistinguishable from a favourite stock."""
    lines = ["The bellwether map — where information enters the chain. "
             "These are the ONLY tickers you may name, and you may name "
             "them only as the source of an event or an observed move.", ""]
    for ticker, spec in BELLWETHERS.items():
        lines.append(f"  {ticker} ({spec['cadence']})")
        lines.append(f"    reprices: {', '.join(spec['reads_across'])}")
        lines.append(f"    {spec['why']}")
        lines.append("")
    return "\n".join(lines)


# =================================================================
# THE BOUNDARY — factual read-across vs a forecast wearing its clothes
#
# The line between "this name steers the link" and "this name is a
# good buy" is one word wide:
#
#   allowed   NVDA reports Wednesday; the compute and memory links
#             reprice off its data-centre guidance.
#   refused   NVDA reports Wednesday and guidance looks strong, which
#             should support the memory link.
#
# The second is worse than a stock tip, because Vera receives it as
# context and is effectively handed the answer — her screen stops
# being an independent opinion, which is the only thing that makes it
# worth having.
#
# So the boundary is enforced as a CONTRACT FAILURE rather than as a
# request in a prompt. These terms are forward-looking or
# recommendation language; past-tense factual reporting ("guidance
# raised", "backlog fell", "orders cancelled") is deliberately absent,
# because reporting what happened is exactly the job.
# =================================================================
DIRECTIONAL_TERMS: tuple[str, ...] = (
    "should", "likely", "expect", "anticipate", "poised", "set to",
    "bullish", "bearish", "undervalued", "overvalued", "cheap",
    "expensive", "attractive", "buy", "sell", "upside", "downside",
    "outperform", "underperform", "favour", "favor", "benefit",
    "tailwind", "headwind", "opportunity", "price target", "recommend",
)


def directional_language(text: str) -> list[str]:
    """PURE. Which forbidden terms appear in this text.

    Matched on a LEFT word boundary rather than as a bare substring,
    so it still fires on suffixed forms ("expected", "sells off") but
    not on a company name that happens to contain a term — "Marvell"
    does not trip "sell", which a substring check would, making the
    guard useless on the one field that names companies.

    It is not precise, and it is tuned to fail CLOSED: "the company
    sells optics" is factual and would still be rejected. That is the
    right direction for a guard on a field this narrow — a false
    rejection costs Atlas a retry, a false acceptance puts a forecast
    in front of Vera.
    """
    lowered = (text or "").lower()
    return sorted({t for t in DIRECTIONAL_TERMS
                   if re.search(rf"(?<![a-z]){re.escape(t)}", lowered)})


def tickers_mentioned(text: str) -> list[str]:
    """PURE. Which universe tickers appear as standalone words.

    Used to keep the prose fields ticker-free. The original prohibition
    on Atlas naming a company is not being relaxed — it is being moved:
    tickers are permitted in the STRUCTURED read-across field, where
    they are validated and auditable, and nowhere else.
    """
    upper = (text or "").upper()
    return sorted({t for t in THEME_UNIVERSE
                   if re.search(rf"(?<![A-Z0-9]){re.escape(t)}(?![A-Z0-9])", upper)})

CAPEX_TAGS = ("PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets")
OCF_TAGS = ("NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations")

# A flow fact covering this many days is treated as one quarter. The
# window is wide because fiscal quarters are 13 weeks, not 3 months,
# and 52/53-week years drift.
QUARTER_DAYS = (80, 100)

# Cash figures only. Mixing units into one sum is the kind of mistake
# that produces a number nobody can trace, so a non-USD unit is
# skipped rather than converted.
CASH_UNIT = "USD"


# =================================================================
# PURE — quarterly flows out of cumulative XBRL
# =================================================================
def _duration_days(row: dict) -> Optional[int]:
    s, e = _as_date(row.get("period_start")), _as_date(row.get("period_end"))
    if s is None or e is None:
        return None
    return (e - s).days


def calendar_quarter(period_end) -> str:
    """CALENDAR quarter from a period end, e.g. '2026Q2'.

    Deliberately not the fiscal quarter. Microsoft's fiscal Q3 and
    Amazon's fiscal Q3 are different three-month windows, so summing
    by fiscal label adds up unlike things.
    """
    e = _as_date(period_end)
    return f"{e.year}Q{(e.month - 1) // 3 + 1}"


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _current_flows(rows: list[dict], tag: str) -> list[dict]:
    """USD flow facts for one tag, one row per distinct PERIOD.

    Restatements are resolved the way `latest_per_period` does it — by
    filing date — but the period key here includes period_start, which
    that helper deliberately omits because it was written for annual
    facts where an end date identifies a period on its own.

    For quarterly cash flow it does not: a single 10-Q reports one
    period_end twice, once as the discrete quarter and once as the
    cumulative year to date. Keyed on period_end alone those two
    collapse into one row, and which one survives depends on iteration
    order. That is exactly the discrete/cumulative distinction this
    module exists to preserve, so it gets its own key.
    """
    best: dict[tuple, dict] = {}
    for r in rows:
        if r["tag"] != tag or r.get("unit") != CASH_UNIT:
            continue
        if r.get("period_start") is None:      # an instant, not a flow
            continue
        key = (str(r["period_start"]), str(r["period_end"]))
        incumbent = best.get(key)
        if incumbent is None or str(r["filed_date"]) > str(incumbent["filed_date"]):
            best[key] = r
    return sorted(best.values(),
                  key=lambda r: (str(r["period_end"]), str(r["period_start"])))


def discrete_quarters(rows: list[dict], tags: tuple[str, ...]) -> dict[str, float]:
    """Discrete quarterly values for the first tag with data, keyed by
    calendar quarter.

    Two passes. First, facts that ALREADY cover about a quarter are
    taken as they are. Then the gaps are filled by differencing the
    cumulative ladder — the set of facts sharing one period_start, which
    is what a fiscal year's year-to-date figures look like in XBRL.
    Differencing consecutive rungs recovers the quarter between them,
    and the 10-K's full-year fact recovers Q4 the same way.

    A differenced value is only kept when the gap between the two rungs
    is itself about a quarter. A ladder missing a rung would otherwise
    hand back six months of cash flow labelled as three.
    """
    for tag in tags:
        tagged = _current_flows(rows, tag)
        if not tagged:
            continue

        out: dict[str, float] = {}

        # ---- 1. facts that are already one quarter ----
        for r in tagged:
            d = _duration_days(r)
            if d is not None and QUARTER_DAYS[0] <= d <= QUARTER_DAYS[1]:
                out[calendar_quarter(r["period_end"])] = float(r["value"])

        # ---- 2. the cumulative ladders, grouped by their shared start ----
        # The first rung is USUALLY a ~90-day fact that step 1 already
        # took — and it must still be included here, because it is the
        # base the second rung is differenced against. Excluding
        # quarter-length facts from the ladder loses Q2 for every filer
        # who reports year-to-date, which is most of them: their Q1 fact
        # IS both a discrete quarter and the ladder's first rung.
        ladders: dict[str, list[dict]] = defaultdict(list)
        for r in tagged:
            d = _duration_days(r)
            if d is None or d < QUARTER_DAYS[0]:
                continue           # too short to be a quarter of anything
            ladders[str(r["period_start"])].append(r)

        for start, rungs in ladders.items():
            rungs.sort(key=lambda r: str(r["period_end"]))
            prev_end, prev_val = _as_date(start), 0.0
            for r in rungs:
                end = _as_date(r["period_end"])
                gap = (end - prev_end).days
                q = calendar_quarter(end)
                if q not in out and QUARTER_DAYS[0] <= gap <= QUARTER_DAYS[1]:
                    out[q] = float(r["value"]) - prev_val
                prev_end, prev_val = end, float(r["value"])

        if out:
            return dict(sorted(out.items()))
    return {}


@dataclass
class CapexVsCashFlow:
    """One company's capex against its operating cash flow, per calendar
    quarter. Every field computed or absent — never a filled-in guess."""

    ticker: str
    quarters: list = field(default_factory=list)   # calendar quarter labels
    capex: dict = field(default_factory=dict)
    ocf: dict = field(default_factory=dict)
    # capex / ocf. Above 1.0 means the quarter's investment exceeded the
    # cash the business generated.
    ratio: dict = field(default_factory=dict)
    unavailable: str = ""

    @property
    def measurable(self) -> bool:
        return bool(self.ratio)

    @property
    def latest_quarter(self) -> Optional[str]:
        covered = [q for q in self.quarters if self.ratio.get(q) is not None]
        return covered[-1] if covered else None

    @property
    def crossed(self) -> Optional[bool]:
        """Is the most recent measurable quarter above 1.0?"""
        q = self.latest_quarter
        return None if q is None else self.ratio[q] > 1.0

    @property
    def consecutive_above_one(self) -> int:
        """How many quarters in a row, ending at the latest. This is the
        number the theme's load-bearing claim is written against — one
        quarter above 1.0 is lumpy capex, several in a row is a change
        in how the build-out is financed."""
        run = 0
        for q in reversed(self.quarters):
            r = self.ratio.get(q)
            if r is None:
                continue
            if r > 1.0:
                run += 1
            else:
                break
        return run


def capex_vs_cashflow(ticker: str, rows: list[dict]) -> CapexVsCashFlow:
    """PURE. Takes stored facts, returns the comparison."""
    if not rows:
        return CapexVsCashFlow(
            ticker=ticker,
            unavailable=f"nothing stored — run `python -m core.fundamentals "
                        f"sync {ticker}` first")

    capex = discrete_quarters(rows, CAPEX_TAGS)
    ocf = discrete_quarters(rows, OCF_TAGS)
    if not capex or not ocf:
        missing = " and ".join(
            n for n, d in (("capex", capex), ("operating cash flow", ocf)) if not d)
        return CapexVsCashFlow(
            ticker=ticker, capex=capex, ocf=ocf,
            unavailable=f"no quarterly {missing} recoverable from the stored facts")

    quarters = sorted(set(capex) | set(ocf))
    result = CapexVsCashFlow(ticker=ticker, quarters=quarters, capex=capex, ocf=ocf)
    for q in quarters:
        c, o = capex.get(q), ocf.get(q)
        # A negative or zero OCF makes the ratio meaningless rather than
        # large — left absent instead of reported as a huge number.
        result.ratio[q] = (c / o) if (c is not None and o is not None and o > 0) else None
    return result


@dataclass
class ThemeMonitor:
    """The aggregate, plus each company. The aggregate is the theme's
    load-bearing claim; the per-company view is where it breaks first."""

    companies: list = field(default_factory=list)
    quarters: list = field(default_factory=list)
    agg_capex: dict = field(default_factory=dict)
    agg_ocf: dict = field(default_factory=dict)
    agg_ratio: dict = field(default_factory=dict)

    @property
    def measurable(self) -> bool:
        return any(v is not None for v in self.agg_ratio.values())

    @property
    def crossed_quarters(self) -> list:
        return [q for q in self.quarters
                if (self.agg_ratio.get(q) or 0) > 1.0]

    def describe(self) -> str:
        if not self.measurable:
            return ("No quarterly capex/cash-flow comparison available — "
                    "sync the hyperscalers first.")
        lines = ["Hyperscaler capex vs operating cash flow — "
                 "above 1.00 means the quarter's investment exceeded the cash "
                 "the business generated.", ""]
        for q in self.quarters:
            r = self.agg_ratio.get(q)
            if r is None:
                continue
            flag = "  <-- capex exceeds cash flow" if r > 1.0 else ""
            lines.append(f"  {q}   capex {self.agg_capex[q]/1e9:8.1f}bn   "
                         f"ocf {self.agg_ocf[q]/1e9:8.1f}bn   "
                         f"ratio {r:5.2f}{flag}")
        lines.append("")
        for c in self.companies:
            if not c.measurable:
                lines.append(f"  {c.ticker:<6} — {c.unavailable}")
                continue
            run = c.consecutive_above_one
            state = (f"{run} quarter(s) above 1.00" if run
                     else "still funded from cash flow")
            lines.append(f"  {c.ticker:<6} latest {c.latest_quarter}  "
                         f"ratio {c.ratio[c.latest_quarter]:.2f}  {state}")
        return "\n".join(lines)


def build_monitor(per_ticker: dict[str, list[dict]]) -> ThemeMonitor:
    """PURE. Aggregates per-company facts into the theme monitor.

    A quarter is only aggregated when EVERY company has both figures
    for it. A partial sum would show the aggregate falling in the most
    recent quarter purely because one filer has not reported yet — a
    number that moves for a reporting reason and reads as a real one.
    """
    companies = [capex_vs_cashflow(t, rows) for t, rows in sorted(per_ticker.items())]
    usable = [c for c in companies if c.measurable]
    monitor = ThemeMonitor(companies=companies)
    if not usable:
        return monitor

    complete = None
    for c in usable:
        qs = {q for q in c.quarters
              if c.capex.get(q) is not None and c.ocf.get(q) is not None}
        complete = qs if complete is None else (complete & qs)

    monitor.quarters = sorted(complete or [])
    for q in monitor.quarters:
        monitor.agg_capex[q] = sum(c.capex[q] for c in usable)
        monitor.agg_ocf[q] = sum(c.ocf[q] for c in usable)
        o = monitor.agg_ocf[q]
        monitor.agg_ratio[q] = (monitor.agg_capex[q] / o) if o > 0 else None
    return monitor


# =================================================================
# DATABASE
# =================================================================
def monitor_hyperscalers(tickers: Optional[list[str]] = None) -> ThemeMonitor:
    tickers = tickers or HYPERSCALERS
    return build_monitor({t: load_facts(t, list(CAPEX_TAGS + OCF_TAGS))
                          for t in tickers})


# =================================================================
# CLI
# =================================================================
_USAGE = """usage: python -m core.theme <command>

  universe    Print the research universe, by link in the chain.
  capex       Hyperscaler capex against operating cash flow, per
              calendar quarter, from stored SEC facts.

`capex` needs the hyperscalers synced first:

  for t in MSFT GOOGL AMZN META ORCL; do
      python -m core.fundamentals sync $t
  done
"""


def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    command = argv[1] if len(argv) > 1 else ""

    if command == "universe":
        print(f"AI data-centre research universe — {len(THEME_UNIVERSE)} names, "
              f"{len(THEME_LINKS)} links\n")
        for link, spec in THEME_LINKS.items():
            print(f"  {spec['label']}  [{link}]")
            print(f"    {' '.join(spec['tickers'])}")
            print(f"    depends on: {spec['depends_on']}")
            print(f"    {spec['note']}\n")
        print("A research universe, not a buy list — inclusion means "
              "'worth studying', nothing more.")
        return 0

    if command == "capex":
        print(monitor_hyperscalers().describe())
        return 0

    print(_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
