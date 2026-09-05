"""
D7 — degraded data must not flow in as if it were data.

THE DEFECT. `fmp_client._get()` turns a 402/403 into
`{"error": "not available on current FMP plan"}` and hands it back to
Claude as a tool result. That graceful degradation is right — one
paywalled ticker should not crash a run. What was missing is that
nothing COUNTED it. Vera could screen six names, receive real
fundamentals for two, and produce confident candidates from the
remainder, and the run would look identical to a clean one.

A desk with a broken data feed does not trade on the fragment that
still works.

Two halves, both pure and both testable without a network:

  1. DataCoverage — does the count reflect what actually happened,
     and is it counted per TICKER rather than per request.
  2. coverage_gate — below the floor, does the desk stop buying while
     still being allowed to sell. That asymmetry is the design; a
     symmetric gate would be the drawdown-breaker mistake again.
"""

import pytest

from agents import solomon
from agents.solomon import (
    MIN_DATA_COVERAGE_PCT, ProposalOutput, coverage_gate,
)
from core.clients.fmp_client import DataCoverage


def proposal(ticker="AAPL", action="new_position"):
    return ProposalOutput(
        ticker=ticker, action=action,
        linked_trigger=f"vera:signal:{ticker}",
        rationale="because", urgency="this_week",
    )


def report(pct, attempted=5, complete=2, degraded=None):
    return {
        "tickers_attempted": attempted,
        "tickers_complete": complete,
        "coverage_pct": pct,
        "degraded_tickers": degraded if degraded is not None else {"NVDA": ["key-metrics"]},
        "requests_attempted": attempted * 2,
        "requests_degraded": (attempted - complete) * 2,
    }


# ===============================================================
# 1. the counter
# ===============================================================
class TestDataCoverage:

    def test_a_clean_run_is_100_percent(self):
        c = DataCoverage()
        for t in ("AAPL", "MSFT"):
            c.record("profile", t, ok=True)
            c.record("key-metrics", t, ok=True)
        s = c.summary()
        assert s["coverage_pct"] == 100.0
        assert s["tickers_complete"] == 2
        assert s["degraded_tickers"] == {}

    def test_coverage_is_per_ticker_not_per_request(self):
        """
        THE MEASUREMENT DECISION. get_company_snapshot makes two calls
        per ticker. A name whose profile arrived and whose valuation
        metrics were paywalled has NOT been researched — but a
        request-level count scores it 50% and moves on. One ticker,
        half its data, is zero usable names.
        """
        c = DataCoverage()
        c.record("profile", "AAPL", ok=True)
        c.record("key-metrics", "AAPL", ok=False)
        s = c.summary()

        assert s["coverage_pct"] == 0.0, "a half-covered ticker is not half a ticker"
        assert s["requests_attempted"] == 2 and s["requests_degraded"] == 1
        assert s["degraded_tickers"] == {"AAPL": ["key-metrics"]}

    def test_partial_coverage_across_a_universe(self):
        c = DataCoverage()
        for t in ("AAPL", "MSFT"):
            c.record("profile", t, ok=True)
            c.record("key-metrics", t, ok=True)
        for t in ("NVDA", "JPM", "AMZN"):
            c.record("profile", t, ok=True)
            c.record("key-metrics", t, ok=False)
        s = c.summary()
        assert s["tickers_attempted"] == 5
        assert s["tickers_complete"] == 2
        assert s["coverage_pct"] == 40.0
        assert sorted(s["degraded_tickers"]) == ["AMZN", "JPM", "NVDA"]

    def test_nothing_requested_reports_none_not_zero(self):
        """
        'We asked for nothing' and 'we asked and got nothing' are
        different claims. A day holding no positions and screening
        nothing is idle, not degraded — scoring it 0% would stand the
        desk down for having had a quiet morning.
        """
        assert DataCoverage().summary()["coverage_pct"] is None

    def test_symbolless_endpoints_do_not_dilute_ticker_coverage(self):
        """The macro calendar and news feeds have no ticker. Counting
        them among ticker coverage would let two failed news calls make
        a fully-covered universe look degraded."""
        c = DataCoverage()
        c.record("profile", "AAPL", ok=True)
        c.record("key-metrics", "AAPL", ok=True)
        c.record("news/general-latest", None, ok=False)
        c.record("economic-calendar", None, ok=False)
        s = c.summary()
        assert s["coverage_pct"] == 100.0, "ticker coverage is about tickers"
        assert s["requests_degraded"] == 2, "but the misses are still on the record"

    def test_reset_clears_the_previous_run(self):
        """Atlas runs before Vera and shares this client. Without the
        reset his four index calls pool into her ticker coverage."""
        c = DataCoverage()
        c.record("quote", "^GSPC", ok=False)
        c.reset()
        c.record("profile", "AAPL", ok=True)
        assert c.summary()["coverage_pct"] == 100.0
        assert c.summary()["tickers_attempted"] == 1

    def test_a_later_verdict_overrides_an_earlier_one(self):
        """A 200 with an empty body is recorded as an HTTP success by
        _get() and corrected in get_stock_quote() once the payload is
        actually looked at."""
        c = DataCoverage()
        c.record("quote", "AAPL", ok=True)   # HTTP 200
        c.record("quote", "AAPL", ok=False)  # ...but empty
        assert c.summary()["coverage_pct"] == 0.0

    def test_symbols_are_normalised(self):
        c = DataCoverage()
        c.record("profile", "aapl", ok=True)
        c.record("key-metrics", "AAPL", ok=False)
        assert c.summary()["tickers_attempted"] == 1, "one ticker, not two"


