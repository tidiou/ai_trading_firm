"""
Vera's run() — which names get monitored, and in what order.

This file replaces a placeholder that asserted `pass`. It exists for
one defect, described in full in tests/test_book_reconciliation.py:
between 17 and 23 September the monitoring pass took "what we hold"
from the OPEN THESES table while the broker's own positions table said
something different, and nothing compared them.

reconcile_book() is tested as pure set arithmetic in that other file.
What is tested HERE is the wiring in run(), which is where both halves
of the bug actually lived:

  1. the monitored set is positions INTERSECT open theses, not the
     open theses alone;
  2. the orphan-adoption pass runs BEFORE the theses are read, so a
     name adopted this morning is monitored this morning — NVDA lost
     a day to that ordering;
  3. a disagreement is logged at WARNING with tickers in it;
  4. a phantom thesis is reported and NOT closed.

run() is stubbed rather than mocked wholesale: every LLM call and
every database read is replaced, and the assertions are about which
tickers reached which stub. Nothing here connects to anything.
"""

import json
import logging
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

import pytest

from agents import vera

TODAY = date(2026, 9, 23)

ATLAS = {"regime_signal": "risk_on", "confidence": "medium",
         "narrative": "capex holds"}


# =================================================================
# Doubles
# =================================================================
def thesis(tid, ticker, opened="2026-09-02"):
    return SimpleNamespace(
        id=tid, ticker=ticker, opened_date=opened, original_conviction=4,
        thesis_text=f"{ticker} thesis", catalyst="something", sector=None,
        closed_date=None,
    )


def position(ticker, market_value=1000.0, unrealized=50.0):
    return SimpleNamespace(ticker=ticker, market_value=market_value,
                           unrealized_pnl=unrealized)


