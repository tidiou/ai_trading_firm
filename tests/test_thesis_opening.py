"""
Tests for Marcus opening a thesis at allocation (S4 groundwork).

WHY THIS MATTERS ENOUGH TO TEST CAREFULLY. Before this, `theses` was
written in exactly one place in the whole codebase — Vera's
orphan-adoption path. Nothing in the pipeline opened one, so every
thesis on record existed because a position turned up UNEXPLAINED and
was adopted after the fact with a conviction invented at adoption time
while the real one sat in new_candidates from days earlier. Daily
monitoring covered only adopted orphans and attribution.thesis_id was
null for everything else.

Follows the FakeSession pattern from test_kill_switch.py: no database,
hand-built ORM objects, the branching exercised directly.
"""

from datetime import date

import pytest

from agents.marcus import open_thesis_if_needed
from core.models import NewCandidate, Thesis

TODAY = date(2026, 9, 15)


class FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *criteria):
        # The real filters are SQLAlchemy expressions; matching them
        # properly would mean reimplementing the ORM. Instead the
        # fixture is seeded with exactly the rows a query should see,
        # which is what the existing suite does.
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class FakeSession:
    def __init__(self, by_model: dict):
        self.by_model = by_model
        self.added = []
        self.flushed = 0

    def query(self, model):
        return FakeQuery(self.by_model.get(model, []))

    def add(self, row):
        self.added.append(row)

    def flush(self):
        self.flushed += 1
        for row in self.added:
            if getattr(row, "id", None) is None:
                row.id = 42


def candidate(**over) -> NewCandidate:
    fields = dict(
        candidate_date=TODAY, ticker="MSFT",
        thesis="Cloud reacceleration with operating leverage.",
        catalyst="Q4 guidance raise",
        conviction_score=4,
        key_risks=["capex cycle"], valuation_snapshot={"pe": 31.2},
        sector="Technology",
        expected_direction="up", expected_move_pct=18.0, horizon_days=120,
    )
    fields.update(over)
    return NewCandidate(**fields)


def open_thesis(ticker="MSFT") -> Thesis:
    return Thesis(id=7, ticker=ticker, opened_date=date(2026, 8, 1),
                  thesis_text="already believed", original_conviction=4)


# =================================================================
# The happy path — provenance carried, nothing invented
# =================================================================
class TestOpensFromTheCandidate:

    def test_a_thesis_is_created(self):
        s = FakeSession({Thesis: [], NewCandidate: [candidate()]})
        tid = open_thesis_if_needed(s, TODAY, "MSFT")
        assert tid == 42
        assert len(s.added) == 1
        assert isinstance(s.added[0], Thesis)

    def test_the_text_comes_from_the_candidate_not_from_nowhere(self):
        c = candidate()
        s = FakeSession({Thesis: [], NewCandidate: [c]})
        open_thesis_if_needed(s, TODAY, "MSFT")
        t = s.added[0]
        assert t.thesis_text == c.thesis
        assert t.catalyst == c.catalyst

    def test_the_real_conviction_is_used_not_a_default(self):
        # The orphan path used `conviction_score or 3`, which fabricated
        # a 3 for positions whose real conviction was on record.
        s = FakeSession({Thesis: [], NewCandidate: [candidate(conviction_score=5)]})
        open_thesis_if_needed(s, TODAY, "MSFT")
        assert s.added[0].original_conviction == 5

    def test_sector_and_risks_carry_so_noras_limit_stays_enforceable(self):
        s = FakeSession({Thesis: [], NewCandidate: [candidate()]})
        open_thesis_if_needed(s, TODAY, "MSFT")
        t = s.added[0]
        assert t.sector == "Technology"
        assert t.key_risks == ["capex cycle"]
        assert t.valuation_snapshot == {"pe": 31.2}

    def test_migration_010_prediction_carries_across(self):
        # So the thesis is gradeable from the day it opens rather than
        # from whenever someone remembers to fill the fields in.
        s = FakeSession({Thesis: [], NewCandidate: [candidate()]})
        open_thesis_if_needed(s, TODAY, "MSFT")
        t = s.added[0]
        assert (t.expected_direction, float(t.expected_move_pct), t.horizon_days) \
            == ("up", 18.0, 120)

    def test_opened_today(self):
        s = FakeSession({Thesis: [], NewCandidate: [candidate()]})
        open_thesis_if_needed(s, TODAY, "MSFT")
        assert s.added[0].opened_date == TODAY

    def test_id_is_available_to_the_caller(self):
        # flush() rather than commit: the id has to exist for the
        # allocation in the same transaction.
        s = FakeSession({Thesis: [], NewCandidate: [candidate()]})
        open_thesis_if_needed(s, TODAY, "MSFT")
        assert s.flushed == 1


# =================================================================
# Idempotency
# =================================================================
class TestDoesNotDuplicate:

    def test_an_existing_open_thesis_is_reused(self):
        s = FakeSession({Thesis: [open_thesis()], NewCandidate: [candidate()]})
        tid = open_thesis_if_needed(s, TODAY, "MSFT")
        assert tid == 7
        assert s.added == []

    def test_a_same_day_rerun_does_not_open_a_second(self):
        # The orchestrator can be re-triggered by hand, and Marcus's
        # persistence is delete-then-replace — but a thesis must not be.
        s = FakeSession({Thesis: [], NewCandidate: [candidate()]})
        first = open_thesis_if_needed(s, TODAY, "MSFT")
        s.by_model[Thesis] = [s.added[0]]
        second = open_thesis_if_needed(s, TODAY, "MSFT")
        assert first == second
        assert len(s.added) == 1


# =================================================================
# The refusal
# =================================================================
class TestRefusesToInvent:

    def test_no_candidate_means_no_thesis(self):
        # An allocation with no research behind it is a
        # process-compliance finding for Clara to raise, not something
        # to paper over with invented thesis text.
        s = FakeSession({Thesis: [], NewCandidate: []})
        assert open_thesis_if_needed(s, TODAY, "MSFT") is None
        assert s.added == []

    def test_the_refusal_is_logged_loudly(self, caplog):
        s = FakeSession({Thesis: [], NewCandidate: []})
        with caplog.at_level("WARNING"):
            open_thesis_if_needed(s, TODAY, "MSFT")
        assert "not opening a thesis" in caplog.text
        assert "compliance finding" in caplog.text

    def test_a_missing_conviction_falls_back_but_only_with_a_candidate(self):
        # A candidate that exists but never got scored is a different
        # situation from no candidate at all: there IS research, so the
        # thesis opens, with the conservative default the rest of the
        # codebase uses.
        s = FakeSession({Thesis: [], NewCandidate: [candidate(conviction_score=None)]})
        assert open_thesis_if_needed(s, TODAY, "MSFT") == 42
        assert s.added[0].original_conviction == 3
