"""
D4 — the desk is measured against something.

THE DEFECT. Absolute P&L was the only performance figure in the
system. A desk returning 4% in a month the index returned 6% lost
money in the only sense that matters, and this codebase would have
reported a good month in confident detail, with a clean audit trail
behind it. There was no SPY, no active return, no separation of alpha
from beta anywhere.

Everything here is pure arithmetic on hand-built series — no database,
no Alpaca, no model. Three properties matter:

  1. THE DECOMPOSITION RECONCILES. selection - cash_drag must equal
     active return exactly, or the split is decoration.
  2. CASH IS NOT ALWAYS A DRAG. Being under-invested helps in a
     falling market, and the sign has to show that.
  3. THE REFUSALS HOLD. A tracking error computed from four days is
     worse than no tracking error, so it must not be computed.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

ET = ZoneInfo("America/New_York")

from core.benchmark import (
    BENCHMARK_TICKER, MIN_SESSIONS_FOR_RISK_STATS, Performance,
    compute_performance,
)


def series(navs, start=date(2026, 9, 1)):
    """(date, nav) ascending over consecutive days."""
    return [(start + timedelta(days=i), v) for i, v in enumerate(navs)]


def bench(closes, start=date(2026, 9, 1)):
    return {start + timedelta(days=i): v for i, v in enumerate(closes)}


# ===============================================================
# 1. the comparison itself
# ===============================================================
class TestActiveReturn:

    def test_beating_the_index_reads_positive(self):
        r = compute_performance(series([100.0, 110.0]), bench([500.0, 525.0]))
        assert r.desk_return_pct == pytest.approx(10.0)
        assert r.benchmark_return_pct == pytest.approx(5.0)
        assert r.active_return_pct == pytest.approx(5.0)

    def test_a_gain_smaller_than_the_index_is_a_loss(self):
        """
        THE CASE THE FINDING WAS ABOUT. Up 4% while the index is up 6%
        is a bad month. The old code would have reported the 4% and
        stopped there.
        """
        r = compute_performance(series([100.0, 104.0]), bench([500.0, 530.0]))
        assert r.desk_return_pct > 0, "absolute return is positive..."
        assert r.active_return_pct == pytest.approx(-2.0), "...and the month was still bad"

    def test_losing_less_than_the_index_is_a_good_month(self):
        r = compute_performance(series([100.0, 97.0]), bench([500.0, 475.0]))
        assert r.desk_return_pct == pytest.approx(-3.0)
        assert r.active_return_pct == pytest.approx(2.0)

    def test_only_dates_in_both_series_are_compared(self):
        """Comparing endpoints that are not the same two sessions would
        silently attribute a weekend's market move to the desk."""
        navs = series([100.0, 101.0, 102.0])
        b = bench([500.0, 505.0])  # the third session is missing
        r = compute_performance(navs, b)
        assert r.sessions == 2
        assert r.end == navs[1][0]


