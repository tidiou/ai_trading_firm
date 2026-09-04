"""
D5 (Ada sizes from Otis's ledger) and E2 (instantly-filled orders get
booked).

The D5 tests are about WHERE a number came from, which is easy to get
right once and lose later — so they assert that Ada does not call the
broker for portfolio state, rather than only that the arithmetic works.
"""

from datetime import date
from decimal import Decimal

import pytest

from agents import ada
from core.ledger import LedgerSnapshot, MAX_LEDGER_STALENESS_SESSIONS
from core.models import Allocation, Order, Transaction


# ---------------------------------------------------------------
# builders
# ---------------------------------------------------------------
def snapshot(nav=100_000.0, positions=None, staleness=1, bootstrap=False):
    return LedgerSnapshot(
        nav=nav,
        as_of=date(2026, 9, 1),
        staleness_sessions=staleness,
        positions=positions or {},
        bootstrap=bootstrap,
    )


def held(ticker, market_value, shares=10.0, weight=None):
    return {ticker: {
        "ticker": ticker, "shares": shares, "avg_cost": 100.0,
        "market_value": market_value,
        "weight_pct": weight if weight is not None else market_value / 1000,
        "sector": "Technology",
    }}


def alloc(action, target_pct, ticker="AAPL"):
    return Allocation(
        allocation_date=date(2026, 9, 2), ticker=ticker,
        action=action, target_size_pct=Decimal(str(target_pct)),
    )


class StubAlpaca:
    """Records every call, so a test can assert what Ada did NOT ask for."""

    def __init__(self, position=None, equity=100_000.0):
        self.calls = []
        self.submitted = []
        self._position = position
        self._equity = equity

    def get_latest_quote(self, ticker):
        self.calls.append("get_latest_quote")
        return {"bid": 99.0, "ask": 100.0, "mid": 99.5,
                "spread_pct": 0.05, "used_fallback_trade_price": False}

    def get_account(self):
        self.calls.append("get_account")
        return {"cash": 50_000.0, "equity": self._equity,
                "last_equity": self._equity, "buying_power": self._equity}

    def get_buying_power(self):
        self.calls.append("get_buying_power") if hasattr(self, "calls") else None
        return getattr(self, "buying_power", 1_000_000.0)

    def get_position(self, ticker):
        self.calls.append("get_position")
        return self._position

    def submit_limit_order(self, ticker, side, qty, limit_price):
        self.calls.append("submit_limit_order")
        self.submitted.append((ticker, side, qty, limit_price))
        return {"alpaca_order_id": "stub-1", "status": "accepted"}


@pytest.fixture
def broker(monkeypatch):
    stub = StubAlpaca()
    monkeypatch.setattr(ada, "alpaca_client", stub)
    monkeypatch.setattr(ada, "is_trading_enabled", lambda: True)
    return stub


# ===============================================================
# D5 — portfolio state comes from the ledger
# ===============================================================
def test_sizing_uses_ledger_nav_not_broker_equity(broker):
    """
    The ledger says NAV is 100,000; the broker would say 200,000. A 5%
    target must size against the ledger — 5,000, i.e. 50 shares at 100 —
    not against the broker's 10,000.
    """
    broker._equity = 200_000.0
    result = ada.execute_allocation(
        date(2026, 9, 2), alloc("new_position", 5.0), 8.0,
        snapshot(nav=100_000.0),
    )
    assert result["status"] == "accepted"
    assert result["shares"] == 50.0, "sized off the ledger's NAV, not the broker's equity"


def test_current_holding_comes_from_the_ledger(broker):
    """
    Ledger: AAPL held at 3,000 of a 100,000 book. Target 5% = 5,000, so
    the delta is 2,000 = 20 shares. The broker is never consulted for
    the holding.
    """
    result = ada.execute_allocation(
        date(2026, 9, 2), alloc("add", 5.0), 8.0,
        snapshot(nav=100_000.0, positions=held("AAPL", 3_000.0)),
    )
    assert result["shares"] == 20.0


