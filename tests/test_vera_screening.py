"""
Tests for Vera's screening summary — the statement she now makes on the
days she surfaces nothing.

No database and no model calls: everything here exercises the three pure
functions (partition_screened, screening_summary, describe_screening)
plus the output contract's defaults. That split is the whole point —
the surfacing decision was moved out of the model's response and into
code precisely so it could be tested.
"""

import pytest

from agents.vera import (
    SURFACING_THRESHOLD,
    NewCandidateOutput,
    describe_screening,
    partition_screened,
    screening_summary,
)

UNIVERSE = ["MSFT", "GOOGL", "AMZN", "NVDA", "JPM"]
HELD = ["AAPL"]


def name(ticker: str, score: int, catalyst: str = "a named catalyst",
         thesis: str = "a thesis") -> NewCandidateOutput:
    return NewCandidateOutput(
        ticker=ticker, thesis=thesis, conviction_score=score, catalyst=catalyst,
    )


def quiet_day() -> list[NewCandidateOutput]:
    """Every name screened, nothing close. The case that prompted this."""
    return [name("MSFT", 2, catalyst=""), name("GOOGL", 2, catalyst=""),
            name("AMZN", 1, catalyst=""), name("NVDA", 3, catalyst=""),
            name("JPM", 2, catalyst="")]


def summarise(screened, universe=None, held=None):
    universe = UNIVERSE if universe is None else universe
    held = HELD if held is None else held
    surfaced, _ = partition_screened(screened)
    s = screening_summary(universe=universe, screened=screened,
                          surfaced=surfaced, held_tickers=held)
    s["statement"] = describe_screening(s)
    return s


# =================================================================
# The output contract
# =================================================================
class TestContract:

    def test_low_conviction_name_needs_only_ticker_thesis_and_score(self):
        # A 2 should not cost a full valuation workup that is then
        # discarded — that was the reason scores below the bar were
        # never reported at all.
        item = NewCandidateOutput(ticker="JPM", thesis="Fairly valued.",
                                  conviction_score=2)
        assert item.catalyst == ""
        assert item.key_risks == []
        assert item.valuation_snapshot == {}

    def test_default_collections_are_not_shared_between_instances(self):
        # The classic mutable-default bug. Field(default_factory=...)
        # is what prevents it; this test is what notices if someone
        # "simplifies" it back to `= []`.
        a = NewCandidateOutput(ticker="A", thesis="t", conviction_score=1)
        b = NewCandidateOutput(ticker="B", thesis="t", conviction_score=1)
        a.key_risks.append("leaked")
        assert b.key_risks == []


# =================================================================
# partition_screened — the surfacing decision, now in code
# =================================================================
class TestPartition:

    def test_at_or_above_threshold_with_a_catalyst_is_surfaced(self):
        surfaced, held = partition_screened([name("NVDA", SURFACING_THRESHOLD)])
        assert [s.ticker for s in surfaced] == ["NVDA"]
        assert held == []

    def test_one_below_the_threshold_is_held_back(self):
        surfaced, held = partition_screened([name("NVDA", SURFACING_THRESHOLD - 1)])
        assert surfaced == []
        assert [h.ticker for h in held] == ["NVDA"]

    def test_high_conviction_without_a_catalyst_is_held_back(self):
        # The prompt says a 4 must name its catalyst. A 4 that doesn't
        # has failed its own standard, and surfacing it would hand
        # Solomon a proposal whose linked_trigger can name nothing.
        surfaced, held = partition_screened([name("NVDA", 5, catalyst="")])
        assert surfaced == []
        assert [h.ticker for h in held] == ["NVDA"]

    def test_whitespace_is_not_a_catalyst(self):
        surfaced, _ = partition_screened([name("NVDA", 5, catalyst="   ")])
        assert surfaced == []

    def test_threshold_matches_solomons_new_position_bar(self):
        # If these ever diverge, Vera surfaces rows Solomon can only
        # reject. Locked down deliberately.
        assert SURFACING_THRESHOLD == 4

    def test_partition_preserves_every_name(self):
        screened = quiet_day() + [name("MSFT", 5)]
        surfaced, held = partition_screened(screened)
        assert len(surfaced) + len(held) == len(screened)

    def test_empty_screen_partitions_to_two_empties(self):
        assert partition_screened([]) == ([], [])


