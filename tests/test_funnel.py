"""
Tests for core/funnel.py.

No database and no network, in keeping with the rest of the suite —
every assertion here runs against compute_funnel(), which takes plain
counts. That is the whole reason the pure/DB split exists.
"""

from datetime import date

import pytest

from core.funnel import (
    DEFAULT_LOOKBACK_DAYS,
    STAGES,
    Funnel,
    compute_funnel,
    _render,
)

START = date(2026, 8, 13)
END = date(2026, 9, 11)


def _counts(**overrides) -> dict:
    """A healthy funnel, with any stage overridable."""
    base = {
        "candidates": 40,
        "proposals": 20,
        "reviewed": 20,
        "approved": 16,
        "allocated": 16,
        "order_rows": 16,
        "submitted": 14,
        "filled": 9,
    }
    base.update(overrides)
    return base


# =================================================================
# Shape
# =================================================================
class TestShape:

    def test_every_stage_is_present_and_in_pipeline_order(self):
        result = compute_funnel(_counts(), START, END)
        assert [s.key for s in result.stages] == [k for k, _, _ in STAGES]

    def test_counts_are_carried_through_unchanged(self):
        result = compute_funnel(_counts(), START, END)
        assert result.stages[0].count == 40
        assert result.stages[-1].count == 9

    def test_a_missing_key_counts_as_zero(self):
        # Absent means "the query found nothing", which is genuinely
        # zero for a row count — unlike a missing price, which is not.
        result = compute_funnel({"candidates": 5}, START, END)
        assert result.stages[0].count == 5
        assert all(s.count == 0 for s in result.stages[1:])

    def test_every_stage_carries_a_diagnosis(self):
        # The diagnosis is the product. A stage without one would
        # report a collapse and not say what it means.
        result = compute_funnel(_counts(), START, END)
        assert all(s.diagnosis.strip() for s in result.stages)


# =================================================================
# Conversion arithmetic
# =================================================================
class TestConversion:

    def test_first_stage_has_no_conversion(self):
        result = compute_funnel(_counts(), START, END)
        assert result.stages[0].conversion_pct is None

    def test_conversion_is_against_the_previous_stage(self):
        result = compute_funnel(_counts(), START, END)
        # proposals 20 of candidates 40
        assert result.stages[1].conversion_pct == pytest.approx(50.0)
        # approved 16 of reviewed 20
        assert result.stages[3].conversion_pct == pytest.approx(80.0)

    def test_zero_denominator_is_none_not_zero(self):
        # THE point of this test: 0/0 is undefined. Reporting it as
        # 0.0% would say "this stage refuses everything" when the
        # truth is that nothing arrived.
        result = compute_funnel(
            _counts(proposals=0, reviewed=0, approved=0), START, END)
        reviewed = next(s for s in result.stages if s.key == "reviewed")
        assert reviewed.conversion_pct is None

    def test_full_conversion_is_a_hundred_percent(self):
        result = compute_funnel(_counts(reviewed=20, approved=20), START, END)
        approved = next(s for s in result.stages if s.key == "approved")
        assert approved.conversion_pct == pytest.approx(100.0)

    def test_end_to_end_is_last_over_first(self):
        result = compute_funnel(_counts(), START, END)
        assert result.end_to_end_pct == pytest.approx(22.5)  # 9 / 40


# =================================================================
# first_break — the answer the module exists to give
# =================================================================
class TestFirstBreak:

    def test_healthy_funnel_has_no_break(self):
        assert compute_funnel(_counts(), START, END).first_break is None

    def test_break_is_the_first_stage_that_got_input_and_produced_none(self):
        result = compute_funnel(
            _counts(allocated=0, order_rows=0, submitted=0, filled=0),
            START, END)
        assert result.first_break.key == "allocated"

    def test_break_reports_the_first_one_not_the_last(self):
        result = compute_funnel(
            {"candidates": 10, "proposals": 0, "reviewed": 0, "approved": 0,
             "allocated": 0, "order_rows": 0, "submitted": 0, "filled": 0},
            START, END)
        assert result.first_break.key == "proposals"

    def test_a_stage_after_an_empty_stage_is_not_a_break(self):
        # Once the pipeline is dry, every later stage is zero too.
        # Reporting each of them as a separate failure would bury the
        # one that matters.
        result = compute_funnel(
            {"candidates": 0, "proposals": 0, "filled": 0}, START, END)
        assert result.first_break is None

    def test_ada_refusing_locally_breaks_at_submitted(self):
        result = compute_funnel(
            _counts(submitted=0, filled=0), START, END)
        assert result.first_break.key == "submitted"
        assert "refusing locally" in result.first_break.diagnosis

    def test_orders_reaching_the_venue_but_never_filling_breaks_at_filled(self):
        result = compute_funnel(_counts(filled=0), START, END)
        assert result.first_break.key == "filled"
        assert "never fill" in result.first_break.diagnosis


