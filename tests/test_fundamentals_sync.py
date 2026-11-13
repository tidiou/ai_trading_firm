"""
Bulk EDGAR sync — containment, and the bounded self-heal.

WHY THIS EXISTS. Widening the screening universe from six names to 27
only helps if the 27 are researched rather than guessed at, and the
valuation half of that research reads filed figures out of
company_facts. Nothing in the codebase had ever called
`fundamentals.sync()`, so that table had never been populated. This is
the layer that fills it.

TWO PROPERTIES ARE WORTH TESTS, and neither is about EDGAR:

  ONE BAD NAME MUST NOT STOP THE REST. Not hypothetical. The theme
  universe contains TSM and ASML, which file 20-F under IFRS rather
  than 10-K under us-gaap, so every us-gaap concept request this
  module makes comes back empty for them. That is an honest answer,
  not a failure, and it must be reported as such — an "empty" name and
  a "failed" name are different facts and are counted separately.

  THE SELF-HEAL MUST BE BOUNDED. Eight EDGAR requests per ticker at
  the SEC's five-per-second throttle is about a minute for a cold
  27-name universe. A premarket run has a window to hit, so the
  backfill inside it is capped: a cold start fills in over several
  sessions instead of holding one hostage. The CLI backfill is the
  intended path; this is the net underneath it.

Everything here is stubbed at the sync() boundary. No network, no
database.
"""

import pytest

from core import fundamentals


@pytest.fixture
def edgar(monkeypatch):
    """Stub `sync`, recording call order. `results` maps ticker to what
    sync should return or raise."""
    state = {"called": [], "results": {}}

    def fake_sync(ticker):
        state["called"].append(ticker)
        outcome = state["results"].get(ticker, {"ticker": ticker, "fetched": 12,
                                                "stored": 12, "cik": "1"})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(fundamentals, "sync", fake_sync)
    return state


# =================================================================
# 1. sync_universe — one bad name does not stop the rest
# =================================================================
class TestSyncUniverse:

    def test_the_happy_path_reports_every_name(self, edgar):
        out = fundamentals.sync_universe(["NVDA", "MU", "GEV"])
        assert out["synced"] == ["NVDA", "MU", "GEV"]
        assert out["empty"] == []
        assert out["failed"] == {}

    def test_a_raising_ticker_does_not_stop_the_others(self, edgar):
        edgar["results"]["MU"] = RuntimeError("EDGAR 503")
        out = fundamentals.sync_universe(["NVDA", "MU", "GEV"])
        assert out["synced"] == ["NVDA", "GEV"]
        assert "EDGAR 503" in out["failed"]["MU"]
        assert edgar["called"] == ["NVDA", "MU", "GEV"]

    def test_an_error_RESULT_is_a_failure_not_a_crash(self, edgar):
        edgar["results"]["XYZ"] = {"ticker": "XYZ", "error": "no CIK on record"}
        out = fundamentals.sync_universe(["XYZ", "MU"])
        assert out["failed"]["XYZ"] == "no CIK on record"
        assert out["synced"] == ["MU"]

    def test_a_filer_reporting_nothing_is_EMPTY_not_FAILED(self, edgar):
        # TSM and ASML file under IFRS. Zero us-gaap facts is the
        # correct answer for them, and conflating it with a failure
        # would have the desk chasing an outage that isn't one.
        edgar["results"]["TSM"] = {"ticker": "TSM", "cik": "1046179",
                                   "fetched": 0, "stored": 0}
        out = fundamentals.sync_universe(["TSM", "MU"])
        assert out["empty"] == ["TSM"]
        assert out["failed"] == {}
        assert out["synced"] == ["MU"]

    def test_tickers_are_upper_cased(self, edgar):
        fundamentals.sync_universe(["nvda", "mu"])
        assert edgar["called"] == ["NVDA", "MU"]

    def test_limit_stops_early(self, edgar):
        out = fundamentals.sync_universe(["A", "B", "C", "D"], limit=2)
        assert edgar["called"] == ["A", "B"]
        assert out["requested"] == ["A", "B"]

    def test_an_empty_list_does_nothing(self, edgar):
        out = fundamentals.sync_universe([])
        assert edgar["called"] == []
        assert out["synced"] == []


# =================================================================
# 2. backfill_missing — bounded, and only for names with NOTHING
# =================================================================
class TestBackfillMissing:

    @pytest.fixture
    def stored(self, monkeypatch):
        have = set()
        monkeypatch.setattr(fundamentals, "tickers_with_facts",
                            lambda tickers: {t for t in tickers if t in have})
        return have

    def test_a_fully_stored_universe_syncs_nothing(self, edgar, stored):
        stored.update({"NVDA", "MU"})
        out = fundamentals.backfill_missing(["NVDA", "MU"])
        assert edgar["called"] == []
        assert out["missing_total"] == 0

    def test_only_the_missing_names_are_fetched(self, edgar, stored):
        stored.add("NVDA")
        fundamentals.backfill_missing(["NVDA", "MU", "GEV"])
        assert edgar["called"] == ["MU", "GEV"]

    def test_it_stops_at_the_cap(self, edgar, stored):
        universe = [f"T{i}" for i in range(20)]
        out = fundamentals.backfill_missing(universe, limit=6)
        assert len(edgar["called"]) == 6
        assert out["missing_total"] == 20
        assert len(out["deferred"]) == 14

    def test_the_default_cap_is_small_enough_to_sit_in_a_premarket_run(self):
        # Eight EDGAR requests per ticker at five per second. The cap
        # is what keeps a cold universe from costing a trading window.
        assert fundamentals.MAX_BACKFILL_PER_RUN <= 8

    def test_deferred_names_are_named_so_nothing_is_lost_silently(
            self, edgar, stored):
        out = fundamentals.backfill_missing(["A", "B", "C"], limit=1)
        assert out["deferred"] == ["B", "C"]

    def test_a_cold_universe_fills_in_across_runs(self, edgar, stored):
        universe = ["A", "B", "C", "D"]

        # Run one takes two; those become stored.
        first = fundamentals.backfill_missing(universe, limit=2)
        stored.update(first["synced"])
        # Run two takes the rest.
        second = fundamentals.backfill_missing(universe, limit=2)
        stored.update(second["synced"])

        assert first["synced"] == ["A", "B"]
        assert second["synced"] == ["C", "D"]
        assert fundamentals.backfill_missing(universe, limit=2)["missing_total"] == 0

    def test_a_name_EDGAR_has_nothing_for_is_retried_but_bounded(
            self, edgar, stored):
        # TSM stores no facts, so it stays "missing" and is retried
        # each session. That is acceptable — it is one name inside a
        # capped budget — but it must not crowd out names that WOULD
        # store, so the cap is what keeps it honest.
        edgar["results"]["TSM"] = {"ticker": "TSM", "fetched": 0, "stored": 0}
        out = fundamentals.backfill_missing(["TSM", "MU"], limit=2)
        assert out["empty"] == ["TSM"]
        assert out["synced"] == ["MU"]