# ===============================================================
# 2. cash drag vs selection
# ===============================================================
class TestDecomposition:
    """
    Active return alone is misleading on this book, in a predictable
    direction: a few hundred dollars of stock against a hundred
    thousand of cash will underperform any rising index, and that says
    nothing about whether the names were any good.
    """

    def test_the_split_reconciles_exactly(self):
        r = compute_performance(series([100.0, 103.0]), bench([500.0, 525.0]),
                                invested_weights=[0.5, 0.5])
        assert r.selection_pct - r.cash_drag_pct == pytest.approx(r.active_return_pct)

    @pytest.mark.parametrize("w", [0.0, 0.25, 0.5, 0.75, 1.0])
    def test_it_reconciles_at_every_exposure(self, w):
        r = compute_performance(series([100.0, 102.0]), bench([500.0, 515.0]),
                                invested_weights=[w, w])
        assert r.selection_pct - r.cash_drag_pct == pytest.approx(r.active_return_pct)

    def test_holding_cash_in_a_rising_market_is_a_drag_not_bad_picking(self):
        """
        Half invested, and the invested half exactly matched the index.
        Selection should be zero — the shortfall is entirely exposure.
        """
        r = compute_performance(series([100.0, 105.0]), bench([500.0, 550.0]),
                                invested_weights=[0.5, 0.5])
        assert r.active_return_pct == pytest.approx(-5.0), "it did trail the index"
        assert r.selection_pct == pytest.approx(0.0), "but the picks were market-neutral"
        assert r.cash_drag_pct == pytest.approx(5.0), "the whole gap was being half-invested"

    def test_cash_helps_when_the_market_falls(self):
        """
        THE SIGN THAT MATTERS. A naive 'cash drag' that were always a
        penalty would misread a defensive month as a failure.
        """
        r = compute_performance(series([100.0, 95.0]), bench([500.0, 450.0]),
                                invested_weights=[0.5, 0.5])
        assert r.cash_drag_pct < 0, "under-invested in a falling market is help, not drag"
        assert r.active_return_pct == pytest.approx(5.0)

    def test_good_picking_shows_through_low_exposure(self):
        """Only 10% invested, yet the desk gained 2% while the index
        gained 5% — the names roughly quadrupled the index's move on
        the money actually committed. Active return alone would call
        this a bad month."""
        r = compute_performance(series([100.0, 102.0]), bench([500.0, 525.0]),
                                invested_weights=[0.1, 0.1])
        assert r.active_return_pct < 0
        assert r.selection_pct > 1.0, "the picking was good and must be visible"

    def test_absent_exposure_history_falls_back_to_plain_active_return(self):
        """The honest fallback: with no weights known, assume full
        exposure and let the decomposition collapse rather than invent
        a series."""
        r = compute_performance(series([100.0, 104.0]), bench([500.0, 525.0]))
        assert r.avg_invested_pct == pytest.approx(100.0)
        assert r.cash_drag_pct == pytest.approx(0.0)
        assert r.selection_pct == pytest.approx(r.active_return_pct)


# ===============================================================
# 3. what it refuses to say
# ===============================================================
class TestRefusals:

    def test_one_session_is_not_a_track_record(self):
        r = compute_performance(series([100.0]), bench([500.0]))
        assert not r.measurable
        assert "two sessions" in r.unavailable

    def test_no_benchmark_stored_names_the_command_that_fixes_it(self):
        r = compute_performance(series([100.0, 101.0]), {})
        assert not r.measurable
        assert "backfill" in r.unavailable

    def test_no_overlap_is_reported_as_no_overlap(self):
        navs = series([100.0, 101.0], start=date(2026, 9, 1))
        b = bench([500.0, 505.0], start=date(2026, 10, 1))
        r = compute_performance(navs, b)
        assert not r.measurable
        assert BENCHMARK_TICKER in r.unavailable

    def test_tracking_error_is_withheld_on_a_short_history(self):
        """
        A tracking error computed from four days is not a small number,
        it is a wrong one — and it would be rendered with two decimal
        places next to numbers that are real.
        """
        r = compute_performance(series([100.0, 101.0, 102.0, 103.0]),
                                bench([500.0, 502.0, 505.0, 507.0]))
        assert r.measurable, "the return comparison is still fine"
        assert r.tracking_error_pct is None, "the risk statistic is not"

    def test_tracking_error_appears_once_there_is_enough_history(self):
        n = MIN_SESSIONS_FOR_RISK_STATS + 2
        navs = series([100.0 + i * 0.5 for i in range(n)])
        b = bench([500.0 + i * 2.0 for i in range(n)])
        r = compute_performance(navs, b)
        assert r.sessions >= MIN_SESSIONS_FOR_RISK_STATS
        assert r.tracking_error_pct is not None

    def test_the_summary_says_when_the_sample_is_too_small(self):
        r = compute_performance(series([100.0, 101.0]), bench([500.0, 502.0]))
        assert "Too few sessions" in r.describe()

    def test_an_unmeasurable_period_describes_itself_rather_than_printing_zero(self):
        r = compute_performance([], {})
        assert not r.measurable
        assert r.describe().startswith("No comparison available")
        assert r.active_return_pct is None, "never a plausible-looking placeholder"


# ===============================================================
# 4. the shape Clara and the dashboard read
# ===============================================================
def test_performance_is_json_safe():
    """It lands in agent_runs.raw_output and on the dashboard, so every
    field has to survive json.dumps with default=str."""
    import json
    r = compute_performance(series([100.0, 104.0]), bench([500.0, 525.0]),
                            invested_weights=[0.4, 0.4])
    payload = {
        "measurable": r.measurable, "summary": r.describe(),
        "desk_return_pct": r.desk_return_pct,
        "benchmark_return_pct": r.benchmark_return_pct,
        "active_return_pct": r.active_return_pct,
        "selection_pct": r.selection_pct, "cash_drag_pct": r.cash_drag_pct,
        "avg_invested_pct": r.avg_invested_pct,
        "tracking_error_pct": r.tracking_error_pct,
    }
    assert json.loads(json.dumps(payload, default=str))["measurable"] is True


