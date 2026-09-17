"""
Tests for core/fundamentals.py and the EDGAR payload parser.

No network. The client's fetch layer is not exercised here — what is
exercised is the part that will actually break, which is the shape
handling, and it is testable precisely because `iter_facts` takes a
payload rather than a URL.
"""

import pytest

from core.clients.edgar_client import iter_facts, latest_per_period
from core.fundamentals import (
    ANNUAL_FORMS,
    CONCEPTS,
    MIN_YEARS_FOR_TREND,
    TRACKED_TAGS,
    annual_rows,
    compute_fundamentals,
    resolve_concept,
)


def fact(end, val, accn, filed, fy=None, fp="FY", form="10-K", start=None):
    f = {"end": end, "val": val, "accn": accn, "filed": filed, "fp": fp, "form": form}
    if fy is not None:
        f["fy"] = fy
    if start is not None:
        f["start"] = start
    return f


def concept_payload(tag, facts, unit="USD", cik=320193):
    """A companyconcept-shaped payload."""
    return {"cik": cik, "taxonomy": "us-gaap", "tag": tag, "units": {unit: facts}}


def facts_payload(concepts, cik=320193):
    """A companyfacts-shaped payload: {tag: {unit: [facts]}}."""
    return {
        "cik": cik, "entityName": "Test Co",
        "facts": {"us-gaap": {
            tag: {"units": units} for tag, units in concepts.items()
        }},
    }


def row(tag, fy, value, filed="2024-02-01", accn="acc-1", form="10-K", fp="FY"):
    return {"cik": "0000320193", "ticker": "TEST", "taxonomy": "us-gaap",
            "tag": tag, "unit": "USD", "fiscal_year": fy, "fiscal_period": fp,
            "period_start": None, "period_end": f"{fy}-12-31", "value": value,
            "form": form, "filed_date": filed, "accession": accn}


# =================================================================
# Parsing both payload shapes
# =================================================================
class TestParser:

    def test_companyconcept_shape(self):
        rows, stats = iter_facts(
            concept_payload("Revenues", [fact("2023-12-31", 100, "a", "2024-02-01", 2023)]),
            "TEST")
        assert len(rows) == 1
        assert rows[0]["tag"] == "Revenues"
        assert rows[0]["value"] == 100.0
        assert rows[0]["ticker"] == "TEST"
        assert stats["rows"] == 1

    def test_companyfacts_shape(self):
        rows, _ = iter_facts(facts_payload({
            "Revenues": {"USD": [fact("2023-12-31", 100, "a", "2024-02-01", 2023)]},
            "GrossProfit": {"USD": [fact("2023-12-31", 40, "a", "2024-02-01", 2023)]},
        }), "TEST")
        assert {r["tag"] for r in rows} == {"Revenues", "GrossProfit"}

    def test_cik_is_zero_padded_to_ten(self):
        rows, _ = iter_facts(
            concept_payload("Revenues", [fact("2023-12-31", 1, "a", "2024-02-01")], cik=320193),
            "TEST")
        assert rows[0]["cik"] == "0000320193"

    def test_tag_filter(self):
        rows, _ = iter_facts(facts_payload({
            "Revenues": {"USD": [fact("2023-12-31", 100, "a", "2024-02-01")]},
            "Noise": {"USD": [fact("2023-12-31", 1, "a", "2024-02-01")]},
        }), "TEST", tags=["Revenues"])
        assert {r["tag"] for r in rows} == {"Revenues"}

    def test_optional_keys_are_optional(self):
        # `start` is absent on instant facts (a balance-sheet item has
        # a date, not a period); `frame` is absent whenever the SEC has
        # not assigned one. Neither should cost the fact.
        rows, stats = iter_facts(
            concept_payload("Revenues", [fact("2023-12-31", 100, "a", "2024-02-01")]),
            "TEST")
        assert len(rows) == 1
        assert rows[0]["period_start"] is None
        assert stats["skipped_missing_keys"] == 0