# =================================================================
# measurable / describe
# =================================================================
class TestMeasurable:

    def test_no_candidates_means_nothing_to_measure(self):
        result = compute_funnel(
            {k: 0 for k in [s[0] for s in STAGES]}, START, END)
        assert result.measurable is False
        assert result.end_to_end_pct is None
        assert "no input" in result.describe()

    def test_describe_names_the_breaking_stage(self):
        result = compute_funnel(_counts(approved=0, allocated=0,
                                        order_rows=0, submitted=0, filled=0),
                                START, END)
        assert "Stops at: Approved" in result.describe()

    def test_describe_says_so_when_nothing_broke(self):
        assert "No stage went to zero." in compute_funnel(
            _counts(), START, END).describe()

    def test_describe_carries_the_window(self):
        text = compute_funnel(_counts(), START, END).describe()
        assert str(START) in text and str(END) in text


# =================================================================
# The current book — a regression lock on the case that prompted this
# =================================================================
class TestNinetyNinePercentCash:

    def test_research_produces_nothing(self):
        """No candidates at all: the funnel says so instead of
        reporting a 0% conversion that implies something was tried."""
        result = compute_funnel({"candidates": 0}, START, END)
        assert result.measurable is False
        assert result.first_break is None

    def test_ideas_that_never_become_proposals(self):
        """Candidates found, Solomon never acts. This is the shape the
        framework predicted is most likely."""
        result = compute_funnel({"candidates": 22}, START, END)
        assert result.measurable is True
        assert result.first_break.key == "proposals"
        assert "action_needed" in result.first_break.diagnosis

    def test_risk_is_not_blamed_when_it_approved_everything(self):
        """Nora approved all 12 and the pipeline still died at Ada.
        The break must not be attributed to the risk gate."""
        result = compute_funnel(
            _counts(candidates=30, proposals=12, reviewed=12, approved=12,
                    allocated=12, order_rows=12, submitted=0, filled=0),
            START, END)
        assert result.first_break.key == "submitted"


# =================================================================
# Rendering
# =================================================================
class TestRender:

    def test_a_real_zero_and_an_undefined_one_render_differently(self):
        # The distinction this module is built around, at the point a
        # human reads it. With 4 candidates and no proposals:
        #   Proposed  0   0.0%   <- DEFINED. Four ideas, none acted on.
        #   Reviewed  0    —     <- UNDEFINED. Nothing arrived to review.
        # Collapsing the second into "0.0%" would accuse Nora of
        # refusing everything when she was never asked.
        result = compute_funnel({"candidates": 4}, START, END)
        lines = {line.split()[0]: line for line in _render(result).splitlines()
                 if line.startswith("  ") and line.strip()}
        assert "0.0%" in lines["Proposed"]
        assert "—" in lines["Reviewed"]
        assert "0.0%" not in lines["Reviewed"]

    def test_local_refusals_are_listed_commonest_first(self):
        result = compute_funnel(
            _counts(submitted=1),
            START, END,
            local_refusals={"rejected_stale_ledger": 2,
                            "halted_by_operator": 9},
        )
        text = _render(result)
        assert text.index("halted_by_operator") < text.index("rejected_stale_ledger")

    def test_refusal_block_is_omitted_when_there_are_none(self):
        text = _render(compute_funnel(_counts(), START, END))
        assert "Never reached the broker" not in text

    def test_every_stage_label_appears(self):
        text = _render(compute_funnel(_counts(), START, END))
        for _, label, _ in STAGES:
            assert label in text


# =================================================================
# Constants
# =================================================================
class TestDefaults:

    def test_lookback_is_about_twenty_sessions(self):
        # Below roughly twenty sessions a single quiet week reads as a
        # collapse, so the default must not be shorter.
        assert DEFAULT_LOOKBACK_DAYS >= 28

    def test_stage_keys_are_unique(self):
        keys = [k for k, _, _ in STAGES]
        assert len(keys) == len(set(keys))
