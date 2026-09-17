"""
Tests for the AI data-centre theme module.

WHY THESE TESTS ARE THE POINT OF THE MODULE. Every failure mode here
produces a CONFIDENT WRONG NUMBER rather than an error:

  - read a nine-month cash-flow fact as a quarter and the ratio is
    wrong by 3x in the flattering direction;
  - sum fiscal quarters across filers with different year ends and the
    aggregate adds three unlike windows together;
  - aggregate a quarter one filer has not reported yet and the series
    falls for a reporting reason that reads exactly like a real one.

None of those throw. They just publish, and a thesis gets written on
them. So the arithmetic is tested directly, against facts shaped like
the ones EDGAR actually returns.

No database and no network: every function under test is pure.
"""

from datetime import date

import pytest

from core.theme import (
    BELLWETHERS,
    BELLWETHER_TICKERS,
    CAPEX_TAGS,
    HYPERSCALERS,
    OCF_TAGS,
    STEERED_BY,
    THEME_LINKS,
    THEME_OF,
    THEME_UNIVERSE,
    bellwethers_for,
    build_monitor,
    calendar_quarter,
    capex_vs_cashflow,
    describe_bellwethers,
    directional_language,
    discrete_quarters,
    links_read_across,
    tickers_mentioned,
)

OCF = OCF_TAGS[0]
CAPEX = CAPEX_TAGS[0]


def fact(tag, start, end, value, *, filed="2026-08-01", unit="USD",
         form="10-Q", accn="0000-26-000001", ticker="MSFT"):
    """One row shaped as core.fundamentals.load_facts returns it.

    fiscal_year / fiscal_period are deliberately set to values that
    would MISLEAD a fiscal-year-based grouping — see
    TestFiscalYearMetadataIsNotTrusted.
    """
    return {
        "cik": "0000789019", "ticker": ticker, "taxonomy": "us-gaap",
        "tag": tag, "unit": unit, "fiscal_year": 2026, "fiscal_period": "Q3",
        "period_start": start, "period_end": end, "value": float(value),
        "form": form, "filed_date": filed, "accession": accn,
    }


def cumulative_ladder(tag, q1, h1, m9, full_year, year=2026, **kw):
    """A filer reporting CUMULATIVE year-to-date cash flow on a calendar
    fiscal year. The common shape, and the one that goes wrong quietly."""
    start = f"{year}-01-01"
    return [
        fact(tag, start, f"{year}-03-31", q1, **kw),
        fact(tag, start, f"{year}-06-30", h1, **kw),
        fact(tag, start, f"{year}-09-30", m9, **kw),
        fact(tag, start, f"{year}-12-31", full_year, form="10-K", **kw),
    ]


def discrete_series(tag, values, year=2026, **kw):
    """A filer reporting each quarter as its own period."""
    bounds = [(f"{year}-01-01", f"{year}-03-31"), (f"{year}-04-01", f"{year}-06-30"),
              (f"{year}-07-01", f"{year}-09-30"), (f"{year}-10-01", f"{year}-12-31")]
    return [fact(tag, s, e, v, **kw) for (s, e), v in zip(bounds, values)]