class TestParserResilience:
    """One unexpected fact should cost that fact, not the company. A
    mega-cap payload carries tens of thousands of facts."""

    def test_a_fact_missing_a_required_key_is_skipped_and_counted(self):
        good = fact("2023-12-31", 100, "a", "2024-02-01")
        bad = {"end": "2022-12-31", "val": 90}          # no accn, no filed
        rows, stats = iter_facts(concept_payload("Revenues", [good, bad]), "TEST")
        assert len(rows) == 1
        assert stats["skipped_missing_keys"] == 1

    def test_a_non_numeric_value_is_skipped_not_coerced(self):
        rows, stats = iter_facts(concept_payload("Revenues", [
            fact("2023-12-31", "not a number", "a", "2024-02-01"),
        ]), "TEST")
        assert rows == []
        assert stats["skipped_unparsable"] == 1

    def test_a_non_dict_entry_is_skipped(self):
        rows, stats = iter_facts(
            concept_payload("Revenues", ["nonsense", None]), "TEST")
        assert rows == []
        assert stats["skipped_unparsable"] == 2

    def test_an_empty_payload_is_not_an_error(self):
        # EDGAR answers 404 with {} for a concept a filer never
        # reported, which is an ordinary answer.
        rows, stats = iter_facts({}, "TEST")
        assert rows == []
        assert stats["rows"] == 0


# =================================================================
# Restatements
# =================================================================
class TestRestatements:

    def test_the_later_filing_wins_for_reading(self):
        original = row("Revenues", 2022, 100, filed="2023-02-01", accn="acc-orig")
        restated = row("Revenues", 2022, 90, filed="2024-02-01", accn="acc-restated")
        latest = latest_per_period([original, restated])
        assert len(latest) == 1
        assert latest[0]["value"] == 90
        assert latest[0]["accession"] == "acc-restated"

    def test_reading_does_not_destroy_the_superseded_figure(self):
        # The whole reason accession is in the unique key: a thesis
        # formed on the original must keep its evidence.
        rows = [row("Revenues", 2022, 100, filed="2023-02-01", accn="orig"),
                row("Revenues", 2022, 90, filed="2024-02-01", accn="new")]
        latest_per_period(rows)
        assert len(rows) == 2

    def test_distinct_periods_both_survive(self):
        latest = latest_per_period([row("Revenues", 2022, 100),
                                    row("Revenues", 2023, 110)])
        assert len(latest) == 2


# =================================================================
# Annual filtering
# =================================================================
class TestAnnualFiltering:

    def test_quarters_are_excluded(self):
        # A ratio whose numerator is a quarter and denominator a year
        # is wrong by 4x and looks merely disappointing.
        rows = [row("Revenues", 2023, 100, fp="FY"),
                row("Revenues", 2023, 25, fp="Q1", form="10-Q")]
        kept = annual_rows(rows)
        assert len(kept) == 1
        assert kept[0]["fiscal_period"] == "FY"

    def test_a_full_year_on_a_quarterly_form_is_excluded(self):
        rows = [row("Revenues", 2023, 100, fp="FY", form="10-Q")]
        assert annual_rows(rows) == []

    def test_annual_forms_include_foreign_filers(self):
        assert "20-F" in ANNUAL_FORMS and "40-F" in ANNUAL_FORMS


# =================================================================
# Tag aliasing — reported, never silent
# =================================================================
class TestTagResolution:

    def test_the_first_candidate_with_data_wins(self):
        rows = [row("Revenues", 2023, 100)]
        s = resolve_concept(rows, "revenue")
        assert s.tag == "Revenues"
        assert s.measurable

    def test_preference_order_is_respected(self):
        # Both tags carry data; the ordered list decides, because the
        # two concepts are not identically defined.
        preferred = CONCEPTS["revenue"][0]
        rows = [row(preferred, 2023, 120), row("Revenues", 2023, 100)]
        assert resolve_concept(rows, "revenue").tag == preferred

    def test_the_resolved_tag_is_always_reported(self):
        # A silent fallback would make a growth rate an artefact of the
        # taxonomy rather than the business, with nothing to say so.
        s = resolve_concept([row("SalesRevenueNet", 2019, 80)], "revenue")
        assert s.tag == "SalesRevenueNet"

    def test_no_data_under_any_candidate_states_which_were_tried(self):
        s = resolve_concept([row("Something", 2023, 1)], "revenue")
        assert not s.measurable
        assert s.tag is None
        assert "Revenues" in s.unavailable

    def test_an_unknown_concept_raises(self):
        with pytest.raises(ValueError, match="unknown concept"):
            resolve_concept([], "ebitda")

    def test_every_tracked_tag_belongs_to_a_concept(self):
        assert set(TRACKED_TAGS) == {t for tags in CONCEPTS.values() for t in tags}


