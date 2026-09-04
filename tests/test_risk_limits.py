"""
The four numeric risk limits of Operating Manual §7.4, under test.

Each test names the failure it exists to prevent. They are pure — no
database, no API, no fixtures beyond hand-built ORM objects — because
every one of these limits is decided by a plain function that takes
state and returns a verdict. That is the whole point of Nora's design:
if the decision needed a live database to exercise, it would not be a
wall, it would be a workflow.

  max_position_pct       test_position_limit_*
  max_sector_pct         test_sector_limit_*
  drawdown_breaker_pct   test_drawdown_* / test_circuit_breaker_*
  min_position_count     test_position_count_*
"""

from decimal import Decimal

import pytest

from agents.nora import (
    check_hard_limits,
    check_position_count,
    check_position_weight_drift,
    check_sector_concentration,
    check_drawdown_circuit_breaker,
    sector_weights,
    SOURCE_PORTFOLIO,
)
from core.models import Position, RiskPolicyVersion, RiskBreach, DailyPnl


# ---------------------------------------------------------------
# builders
# ---------------------------------------------------------------
def policy(max_position=8.0, max_sector=25.0, drawdown=-10.0, min_count=12):
    return RiskPolicyVersion(
        max_position_pct=Decimal(str(max_position)),
        max_sector_pct=Decimal(str(max_sector)),
        drawdown_breaker_pct=Decimal(str(drawdown)),
        min_position_count=min_count,
        loss_review_pct=Decimal("-10.0"),
        profit_review_pct=Decimal("20.0"),
    )


def pos(ticker, weight, sector=None):
    return Position(
        ticker=ticker,
        shares=Decimal("10"),
        avg_cost=Decimal("100"),
        market_value=Decimal("1000"),
        weight_pct=Decimal(str(weight)),
        sector=sector,
    )


class FakeSession:
    """Stands in for a session for check_drawdown_circuit_breaker only,
    which does exactly one query. Anything more elaborate would be a
    sign the function under test is doing too much."""

    def __init__(self, rows):
        self._rows = rows

    def query(self, _model):
        return self

    def filter(self, *_a, **_k):
        return self

    def order_by(self, *_a, **_k):
        return self

    def all(self):
        return self._rows


# ===============================================================
# LIMIT 1 — max_position_pct (8%)
# ===============================================================
def test_position_limit_new_position_gets_full_headroom():
    result = check_hard_limits("NVDA", "new_position", [], policy())
    assert result["decision"] == "approved"
    assert result["max_size_pct"] == 8.0


def test_position_limit_add_gets_remaining_headroom_only():
    """
    THE REGRESSION THIS SUITE EXISTS FOR.

    An `add` to a name already at 7% used to receive a ceiling of the
    full 8% limit rather than the 1% actually remaining. Marcus then
    sized 8 x 0.7 = 5.6% and Ada bought that much MORE, landing the
    position near 12.6% against an 8% cap — with every check in the
    chain recording the trade as approved.
    """
    book = [pos("AAPL", 7.0, "Technology")]
    result = check_hard_limits("AAPL", "add", book, policy(), proposed_sector="Technology")

    assert result["decision"] == "approved"
    assert result["max_size_pct"] == pytest.approx(1.0), (
        "ceiling must be REMAINING headroom (8 - 7), never the raw limit"
    )


def test_position_limit_add_at_the_cap_is_rejected():
    book = [pos("AAPL", 8.0, "Technology")]
    result = check_hard_limits("AAPL", "add", book, policy(), proposed_sector="Technology")
    assert result["decision"] == "rejected"
    assert result["max_size_pct"] == 0.0