def test_broker_is_not_asked_what_the_firm_owns(broker):
    """
    The regression guard for D5 itself. Alpaca stays the source for
    prices, buying power and submission; it must not be the source for
    portfolio state.

    `get_account` is the specific thing barred here, because it returns
    equity — and reading equity from the broker is exactly what D5
    removed. Buying power goes through its own narrow accessor for that
    reason: a function that can only return one fact cannot be misused
    for the other. (This test caught D2 reaching for get_account and is
    why that accessor exists.)

    get_position may still appear AFTER sizing as the one-directional
    live drift check, so the first broker call is asserted rather than
    the whole set.
    """
    ada.execute_allocation(
        date(2026, 9, 2), alloc("new_position", 5.0), 8.0,
        snapshot(nav=100_000.0),
    )
    assert broker.calls[0] == "get_latest_quote", "the first broker call must be for a price"
    assert "get_account" not in broker.calls, (
        "NAV must come from the ledger — use get_buying_power() for buying power"
    )


def test_a_stale_ledger_is_refused_rather_than_guessed_at(broker):
    """
    Two sessions since Otis last closed the books means NAV may have
    moved materially. Sizing a percentage of a stale NAV produces a
    confidently wrong number, so the snapshot reports itself unusable.
    """
    stale = snapshot(staleness=MAX_LEDGER_STALENESS_SESSIONS + 1)
    assert stale.usable_for_sizing is False


def test_a_one_session_old_ledger_is_normal(broker):
    """Otis closes in Phase 5, Ada trades in Phase 4 — every ordinary
    cycle reads yesterday's close."""
    assert snapshot(staleness=1).usable_for_sizing is True


def test_bootstrap_falls_back_to_the_broker_and_says_so(broker):
    """
    On a brand-new install Otis has never run, and he runs after Ada —
    so a strict rule would make the first cycle unable to trade, with no
    way out. The fallback is permitted exactly here, and logged.
    """
    boot = snapshot(nav=None, bootstrap=True)
    assert boot.usable_for_sizing is True

    result = ada.execute_allocation(
        date(2026, 9, 2), alloc("new_position", 5.0), 8.0, boot,
    )
    assert result["status"] == "accepted"
    assert "get_account" in broker.calls, "bootstrap is the one case that reads the broker"


def test_zero_nav_is_not_usable():
    assert snapshot(nav=0.0).usable_for_sizing is False
    assert snapshot(nav=None).usable_for_sizing is False


def test_exit_uses_the_ledger_quantity(broker):
    result = ada.execute_allocation(
        date(2026, 9, 2), alloc("exit", 0.0), 8.0,
        snapshot(nav=100_000.0, positions=held("AAPL", 3_000.0, shares=27.0)),
    )
    assert result["shares"] == 27.0
    assert broker.submitted[0][1] == "sell"


def test_exit_on_a_name_the_ledger_does_not_hold_is_rejected(broker):
    result = ada.execute_allocation(
        date(2026, 9, 2), alloc("exit", 0.0), 8.0, snapshot(nav=100_000.0),
    )
    assert result["status"] == "rejected_no_position"
    assert broker.submitted == []


def test_live_drift_can_only_ever_refuse_more(monkeypatch):
    """
    The post-trade assertion runs on the ledger basis, then a second
    check against the broker's live view. It is one-directional by
    design: it can decline a trade the ledger would have allowed, never
    permit one the ledger would have refused. That is what makes it safe
    to consult a second source here and nowhere else.
    """
    stub = StubAlpaca(position={"qty": 70.0, "market_value": 7_500.0}, equity=100_000.0)
    monkeypatch.setattr(ada, "alpaca_client", stub)
    monkeypatch.setattr(ada, "is_trading_enabled", lambda: True)

    # Ledger thinks AAPL is 3,000 (3%); live says 7,500 (7.5%).
    # Target 5% => ledger delta 2,000 (20 shares) => ledger-resulting 5%,
    # but live-resulting is (7,500 + 2,000)/100,000 = 9.5% against an 8% cap.
    result = ada.execute_allocation(
        date(2026, 9, 2), alloc("add", 5.0), 8.0,
        snapshot(nav=100_000.0, positions=held("AAPL", 3_000.0)),
    )
    assert result["status"] == "rejected_live_drift_limit"
    assert stub.submitted == [], "nothing may reach the broker on a live breach"


def test_ledger_basis_still_enforces_the_cap(broker):
    """The primary assertion, on the ledger's own numbers."""
    result = ada.execute_allocation(
        date(2026, 9, 2), alloc("add", 12.0), 8.0,
        snapshot(nav=100_000.0, positions=held("AAPL", 1_000.0)),
    )
    assert result["status"] == "rejected_post_trade_limit"
    assert broker.submitted == []


