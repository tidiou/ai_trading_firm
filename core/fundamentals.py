"""
Derived fundamental series — what the reported numbers actually say (S2a).

=====================================================================
WHAT THIS IS FOR
=====================================================================

core/clients/edgar_client.py fetches facts. This turns them into the
four or five things a thesis assumption can be written against:

    cash conversion       does reported profit become cash?
    capex intensity       how much revenue goes back into the plant?
    reinvestment rate     how much of the cash generated is redeployed?
    margin trajectory     gross and operating, over years not quarters
    share count           is the count shrinking or diluting?

All of it computed in deterministic Python from filed figures, with no
model involved. That is the pattern the rest of this codebase runs on —
Nora's limits, Ada's sizing, the benchmark decomposition — and it is
why this is the cheapest part of the research work to maintain.

=====================================================================
TAG ALIASING IS THE REAL PROBLEM, AND IT IS HANDLED VISIBLY
=====================================================================

There is no single XBRL tag for "revenue". A filer may report
`RevenueFromContractWithCustomerExcludingAssessedTax`, or `Revenues`,
or the retired `SalesRevenueNet`, and may switch between them across
years. Same for capex and operating cash flow.

So each concept below has an ORDERED list of candidate tags, and the
first one with data wins. Critically, `resolve_concept` returns WHICH
tag it used, and `series()` carries that through to the caller.

A silent fallback would be worse than no series at all: if a company's
revenue quietly resolves to a narrower tag in 2019 and a broader one
in 2023, the growth rate between them is an artefact of the taxonomy
rather than the business, and nothing anywhere would say so. Reporting
the tag makes that inspectable.

=====================================================================
ANNUAL ONLY, FROM 10-Ks
=====================================================================

Every series here filters to `fiscal_period == "FY"` on an annual
form. Mixing quarterly and annual figures in one series produces
nonsense — a ratio whose numerator is a quarter and denominator a year
is off by 4x and looks merely disappointing rather than wrong.

Quarterly analysis is a different job with different comparability
problems (seasonality, restated interims), deliberately not attempted
here.

=====================================================================
WHAT THIS REFUSES TO REPORT
=====================================================================

A ratio with a missing or zero denominator is None with a reason, not
zero and not a guess. A series with fewer than two years yields no
trend. Following core/benchmark.py: a number that was computed, or
None with a stated reason, never a plausible-looking placeholder.
"""

import logging
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.clients import edgar_client
from core.db import session_scope
from core.models import CompanyFact

logger = logging.getLogger(__name__)

# Ordered candidates per concept — first with data wins. See the
# module docstring on why the resolved tag is reported back.
CONCEPTS: dict[str, list[str]] = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ],
    "net_income": ["NetIncomeLoss"],
    "operating_cash_flow": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "buybacks": ["PaymentsForRepurchaseOfCommonStock"],
    "diluted_shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
}

# Every tag worth storing, for the sync pass. Narrower than "all of
# companyfacts" on purpose: a mega-cap payload carries hundreds of
# concepts and tens of thousands of facts, and storing all of it would
# make the table enormous to no end.
TRACKED_TAGS = sorted({tag for tags in CONCEPTS.values() for tag in tags})

# Annual figures only, and only from a form that reports a full year.
ANNUAL_FORMS = ("10-K", "10-K/A", "20-F", "40-F")

MIN_YEARS_FOR_TREND = 2


# =================================================================
# PURE — everything below this line to `sync` takes plain rows
# =================================================================
@dataclass
class Series:
    """One concept's annual history, plus the provenance that makes it
    interpretable."""

    concept: str
    tag: Optional[str]            # which candidate actually resolved
    unit: Optional[str]
    points: list[tuple[int, float]] = field(default_factory=list)  # (fiscal_year, value)
    unavailable: str = ""

    @property
    def measurable(self) -> bool:
        return bool(self.points)

    @property
    def years(self) -> list[int]:
        return [y for y, _ in self.points]

    def value_for(self, year: int) -> Optional[float]:
        for y, v in self.points:
            if y == year:
                return v
        return None

    @property
    def latest(self) -> Optional[tuple[int, float]]:
        return self.points[-1] if self.points else None


def annual_rows(rows: list[dict]) -> list[dict]:
    """Keep only full-year figures from an annual form, newest filing
    winning on any period reported more than once."""
    annual = [
        r for r in rows
        if str(r.get("fiscal_period") or "") == "FY"
        and str(r.get("form") or "") in ANNUAL_FORMS
    ]
    return edgar_client.latest_per_period(annual)