# =================================================================
# The universe
# =================================================================
class TestUniverse:

    def test_every_link_has_tickers_a_dependency_and_a_note(self):
        # The dependency text is not decoration: it is what a thesis's
        # load-bearing assumption gets written from.
        for link, spec in THEME_LINKS.items():
            assert spec["tickers"], link
            assert spec["depends_on"].strip(), link
            assert spec["note"].strip(), link

    def test_every_ticker_maps_to_exactly_one_link(self):
        # A name counted twice makes a theme cap read as less
        # concentrated than it is, which is what THEME_OF exists to stop.
        assert len(THEME_UNIVERSE) == len(set(THEME_UNIVERSE))
        for ticker in THEME_UNIVERSE:
            assert THEME_OF[ticker] in THEME_LINKS

    def test_a_ticker_in_two_links_is_assigned_deterministically(self):
        # First link by declaration order wins, so risk aggregation
        # gives the same answer on every run.
        for ticker in THEME_UNIVERSE:
            first = next(link for link, spec in THEME_LINKS.items()
                          if ticker in spec["tickers"])
            assert THEME_OF[ticker] == first

    def test_the_payers_are_part_of_the_universe(self):
        assert set(HYPERSCALERS) <= set(THEME_UNIVERSE)

    def test_the_memory_link_is_honest_about_being_one_name(self):
        # Samsung and SK Hynix are not reachable through Alpaca, and a
        # single-name link must not be mistaken for a diversified
        # sector. The warning lives in the data, where it gets read.
        memory = THEME_LINKS["memory"]
        assert len(memory["tickers"]) == 1
        assert "ONE ACCESSIBLE NAME" in memory["note"]


# =================================================================
# Trap 2: calendar quarters, not fiscal ones
# =================================================================
class TestCalendarQuarter:

    @pytest.mark.parametrize("end,expected", [
        ("2026-03-31", "2026Q1"),
        ("2026-06-30", "2026Q2"),
        ("2026-09-30", "2026Q3"),
        ("2026-12-31", "2026Q4"),
        ("2026-01-31", "2026Q1"),
        ("2026-05-31", "2026Q2"),      # Oracle's fiscal year end
    ])
    def test_the_quarter_comes_from_the_month_not_a_fiscal_label(self, end, expected):
        assert calendar_quarter(end) == expected

    def test_a_date_object_works_as_well_as_a_string(self):
        # load_facts returns date objects; fixtures use strings. Both
        # paths have to agree or the DB and the tests measure
        # different things.
        assert calendar_quarter(date(2026, 6, 30)) == "2026Q2"

    def test_two_filers_with_different_year_ends_align_on_the_window(self):
        # THE TRAP, stated as arithmetic. Microsoft's fiscal Q4 and
        # Amazon's fiscal Q2 both end 30 June. Summing by fiscal label
        # would add a Q4 to a Q2; by calendar quarter both are 2026Q2,
        # which is the only sum that means anything.
        msft_fiscal_q4 = calendar_quarter("2026-06-30")
        amzn_fiscal_q2 = calendar_quarter("2026-06-30")
        assert msft_fiscal_q4 == amzn_fiscal_q2 == "2026Q2"


# =================================================================
# Trap 1: cumulative XBRL
# =================================================================
class TestDiscreteFacts:

    def test_quarter_length_facts_are_taken_as_they_are(self):
        rows = discrete_series(OCF, [10.0, 20.0, 30.0, 40.0])
        assert discrete_quarters(rows, (OCF,)) == {
            "2026Q1": 10.0, "2026Q2": 20.0, "2026Q3": 30.0, "2026Q4": 40.0}

    def test_an_instant_fact_is_ignored_because_it_is_not_a_flow(self):
        # Balance-sheet items have a date, not a period. Treating one
        # as a flow would put a stock figure into a cash-flow sum.
        rows = [fact(OCF, None, "2026-03-31", 999.0)]
        assert discrete_quarters(rows, (OCF,)) == {}


