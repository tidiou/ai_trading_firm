"""
Tests for core/thesis.py — the verdict derived from assumption checks.

Entirely pure: no database, no model, no clock. That is the point of
the module. The verdict is the one thing in this change that decides
something, so it is the one thing that must not be able to disagree
with its own evidence.
"""

import pytest

from core.thesis import (
    ASSUMPTION_STATUSES,
    VERDICTS,
    AssumptionCheck,
    compute_verdict,
    verdict_transition,
)


def chk(status: str, claim: str = "a claim", load_bearing: bool = True) -> AssumptionCheck:
    return AssumptionCheck(claim=claim, status=status, is_load_bearing=load_bearing)


# =================================================================
# Vocabulary
# =================================================================
class TestVocabulary:

    def test_an_unknown_status_is_refused_at_construction(self):
        # Fail where the typo is, not three layers later in a verdict
        # that quietly treated it as "holding".
        with pytest.raises(ValueError, match="unknown assumption status"):
            AssumptionCheck(claim="c", status="improved")

    def test_every_status_is_constructible(self):
        for s in ASSUMPTION_STATUSES:
            assert AssumptionCheck(claim="c", status=s).status == s

    def test_strengthened_exists_at_all(self):
        # The hole this change was built to fill: the old
        # intact|at_risk|broken vocabulary could not say "better".
        assert "strengthened" in VERDICTS


# =================================================================
# Precedence
# =================================================================
class TestPrecedence:

    def test_load_bearing_break_is_broken(self):
        v = compute_verdict([chk("holding"), chk("broken", "margins hold", True)])
        assert v.verdict == "broken"
        assert "margins hold" in v.reason

    def test_non_load_bearing_break_is_only_weakened(self):
        # Without this distinction every thesis reads `broken` within a
        # month, for reasons nobody considered material.
        v = compute_verdict([chk("holding"), chk("broken", "minor claim", False)])
        assert v.verdict == "weakened"

    def test_strained_is_weakened(self):
        assert compute_verdict([chk("holding"), chk("strained")]).verdict == "weakened"

    def test_broken_beats_improving(self):
        # A thesis with one failed load-bearing claim is not
        # strengthened by good news elsewhere.
        v = compute_verdict([chk("improving"), chk("broken", "core claim", True)])
        assert v.verdict == "broken"

    def test_strained_beats_improving(self):
        v = compute_verdict([chk("improving"), chk("strained")])
        assert v.verdict == "weakened"

    def test_improving_with_nothing_against_is_strengthened(self):
        v = compute_verdict([chk("holding"), chk("improving", "pricing power")])
        assert v.verdict == "strengthened"
        assert "pricing power" in v.reason

    def test_all_holding_is_unchanged(self):
        assert compute_verdict([chk("holding"), chk("holding")]).verdict == "unchanged"

    def test_precedence_is_total(self):
        # One check of each kind: the worst must win.
        v = compute_verdict([chk("improving"), chk("strained"),
                             chk("broken", "c", True), chk("holding"),
                             chk("unchecked")])
        assert v.verdict == "broken"


# =================================================================
# The refusals
# =================================================================
class TestRefusals:

    def test_no_assumptions_means_no_verdict_not_unchanged(self):
        # Every thesis opened before migration 011 is in this state.
        # Calling them `unchanged` would assert their claims were
        # checked and held, which never happened.
        v = compute_verdict([])
        assert v.verdict is None
        assert v.measurable is False
        assert "no assumptions on record" in v.reason
        assert "No verdict" in v.describe()

    def test_all_unchecked_is_unchanged_but_says_why(self):
        # Honest: the thesis stands, but nobody re-verified it. A
        # reader must be able to tell that from an actively confirmed
        # day.
        v = compute_verdict([chk("unchecked"), chk("unchecked")])
        assert v.verdict == "unchanged"
        assert v.checked == 0
        assert "no information bearing on any assumption" in v.reason

    def test_confirmed_unchanged_reads_differently_from_quiet_unchanged(self):
        quiet = compute_verdict([chk("unchecked")])
        confirmed = compute_verdict([chk("holding")])
        assert quiet.verdict == confirmed.verdict == "unchanged"
        assert quiet.reason != confirmed.reason


# =================================================================
# What the verdict carries with it
# =================================================================
class TestEvidence:

    def test_counts_omit_statuses_nobody_used(self):
        v = compute_verdict([chk("holding"), chk("holding"), chk("strained")])
        assert v.counts == {"holding": 2, "strained": 1}

    def test_checked_excludes_unchecked(self):
        v = compute_verdict([chk("holding"), chk("unchecked"), chk("improving")])
        assert v.checked == 2
        assert v.total == 3

    def test_named_claims_are_carried_for_each_movement(self):
        v = compute_verdict([
            chk("broken", "b1", False), chk("strained", "s1"),
            chk("improving", "i1"), chk("holding", "h1"),
        ])
        assert v.broken_claims == ["b1"]
        assert v.strained_claims == ["s1"]
        assert v.improved_claims == ["i1"]

    def test_describe_states_the_sample(self):
        text = compute_verdict([chk("holding"), chk("unchecked")]).describe()
        assert "UNCHANGED" in text
        assert "1 of 2 assumption(s) checked" in text

    def test_the_verdict_is_reproducible_from_its_own_inputs(self):
        # The stored assumption_checks must be enough to re-derive the
        # stored verdict — that is what makes a verdict auditable
        # rather than merely recorded.
        checks = [chk("holding"), chk("strained", "s")]
        assert compute_verdict(checks).verdict == compute_verdict(checks).verdict


# =================================================================
# Transitions — the calibration unit
# =================================================================
class TestTransition:

    def test_same_verdict_held(self):
        assert verdict_transition("unchanged", "unchanged") == "held"

    def test_moving_up_the_scale_improved(self):
        assert verdict_transition("weakened", "unchanged") == "improved"
        assert verdict_transition("unchanged", "strengthened") == "improved"

    def test_moving_down_deteriorated(self):
        assert verdict_transition("strengthened", "unchanged") == "deteriorated"
        assert verdict_transition("weakened", "broken") == "deteriorated"

    def test_nothing_to_compare_gives_none(self):
        assert verdict_transition(None, "unchanged") is None
        assert verdict_transition("unchanged", None) is None

    def test_a_thesis_walking_down_over_weeks_is_visible(self):
        # The case that makes monitoring worth having: lead time.
        path = ["unchanged", "weakened", "weakened", "broken"]
        moves = [verdict_transition(a, b) for a, b in zip(path, path[1:])]
        assert moves == ["deteriorated", "held", "deteriorated"]


# =================================================================
# The asymmetry this change exists to fix
# =================================================================
class TestUpgradePath:

    def test_a_thesis_can_now_improve(self):
        """Under intact|at_risk|broken there was no way to record this,
        so Vera could only ever downgrade or hold — the loop drifted
        pessimistic and could never justify adding to a winner."""
        v = compute_verdict([chk("improving", "unit economics"), chk("holding")])
        assert v.verdict == "strengthened"

    def test_recovery_from_weakened_is_expressible(self):
        assert verdict_transition("weakened", "strengthened") == "improved"