def resolve_concept(rows: list[dict], concept: str) -> Series:
    """Pick the first candidate tag that has annual data, and say which.

    Ties are impossible by construction — the list is ordered and the
    first hit wins — which matters because two tags can both carry data
    for overlapping years with different definitions.
    """
    candidates = CONCEPTS.get(concept)
    if candidates is None:
        raise ValueError(f"unknown concept {concept!r} — expected one of "
                         f"{', '.join(sorted(CONCEPTS))}")

    annual = annual_rows(rows)
    for tag in candidates:
        matching = [r for r in annual if r["tag"] == tag]
        if not matching:
            continue
        units = {r["unit"] for r in matching}
        # More than one unit for the same concept is a data problem, not
        # something to average over. Take the commonest and say so.
        unit = sorted(units, key=lambda u: -sum(1 for r in matching if r["unit"] == u))[0]
        matching = [r for r in matching if r["unit"] == unit]
        points = sorted(
            {int(r["fiscal_year"]): float(r["value"])
             for r in matching if r.get("fiscal_year") is not None}.items()
        )
        if not points:
            continue
        return Series(concept=concept, tag=tag, unit=unit, points=points)

    return Series(
        concept=concept, tag=None, unit=None,
        unavailable=f"no annual data under any of: {', '.join(candidates)}",
    )