class TestCumulativeFactsAreDifferenced:
    """The failure this module was written to prevent."""

    def test_a_year_to_date_ladder_becomes_four_quarters(self):
        # 10, 30, 60, 100 cumulative -> 10, 20, 30, 40 discrete.
        rows = cumulative_ladder(OCF, 10.0, 30.0, 60.0, 100.0)
        assert discrete_quarters(rows, (OCF,)) == {
            "2026Q1": 10.0, "2026Q2": 20.0, "2026Q3": 30.0, "2026Q4": 40.0}

    def test_reading_the_ladder_naively_would_have_overstated_q3_by_2x(self):
        # The number this test guards: the raw nine-month fact is 60,
        # the quarter is 30. Publishing 60 as a quarter is the confident
        # wrong answer, and it is wrong in the flattering direction for
        # cash flow and the alarming one for capex.
        rows = cumulative_ladder(OCF, 10.0, 30.0, 60.0, 100.0)
        raw_nine_month = next(r["value"] for r in rows
                               if str(r["period_end"]) == "2026-09-30")
        assert raw_nine_month == 60.0
        assert discrete_quarters(rows, (OCF,))["2026Q3"] == 30.0

    def test_the_first_rung_serves_twice_and_q2_survives(self):
        # REGRESSION. The Q1 fact is BOTH a discrete quarter and the
        # base the half-year fact is differenced against. An earlier
        # version excluded quarter-length facts from the ladder, which
        # silently lost Q2 for every year-to-date filer — i.e. most of
        # them.
        rows = cumulative_ladder(OCF, 10.0, 30.0, 60.0, 100.0)
        out = discrete_quarters(rows, (OCF,))
        assert "2026Q2" in out and out["2026Q2"] == 20.0

    def test_the_full_year_fact_recovers_q4(self):
        # Q4 is never filed as a quarter — it only exists as the 10-K's
        # full year minus the nine months. Differencing gets it for free.
        rows = cumulative_ladder(OCF, 10.0, 30.0, 60.0, 100.0)
        assert discrete_quarters(rows, (OCF,))["2026Q4"] == 40.0

    def test_a_partial_year_stops_where_the_filings_stop(self):
        start = "2026-01-01"
        rows = [fact(OCF, start, "2026-03-31", 10.0),
                fact(OCF, start, "2026-06-30", 30.0)]
        assert discrete_quarters(rows, (OCF,)) == {"2026Q1": 10.0, "2026Q2": 20.0}

    def test_a_ladder_with_a_missing_rung_refuses_to_guess(self):
        # Q1 filed, Q2 missing, nine months filed. The gap from Q1 to
        # the nine-month fact is ~183 days, so differencing would hand
        # back six months of cash flow labelled as one quarter. It is
        # left out instead.
        start = "2026-01-01"
        rows = [fact(OCF, start, "2026-03-31", 10.0),
                fact(OCF, start, "2026-09-30", 60.0)]
        out = discrete_quarters(rows, (OCF,))
        assert out == {"2026Q1": 10.0}
        assert "2026Q3" not in out

    def test_a_half_year_with_no_first_quarter_yields_nothing_splittable(self):
        rows = [fact(OCF, "2026-01-01", "2026-06-30", 30.0)]
        assert discrete_quarters(rows, (OCF,)) == {}

    def test_a_non_calendar_fiscal_year_differences_correctly(self):
        # Microsoft: fiscal year starts 1 July. The ladder's shared
        # start is 2025-07-01, and the quarters land in the calendar
        # quarters they actually cover.
        start = "2025-07-01"
        rows = [fact(OCF, start, "2025-09-30", 10.0),
                fact(OCF, start, "2025-12-31", 30.0),
                fact(OCF, start, "2026-03-31", 60.0),
                fact(OCF, start, "2026-06-30", 100.0, form="10-K")]
        assert discrete_quarters(rows, (OCF,)) == {
            "2025Q3": 10.0, "2025Q4": 20.0, "2026Q1": 30.0, "2026Q2": 40.0}

    def test_two_fiscal_years_do_not_bleed_into_each_other(self):
        # Each fiscal year is its own ladder because each has its own
        # shared start date. The counter resetting at the fiscal year
        # is what makes this work.
        rows = (cumulative_ladder(OCF, 10.0, 30.0, 60.0, 100.0, year=2025)
                + cumulative_ladder(OCF, 11.0, 33.0, 66.0, 110.0, year=2026))
        out = discrete_quarters(rows, (OCF,))
        assert out["2025Q1"] == 10.0 and out["2025Q4"] == 40.0
        assert out["2026Q1"] == 11.0 and out["2026Q4"] == 44.0

    def test_a_discrete_fact_wins_over_a_differenced_one(self):
        # Where a filer gives both, the reported quarter is the filer's
        # own number and needs no arithmetic. Differencing is the
        # fallback, not the default.
        start = "2026-01-01"
        rows = [fact(OCF, start, "2026-03-31", 10.0),
                fact(OCF, start, "2026-06-30", 30.0),
                fact(OCF, "2026-04-01", "2026-06-30", 21.0)]   # reported Q2
        assert discrete_quarters(rows, (OCF,))["2026Q2"] == 21.0


