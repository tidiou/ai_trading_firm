"""
Tests for Vera's assumption matching and the verdict it produces.

`_match_checks` is the seam between a model's free text and a
computed verdict, so it is where a quiet failure would do the most
damage: a claim silently dropped shrinks the denominator and makes a
thesis look better verified than it was.

No database, no model calls — the matcher takes stored rows and
reported checks, which is why it is testable at all.
"""

from datetime import date

import pytest

from agents.vera import AssumptionCheckOut, _format_assumptions, _match_checks
from core.models import ThesisAssumption
from core.thesis import compute_verdict

TODAY = date(2026, 9, 15)


def stored(claim: str, load_bearing: bool = True, status: str = "holding",
           aid: int = 1, metric=None, threshold=None, direction=None) -> ThesisAssumption:
    return ThesisAssumption(
        id=aid, thesis_id=1, claim=claim, is_load_bearing=load_bearing,
        status=status, opened_date=TODAY, metric=metric,
        falsify_threshold=threshold, direction=direction)


def reported(claim: str, status: str, evidence: str = "") -> AssumptionCheckOut:
    return AssumptionCheckOut(claim=claim, status=status, evidence=evidence)


# =================================================================
# Matching
# =================================================================
class TestMatching:

    def test_an_exact_claim_matches(self):
        checks, unmatched = _match_checks(
            [stored("cloud growth stays above 20%")],
            [reported("cloud growth stays above 20%", "holding", "Q3 at 23%")])
        assert len(checks) == 1
        assert checks[0].status == "holding"
        assert checks[0].evidence == "Q3 at 23%"
        assert unmatched == []

    def test_load_bearing_comes_from_the_stored_row_not_the_model(self):
        # Whether a claim is load-bearing was decided when the thesis
        # was decomposed. The model does not get to revise it daily,
        # because that is exactly how every claim becomes critical on
        # a bad day.
        checks, _ = _match_checks(
            [stored("minor claim", load_bearing=False)],
            [reported("minor claim", "broken")])
        assert checks[0].is_load_bearing is False
        assert compute_verdict(checks).verdict == "weakened"

    def test_the_assumption_id_is_carried_so_status_can_be_written_back(self):
        checks, _ = _match_checks([stored("c", aid=99)], [reported("c", "improving")])
        assert checks[0].assumption_id == 99


class TestUnreportedClaims:
    """The failure this function exists to prevent."""

    def test_a_claim_the_model_ignored_becomes_unchecked_not_dropped(self):
        checks, _ = _match_checks(
            [stored("claim A", aid=1), stored("claim B", aid=2)],
            [reported("claim A", "holding")])
        assert len(checks) == 2
        by_claim = {c.claim: c.status for c in checks}
        assert by_claim["claim B"] == "unchecked"

    def test_dropping_it_would_have_overstated_the_verification(self):
        # With the claim dropped the verdict would read "1 of 1
        # checked"; kept as unchecked it correctly reads 1 of 2.
        checks, _ = _match_checks(
            [stored("claim A", aid=1), stored("claim B", aid=2)],
            [reported("claim A", "holding")])
        v = compute_verdict(checks)
        assert (v.checked, v.total) == (1, 2)

    def test_every_claim_unreported_yields_no_false_confirmation(self):
        checks, _ = _match_checks([stored("a", aid=1), stored("b", aid=2)], [])
        v = compute_verdict(checks)
        assert v.verdict == "unchanged"
        assert v.checked == 0
        assert "no information bearing" in v.reason


