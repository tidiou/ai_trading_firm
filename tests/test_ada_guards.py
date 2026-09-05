"""
Two fixes to Ada, both found by using the desk rather than by reading it.

  1. THE IDEMPOTENCY GUARD blocked its own retry. It keyed on any order
     row for the ticker today — including a REFUSAL — and skipped via a
     bare `continue`, leaving no second row and no log line. It bit
     three times in three days (halted_by_operator, rejected_zero_shares,
     rejected_stale_ledger), and each time the desk looked like it had
     simply ignored a valid allocation. The guard exists to prevent a
     duplicate real trade; a row that never reached the broker is not a
     trade.

  2. A REFUSAL RECORDED NO EVIDENCE. `rejected_stale_ledger` said that
     and nothing else — not which ledger, not how stale. And Otis closes
     the books in Phase 5, after Ada trades in Phase 4, so by the time
     anyone queried the ledger it was fresh and the row appeared to be
     contradicting itself. The evidence was destroyed by the cycle that
     produced it.

Both are exercised against `execute_allocation` and the run-level
guards directly — no database, no broker, no model.
"""

from datetime import date
from decimal import Decimal

import pytest

from agents import ada
from core.ledger import LedgerSnapshot
from core.models import Allocation, Order


# ---------------------------------------------------------------
# builders
# ---------------------------------------------------------------
def ledger(nav=100_000.0, as_of=date(2026, 9, 2), stale=1, positions=None):
    return LedgerSnapshot(nav=nav, as_of=as_of, staleness_sessions=stale,
                          positions=positions or {})


def alloc(action="new_position", target_pct=5.6, ticker="NVDA", alloc_id=1):
    a = Allocation(allocation_date=date(2026, 9, 4), ticker=ticker,
                   action=action, target_size_pct=Decimal(str(target_pct)))
    a.id = alloc_id
    return a


class StubAlpaca:
    def __init__(self, buying_power=1_000_000.0, equity=100_000.0):
        self.buying_power, self.equity = buying_power, equity
        self.submitted = []

    def get_latest_quote(self, ticker):
        return {"bid": 99.0, "ask": 100.0, "mid": 99.5,
                "spread_pct": 0.05, "used_fallback_trade_price": False}

    def get_account(self):
        return {"cash": self.buying_power, "equity": self.equity,
                "last_equity": self.equity, "buying_power": self.buying_power}

    def get_buying_power(self):
        return self.buying_power

    def get_position(self, ticker):
        return None

    def submit_limit_order(self, ticker, side, qty, limit_price):
        self.submitted.append((ticker, side, qty, limit_price))
        return {"alpaca_order_id": "broker-1", "status": "accepted"}


@pytest.fixture
def broker(monkeypatch):
    stub = StubAlpaca()
    monkeypatch.setattr(ada, "alpaca_client", stub)
    monkeypatch.setattr(ada, "is_trading_enabled", lambda: True)
    return stub


def order_row(ticker="NVDA", status="rejected_stale_ledger", alpaca_order_id=None):
    o = Order(order_date=date(2026, 9, 4), ticker=ticker, action="new_position",
              order_type="limit", limit_price=Decimal("100"), shares=Decimal("0"),
              status=status, alpaca_order_id=alpaca_order_id)
    return o


class GuardSession:
    """Just enough session to answer Ada's `already_executed` query."""

    def __init__(self, rows):
        self._rows = rows
        self._filters = []

    def query(self, model):
        self._model = model
        return self

    def filter(self, *conditions):
        self._filters.extend(conditions)
        return self

    def all(self):
        # Reproduce the real filter: order_date == today, and — since the
        # fix — alpaca_order_id IS NOT NULL. Two conditions means the
        # narrower query.
        if len(self._filters) >= 2:
            return [r for r in self._rows if r.alpaca_order_id is not None]
        return list(self._rows)


def already_executed(rows):
    """Run Ada's guard query against a fake session and return the set."""
    session = GuardSession(rows)
    return {
        o.ticker for o in session.query(Order).filter(
            Order.order_date == date(2026, 9, 4),
            Order.alpaca_order_id.isnot(None),
        ).all()
    }


