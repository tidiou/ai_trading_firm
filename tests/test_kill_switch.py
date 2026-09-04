"""
The kill switch (D1), under test.

Two halves:

  1. The control state itself — latest row wins, bootstrap defaults to
     enabled, a reason is mandatory in both directions.

  2. Ada's enforcement — and specifically the two properties that make
     this a kill switch rather than a second circuit breaker:
     it stops SELLS as well as buys, and it is re-read before EVERY
     submission rather than once per run.

No database and no broker. The control functions take their session
from a monkeypatched `session_scope`, and Ada's Alpaca client is
stubbed — which also means a bug in these tests can never place an
order, which felt worth guaranteeing structurally rather than by
being careful.
"""

from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

import core.trading_control as tc
from agents import ada
from core.ledger import LedgerSnapshot
from core.models import Allocation, TradingControl


# ---------------------------------------------------------------
# a fake session that holds TradingControl rows in a list
# ---------------------------------------------------------------
class FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def order_by(self, *_a, **_k):
        return FakeQuery(sorted(self._rows, key=lambda r: r.id, reverse=True))

    def limit(self, n):
        return FakeQuery(self._rows[:n])

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class FakeSession:
    def __init__(self, store):
        self.store = store

    def query(self, _model):
        return FakeQuery(self.store)

    def add(self, row):
        row.id = len(self.store) + 1
        if row.changed_at is None:
            row.changed_at = datetime.now(timezone.utc)
        self.store.append(row)


@pytest.fixture
def store(monkeypatch):
    rows = []

    @contextmanager
    def fake_scope():
        yield FakeSession(rows)

    monkeypatch.setattr(tc, "session_scope", fake_scope)
    return rows


def row(enabled, by="malick", reason="test", rid=None):
    r = TradingControl(trading_enabled=enabled, changed_by=by, reason=reason)
    r.id = rid
    r.changed_at = datetime.now(timezone.utc)
    return r


# ===============================================================
# 1. control state
# ===============================================================
def test_empty_table_reads_as_enabled_bootstrap(store):
    """A fresh install has no rows and must still work. This is the one
    permitted default-to-enabled path, and it is flagged as bootstrap
    so it can be told apart from a real enabled row."""
    state = tc.get_state()
    assert state.trading_enabled is True
    assert state.bootstrap is True


def test_halt_then_read_reports_halted(store):
    tc.halt("investigating fill discrepancy", changed_by="malick")
    state = tc.get_state()
    assert state.trading_enabled is False
    assert state.bootstrap is False
    assert state.reason == "investigating fill discrepancy"
    assert state.changed_by == "malick"


def test_latest_row_wins_not_the_first(store):
    tc.halt("something looked wrong", changed_by="malick")
    tc.resume("root cause fixed and verified", changed_by="malick")
    assert tc.is_trading_enabled() is True

    tc.halt("second incident", changed_by="malick")
    assert tc.is_trading_enabled() is False


def test_history_is_append_only_nothing_is_mutated(store):
    tc.halt("first", changed_by="a")
    tc.resume("second", changed_by="b")
    tc.halt("third", changed_by="c")

    assert len(store) == 3, "halt/resume must INSERT, never update in place"
    assert [r.reason for r in store] == ["first", "second", "third"]


@pytest.mark.parametrize("bad_reason", ["", "   ", None])
def test_reason_is_mandatory_in_both_directions(store, bad_reason):
    """A halt with no reason is unreadable tomorrow; a RESUME with no
    reason is how a halt gets lifted because it was in the way rather
    than because it was resolved."""
    with pytest.raises(ValueError):
        tc.halt(bad_reason)
    with pytest.raises(ValueError):
        tc.resume(bad_reason)


def test_changed_by_defaults_to_the_os_user(store, monkeypatch):
    monkeypatch.setattr(tc.getpass, "getuser", lambda: "someone")
    tc.halt("no --by given")
    assert tc.get_state().changed_by == "someone"


# ===============================================================
# 2. Ada's enforcement
# ===============================================================
class StubAlpaca:
    """Records what it was asked to do. submit_limit_order failing the
    test outright if called while halted is the point of the class."""

    def __init__(self):
        self.submitted = []

    def get_latest_quote(self, ticker):
        return {"bid": 99.0, "ask": 100.0, "mid": 99.5,
                "spread_pct": 0.05, "used_fallback_trade_price": False}

    def get_account(self):
        return {"cash": 50_000.0, "equity": 100_000.0,
                "last_equity": 100_000.0, "buying_power": 100_000.0}

    def get_buying_power(self):
        self.calls.append("get_buying_power") if hasattr(self, "calls") else None
        return getattr(self, "buying_power", 1_000_000.0)

    def get_position(self, ticker):
        return {"qty": 50.0, "market_value": 5_000.0}

    def submit_limit_order(self, ticker, side, qty, limit_price):
        self.submitted.append((ticker, side, qty, limit_price))
        return {"alpaca_order_id": "stub-order-1", "status": "accepted"}