def _ratio(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    """None, not zero, when it cannot be computed. A zero denominator is
    undefined — and a revenue of zero is a real thing for a
    pre-commercial filer, so this is not hypothetical."""
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


@dataclass
class Fundamentals:
    """The derived picture for one company. Every field is a number
    that was computed or None with the reason available on the series
    it came from."""

    ticker: str
    series: dict = field(default_factory=dict)
    years: list = field(default_factory=list)
    # year -> ratio, None where the inputs were missing
    cash_conversion: dict = field(default_factory=dict)
    capex_intensity: dict = field(default_factory=dict)
    reinvestment_rate: dict = field(default_factory=dict)
    gross_margin: dict = field(default_factory=dict)
    operating_margin: dict = field(default_factory=dict)
    share_count_change_pct: Optional[float] = None
    unavailable: str = ""

    @property
    def measurable(self) -> bool:
        return bool(self.years)

    def trend(self, name: str) -> Optional[float]:
        """Change in a ratio from the earliest year with a value to the
        latest, in percentage points. None below MIN_YEARS_FOR_TREND —
        a "trend" from one observation is not one."""
        table = getattr(self, name, {})
        present = [(y, v) for y, v in sorted(table.items()) if v is not None]
        if len(present) < MIN_YEARS_FOR_TREND:
            return None
        return (present[-1][1] - present[0][1]) * 100.0

    def describe(self) -> str:
        if not self.measurable:
            return f"{self.ticker}: no annual figures on record — {self.unavailable}"
        lines = [f"{self.ticker} — {self.years[0]} to {self.years[-1]} "
                 f"({len(self.years)} fiscal years)"]
        for label, name in (("cash conversion", "cash_conversion"),
                            ("capex / revenue", "capex_intensity"),
                            ("capex / op. cash", "reinvestment_rate"),
                            ("gross margin", "gross_margin"),
                            ("operating margin", "operating_margin")):
            table = getattr(self, name)
            latest = next((table[y] for y in reversed(self.years)
                           if table.get(y) is not None), None)
            t = self.trend(name)
            if latest is None:
                lines.append(f"  {label:<18} —  (inputs not reported)")
            else:
                trend = f"{t:+.1f}pp over the period" if t is not None else "—"
                lines.append(f"  {label:<18} {latest * 100:6.1f}%   {trend}")
        if self.share_count_change_pct is not None:
            lines.append(f"  diluted shares     {self.share_count_change_pct:+.1f}% "
                         f"over the period")
        return "\n".join(lines)


def compute_fundamentals(ticker: str, rows: list[dict]) -> Fundamentals:
    """Derive the ratios from stored facts.

    PURE. Takes rows, returns numbers. The whole of the reasoning above
    is exercisable with a list of dicts and no database, which is what
    keeps this project's suite at a few seconds and therefore run.
    """
    if not rows:
        return Fundamentals(ticker=ticker,
                            unavailable="nothing stored for this ticker — "
                                        "run `python -m core.fundamentals sync "
                                        f"{ticker}` first")

    series = {c: resolve_concept(rows, c) for c in CONCEPTS}

    # The year axis is the union of years any concept reported, so a
    # concept missing one year leaves a hole rather than truncating
    # every other series to its shortest member.
    years = sorted({y for s in series.values() for y in s.years})
    if not years:
        return Fundamentals(ticker=ticker, series=series,
                            unavailable="facts are stored but none are full-year "
                                        "figures from an annual form")

    rev, ni = series["revenue"], series["net_income"]
    ocf, capex = series["operating_cash_flow"], series["capex"]
    gp, oi = series["gross_profit"], series["operating_income"]

    f = Fundamentals(ticker=ticker, series=series, years=years)
    for y in years:
        f.cash_conversion[y] = _ratio(ocf.value_for(y), ni.value_for(y))
        f.capex_intensity[y] = _ratio(capex.value_for(y), rev.value_for(y))
        f.reinvestment_rate[y] = _ratio(capex.value_for(y), ocf.value_for(y))
        f.gross_margin[y] = _ratio(gp.value_for(y), rev.value_for(y))
        f.operating_margin[y] = _ratio(oi.value_for(y), rev.value_for(y))

    shares = series["diluted_shares"]
    if len(shares.points) >= MIN_YEARS_FOR_TREND:
        first, last = shares.points[0][1], shares.points[-1][1]
        if first:
            f.share_count_change_pct = (last / first - 1.0) * 100.0

    return f


# =================================================================
# DATABASE
# =================================================================
def store_facts(rows: list[dict]) -> int:
    """Upsert facts. Conflicts are ignored rather than updated: the
    unique key already includes `accession`, so a conflict means the
    identical fact from the identical filing — there is nothing to
    change, and a restatement arrives as a different accession and a
    new row.

    `period_start` is in the conflict target because one filing reports
    the same cash-flow concept twice for one period_end — the discrete
    quarter and the cumulative year to date. Leaving it out made those
    two collide, so whichever arrived second was dropped and a
    nine-month figure could be read as a quarter. See migration 012."""
    if not rows:
        return 0
    stored = 0
    with session_scope() as session:
        for r in rows:
            stmt = pg_insert(CompanyFact).values(**r).on_conflict_do_nothing(
                index_elements=["cik", "taxonomy", "tag", "unit",
                                "period_start", "period_end", "accession"])
            result = session.execute(stmt)
            stored += result.rowcount or 0
    return stored


def load_facts(ticker: str, tags: Optional[list[str]] = None) -> list[dict]:
    """Stored facts for one ticker, as plain dicts for the pure layer."""
    with session_scope() as session:
        q = session.query(CompanyFact).filter(CompanyFact.ticker == ticker.upper())
        if tags:
            q = q.filter(CompanyFact.tag.in_(tags))
        return [{
            "cik": r.cik, "ticker": r.ticker, "taxonomy": r.taxonomy,
            "tag": r.tag, "unit": r.unit, "fiscal_year": r.fiscal_year,
            "fiscal_period": r.fiscal_period, "period_start": r.period_start,
            "period_end": r.period_end, "value": float(r.value),
            "form": r.form, "filed_date": r.filed_date, "accession": r.accession,
        } for r in q.all()]


def sync(ticker: str) -> dict:
    """Fetch and store the tracked concepts for one ticker.

    Uses companyconcept per tag rather than companyfacts, deliberately.
    companyfacts for a mega-cap is tens of megabytes of which we want a
    few percent; eight narrow requests at five per second is under two
    seconds and downloads a fraction of the bytes. It also degrades
    better: one 404 costs one concept instead of the company.
    """
    cik = edgar_client.cik_for(ticker)
    if cik is None:
        return {"ticker": ticker, "error": f"no CIK on record at EDGAR for {ticker!r}"}

    all_rows, per_tag = [], {}
    for tag in TRACKED_TAGS:
        payload = edgar_client.company_concept(cik, tag)
        if not payload:
            per_tag[tag] = 0
            continue
        rows, stats = edgar_client.iter_facts(payload, ticker)
        all_rows.extend(rows)
        per_tag[tag] = stats["rows"]

    stored = store_facts(all_rows)
    logger.info("%s: fetched %s fact(s) across %s tag(s), stored %s new.",
                ticker, len(all_rows), len(TRACKED_TAGS), stored)
    return {"ticker": ticker, "cik": cik, "fetched": len(all_rows),
            "stored": stored, "per_tag": per_tag}


def fundamentals_for(ticker: str) -> Fundamentals:
    return compute_fundamentals(ticker, load_facts(ticker))


# =================================================================
# CLI
# =================================================================
_USAGE = """usage: python -m core.fundamentals <command> [TICKER]

  sync TICKER     Fetch the tracked concepts from SEC EDGAR and store
                  them. Safe to re-run; only new facts are inserted.
  report TICKER   Print the derived annual series from what is stored.
  tags            List the XBRL concepts this module tracks.

Requires EDGAR_USER_AGENT in .env — the SEC requires automated clients
to identify themselves with contact information.
"""


def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    command = argv[1] if len(argv) > 1 else ""

    if command == "tags":
        for concept, tags in sorted(CONCEPTS.items()):
            print(f"  {concept:<22} {', '.join(tags)}")
        return 0

    if command in ("sync", "report"):
        if len(argv) < 3:
            print(f"{command} needs a ticker.", file=sys.stderr)
            return 2
        ticker = argv[2].upper()
        if command == "sync":
            result = sync(ticker)
            if result.get("error"):
                print(result["error"], file=sys.stderr)
                return 1
            print(f"{ticker} (CIK {result['cik']}): fetched {result['fetched']}, "
                  f"stored {result['stored']} new.")
            missing = [t for t, n in result["per_tag"].items() if n == 0]
            if missing:
                print(f"  no data reported under: {', '.join(missing)}")
            return 0
        print(fundamentals_for(ticker).describe())
        return 0

    print(_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