class FakeQuery:
    """Enough of a SQLAlchemy Query for run()'s persistence block. It
    records nothing it is not asked about — the assertions in this
    file are about tickers, not SQL."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def delete(self, *a, **k):
        return 0

    def update(self, *a, **k):
        return 0


class FakeSession:
    def __init__(self, positions):
        self._positions = positions
        self.added = []
        self.executed = []

    def query(self, model, *rest):
        if model is vera.Position:
            return FakeQuery(self._positions)
        return FakeQuery([])

    def execute(self, stmt):
        self.executed.append(stmt)

    def add(self, obj):
        self.added.append(obj)


@pytest.fixture
def desk(monkeypatch):
    """A whole stubbed desk. `state` is mutable so a test can make the
    orphan pass ADOPT a name and see whether the theses read afterwards
    picks it up — which is the entire point of moving that call."""
    state = {
        "positions": [],        # tickers the broker says are held
        "theses": [],           # SimpleNamespace thesis rows, open
        "calls": [],            # ordered record of what ran
        "monitoring_prompt": None,
        "screening_prompt": None,
        "orphan_adopts": [],    # tickers the orphan pass will adopt
        "orphan_raises": None,
    }

    @contextmanager
    def fake_scope():
        yield FakeSession([position(t) for t in state["positions"]])

    def fake_open_theses(session):
        return list(state["theses"])

    def fake_position_tickers(session):
        return sorted(set(state["positions"]))

    def fake_orphan_review(today):
        state["calls"].append("orphan_review")
        if state["orphan_raises"]:
            raise state["orphan_raises"]
        for t in state["orphan_adopts"]:
            if t not in [x.ticker for x in state["theses"]]:
                state["theses"].append(thesis(99, t, str(today)))
        return [{"ticker": t, "recommendation": "adopt"}
                for t in state["orphan_adopts"]]

    def fake_loop(system_prompt, user_prompt, **kw):
        if "MONITOR" in system_prompt.upper() or "monitoring" in system_prompt:
            state["calls"].append("monitoring")
            state["monitoring_prompt"] = user_prompt
            # One entry per ticker named in the prompt block headers.
            out = []
            for t in state["theses"]:
                if f"- {t.ticker}:" in user_prompt:
                    out.append({"ticker": t.ticker, "status": "intact",
                                "trigger": "none", "reasoning": "unchanged",
                                "conviction_score": 4, "assumption_checks": []})
            return json.dumps(out)
        state["calls"].append("screening")
        state["screening_prompt"] = user_prompt
        return "[]"

    monkeypatch.setattr(vera, "session_scope", fake_scope)
    monkeypatch.setattr(vera, "get_open_theses", fake_open_theses)
    monkeypatch.setattr(vera, "get_position_tickers", fake_position_tickers)
    monkeypatch.setattr(vera, "review_orphan_positions", fake_orphan_review)
    monkeypatch.setattr(vera, "run_agent_loop", fake_loop)
    monkeypatch.setattr(vera, "open_assumptions", lambda s, tid: [])
    monkeypatch.setattr(vera, "author_assumptions", lambda d, t: [])
    monkeypatch.setattr(vera, "_get_trailing_pnl_history",
                        lambda s, d, tickers, days=7: {})
    monkeypatch.setattr(
        vera, "get_active_risk_policy_for_review",
        lambda s, d: SimpleNamespace(loss_review_pct=-8.0, profit_review_pct=20.0))
    return state


def monitored_from(out):
    """Which tickers actually reached the monitoring loop."""
    return sorted(m["ticker"] for m in out["monitoring"])


# =================================================================
# 1. The monitored set
# =================================================================
class TestWhatGetsMonitored:

    def test_a_matched_book_monitors_everything(self, desk):
        desk["positions"] = ["AAPL", "NVDA"]
        desk["theses"] = [thesis(1, "AAPL"), thesis(3, "NVDA")]
        out = vera.run(TODAY, ATLAS)
        assert monitored_from(out) == ["AAPL", "NVDA"]
        assert out["book"]["reconciled"] is True

    def test_a_phantom_thesis_is_NOT_monitored(self, desk):
        # THE 23 SEPT STATE. GOOGL has a thesis and no position; the
        # old code checked its assumptions daily against nothing.
        desk["positions"] = ["AAPL", "NVDA"]
        desk["theses"] = [thesis(1, "AAPL"), thesis(2, "GOOGL"),
                          thesis(3, "NVDA")]
        out = vera.run(TODAY, ATLAS)
        assert monitored_from(out) == ["AAPL", "NVDA"]
        assert "GOOGL" not in (desk["monitoring_prompt"] or "")
        assert out["book"]["phantoms"] == ["GOOGL"]

    def test_the_old_behaviour_would_have_monitored_the_phantom(self, desk):
        # Stated as a contrast so a future edit that reverts to the
        # theses list fails here rather than silently.
        desk["positions"] = ["AAPL", "NVDA"]
        desk["theses"] = [thesis(1, "AAPL"), thesis(2, "GOOGL"),
                          thesis(3, "NVDA")]
        out = vera.run(TODAY, ATLAS)
        open_thesis_tickers = sorted(t.ticker for t in desk["theses"])
        assert open_thesis_tickers == ["AAPL", "GOOGL", "NVDA"]
        assert monitored_from(out) != open_thesis_tickers

    def test_nothing_held_monitors_nothing_and_does_not_crash(self, desk):
        desk["positions"] = []
        desk["theses"] = [thesis(2, "GOOGL")]
        out = vera.run(TODAY, ATLAS)
        assert out["monitoring"] == []
        assert out["book"]["phantoms"] == ["GOOGL"]


# =================================================================
# 2. Ordering — the half that cost NVDA a day
# =================================================================
class TestOrphanReviewRunsFirst:

    def test_the_orphan_pass_runs_before_the_monitoring_pass(self, desk):
        desk["positions"] = ["AAPL"]
        desk["theses"] = [thesis(1, "AAPL")]
        vera.run(TODAY, ATLAS)
        assert desk["calls"].index("orphan_review") < desk["calls"].index("monitoring")

    def test_a_name_adopted_TODAY_is_monitored_TODAY(self, desk):
        # THE NVDA CASE. Held, undocumented at the start of the run.
        # The orphan pass adopts it; under the old ordering the
        # monitoring list had already been chosen and it waited a day.
        desk["positions"] = ["AAPL", "NVDA"]
        desk["theses"] = [thesis(1, "AAPL")]
        desk["orphan_adopts"] = ["NVDA"]
        out = vera.run(TODAY, ATLAS)
        assert monitored_from(out) == ["AAPL", "NVDA"]
        assert out["book"]["orphans"] == []
        assert out["book"]["reconciled"] is True

    def test_an_adopted_name_leaves_the_screening_universe_same_day(self, desk):
        desk["positions"] = ["NVDA"]
        desk["theses"] = []
        desk["orphan_adopts"] = ["NVDA"]
        vera.run(TODAY, ATLAS)
        assert "NVDA" not in desk["screening_prompt"]


class TestAFailedOrphanPassIsContained:

    def test_monitoring_still_runs(self, desk):
        desk["positions"] = ["AAPL"]
        desk["theses"] = [thesis(1, "AAPL")]
        desk["orphan_raises"] = RuntimeError("model unavailable")
        out = vera.run(TODAY, ATLAS)
        assert monitored_from(out) == ["AAPL"]

    def test_the_failure_is_reported_not_swallowed(self, desk):
        # An empty orphan_reviews list must never be readable as
        # "no orphans" when the pass never ran.
        desk["positions"] = ["AAPL"]
        desk["theses"] = [thesis(1, "AAPL")]
        desk["orphan_raises"] = RuntimeError("model unavailable")
        out = vera.run(TODAY, ATLAS)
        assert out["orphan_reviews"] == []
        assert "model unavailable" in out["orphan_review_error"]

    def test_a_clean_run_reports_no_error(self, desk):
        desk["positions"] = ["AAPL"]
        desk["theses"] = [thesis(1, "AAPL")]
        out = vera.run(TODAY, ATLAS)
        assert out["orphan_review_error"] is None


# =================================================================
# 3. The WARNING
# =================================================================
class TestItSaysSoOutLoud:

    def test_a_disagreement_warns_and_names_the_ticker(self, desk, caplog):
        desk["positions"] = ["AAPL", "NVDA"]
        desk["theses"] = [thesis(1, "AAPL"), thesis(2, "GOOGL"),
                          thesis(3, "NVDA")]
        with caplog.at_level(logging.WARNING, logger=vera.logger.name):
            vera.run(TODAY, ATLAS)
        warnings = [r.getMessage() for r in caplog.records
                    if r.levelno >= logging.WARNING]
        assert any("GOOGL" in m for m in warnings), warnings

    def test_a_clean_book_does_not_warn(self, desk, caplog):
        desk["positions"] = ["AAPL", "NVDA"]
        desk["theses"] = [thesis(1, "AAPL"), thesis(3, "NVDA")]
        with caplog.at_level(logging.INFO, logger=vera.logger.name):
            vera.run(TODAY, ATLAS)
        assert not [r for r in caplog.records
                    if r.levelno >= logging.WARNING
                    and "reconcil" in r.getMessage().lower()]

    def test_the_statement_carries_the_names_into_the_run_ledger(self, desk):
        # caplog only proves it reached a handler. The orchestrator
        # persists the return dict, and that is what survives.
        desk["positions"] = ["AAPL", "NVDA"]
        desk["theses"] = [thesis(1, "AAPL"), thesis(2, "GOOGL"),
                          thesis(3, "NVDA")]
        out = vera.run(TODAY, ATLAS)
        assert "GOOGL" in out["book"]["statement"]


# =================================================================
# 4. A phantom is surfaced, never silently resolved
# =================================================================
class TestAPhantomIsNotCleanedUp:

    def test_no_thesis_is_closed_by_the_reconciliation(self, desk):
        desk["positions"] = ["AAPL"]
        desk["theses"] = [thesis(1, "AAPL"), thesis(2, "GOOGL")]
        vera.run(TODAY, ATLAS)
        assert all(t.closed_date is None for t in desk["theses"])

    def test_it_reappears_every_day_until_a_human_acts(self, desk):
        desk["positions"] = ["AAPL"]
        desk["theses"] = [thesis(1, "AAPL"), thesis(2, "GOOGL")]
        first = vera.run(TODAY, ATLAS)
        second = vera.run(date(2026, 9, 24), ATLAS)
        assert first["book"]["phantoms"] == ["GOOGL"]
        assert second["book"]["phantoms"] == ["GOOGL"]


# =================================================================
# 5. Screening exclusions
# =================================================================
class TestScreeningUniverse:

    def test_a_held_name_is_excluded(self, desk):
        desk["positions"] = ["AAPL"]
        desk["theses"] = [thesis(1, "AAPL")]
        vera.run(TODAY, ATLAS)
        universe = desk["screening_prompt"].rsplit(":", 1)[1]
        assert "AAPL" not in universe

    def test_a_phantom_is_excluded_too_without_being_called_held(self, desk):
        # Not proposing a second thesis for a name that already has one
        # open is different from claiming the name is held. The
        # screening summary's `excluded_as_held` must stay TRUE.
        desk["positions"] = ["AAPL"]
        desk["theses"] = [thesis(1, "AAPL"), thesis(2, "GOOGL")]
        out = vera.run(TODAY, ATLAS)
        universe = desk["screening_prompt"].rsplit(":", 1)[1]
        assert "GOOGL" not in universe
        assert out["screening"]["excluded_as_held"] == ["AAPL"]

    def test_an_unheld_undocumented_name_is_screened(self, desk):
        desk["positions"] = ["AAPL"]
        desk["theses"] = [thesis(1, "AAPL")]
        vera.run(TODAY, ATLAS)
        assert "MSFT" in desk["screening_prompt"]
