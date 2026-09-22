"""
Clara's attribution: a daily flow over a daily flow.

THE DEFECT THIS LOCKS DOWN. The numerator used to be a position's
LIFETIME open gain (a stock); the denominator was today's P&L (a
flow). On 2026-09-17 that reported AAPL at +31.08% and NVDA at
-188.48% of the day's P&L — figures summing to -157% rather than 100%,
and large enough that a worse book would have overflowed the
NUMERIC(6,3) column they are stored in.

`agents/otis.py` had already fixed this exact confusion on its own
side, and its docstring names the downstream consumer: "subtracting a
cumulative stock from a daily flow ... and it fed Clara's
attribution". The upstream half was repaired and the follow-through
never happened. This is that follow-through.

The tests are written around invariants — contributions summing to the
day, a refusal where a number would be meaningless — rather than
around example values, because an invariant is what a future edit
trips over.

NOT A DEFECT, recorded here because it cost a round trip: the cash
drag term in core/benchmark.py looks like a sign error and is not.
Its convention is `active = selection - cash_drag`, with cash_drag as
a signed COST, and TestDecomposition in tests/test_benchmark.py
already pins that identity at every exposure. Read those before
touching it.
"""

from datetime import date

import pytest

from agents.clara import compute_contributions, open_unrealized_usd

TODAY = date(2026, 9, 17)


# =================================================================
# 1. Reconstructing a position's absolute open gain
# =================================================================
class TestOpenUnrealizedUsd:

    def test_a_gain_is_recovered_from_value_and_percentage(self):
        # 110 market value at +10% means cost 100, open gain 10.
        assert open_unrealized_usd(110.0, 10.0) == pytest.approx(10.0)

    def test_a_loss_is_recovered(self):
        # 90 at -10% means cost 100, open loss -10.
        assert open_unrealized_usd(90.0, -10.0) == pytest.approx(-10.0)

    def test_flat_is_zero(self):
        assert open_unrealized_usd(100.0, 0.0) == pytest.approx(0.0)

    def test_a_total_loss_returns_none_rather_than_dividing_by_zero(self):
        assert open_unrealized_usd(0.0, -100.0) is None

    @pytest.mark.parametrize("mv,pct", [(None, 5.0), (100.0, None), (None, None)])
    def test_a_missing_input_returns_none(self, mv, pct):
        assert open_unrealized_usd(mv, pct) is None


# =================================================================
# 2. Attribution — a daily flow over a daily flow
# =================================================================
def snap(ticker, market_value, pct):
    return {"ticker": ticker, "market_value": market_value,
            "unrealized_pnl_pct": pct}


class TestContributionsOnAnOrdinaryDay:

    def test_a_days_change_is_the_numerator(self):
        # AAPL: cost 100, was 110 (+10%), now 115 (+15%). Day = +5.
        out = compute_contributions(
            [snap("AAPL", 115.0, 15.0)], [snap("AAPL", 110.0, 10.0)],
            total_pnl=10.0, sessions_since_prior=1)
        assert out[0]["contribution_usd"] == pytest.approx(5.0)
        assert out[0]["contribution_pct"] == pytest.approx(50.0)

    def test_the_lifetime_gain_is_NOT_the_numerator(self):
        # THE REGRESSION. The old code would have used the position's
        # whole +15 open gain over a 10 P&L day and reported 150%.
        out = compute_contributions(
            [snap("AAPL", 115.0, 15.0)], [snap("AAPL", 110.0, 10.0)],
            total_pnl=10.0, sessions_since_prior=1)
        assert out[0]["contribution_pct"] != pytest.approx(150.0)
        assert out[0]["contribution_pct"] == pytest.approx(50.0)

    def test_contributions_sum_to_the_unrealized_share_of_the_day(self):
        # Two positions moving +5 and -2 on a day whose total P&L was
        # +3 account for 100% of it between them.
        out = compute_contributions(
            [snap("AAPL", 115.0, 15.0), snap("NVDA", 98.0, -2.0)],
            [snap("AAPL", 110.0, 10.0), snap("NVDA", 100.0, 0.0)],
            total_pnl=3.0, sessions_since_prior=1)
        assert sum(e["contribution_usd"] for e in out) == pytest.approx(3.0)
        assert sum(e["contribution_pct"] for e in out) == pytest.approx(100.0)

    def test_a_loser_is_reported_as_a_negative_share_not_a_wild_one(self):
        out = compute_contributions(
            [snap("NVDA", 90.0, -10.0)], [snap("NVDA", 100.0, 0.0)],
            total_pnl=-10.0, sessions_since_prior=1)
        assert out[0]["contribution_usd"] == pytest.approx(-10.0)
        assert out[0]["contribution_pct"] == pytest.approx(100.0)

    def test_output_is_ordered_by_ticker(self):
        out = compute_contributions(
            [snap("NVDA", 100.0, 0.0), snap("AAPL", 100.0, 0.0)],
            [snap("NVDA", 100.0, 0.0), snap("AAPL", 100.0, 0.0)],
            total_pnl=1.0, sessions_since_prior=1)
        assert [e["ticker"] for e in out] == ["AAPL", "NVDA"]