def test_the_summary_names_both_returns_and_the_exposure():
    """It is read by a human in the daily report, and it has to be
    self-contained: a bare 'active return -2pp' invites the reader to
    conclude the picking was bad."""
    r = compute_performance(series([100.0, 102.0]), bench([500.0, 525.0]),
                            invested_weights=[0.3, 0.3])
    text = r.describe()
    assert "SPY" in text
    assert "invested" in text
    assert "Selection" in text and "cash drag" in text


# ===============================================================
# 5. the two things the first live backfill taught us
#
# Both found by running it against a real free-tier Alpaca key rather
# than by reading the SDK — the same pattern as every other defect in
# this project's second half.
# ===============================================================
class TestPartialSessions:
    """
    A daily bar requested mid-session is a PARTIAL bar: its "close" is
    the last print so far. Stored as a close it is a benchmark level
    for a day that had not ended, and every later return would be
    measured against it. Otis syncs after the close and never sees one;
    `backfill` run in the morning does.
    """

    def test_todays_bar_is_dropped_while_the_session_is_open(self):
        from core.benchmark import drop_incomplete_session
        now = datetime(2026, 9, 8, 11, 0, tzinfo=ET)          # mid-session
        bars = [{"date": date(2026, 9, 4), "close": 640.0},
                {"date": date(2026, 9, 8), "close": 643.1}]   # partial
        kept = drop_incomplete_session(bars, now=now)
        assert [b["date"] for b in kept] == [date(2026, 9, 4)]

    def test_todays_bar_is_kept_once_the_session_has_closed(self):
        from core.benchmark import drop_incomplete_session
        now = datetime(2026, 9, 8, 16, 30, tzinfo=ET)
        bars = [{"date": date(2026, 9, 4), "close": 640.0},
                {"date": date(2026, 9, 8), "close": 646.4}]
        assert len(drop_incomplete_session(bars, now=now)) == 2

    def test_a_non_session_day_leaves_everything_alone(self):
        """Saturday. Nothing dated today can be partial because there
        is no session to be partway through."""
        from core.benchmark import drop_incomplete_session
        now = datetime(2026, 9, 5, 12, 0, tzinfo=ET)
        bars = [{"date": date(2026, 9, 4), "close": 640.0}]
        assert len(drop_incomplete_session(bars, now=now)) == 1

    def test_an_empty_batch_is_returned_unchanged(self):
        from core.benchmark import drop_incomplete_session
        assert drop_incomplete_session([]) == []


class TestFeedAndAdjustment:
    """
    The first backfill died on HTTP 403, "subscription does not permit
    querying recent SIP data" — the free Alpaca data plan serves IEX
    and refuses SIP rather than degrading to it.

    Asserted against the source because the alternative is a live API
    key in the test suite, which would cost the property that makes
    this suite worth running: it needs no network and no secrets.
    """

    def test_the_request_names_a_feed_rather_than_letting_it_default(self):
        import inspect
        from core.clients import alpaca_client
        src = inspect.getsource(alpaca_client.get_daily_bars)
        assert "feed=DataFeed(ALPACA_DATA_FEED)" in src

    def test_the_default_feed_is_the_one_a_free_plan_serves(self):
        import os
        assert os.environ.get("ALPACA_DATA_FEED", "iex").lower() == "iex"

    def test_bars_are_dividend_adjusted(self):
        """
        NOT COSMETIC. The desk's NAV already includes dividends it
        received, so a price-only index would flatter the desk by
        roughly the index's yield every year — silently, and in one
        direction. SPY yields over 1%, which is larger than any active
        return this desk is likely to produce.
        """
        import inspect
        from core.clients import alpaca_client
        src = inspect.getsource(alpaca_client.get_daily_bars)
        assert "adjustment=Adjustment.ALL" in src

    def test_a_subscription_error_names_the_setting_that_fixes_it(self):
        import inspect
        from core.clients import alpaca_client
        src = inspect.getsource(alpaca_client.get_daily_bars)
        assert "ALPACA_DATA_FEED=iex" in src, \
            "a raw 403 about SIP says nothing about which knob to turn"