class TestFiscalYearMetadataIsNotTrusted:

    def test_grouping_does_not_depend_on_fiscal_year(self):
        # EDGAR's fy/fp describe the FILING a fact appeared in, not the
        # period it covers — a 10-Q's prior-year comparative carries
        # THIS year's fy. Every fixture above has fiscal_year=2026 and
        # fiscal_period='Q3' regardless of the period, and two fiscal
        # years still separate correctly, which is only possible if the
        # grouping ignores those fields.
        rows = (cumulative_ladder(OCF, 10.0, 30.0, 60.0, 100.0, year=2025)
                + cumulative_ladder(OCF, 11.0, 33.0, 66.0, 110.0, year=2026))
        assert all(r["fiscal_year"] == 2026 for r in rows)
        out = discrete_quarters(rows, (OCF,))
        assert len(out) == 8


class TestRestatementsAndUnits:

    def test_the_latest_filing_wins_for_the_same_period(self):
        rows = [fact(OCF, "2026-01-01", "2026-03-31", 10.0,
                     filed="2026-04-20", accn="a-1"),
                fact(OCF, "2026-01-01", "2026-03-31", 9.5,
                     filed="2026-11-02", accn="a-2")]   # restated
        assert discrete_quarters(rows, (OCF,)) == {"2026Q1": 9.5}

    def test_the_discrete_and_cumulative_facts_of_one_period_both_survive(self):
        # REGRESSION, matching migration 012's unique key. Both facts
        # share a period_end AND a filing. Deduplicating on period_end
        # alone drops one of them, and which one survives depends on
        # iteration order — so a nine-month figure could be read as a
        # quarter with nothing in the logs.
        rows = [fact(OCF, "2026-01-01", "2026-06-30", 30.0, accn="same"),
                fact(OCF, "2026-04-01", "2026-06-30", 21.0, accn="same")]
        assert discrete_quarters(rows, (OCF,)) == {"2026Q2": 21.0}

    def test_a_non_usd_unit_is_skipped_rather_than_mixed_in(self):
        rows = discrete_series(OCF, [10.0, 20.0, 30.0, 40.0])
        rows.append(fact(OCF, "2026-01-01", "2026-03-31", 8_000.0, unit="JPY"))
        assert discrete_quarters(rows, (OCF,)) == {
            "2026Q1": 10.0, "2026Q2": 20.0, "2026Q3": 30.0, "2026Q4": 40.0}

    def test_the_first_tag_with_data_is_used(self):
        # CAPEX_TAGS is an ordered candidate list because filers differ
        # on which concept they report. The fallback must not be
        # consulted when the primary has data.
        rows = discrete_series(CAPEX_TAGS[1], [5.0, 6.0, 7.0, 8.0])
        assert discrete_quarters(rows, CAPEX_TAGS) == {
            "2026Q1": 5.0, "2026Q2": 6.0, "2026Q3": 7.0, "2026Q4": 8.0}

    def test_a_tag_with_no_data_falls_through_to_the_next(self):
        rows = discrete_series(CAPEX_TAGS[1], [5.0, 6.0, 7.0, 8.0])
        assert discrete_quarters(rows, ("NotATag",) + CAPEX_TAGS)["2026Q1"] == 5.0