class TestDriftedWording:

    def test_a_reported_claim_that_matches_nothing_is_surfaced(self):
        # Almost always the model paraphrased. Reported rather than
        # swallowed, because tomorrow it will paraphrase again.
        checks, unmatched = _match_checks(
            [stored("cloud growth stays above 20%")],
            [reported("cloud growth remains above twenty percent", "holding")])
        assert unmatched == ["cloud growth remains above twenty percent"]

    def test_and_the_stored_claim_is_then_unchecked_not_guessed(self):
        checks, _ = _match_checks(
            [stored("cloud growth stays above 20%")],
            [reported("something else entirely", "broken")])
        assert checks[0].status == "unchecked"
        # Critically NOT broken — a paraphrase must never be able to
        # break a thesis by accident.
        assert compute_verdict(checks).verdict == "unchanged"

    def test_partial_overlap_matches_only_the_exact_one(self):
        checks, unmatched = _match_checks(
            [stored("claim A", aid=1), stored("claim B", aid=2)],
            [reported("claim A", "improving"), reported("claim C", "broken")])
        assert {c.claim: c.status for c in checks} == {
            "claim A": "improving", "claim B": "unchecked"}
        assert unmatched == ["claim C"]


# =================================================================
# The empty case
# =================================================================
class TestNoAssumptions:

    def test_a_thesis_with_no_claims_produces_no_checks(self):
        checks, unmatched = _match_checks([], [])
        assert checks == []

    def test_and_therefore_no_verdict(self):
        # Every thesis that predates migration 011 is in this state
        # until Vera authors claims for it.
        checks, _ = _match_checks([], [])
        v = compute_verdict(checks)
        assert v.verdict is None
        assert "no assumptions on record" in v.reason


# =================================================================
# The prompt rendering
# =================================================================
class TestFormatting:

    def test_the_claim_is_rendered_verbatim_so_it_can_be_echoed_back(self):
        text = _format_assumptions([stored("cloud growth stays above 20%")])
        assert "cloud growth stays above 20%" in text

    def test_the_threshold_is_shown_because_broken_depends_on_it(self):
        text = _format_assumptions([stored(
            "growth holds", threshold="two consecutive quarters below 15%")])
        assert "falsified if: two consecutive quarters below 15%" in text

    def test_load_bearing_and_last_status_are_both_shown(self):
        text = _format_assumptions([stored("c", load_bearing=False, status="strained")])
        assert "load-bearing: no" in text
        assert "last status: strained" in text

    def test_the_metric_carries_its_direction(self):
        text = _format_assumptions([stored("c", metric="gross margin", direction="up")])
        assert "gross margin" in text and "up supports it" in text

    def test_claims_are_numbered(self):
        text = _format_assumptions([stored("first", aid=1), stored("second", aid=2)])
        assert "1. first" in text and "2. second" in text

    def test_an_empty_list_says_so_rather_than_rendering_nothing(self):
        # A blank section would read as "no claims were shown", which
        # is indistinguishable from a formatting bug.
        assert "no assumptions on record" in _format_assumptions([])


# =================================================================
# End to end, through the pure layer
# =================================================================
class TestRealisticDays:

    def test_a_quiet_day_on_a_decomposed_thesis(self):
        rows = [stored("growth holds", aid=1), stored("margins hold", aid=2),
                stored("buybacks continue", aid=3, load_bearing=False)]
        checks, _ = _match_checks(rows, [
            reported("growth holds", "unchecked"),
            reported("margins hold", "unchecked"),
            reported("buybacks continue", "unchecked")])
        v = compute_verdict(checks)
        assert v.verdict == "unchanged"
        assert v.checked == 0

    def test_an_earnings_day_that_improves_the_thesis(self):
        rows = [stored("growth holds", aid=1), stored("margins hold", aid=2)]
        checks, _ = _match_checks(rows, [
            reported("growth holds", "improving", "Q3 accelerated to 26%"),
            reported("margins hold", "holding", "flat at 44%")])
        v = compute_verdict(checks)
        assert v.verdict == "strengthened"
        assert v.improved_claims == ["growth holds"]

    def test_the_day_a_thesis_breaks(self):
        rows = [stored("growth stays above 20%", aid=1),
                stored("buybacks continue", aid=2, load_bearing=False)]
        checks, _ = _match_checks(rows, [
            reported("growth stays above 20%", "broken", "second quarter at 12%"),
            reported("buybacks continue", "holding")])
        v = compute_verdict(checks)
        assert v.verdict == "broken"
        assert "growth stays above 20%" in v.reason
