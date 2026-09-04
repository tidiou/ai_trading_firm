"""
D3 (end-of-day order sweep), D2 (buying power) and E9 (allocation link).

The through-line: an order that doesn't fill should leave a record
saying so. Before this, it lapsed overnight, the allocation behind it
was forgotten, and Otis raised a discrepancy nothing actioned — so
"we decided to buy it and didn't" read identically to "we never
decided".
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from agents import ada, otis
from core.ledger import LedgerSnapshot
from core.market_calendar import market_is_open_now
from core.models import Allocation, Order


# ---------------------------------------------------------------
# builders
# ---------------------------------------------------------------
def ledger(nav=100_000.0, positions=None):
    return LedgerSnapshot(
        nav=nav, as_of=date(2026, 9, 1), staleness_sessions=1,
        positions=positions or {},
    )


def alloc(action="new_position", target_pct=5.0, ticker="AAPL", alloc_id=42):
    a = Allocation(
        allocation_date=date(2026, 9, 2), ticker=ticker,
        action=action, target_size_pct=Decimal(str(target_pct)),
    )
    a.id = alloc_id
    return a


class StubAlpaca:
    def __init__(self, buying_power=100_000.0, equity=100_000.0):
        self.buying_power = buying_power
        self.equity = equity
        self.submitted = []
        self.cancelled = []
        self.order_states = {}

    def get_latest_quote(self, ticker):
        return {"bid": 99.0, "ask": 100.0, "mid": 99.5,
                "spread_pct": 0.05, "used_fallback_trade_price": False}

    def get_account(self):
        return {"cash": self.buying_power, "equity": self.equity,
                "last_equity": self.equity, "buying_power": self.buying_power}

    def get_buying_power(self):
        self.calls.append("get_buying_power") if hasattr(self, "calls") else None
        return getattr(self, "buying_power", 1_000_000.0)

    def get_position(self, ticker):
        return None

    def submit_limit_order(self, ticker, side, qty, limit_price):
        self.submitted.append((ticker, side, qty, limit_price))
        return {"alpaca_order_id": "stub-1", "status": "accepted"}

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return {"cancelled": True, "error": None}

    def get_order_by_id(self, order_id):
        return self.order_states.get(
            order_id, {"status": "canceled", "filled_qty": 0.0, "filled_avg_price": None}
        )


@pytest.fixture
def broker(monkeypatch):
    stub = StubAlpaca()
    monkeypatch.setattr(ada, "alpaca_client", stub)
    monkeypatch.setattr(ada, "is_trading_enabled", lambda: True)
    return stub


# ===============================================================
# E9 — the order carries its allocation
# ===============================================================
def test_every_order_path_records_its_allocation(broker):
    """
    Set on EVERY path, including refusals — those are precisely the ones
    worth tracing back. Without it, Clara's approval-chain audit ran on
    ticker/date joins and the sweep could not say which decision went
    unfilled.
    """
    submitted = ada.execute_allocation(date(2026, 9, 2), alloc(), 8.0, ledger())
    assert submitted["allocation_id"] == 42

    refused = ada._order_result(alloc(alloc_id=7), "halted_by_operator")
    assert refused["allocation_id"] == 7


# ===============================================================
# D2 — buying power
# ===============================================================
def test_order_is_clipped_to_available_buying_power(monkeypatch):
    """
    NAV says a 5% target is 5,000. Buying power is 2,000 — a sale that
    hasn't settled. Clipping to 1,960 (2,000 x 0.98) beats an opaque
    rejection from the broker.
    """
    stub = StubAlpaca(buying_power=2_000.0, equity=100_000.0)
    monkeypatch.setattr(ada, "alpaca_client", stub)
    monkeypatch.setattr(ada, "is_trading_enabled", lambda: True)

    result = ada.execute_allocation(date(2026, 9, 2), alloc(target_pct=5.0), 8.0, ledger())

    assert result["status"] == "accepted"
    assert result["shares"] == 19.0, "1,960 / 100 = 19 whole shares"


def test_full_size_when_buying_power_is_ample(broker):
    result = ada.execute_allocation(date(2026, 9, 2), alloc(target_pct=5.0), 8.0, ledger())
    assert result["shares"] == 50.0, "5,000 / 100, unclipped"


def test_no_buying_power_is_a_named_rejection(monkeypatch):
    """
    'Rejected because we're out of money' has to be distinguishable from
    any other rejection, or you cannot diagnose either.
    """
    stub = StubAlpaca(buying_power=10.0, equity=100_000.0)
    monkeypatch.setattr(ada, "alpaca_client", stub)
    monkeypatch.setattr(ada, "is_trading_enabled", lambda: True)

    result = ada.execute_allocation(date(2026, 9, 2), alloc(target_pct=5.0), 8.0, ledger())

    assert result["status"] == "rejected_insufficient_buying_power"
    assert stub.submitted == []


def test_sells_are_not_clipped(monkeypatch):
    """Buying power constrains buying. Selling releases cash rather than
    consuming it."""
    stub = StubAlpaca(buying_power=0.0, equity=100_000.0)
    stub.get_position = lambda t: {"qty": 30.0, "market_value": 3_000.0}
    monkeypatch.setattr(ada, "alpaca_client", stub)
    monkeypatch.setattr(ada, "is_trading_enabled", lambda: True)

    result = ada.execute_allocation(
        date(2026, 9, 2), alloc(action="exit", target_pct=0.0), 8.0,
        ledger(positions={"AAPL": {
            "ticker": "AAPL", "shares": 30.0, "avg_cost": 90.0,
            "market_value": 3_000.0, "weight_pct": 3.0, "sector": "Technology"}}),
    )
    assert result["status"] == "accepted"
    assert stub.submitted[0][1] == "sell"


# ===============================================================
# D3 — the end-of-day sweep
# ===============================================================
class SweepSession:
    def __init__(self, orders):
        self._orders = orders

    def query(self, _m): return self
    def filter(self, *_a, **_k): return self
    def first(self): return None
    def all(self): return self._orders


def _order(status, order_id="live-1", allocation_id=42):
    o = Order(
        order_date=date(2026, 9, 2), ticker="AAPL", action="new_position",
        order_type="limit", limit_price=Decimal("100.00"), shares=Decimal("10"),
        status=status, alpaca_order_id=order_id,
    )
    o.allocation_id = allocation_id
    return o


@pytest.fixture
def closed_market(monkeypatch):
    monkeypatch.setattr(otis, "market_is_open_now", lambda: False)


def test_an_unfilled_order_is_cancelled_and_marked(monkeypatch, closed_market):
    stub = StubAlpaca()
    monkeypatch.setattr(otis, "alpaca_client", stub)

    order = _order("accepted")
    expired, filled = otis._sweep_unfilled_orders(
        SweepSession([order]), date(2026, 9, 2), booked_ids=set()
    )

    assert stub.cancelled == ["live-1"], "cancelled deliberately, not left to lapse"
    assert order.status == "expired_unfilled"
    assert len(expired) == 1
    assert expired[0]["allocation_id"] == 42, "the lost intent is identifiable"
    assert filled == []


def test_the_sweep_stands_down_while_the_session_is_open(monkeypatch):
    """
    THE GUARD THAT MATTERS. Otis normally runs after the close, but
    running him mid-session to look at the books must not cancel orders
    still capable of filling. An inspection command with a destructive
    side effect is a bad thing to leave lying around.
    """
    stub = StubAlpaca()
    monkeypatch.setattr(otis, "alpaca_client", stub)
    monkeypatch.setattr(otis, "market_is_open_now", lambda: True)

    order = _order("accepted")
    expired, filled = otis._sweep_unfilled_orders(
        SweepSession([order]), date(2026, 9, 2), booked_ids=set()
    )

    assert stub.cancelled == [], "no live order may be cancelled mid-session"
    assert order.status == "accepted", "and nothing is relabelled"
    assert (expired, filled) == ([], [])


def test_an_order_that_fills_during_cancellation_is_booked(monkeypatch, closed_market):
    """
    A real race: the order can fill between the status read and the
    cancel. That is a fill, not an error — it comes back for booking.
    """
    stub = StubAlpaca()
    stub.order_states["live-1"] = {
        "status": "filled", "filled_qty": 10.0, "filled_avg_price": 101.0,
    }
    monkeypatch.setattr(otis, "alpaca_client", stub)

    order = _order("accepted")
    expired, filled = otis._sweep_unfilled_orders(
        SweepSession([order]), date(2026, 9, 2), booked_ids=set()
    )

    assert expired == []
    assert len(filled) == 1
    assert order.status == "filled"
    assert float(order.fill_price) == 101.0
    assert float(order.slippage_bps) == pytest.approx(100.0)


def test_terminal_orders_are_left_alone(monkeypatch, closed_market):
    stub = StubAlpaca()
    monkeypatch.setattr(otis, "alpaca_client", stub)

    orders = [
        _order("filled", "a"),
        _order("halted_by_operator", "b"),
        _order("rejected_post_trade_limit", "c"),
        _order("expired_unfilled", "d"),
    ]
    expired, filled = otis._sweep_unfilled_orders(
        SweepSession(orders), date(2026, 9, 2), booked_ids=set()
    )

    assert stub.cancelled == [], "nothing already settled should be touched"
    assert (expired, filled) == ([], [])


def test_a_failed_cancel_is_recorded_not_raised(monkeypatch, closed_market):
    """
    Alpaca refuses a cancel on an already-terminal order. Losing that
    race is legitimate and must not fail the whole reconciliation.
    """
    stub = StubAlpaca()
    stub.cancel_order = lambda oid: {"cancelled": False, "error": "order is not cancelable"}
    monkeypatch.setattr(otis, "alpaca_client", stub)

    order = _order("accepted")
    expired, _ = otis._sweep_unfilled_orders(
        SweepSession([order]), date(2026, 9, 2), booked_ids=set()
    )

    assert len(expired) == 1
    assert expired[0]["cancel_confirmed"] is False
    assert "not cancelable" in expired[0]["cancel_error"]


# ===============================================================
# the session-hours check itself
# ===============================================================
@pytest.mark.parametrize("when,expected", [
    (datetime(2026, 9, 2, 14, 0, tzinfo=timezone.utc), True),   # 10:00 ET
    (datetime(2026, 9, 2, 19, 30, tzinfo=timezone.utc), True),  # 15:30 ET
    (datetime(2026, 9, 2, 21, 0, tzinfo=timezone.utc), False),  # 17:00 ET
    (datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc), False),  # 08:00 ET
    (datetime(2026, 9, 5, 16, 0, tzinfo=timezone.utc), False),  # Saturday
])
def test_market_hours(when, expected):
    assert market_is_open_now(when) is expected