# =================================================================
# One company
# =================================================================
class TestCapexVsCashFlow:

    def rows(self, capex, ocf, ticker="MSFT"):
        return (discrete_series(CAPEX, capex, ticker=ticker)
                + discrete_series(OCF, ocf, ticker=ticker))

    def test_the_ratio_is_capex_over_cash_flow(self):
        c = capex_vs_cashflow("MSFT", self.rows([50.0] * 4, [100.0] * 4))
        assert c.ratio["2026Q1"] == 0.5
        assert c.measurable is True

    def test_nothing_stored_says_so_instead_of_reporting_zero(self):
        c = capex_vs_cashflow("MSFT", [])
        assert c.measurable is False
        assert "nothing stored" in c.unavailable
        assert "core.fundamentals sync" in c.unavailable

    def test_capex_with_no_cash_flow_names_what_is_missing(self):
        c = capex_vs_cashflow("MSFT", discrete_series(CAPEX, [50.0] * 4))
        assert c.measurable is False
        assert "operating cash flow" in c.unavailable

    def test_a_negative_cash_flow_quarter_leaves_the_ratio_absent(self):
        # capex/negative-OCF is not a large ratio, it is a meaningless
        # one. Absent beats a confident number with the wrong sign.
        c = capex_vs_cashflow("MSFT", self.rows([50.0] * 4, [100.0, -20.0, 100.0, 100.0]))
        assert c.ratio["2026Q2"] is None
        assert c.ratio["2026Q1"] == 0.5

    def test_the_latest_quarter_skips_unmeasurable_ones(self):
        c = capex_vs_cashflow("MSFT", self.rows([50.0] * 4, [100.0, 100.0, 100.0, 0.0]))
        assert c.latest_quarter == "2026Q3"

    def test_crossed_reports_the_latest_measurable_quarter(self):
        c = capex_vs_cashflow("MSFT", self.rows([50.0, 50.0, 50.0, 120.0], [100.0] * 4))
        assert c.crossed is True

    def test_crossed_is_none_when_nothing_is_measurable(self):
        # Not False. "We cannot tell" and "it has not crossed" are
        # different answers and must not collapse into one.
        assert capex_vs_cashflow("MSFT", []).crossed is None

    def test_a_run_above_one_is_counted_from_the_latest_backwards(self):
        c = capex_vs_cashflow("MSFT", self.rows([50.0, 120.0, 130.0, 140.0], [100.0] * 4))
        assert c.consecutive_above_one == 3

    def test_one_quarter_above_one_is_a_run_of_one(self):
        # The distinction the theme's load-bearing claim rests on: one
        # quarter is lumpy capex, several in a row is a change in how
        # the build-out is financed.
        c = capex_vs_cashflow("MSFT", self.rows([50.0, 50.0, 50.0, 140.0], [100.0] * 4))
        assert c.consecutive_above_one == 1

    def test_a_run_is_broken_by_a_quarter_back_below_one(self):
        c = capex_vs_cashflow("MSFT", self.rows([120.0, 130.0, 50.0, 140.0], [100.0] * 4))
        assert c.consecutive_above_one == 1

    def test_still_funded_from_cash_flow_is_a_run_of_zero(self):
        c = capex_vs_cashflow("MSFT", self.rows([50.0] * 4, [100.0] * 4))
        assert c.consecutive_above_one == 0
        assert c.crossed is False

    def test_exactly_one_point_zero_is_not_a_crossing(self):
        # Capex equal to cash flow is the boundary, not past it.
        c = capex_vs_cashflow("MSFT", self.rows([100.0] * 4, [100.0] * 4))
        assert c.crossed is False
        assert c.consecutive_above_one == 0