class TestItRefusesRatherThanApproximating:
    """Three cases where a number could be produced and would be the
    original bug in a new disguise."""

    def test_a_MULTI_SESSION_GAP_yields_no_percentage(self):
        # EXACTLY THE 17 SEPT STATE: snapshots on the 5th and the 17th,
        # eight sessions apart, against a one-day P&L figure. This is
        # the case that produced -188.48%.
        out = compute_contributions(
            [snap("NVDA", 90.0, -10.0)], [snap("NVDA", 100.0, 0.0)],
            total_pnl=137.18, sessions_since_prior=8)
        assert out[0]["contribution_pct"] is None
        assert out[0]["contribution_usd"] is None
        assert "8 session(s) back" in out[0]["basis"]
        assert "meaningless" in out[0]["basis"]

    def test_no_prior_snapshot_at_all_is_stated_not_zeroed(self):
        out = compute_contributions(
            [snap("AAPL", 100.0, 0.0)], [], total_pnl=50.0,
            sessions_since_prior=None)
        assert out[0]["contribution_pct"] is None
        assert "first close" in out[0]["basis"]

    def test_a_position_opened_today_has_no_prior_to_difference(self):
        # The book's other positions are still attributable; only the
        # new one abstains.
        out = compute_contributions(
            [snap("AAPL", 115.0, 15.0), snap("GOOGL", 200.0, 0.0)],
            [snap("AAPL", 110.0, 10.0)],
            total_pnl=5.0, sessions_since_prior=1)
        by = {e["ticker"]: e for e in out}
        assert by["AAPL"]["contribution_pct"] == pytest.approx(100.0)
        assert by["GOOGL"]["contribution_pct"] is None
        assert "opened since the last close" in by["GOOGL"]["basis"]

    def test_a_zero_pnl_day_gives_the_dollar_change_but_no_percentage(self):
        # A share of zero is undefined. The old code returned 0.0,
        # which reads as "this position contributed nothing" on a day
        # it may well have moved.
        out = compute_contributions(
            [snap("AAPL", 115.0, 15.0)], [snap("AAPL", 110.0, 10.0)],
            total_pnl=0.0, sessions_since_prior=1)
        assert out[0]["contribution_usd"] == pytest.approx(5.0)
        assert out[0]["contribution_pct"] is None
        assert "undefined" in out[0]["basis"]

    def test_a_missing_figure_on_either_snapshot_abstains(self):
        out = compute_contributions(
            [snap("AAPL", None, 15.0)], [snap("AAPL", 110.0, 10.0)],
            total_pnl=10.0, sessions_since_prior=1)
        assert out[0]["contribution_usd"] is None
        assert "missing" in out[0]["basis"]


class TestEveryEntryExplainsItself:

    def test_a_basis_is_always_present(self):
        # The basis is what stops a None reading as a bug. Every path
        # sets one.
        cases = [
            ([snap("A", 110.0, 10.0)], [snap("A", 100.0, 0.0)], 10.0, 1),
            ([snap("A", 110.0, 10.0)], [snap("A", 100.0, 0.0)], 10.0, 8),
            ([snap("A", 110.0, 10.0)], [], 10.0, None),
            ([snap("A", 110.0, 10.0)], [snap("A", 100.0, 0.0)], 0.0, 1),
        ]
        for today_rows, prior_rows, total, sessions in cases:
            out = compute_contributions(today_rows, prior_rows, total, sessions)
            assert out[0]["basis"].strip(), (today_rows, sessions)

    def test_an_empty_book_produces_an_empty_list(self):
        assert compute_contributions([], [], 0.0, 1) == []

    def test_the_percentage_fits_the_numeric_6_3_column(self):
        # attribution.contribution_pct is NUMERIC(6,3) — max +/-999.999.
        # The old stock-over-flow ratio could exceed that and raise a
        # numeric overflow on insert; a daily-over-daily ratio on a
        # real book does not.
        out = compute_contributions(
            [snap("A", 115.0, 15.0), snap("B", 98.0, -2.0)],
            [snap("A", 110.0, 10.0), snap("B", 100.0, 0.0)],
            total_pnl=3.0, sessions_since_prior=1)
        for e in out:
            assert abs(e["contribution_pct"]) < 1000