def test_position_limit_full_chain_cannot_exceed_the_cap():
    """
    Walks Nora -> Marcus -> Ada arithmetic end to end for a range of
    starting weights and convictions, asserting the resulting weight
    never breaches. This is the property that actually matters; the
    individual assertions above are just where it breaks first.
    """
    max_pct, nav = 8.0, 100_000.0
    tiers = {5: 1.0, 4: 0.7, 3: 0.4}

    for existing_weight in (0.0, 1.5, 4.0, 6.9, 7.99):
        for conviction, multiplier in tiers.items():
            book = [pos("AAPL", existing_weight, "Technology")] if existing_weight else []
            review = check_hard_limits(
                "AAPL", "add" if existing_weight else "new_position",
                book, policy(max_position=max_pct), proposed_sector="Technology",
            )
            if review["decision"] == "rejected":
                continue

            # Marcus: ceiling and target are RESULTING TOTAL weights.
            headroom = review["max_size_pct"]
            ceiling = existing_weight + headroom
            target = existing_weight + headroom * multiplier
            target = max(existing_weight, min(target, ceiling))

            # Ada: trades the DIFFERENCE to the target, not the target.
            current_value = nav * existing_weight / 100
            delta_dollars = nav * target / 100 - current_value
            resulting_value = current_value + max(delta_dollars, 0.0)
            resulting_weight = resulting_value / nav * 100

            assert resulting_weight <= max_pct + 1e-9, (
                f"breach: {existing_weight}% + conviction {conviction} "
                f"-> {resulting_weight:.4f}% against a {max_pct}% cap"
            )


def test_position_limit_drift_is_caught_without_any_proposal():
    """A position appreciating past the cap on price alone. Nobody
    proposes anything about it, so this only surfaces because
    monitor_portfolio() runs daily."""
    book = [pos("AAPL", 11.0, "Technology"), pos("JPM", 4.0, "Financials")]
    breaches = check_position_weight_drift(book, policy())

    assert len(breaches) == 1
    assert breaches[0]["ticker"] == "AAPL"
    assert breaches[0]["rule_violated"] == "max_position_pct"


# ===============================================================
# LIMIT 2 — max_sector_pct (25%)
# ===============================================================
def test_sector_limit_binds_even_when_the_name_has_room():
    """AAPL at 2% has 6% of position headroom, but Technology is
    already at 24% of a 25% cap — so the sector is the binding
    constraint and the ceiling must reflect it."""
    book = [
        pos("AAPL", 2.0, "Technology"),
        pos("MSFT", 8.0, "Technology"),
        pos("NVDA", 7.0, "Technology"),
        pos("GOOGL", 7.0, "Technology"),
    ]
    result = check_hard_limits("AAPL", "add", book, policy(), proposed_sector="Technology")

    assert result["decision"] == "approved"
    assert result["max_size_pct"] == pytest.approx(1.0), "25 - 24 = 1, not the 6% of position room"
    assert any("binding constraint: sector" in r for r in result["rules_checked"])


def test_sector_limit_rejects_when_sector_is_full():
    book = [pos("MSFT", 13.0, "Technology"), pos("NVDA", 12.0, "Technology")]
    result = check_hard_limits("AAPL", "new_position", book, policy(), proposed_sector="Technology")
    assert result["decision"] == "rejected"


def test_sector_limit_unknown_sector_is_its_own_bucket():
    """Unknown-sector names must not pool together — pooling would let
    real concentration hide inside a bucket labelled 'unknown'."""
    book = [pos("AAA", 20.0, None), pos("BBB", 20.0, None)]
    weights = sector_weights(book)

    assert len(weights) == 2, "two unknown names must not share one bucket"
    assert all(v == 20.0 for v in weights.values())
    # 40% of unknown-sector exposure, but no single bucket over 25%.
    assert check_sector_concentration(book, policy()) == []


def test_sector_limit_concentration_breach_is_detected():
    book = [pos("MSFT", 14.0, "Technology"), pos("NVDA", 13.0, "Technology")]
    breaches = check_sector_concentration(book, policy())
    assert len(breaches) == 1
    assert breaches[0]["rule_violated"] == "max_sector_pct:Technology"
    assert breaches[0]["current_value"] == pytest.approx(27.0)


# ===============================================================
# LIMIT 3 — drawdown_breaker_pct (-10%)
# ===============================================================
def _pnl(nav):
    return DailyPnl(
        realized_pnl=Decimal("0"), unrealized_pnl=Decimal("0"),
        total_pnl=Decimal("0"), cash_balance=Decimal("0"),
        nav=Decimal(str(nav)) if nav is not None else None,
    )


def test_drawdown_is_not_breached_within_tolerance():
    session = FakeSession([_pnl(100_000), _pnl(105_000), _pnl(99_000)])
    result = check_drawdown_circuit_breaker(session, policy())
    assert result["breached"] is False
    assert result["current_drawdown_pct"] == pytest.approx(-5.71, abs=0.01)