# =================================================================
# screening_summary — every field computed, none narrated
# =================================================================
class TestSummary:

    def test_best_conviction_reports_a_name_that_was_not_surfaced(self):
        # THE point of the change. NVDA scored 3, never became a row,
        # and previously vanished without trace.
        s = summarise(quiet_day())
        assert s["surfaced_count"] == 0
        assert s["best_conviction"] == 3
        assert s["best_conviction_ticker"] == "NVDA"

    def test_best_conviction_is_none_when_nothing_was_screened(self):
        # None, not 0. "No read on anything" and "everything scored
        # zero" are different claims and zero is not even a valid score.
        s = summarise([])
        assert s["best_conviction"] is None
        assert s["best_conviction_ticker"] is None

    def test_counts_and_tickers_agree(self):
        s = summarise(quiet_day() + [name("MSFT", 4)])
        assert s["screened_count"] == 6
        assert s["surfaced_count"] == 1
        assert s["surfaced_tickers"] == ["MSFT"]
        assert s["held_back_count"] == 5

    def test_held_tickers_are_reported_and_sorted(self):
        s = summarise(quiet_day(), held=["NVDA", "AAPL"])
        assert s["excluded_as_held"] == ["AAPL", "NVDA"]

    def test_a_name_the_model_ignored_is_named_not_just_counted(self):
        # Which ticker went missing is the actionable half.
        s = summarise(quiet_day()[:3])
        assert s["not_returned"] == ["NVDA", "JPM"]

    def test_full_coverage_leaves_not_returned_empty(self):
        assert summarise(quiet_day())["not_returned"] == []

    def test_distribution_omits_scores_nobody_gave(self):
        s = summarise(quiet_day())
        assert s["conviction_distribution"] == {"1": 1, "2": 3, "3": 1}
        assert "4" not in s["conviction_distribution"]

    def test_threshold_is_carried_so_a_reader_can_judge_the_gap(self):
        assert summarise(quiet_day())["threshold"] == SURFACING_THRESHOLD

    def test_summary_has_no_free_text_field_of_its_own(self):
        # Deliberate: asking the model to explain an empty result
        # invites confabulation and creates pressure to find something
        # to say. Every field is derived. `statement` is added
        # afterwards by describe_screening, from these numbers only.
        s = screening_summary(UNIVERSE, quiet_day(), [], HELD)
        assert "narrative" not in s
        assert "reasoning" not in s
        assert "statement" not in s


# =================================================================
# describe_screening — the one line a human reads
# =================================================================
class TestStatement:

    def test_a_quiet_day_says_how_close_it_came(self):
        text = summarise(quiet_day())["statement"]
        assert "Nothing surfaced" in text
        assert "Best conviction 3 (NVDA)" in text
        assert "bar of 4" in text

    def test_it_names_what_was_excluded_as_held(self):
        assert "AAPL held" in summarise(quiet_day())["statement"]

    def test_a_productive_day_names_what_was_surfaced(self):
        text = summarise(quiet_day() + [name("MSFT", 5)])["statement"]
        assert "Surfaced 1: MSFT" in text
        assert "Nothing surfaced" not in text

    def test_an_empty_pass_is_called_a_failure_not_a_quiet_day(self):
        # The distinction the whole change exists to make.
        text = summarise([])["statement"]
        assert "failed pass" in text
        assert "Nothing surfaced" not in text

    def test_a_partial_pass_flags_the_names_it_never_read(self):
        text = summarise(quiet_day()[:2])["statement"]
        assert "no read on" in text
        assert "AMZN" in text

    def test_statement_mentions_the_screened_count(self):
        assert "5 of 5 screened" in summarise(quiet_day())["statement"]


# =================================================================
# The case from 14 Sept 2026 — a regression lock
# =================================================================
class TestTheDayThatPromptedThis:

    def test_interesting_macro_and_no_thesis_is_now_explicable(self):
        """Atlas produced a notable brief, Vera issued no thesis, and
        nothing on record said whether anything came close. After this
        change the same day answers the question itself."""
        s = summarise(quiet_day())
        assert s["surfaced_count"] == 0          # same trading behaviour
        assert s["screened_count"] == 5          # she did do the work
        assert s["best_conviction"] == 3         # and it was one notch short
        assert "Best conviction 3" in s["statement"]

    def test_the_universe_being_too_narrow_looks_different_from_a_high_bar(self):
        # Clustering at 1-2 means no threshold change helps; clustering
        # at 3-4 is a calibration question. The distribution is what
        # separates them, so it must survive a day with no candidates.
        barren = [name(t, 1, catalyst="") for t in UNIVERSE]
        s = summarise(barren)
        assert s["best_conviction"] == 1
        assert s["conviction_distribution"] == {"1": 5}
        assert s["surfaced_count"] == 0