# =================================================================
# The aggregate
# =================================================================
class TestBuildMonitor:

    def per_ticker(self, spec):
        return {t: (discrete_series(CAPEX, capex, ticker=t)
                    + discrete_series(OCF, ocf, ticker=t))
                for t, (capex, ocf) in spec.items()}

    def test_the_aggregate_sums_both_sides_before_dividing(self):
        # Sum then divide, never average the ratios: a small company
        # with a wild ratio would otherwise move the aggregate as much
        # as a large one.
        m = build_monitor(self.per_ticker({
            "AMZN": ([60.0] * 4, [100.0] * 4),
            "MSFT": ([40.0] * 4, [100.0] * 4)}))
        assert m.agg_capex["2026Q1"] == 100.0
        assert m.agg_ocf["2026Q1"] == 200.0
        assert m.agg_ratio["2026Q1"] == 0.5

    def test_only_quarters_every_company_has_are_aggregated(self):
        # THE REPORTING ARTEFACT. One filer three quarters in, another
        # four: the fourth quarter must not be published as an
        # aggregate that fell, because it fell only because someone
        # has not filed yet.
        rows = self.per_ticker({"AMZN": ([60.0] * 4, [100.0] * 4),
                                "MSFT": ([40.0] * 4, [100.0] * 4)})
        rows["MSFT"] = [r for r in rows["MSFT"]
                        if str(r["period_end"]) != "2026-12-31"]
        m = build_monitor(rows)
        assert m.quarters == ["2026Q1", "2026Q2", "2026Q3"]
        assert "2026Q4" not in m.agg_ratio

    def test_a_company_with_nothing_stored_does_not_shrink_the_series(self):
        # An unsynced name would otherwise reduce the intersection to
        # nothing and make the whole monitor read as unavailable.
        rows = self.per_ticker({"AMZN": ([60.0] * 4, [100.0] * 4)})
        rows["ORCL"] = []
        m = build_monitor(rows)
        assert m.measurable is True
        assert m.quarters == ["2026Q1", "2026Q2", "2026Q3", "2026Q4"]

    def test_but_it_is_still_listed_so_the_gap_is_visible(self):
        rows = self.per_ticker({"AMZN": ([60.0] * 4, [100.0] * 4)})
        rows["ORCL"] = []
        m = build_monitor(rows)
        assert [c.ticker for c in m.companies] == ["AMZN", "ORCL"]
        assert "nothing stored" in next(
            c for c in m.companies if c.ticker == "ORCL").unavailable

    def test_nothing_stored_at_all_is_unmeasurable_not_zero(self):
        m = build_monitor({"AMZN": [], "MSFT": []})
        assert m.measurable is False
        assert m.crossed_quarters == []
        assert "sync the hyperscalers first" in m.describe()

    def test_one_company_crossing_does_not_drag_the_aggregate_over(self):
        m = build_monitor(self.per_ticker({
            "AMZN": ([60.0, 60.0, 120.0, 130.0], [100.0] * 4),
            "MSFT": ([40.0, 40.0, 40.0, 40.0], [100.0] * 4)}))
        # Q3: (120+40)/200 = 0.80 — not crossed.
        # Q4: (130+40)/200 = 0.85 — not crossed either.
        assert m.crossed_quarters == []

    def test_an_aggregate_crossing_is_reported(self):
        m = build_monitor(self.per_ticker({
            "AMZN": ([60.0, 60.0, 140.0, 160.0], [100.0] * 4),
            "MSFT": ([40.0, 40.0, 90.0, 100.0], [100.0] * 4)}))
        assert m.crossed_quarters == ["2026Q3", "2026Q4"]

    def test_describe_flags_the_crossed_quarters_and_names_every_company(self):
        m = build_monitor(self.per_ticker({
            "AMZN": ([60.0, 60.0, 140.0, 160.0], [100.0] * 4),
            "MSFT": ([40.0, 40.0, 90.0, 100.0], [100.0] * 4)}))
        text = m.describe()
        assert "capex exceeds cash flow" in text
        assert "AMZN" in text and "MSFT" in text

    def test_describe_says_still_funded_from_cash_flow_when_it_is(self):
        m = build_monitor(self.per_ticker({"AMZN": ([50.0] * 4, [100.0] * 4)}))
        assert "still funded from cash flow" in m.describe()

    def test_a_zero_aggregate_cash_flow_quarter_is_absent_not_infinite(self):
        m = build_monitor(self.per_ticker({"AMZN": ([50.0] * 4, [100.0, 0.0, 100.0, 100.0])}))
        # Q2 drops out of the company's own measurable set, so it is
        # not in the intersection at all.
        assert m.agg_ratio.get("2026Q2") is None
        assert "2026Q2" not in m.crossed_quarters

    def test_companies_are_ordered_deterministically(self):
        m = build_monitor(self.per_ticker({
            "MSFT": ([40.0] * 4, [100.0] * 4),
            "AMZN": ([60.0] * 4, [100.0] * 4)}))
        assert [c.ticker for c in m.companies] == ["AMZN", "MSFT"]

    def test_mismatched_fiscal_calendars_aggregate_on_the_calendar_quarter(self):
        # END TO END on the second trap. Microsoft files a June fiscal
        # year cumulatively; Amazon files calendar quarters discretely.
        # The only quarters they share as CALENDAR windows are the two
        # in the middle, and that is what the aggregate covers.
        msft_start = "2025-07-01"
        msft = [fact(CAPEX, msft_start, "2025-09-30", 10.0, ticker="MSFT"),
                fact(CAPEX, msft_start, "2025-12-31", 22.0, ticker="MSFT"),
                fact(CAPEX, msft_start, "2026-03-31", 36.0, ticker="MSFT"),
                fact(OCF, msft_start, "2025-09-30", 30.0, ticker="MSFT"),
                fact(OCF, msft_start, "2025-12-31", 65.0, ticker="MSFT"),
                fact(OCF, msft_start, "2026-03-31", 100.0, ticker="MSFT")]
        amzn = [fact(CAPEX, "2025-10-01", "2025-12-31", 20.0, ticker="AMZN"),
                fact(CAPEX, "2026-01-01", "2026-03-31", 25.0, ticker="AMZN"),
                fact(OCF, "2025-10-01", "2025-12-31", 40.0, ticker="AMZN"),
                fact(OCF, "2026-01-01", "2026-03-31", 50.0, ticker="AMZN")]
        m = build_monitor({"MSFT": msft, "AMZN": amzn})
        assert m.quarters == ["2025Q4", "2026Q1"]
        # 2025Q4: MSFT 22-10=12 capex, 65-30=35 ocf; AMZN 20 and 40.
        assert m.agg_capex["2025Q4"] == 32.0
        assert m.agg_ocf["2025Q4"] == 75.0
        assert m.agg_ratio["2025Q4"] == pytest.approx(32.0 / 75.0)