@pytest.fixture
def broker(monkeypatch):
    stub = StubAlpaca()
    monkeypatch.setattr(ada, "alpaca_client", stub)
    return stub


def alloc(action, target_pct):
    return Allocation(
        allocation_date=date(2026, 9, 2),
        ticker="AAPL",
        action=action,
        target_size_pct=Decimal(str(target_pct)),
    )


def ledger():
    """
    Portfolio state as Otis would have left it: AAPL at 5,000 of a
    100,000 book. Ada sizes from this rather than from the broker
    (D5), so these tests supply it explicitly.
    """
    return LedgerSnapshot(
        nav=100_000.0,
        as_of=date(2026, 9, 1),
        staleness_sessions=1,
        positions={"AAPL": {
            "ticker": "AAPL", "shares": 50.0, "avg_cost": 100.0,
            "market_value": 5_000.0, "weight_pct": 5.0, "sector": "Technology",
        }},
    )


def test_halted_blocks_a_buy(broker, monkeypatch):
    monkeypatch.setattr(ada, "is_trading_enabled", lambda: False)
    result = ada.execute_allocation(date(2026, 9, 2), alloc("add", 7.0), 8.0, ledger())

    assert result["status"] == "halted_by_operator"
    assert result["alpaca_order_id"] is None
    assert broker.submitted == [], "nothing may reach the broker while halted"


@pytest.mark.parametrize("action,target", [("exit", 0.0), ("trim", 2.5)])
def test_halted_blocks_sells_too(broker, monkeypatch, action, target):
    """
    THE PROPERTY THAT DISTINGUISHES THIS FROM THE CIRCUIT BREAKER.

    Nora's drawdown breaker deliberately still permits exits and trims —
    stopping someone de-risking would be worse than no breaker. The kill
    switch is the opposite: you reach for it when you no longer trust
    the system, and a system you don't trust should not be choosing what
    to sell either.
    """
    monkeypatch.setattr(ada, "is_trading_enabled", lambda: False)
    result = ada.execute_allocation(date(2026, 9, 2), alloc(action, target), 8.0, ledger())

    assert result["status"] == "halted_by_operator"
    assert broker.submitted == [], f"{action} must be blocked while halted"


def test_not_halted_submits_normally(broker, monkeypatch):
    monkeypatch.setattr(ada, "is_trading_enabled", lambda: True)
    result = ada.execute_allocation(date(2026, 9, 2), alloc("add", 7.0), 8.0, ledger())

    assert result["status"] == "accepted"
    assert result["alpaca_order_id"] == "stub-order-1"
    assert len(broker.submitted) == 1


def test_state_is_reread_before_every_order_not_once_per_run(broker, monkeypatch):
    """
    A cycle with several orders can span minutes. If the switch were
    read once at the top of the run, flipping it mid-batch would do
    nothing for the orders already queued behind the one you were
    worried about — which is precisely the moment you flipped it.

    Here the stub returns True on the first read and False on every read
    after, so the second order must be blocked.
    """
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        return calls["n"] == 1

    monkeypatch.setattr(ada, "is_trading_enabled", flaky)

    first = ada.execute_allocation(date(2026, 9, 2), alloc("add", 7.0), 8.0, ledger())
    second = ada.execute_allocation(date(2026, 9, 2), alloc("add", 7.0), 8.0, ledger())

    assert first["status"] == "accepted"
    assert second["status"] == "halted_by_operator"
    assert len(broker.submitted) == 1, "only the pre-halt order may have been sent"


def test_halt_is_checked_after_sizing_so_the_intent_is_still_recorded(broker, monkeypatch):
    """
    A halted order is recorded with the price it would have used, not as
    a blank row. A halted day should still show you what the desk would
    have done — that is the diagnostic you want during an incident.
    """
    monkeypatch.setattr(ada, "is_trading_enabled", lambda: False)
    result = ada.execute_allocation(date(2026, 9, 2), alloc("add", 7.0), 8.0, ledger())

    assert result["status"] == "halted_by_operator"
    assert result["limit_price"] == 100.0
    assert result["ticker"] == "AAPL"
    assert result["action"] == "add"