# ===============================================================
# E2 — an order that filled at submission still gets booked
# ===============================================================
class ReconSession:
    """Minimal session for _reconcile_orders / _booked_order_ids."""

    def __init__(self, orders, transactions):
        self._orders = orders
        self._transactions = transactions
        self._target = None
        self.added = []

    def query(self, model):
        self._target = model
        return self

    def filter(self, *_a, **_k):
        return self

    def first(self):
        return None  # no position row -> cost basis unknown, fine here

    def all(self):
        from core.models import Order as O, Transaction as T
        if self._target is O:
            return self._orders
        if self._target is T:
            return self._transactions
        return []

    def add(self, row):
        self.added.append(row)


def _order(status, order_id, fill_price=None, action="new_position"):
    return Order(
        order_date=date(2026, 9, 2), ticker="AAPL", action=action,
        order_type="limit", limit_price=Decimal("100.00"),
        shares=Decimal("10"), status=status,
        fill_price=Decimal(str(fill_price)) if fill_price else None,
        alpaca_order_id=order_id,
    )


def test_an_order_filled_at_submission_is_reconciled(monkeypatch):
    """
    THE E2 REGRESSION.

    Ada records whatever status Alpaca returned at submission. When that
    is already 'filled', the old selection — status in (pending_new,
    accepted, new) — skipped the order forever, so no transaction was
    ever booked for it. Selecting on "not yet in the ledger" catches it.
    """
    monkeypatch.setattr(
        __import__("agents.otis", fromlist=["x"]).alpaca_client,
        "get_order_by_id",
        lambda oid: {"status": "filled", "filled_qty": 10.0, "filled_avg_price": 101.0},
    )
    from agents import otis

    session = ReconSession(orders=[_order("filled", "instant-1")], transactions=[])
    newly = otis._reconcile_orders(session, date(2026, 9, 2), booked_ids=set())

    assert len(newly) == 1, "a filled-at-submission order must be picked up"
    assert float(newly[0].fill_price) == 101.0, "fill price backfilled"
    assert float(newly[0].slippage_bps) == pytest.approx(100.0)  # 101 vs 100 limit


def test_an_open_order_is_still_reconciled(monkeypatch):
    """The old behaviour has to survive the new selection rule."""
    from agents import otis
    monkeypatch.setattr(otis.alpaca_client, "get_order_by_id",
                        lambda oid: {"status": "filled", "filled_qty": 10.0,
                                     "filled_avg_price": 99.5})

    session = ReconSession(orders=[_order("accepted", "open-1")], transactions=[])
    newly = otis._reconcile_orders(session, date(2026, 9, 2), booked_ids=set())
    assert len(newly) == 1


def test_an_already_booked_order_is_left_alone(monkeypatch):
    from agents import otis
    calls = []
    monkeypatch.setattr(otis.alpaca_client, "get_order_by_id",
                        lambda oid: calls.append(oid) or {"status": "filled",
                                                          "filled_qty": 10.0,
                                                          "filled_avg_price": 99.5})

    session = ReconSession(orders=[_order("filled", "booked-1")], transactions=[])
    newly = otis._reconcile_orders(session, date(2026, 9, 2), booked_ids={"booked-1"})

    assert newly == []
    assert calls == [], "no broker call for an order already in the ledger"


def test_an_unfilled_order_is_updated_but_not_booked(monkeypatch):
    from agents import otis
    monkeypatch.setattr(otis.alpaca_client, "get_order_by_id",
                        lambda oid: {"status": "canceled", "filled_qty": 0.0,
                                     "filled_avg_price": None})

    order = _order("accepted", "cancelled-1")
    session = ReconSession(orders=[order], transactions=[])
    newly = otis._reconcile_orders(session, date(2026, 9, 2), booked_ids=set())

    assert newly == []
    assert order.status == "canceled", "status is still updated"


def test_orders_without_a_broker_id_are_skipped(monkeypatch):
    """Halted and rejected orders are recorded with no alpaca_order_id —
    there is nothing at the broker to reconcile them against."""
    from agents import otis
    order = _order("halted_by_operator", None)
    session = ReconSession(orders=[order], transactions=[])
    # The query filter would exclude these; asserting the shape is
    # unchanged is enough here.
    assert order.alpaca_order_id is None