# =================================================================
# Bellwethers — where information enters the chain
# =================================================================
class TestBellwetherMap:

    def test_every_bellwether_is_in_the_universe(self):
        # A bellwether the desk cannot research is a name nobody can
        # follow up on.
        for ticker in BELLWETHER_TICKERS:
            assert ticker in THEME_UNIVERSE, ticker

    def test_every_edge_points_at_a_real_link(self):
        # A typo here would silently produce a read-across to nothing,
        # and the brief would name a name for no reason.
        for ticker, spec in BELLWETHERS.items():
            for link in spec["reads_across"]:
                assert link in THEME_LINKS, f"{ticker} -> {link}"

    def test_no_bellwether_steers_nothing(self):
        for ticker, spec in BELLWETHERS.items():
            assert spec["reads_across"], ticker

    def test_every_bellwether_states_a_cadence_and_a_reason(self):
        # A bellwether with no stated reason is indistinguishable from
        # a favourite stock, which is the whole thing this map is not.
        for ticker, spec in BELLWETHERS.items():
            assert spec["cadence"].strip(), ticker
            assert len(spec["why"].strip()) > 40, ticker

    def test_the_forward_and_inverse_maps_agree(self):
        # STEERED_BY is built by inversion, so this is a guard against
        # someone later hand-editing one direction.
        for ticker, spec in BELLWETHERS.items():
            for link in spec["reads_across"]:
                assert ticker in bellwethers_for(link)
        for link, tickers in STEERED_BY.items():
            for ticker in tickers:
                assert link in links_read_across(ticker)

    def test_a_non_bellwether_returns_empty_not_an_error(self):
        # "Nothing reads across from HUBB" is an answer.
        assert links_read_across("HUBB") == []
        assert links_read_across("NOTATICKER") == []

    def test_lookup_is_case_insensitive(self):
        assert links_read_across("nvda") == links_read_across("NVDA")

    def test_every_link_is_steered_by_something(self):
        # A link nothing steers is a link whose moves the desk can
        # never attribute — worth failing on rather than discovering
        # during a drawdown.
        for link in THEME_LINKS:
            assert bellwethers_for(link), f"{link} has no bellwether"

    def test_the_memory_link_is_steered_by_more_than_its_own_name(self):
        # The point of the map: MU's moves are often NVDA's news.
        steered = bellwethers_for("memory")
        assert "MU" in steered and "NVDA" in steered

    def test_the_prompt_rendering_names_the_tickers_and_the_limit(self):
        text = describe_bellwethers()
        for ticker in BELLWETHER_TICKERS:
            assert ticker in text
        assert "ONLY tickers you may name" in text