# ===============================================================
# 2. the gate
# ===============================================================
class TestCoverageGate:

    def test_good_coverage_changes_nothing(self):
        kept, rejected = coverage_gate([proposal("MSFT", "new_position")], report(80.0))
        assert len(kept) == 1 and rejected == []

    def test_poor_coverage_refuses_a_new_position(self):
        kept, rejected = coverage_gate([proposal("MSFT", "new_position")], report(40.0))
        assert kept == []
        assert "below the 60.0% floor" in rejected[0]["rejection_reason"]

    def test_poor_coverage_refuses_an_add(self):
        kept, rejected = coverage_gate([proposal("AAPL", "add")], report(40.0))
        assert kept == [] and len(rejected) == 1

    @pytest.mark.parametrize("action", ["trim", "exit"])
    def test_poor_coverage_still_permits_de_risking(self, action):
        """
        THE ASYMMETRY, AND THE REASON FOR IT. This is the drawdown
        breaker's logic, not the kill switch's. A broken fundamentals
        feed means we cannot see well enough to CHOOSE among names — so
        we stop buying. It is not a reason to keep holding a thesis we
        can see perfectly well is broken. A gate that froze exits
        because FMP was paywalled would be strictly worse than no gate.
        """
        kept, rejected = coverage_gate([proposal("AAPL", action)], report(10.0))
        assert len(kept) == 1, f"{action} must survive a degraded day"
        assert rejected == []

    def test_a_mixed_batch_splits_by_direction(self):
        kept, rejected = coverage_gate(
            [proposal("MSFT", "new_position"), proposal("AAPL", "exit"),
             proposal("JPM", "add"), proposal("AMZN", "trim")],
            report(20.0),
        )
        assert {p.ticker for p in kept} == {"AAPL", "AMZN"}
        assert {r["ticker"] for r in rejected} == {"MSFT", "JPM"}

    def test_exactly_at_the_floor_is_permitted(self):
        kept, rejected = coverage_gate([proposal("MSFT", "new_position")],
                                       report(MIN_DATA_COVERAGE_PCT))
        assert len(kept) == 1, "the floor is a floor, not a wall"

    def test_a_quiet_day_is_not_a_degraded_day(self):
        """coverage_pct None means nothing was requested."""
        kept, rejected = coverage_gate([proposal("MSFT", "new_position")],
                                       report(None, attempted=0, complete=0, degraded={}))
        assert len(kept) == 1 and rejected == []

    def test_a_missing_report_is_not_treated_as_a_bad_one(self):
        """An older Vera, or a stubbed one, reports no coverage at all.
        Absence of a measurement is not evidence of a bad measurement —
        inventing a failure here would stand the desk down for a
        version mismatch."""
        for absent in (None, {}):
            kept, rejected = coverage_gate([proposal("MSFT", "new_position")], absent)
            assert len(kept) == 1 and rejected == []

    def test_the_reason_names_the_degraded_tickers(self):
        """It lands in agent_runs.raw_output. 'Coverage was low' is not
        something you can act on a week later."""
        _, rejected = coverage_gate(
            [proposal("MSFT", "new_position")],
            report(40.0, degraded={"NVDA": ["key-metrics"], "JPM": ["profile"]}),
        )
        reason = rejected[0]["rejection_reason"]
        assert "JPM" in reason and "NVDA" in reason
        assert "De-risking is unaffected" in reason


# ===============================================================
# 3. the two layers together
# ===============================================================
def test_provenance_is_reported_before_coverage():
    """
    A hallucinated ticker on a degraded day is still a hallucination,
    and that is the more serious of the two facts about it. Ordering
    the checks provenance-first means the recorded reason is the one
    worth reading.
    """
    from agents.solomon import validate_proposals

    vera = {"monitoring": [], "candidates": [], "orphan_reviews": [],
            "data_coverage": report(10.0)}

    kept, rejected = validate_proposals([proposal("NVDA", "new_position")], vera, set())
    kept, cov_rejected = coverage_gate(kept, vera["data_coverage"])

    assert kept == []
    assert len(rejected) == 1 and cov_rejected == []
    assert "appears nowhere in Vera's output" in rejected[0]["rejection_reason"]