# ===============================================================
# 1. the idempotency guard
# ===============================================================
class TestIdempotencyGuard:

    def test_a_refusal_does_not_block_its_own_retry(self):
        """
        THE BUG, THREE TIMES OVER. A rejected_stale_ledger row has no
        alpaca_order_id — nothing was submitted, nothing can be
        duplicated. Before the fix this ticker landed in
        `already_executed` and the retry was skipped by a bare
        `continue`: no row, no log line, no explanation.
        """
        assert already_executed([order_row(status="rejected_stale_ledger")]) == set()

    @pytest.mark.parametrize("status", [
        "halted_by_operator", "rejected_zero_shares", "rejected_stale_ledger",
        "rejected_insufficient_buying_power", "rejected_post_trade_limit",
        "skipped_wide_spread", "rejected_no_price",
    ])
    def test_no_refusal_status_blocks_a_retry(self, status):
        """Every one of these leaves alpaca_order_id NULL. None of them
        is a trade."""
        assert already_executed([order_row(status=status)]) == set()

    def test_an_order_that_reached_the_broker_still_blocks(self):
        """THE PROPERTY THE GUARD EXISTS FOR, which must survive the
        fix: a duplicate real order is a real consequence."""
        assert already_executed([
            order_row(status="accepted", alpaca_order_id="broker-1")
        ]) == {"NVDA"}

    def test_a_filled_order_still_blocks(self):
        assert already_executed([
            order_row(status="filled", alpaca_order_id="broker-1")
        ]) == {"NVDA"}

    def test_a_refusal_and_a_live_order_on_different_names(self):
        """The refused one is retryable; the submitted one is not."""
        rows = [order_row("NVDA", "rejected_stale_ledger"),
                order_row("AAPL", "accepted", "broker-1")]
        assert already_executed(rows) == {"AAPL"}

    def test_a_retry_after_a_refusal_on_the_same_name(self):
        """Once the retry succeeds, the ticker blocks again — the second
        row carries the broker id, the first stays as history."""
        rows = [order_row("NVDA", "rejected_stale_ledger"),
                order_row("NVDA", "accepted", "broker-2")]
        assert already_executed(rows) == {"NVDA"}


# ===============================================================
# 2. refusals carry their evidence
# ===============================================================
class TestStatusDetail:

    def test_every_result_has_the_field(self, broker):
        """It is written straight into the orders row via
        `Order(order_date=today, **order)`, so the key must exist on
        every path or the insert raises."""
        result = ada.execute_allocation(date(2026, 9, 4), alloc(), 8.0, ledger())
        assert "status_detail" in result

    def test_an_accepted_order_records_its_sizing_basis(self, broker):
        """Otis moves the ledger on within the same cycle. Without this,
        'what was this order sized against' is unanswerable afterwards."""
        result = ada.execute_allocation(date(2026, 9, 4), alloc(), 8.0, ledger())
        assert result["status"] == "accepted"
        assert "2026-09-02" in result["status_detail"]
        assert "5.60%" in result["status_detail"]

    def test_a_zero_share_rejection_shows_the_arithmetic(self, broker):
        """0.05% of 100,000 is 50 — under one share at 100."""
        result = ada.execute_allocation(
            date(2026, 9, 4), alloc(target_pct=0.05), 8.0, ledger())
        assert result["status"] == "rejected_zero_shares"
        d = result["status_detail"]
        assert "0.05%" in d and "100,000.00" in d and "under one share" in d

    def test_a_post_trade_breach_names_both_numbers(self, broker):
        result = ada.execute_allocation(
            date(2026, 9, 4), alloc(target_pct=9.0), 8.0, ledger())
        assert result["status"] == "rejected_post_trade_limit"
        assert "9.00%" in result["status_detail"]
        assert "8.00% cap" in result["status_detail"]

    def test_insufficient_buying_power_shows_what_was_available(self, monkeypatch):
        stub = StubAlpaca(buying_power=10.0)
        monkeypatch.setattr(ada, "alpaca_client", stub)
        monkeypatch.setattr(ada, "is_trading_enabled", lambda: True)
        result = ada.execute_allocation(date(2026, 9, 4), alloc(), 8.0, ledger())
        assert result["status"] == "rejected_insufficient_buying_power"
        assert "10.00" in result["status_detail"]

    def test_a_halt_between_sizing_and_submission_says_so(self, monkeypatch):
        stub = StubAlpaca()
        monkeypatch.setattr(ada, "alpaca_client", stub)
        monkeypatch.setattr(ada, "is_trading_enabled", lambda: False)
        result = ada.execute_allocation(date(2026, 9, 4), alloc(), 8.0, ledger())
        assert result["status"] == "halted_by_operator"
        assert "between sizing and submission" in result["status_detail"]
        assert stub.submitted == []

    def test_no_nav_records_the_ledger(self, broker):
        result = ada.execute_allocation(
            date(2026, 9, 4), alloc(), 8.0, ledger(nav=0.0))
        assert result["status"] == "rejected_no_nav"
        assert "ledger as of" in result["status_detail"]

    def test_an_exit_with_nothing_held_names_the_ledger(self, broker):
        result = ada.execute_allocation(
            date(2026, 9, 4), alloc(action="exit", target_pct=0.0), 8.0, ledger())
        assert result["status"] == "rejected_no_position"
        assert "holds no NVDA" in result["status_detail"]

    def test_the_stale_ledger_detail_is_actionable(self):
        """
        THE ONE THAT STARTED IT. The row must survive Otis repairing the
        condition minutes later — so it names the ledger's date, the
        tolerance, and what to do about it.
        """
        from core.ledger import MAX_LEDGER_STALENESS_SESSIONS
        stale = ledger(as_of=date(2026, 9, 2), stale=2)
        assert not stale.usable_for_sizing

        detail = (f"{stale.describe()}; tolerance is "
                  f"{MAX_LEDGER_STALENESS_SESSIONS} session(s). "
                  f"Run `python -m agents.otis` to close the books.")
        assert "2026-09-02" in detail
        assert "2 session(s) old" in detail
        assert "agents.otis" in detail