def test_drawdown_breaches_past_the_threshold():
    session = FakeSession([_pnl(100_000), _pnl(110_000), _pnl(95_000)])
    result = check_drawdown_circuit_breaker(session, policy())
    assert result["breached"] is True
    assert result["current_drawdown_pct"] == pytest.approx(-13.64, abs=0.01)


def test_drawdown_flat_day_after_a_good_one_is_not_a_drawdown():
    """
    The old implementation took the peak of daily total_pnl, so a day
    that made 100 followed by a flat day computed (0-100)/100 = -100%
    and tripped a -10% breaker on a day nothing happened. On NAV, the
    same two sessions are flat.
    """
    session = FakeSession([_pnl(100_000), _pnl(100_100), _pnl(100_100)])
    result = check_drawdown_circuit_breaker(session, policy())
    assert result["breached"] is False
    assert result["current_drawdown_pct"] == pytest.approx(0.0)


def test_drawdown_with_null_nav_rows_reports_insufficient_history():
    """Rows predating the nav column are skipped, not back-filled with a
    guess — the breaker stays quiet rather than firing on invented
    history."""
    session = FakeSession([])  # the query filters nav IS NOT NULL
    result = check_drawdown_circuit_breaker(session, policy())
    assert result["breached"] is False
    assert "insufficient NAV history" in result["basis"]


def test_circuit_breaker_freezes_new_risk():
    result = check_hard_limits("AAPL", "new_position", [], policy(), circuit_breaker_active=True)
    assert result["decision"] == "rejected"
    assert result["max_size_pct"] == 0.0
    assert any("circuit breaker is ACTIVE" in r for r in result["rules_checked"])


def test_circuit_breaker_still_permits_reductions():
    """A breaker that stopped you de-risking would be worse than none."""
    book = [pos("AAPL", 7.0, "Technology")]
    for action in ("exit", "trim"):
        result = check_hard_limits("AAPL", action, book, policy(), circuit_breaker_active=True)
        assert result["decision"] == "approved", f"{action} must survive a frozen book"


def test_breach_rows_carry_source_and_are_constructible():
    """
    Every breach dict is splatted into RiskBreach(**b), and
    risk_breaches.source is NOT NULL — so a missing key was an
    IntegrityError waiting for the first real breach, on the one code
    path that runs only when things are going badly.
    """
    book = [pos("AAPL", 11.0, "Technology"), pos("MSFT", 14.0, "Technology"),
            pos("NVDA", 13.0, "Technology")]
    p = policy()
    breaches = (
        check_position_weight_drift(book, p)
        + check_sector_concentration(book, p)
        + check_position_count(book, p)
    )
    assert breaches, "this book breaches several limits"
    for b in breaches:
        assert b["source"] == SOURCE_PORTFOLIO
        RiskBreach(risk_review_id=1, **b)  # raises TypeError on any key mismatch


# ===============================================================
# LIMIT 4 — min_position_count (12 names), WARN ONLY
# ===============================================================
def test_position_count_warns_below_the_minimum():
    book = [pos(f"T{i}", 2.0, "Technology") for i in range(4)]
    warnings = check_position_count(book, policy())
    assert len(warnings) == 1
    assert warnings[0]["rule_violated"] == "min_position_count"
    assert warnings[0]["current_value"] == 4
    assert warnings[0]["limit_value"] == 12


def test_position_count_silent_at_or_above_the_minimum():
    book = [pos(f"T{i}", 2.0, "Technology") for i in range(12)]
    assert check_position_count(book, policy()) == []


def test_position_count_silent_on_an_empty_book():
    """A book that hasn't started is not under-diversified."""
    assert check_position_count([], policy()) == []


def test_position_count_never_blocks_a_trade():
    """
    Warn-only is a deliberate choice, not an oversight. Enforcing a
    minimum position count as a rejection is self-defeating: the only
    way out of a 4-name book is to open more names, and blocking new
    names would make the breach permanent.
    """
    book = [pos("AAPL", 2.0, "Technology")]  # 1 name, far below the minimum
    result = check_hard_limits("NVDA", "new_position", book, policy(), proposed_sector="Technology")
    assert result["decision"] == "approved"