# =================================================================
# The derived ratios
# =================================================================
def three_year_company():
    r = []
    for fy, (rev, ni, ocf, capex, gp, oi) in {
        2021: (100, 10, 12, 5, 40, 15),
        2022: (110, 11, 14, 6, 45, 17),
        2023: (120, 12, 18, 9, 54, 21),
    }.items():
        r += [row("Revenues", fy, rev), row("NetIncomeLoss", fy, ni),
              row("NetCashProvidedByUsedInOperatingActivities", fy, ocf),
              row("PaymentsToAcquirePropertyPlantAndEquipment", fy, capex),
              row("GrossProfit", fy, gp), row("OperatingIncomeLoss", fy, oi)]
    return r


class TestRatios:

    def test_cash_conversion_is_operating_cash_over_net_income(self):
        f = compute_fundamentals("TEST", three_year_company())
        assert f.cash_conversion[2023] == pytest.approx(18 / 12)

    def test_capex_intensity_is_capex_over_revenue(self):
        f = compute_fundamentals("TEST", three_year_company())
        assert f.capex_intensity[2023] == pytest.approx(9 / 120)

    def test_reinvestment_rate_is_capex_over_operating_cash(self):
        f = compute_fundamentals("TEST", three_year_company())
        assert f.reinvestment_rate[2023] == pytest.approx(9 / 18)

    def test_margins(self):
        f = compute_fundamentals("TEST", three_year_company())
        assert f.gross_margin[2023] == pytest.approx(54 / 120)
        assert f.operating_margin[2023] == pytest.approx(21 / 120)

    def test_trend_is_in_percentage_points_first_to_last(self):
        f = compute_fundamentals("TEST", three_year_company())
        # gross margin 40% -> 45%
        assert f.trend("gross_margin") == pytest.approx(5.0)

    def test_years_are_the_union_so_a_gap_is_a_hole_not_a_truncation(self):
        rows = three_year_company() + [row("Revenues", 2024, 130)]
        f = compute_fundamentals("TEST", rows)
        assert f.years == [2021, 2022, 2023, 2024]
        assert f.gross_margin[2024] is None      # not reported that year


class TestRatioRefusals:

    def test_a_missing_denominator_gives_none_not_zero(self):
        rows = [row("PaymentsToAcquirePropertyPlantAndEquipment", 2023, 9)]
        f = compute_fundamentals("TEST", rows)
        assert f.capex_intensity[2023] is None

    def test_a_zero_denominator_gives_none(self):
        # Real for a pre-commercial filer, not hypothetical.
        rows = [row("Revenues", 2023, 0), row("GrossProfit", 2023, 0)]
        assert compute_fundamentals("TEST", rows).gross_margin[2023] is None

    def test_one_year_yields_no_trend(self):
        rows = [row("Revenues", 2023, 100), row("GrossProfit", 2023, 40)]
        assert compute_fundamentals("TEST", rows).trend("gross_margin") is None
        assert MIN_YEARS_FOR_TREND == 2

    def test_nothing_stored_says_to_sync(self):
        f = compute_fundamentals("AAPL", [])
        assert not f.measurable
        assert "sync AAPL" in f.unavailable

    def test_facts_with_no_annual_figures_says_so(self):
        f = compute_fundamentals("TEST", [row("Revenues", 2023, 1, fp="Q1", form="10-Q")])
        assert not f.measurable
        assert "none are full-year" in f.unavailable

    def test_describe_renders_a_dash_rather_than_a_number_it_lacks(self):
        f = compute_fundamentals("TEST", [row("Revenues", 2023, 100),
                                          row("Revenues", 2022, 90)])
        text = f.describe()
        assert "inputs not reported" in text     # no margins available
        assert "TEST" in text


class TestShareCount:

    def test_a_shrinking_count_is_negative(self):
        rows = [row("WeightedAverageNumberOfDilutedSharesOutstanding", 2021, 1000),
                row("WeightedAverageNumberOfDilutedSharesOutstanding", 2023, 900)]
        f = compute_fundamentals("TEST", rows)
        assert f.share_count_change_pct == pytest.approx(-10.0)

    def test_dilution_is_positive(self):
        rows = [row("WeightedAverageNumberOfDilutedSharesOutstanding", 2021, 1000),
                row("WeightedAverageNumberOfDilutedSharesOutstanding", 2023, 1100)]
        assert compute_fundamentals("TEST", rows).share_count_change_pct == pytest.approx(10.0)

    def test_one_observation_gives_no_change(self):
        rows = [row("WeightedAverageNumberOfDilutedSharesOutstanding", 2023, 1000)]
        assert compute_fundamentals("TEST", rows).share_count_change_pct is None