class TestDirectionalLanguageGuard:
    """The one-word-wide line between a read-across and a stock tip."""

    def test_a_factual_read_across_passes(self):
        assert directional_language(
            "Q3 earnings; compute and memory reprice off data-centre guidance"
        ) == []

    def test_past_tense_reporting_passes(self):
        # Reporting what happened is the job. Only forward-looking and
        # recommendation language is forbidden.
        for ok in ["guidance raised", "backlog fell 8%", "two orders cancelled",
                   "monthly revenue released", "bookings declined"]:
            assert directional_language(ok) == [], ok

    @pytest.mark.parametrize("bad,term", [
        ("guidance looks strong, which should support memory", "should"),
        ("likely to reprice the whole link", "likely"),
        ("we expect the backlog to convert", "expect"),
        ("the name looks cheap here", "cheap"),
        ("bullish setup into the print", "bullish"),
        ("networking benefits from this", "benefit"),
        ("clear upside from here", "upside"),
        ("a tailwind for cooling", "tailwind"),
    ])
    def test_a_forecast_is_caught(self, bad, term):
        assert term in directional_language(bad)

    def test_suffixed_forms_are_caught(self):
        # "expected" and "expects" are the same forecast as "expect".
        assert "expect" in directional_language("Expected to reprice")
        assert "expect" in directional_language("The desk expects more")

    def test_a_company_name_containing_a_term_is_not_caught(self):
        # Marvell contains "sell". A substring check would make this
        # guard useless on the one field that names companies.
        assert directional_language("Marvell reported Q3") == []

    def test_empty_and_none_are_safe(self):
        assert directional_language("") == []
        assert directional_language(None) == []


class TestTickersInProse:
    """The original prohibition, moved rather than relaxed."""

    def test_a_ticker_free_narrative_passes(self):
        assert tickers_mentioned(
            "Volatility compressed and breadth narrowed into the close.") == []

    def test_a_ticker_in_prose_is_found(self):
        assert tickers_mentioned("The complex led, with NVDA down 7%") == ["NVDA"]

    def test_several_are_all_reported(self):
        assert tickers_mentioned("NVDA and MU both fell") == ["MU", "NVDA"]

    def test_a_ticker_inside_a_longer_word_is_not_a_mention(self):
        # "MU" sits inside "MUCH", "AMD" inside "AMDAHL". A naive
        # substring check would make every brief unpublishable.
        assert tickers_mentioned("MUCH of the move was mechanical") == []
        assert tickers_mentioned("AMDAHL's law") == []

    def test_lowercase_prose_still_matches_a_ticker(self):
        # Atlas writing "nvda" rather than "NVDA" must not slip past.
        assert tickers_mentioned("nvda led the move") == ["NVDA"]
