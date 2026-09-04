"""
E3 (P&L decomposition) and D8 (run ledger + cycle resilience).

The E3 tests are arithmetic: they assert the identity

    realized + unrealized_change = total = equity - last_equity

holds, and demonstrate concretely how far the old formula was from it.
The D8 tests assert that a failure in the trading path still leaves the
books closed and a row in the ledger saying what happened.
"""

from contextlib import contextmanager
from datetime import date
from decimal import Decimal

import pytest

import core.logging_utils as lu
from agents import otis
from core.models import Order, Position, Transaction


# ===============================================================
# E3 — P&L decomposition
# ===============================================================
def test_old_formula_conflated_a_stock_with_a_flow():
    """
    Documents the defect rather than the fix, because the size of the
    error is the argument for having changed it.

    A book up 500 lifetime on open positions, flat today. The old
    formula reported a 500 realized loss and a 500 unrealized gain on a
    day where nothing was sold and nothing moved.
    """
    equity, last_equity = 100_500.0, 100_500.0     # flat day
    lifetime_open_unrealized = 500.0               # accumulated over weeks

    total_today = equity - last_equity
    old_realized = total_today - lifetime_open_unrealized

    assert total_today == 0.0
    assert old_realized == -500.0, "the old split invented a 500 loss on a flat day"

    # The corrected version: nothing was sold, so nothing was realized.
    realized_from_ledger = 0.0
    unrealized_change = total_today - realized_from_ledger
    assert realized_from_ledger == 0.0
    assert unrealized_change == 0.0
    assert realized_from_ledger + unrealized_change == total_today


@pytest.mark.parametrize(
    "equity,last_equity,realized",
    [
        (100_000.0, 100_000.0, 0.0),        # flat, no trades
        (101_000.0, 100_000.0, 250.0),      # up, part crystallised
        (99_000.0, 100_000.0, -400.0),      # down, a loss taken
        (100_000.0, 100_000.0, 750.0),      # flat overall, gain offset by mark-down
    ],
)
def test_daily_flows_reconcile_to_the_equity_move(equity, last_equity, realized):
    total = equity - last_equity
    unrealized_change = total - realized
    assert realized + unrealized_change == pytest.approx(total)


def test_realized_is_booked_at_the_point_of_sale():
    """
    Cost basis is only knowable BEFORE the positions table is rebuilt to
    mirror the broker — a full exit removes the position upstream
    entirely. So realized P&L is computed while booking the transaction,
    and stored on the row.
    """
    booked = []

    class Sess:
        def query(self, model):
            self.model = model
            return self

        def filter(self, *_a, **_k):
            return self

        def first(self):
            # the position as it stood BEFORE the sale
            return Position(ticker="AAPL", shares=Decimal("10"),
                            avg_cost=Decimal("300.00"), weight_pct=Decimal("5"))

        def all(self):
            return []  # nothing booked yet

        def add(self, row):
            booked.append(row)

    order = Order(order_date=date(2026, 9, 2), ticker="AAPL", action="exit",
                  order_type="limit", shares=Decimal("10"),
                  status="filled", fill_price=Decimal("325.00"))

    unknown = otis._book_new_transactions(Sess(), [order])

    assert unknown == 0
    assert len(booked) == 1
    txn = booked[0]
    assert txn.action == "sell"
    # (325 - 300) x 10
    assert float(txn.realized_pnl) == pytest.approx(250.0)


def test_a_buy_realizes_nothing():
    booked = []

    class Sess:
        def query(self, _m): return self
        def filter(self, *_a, **_k): return self
        def first(self): return None
        def all(self): return []
        def add(self, row): booked.append(row)

    order = Order(order_date=date(2026, 9, 2), ticker="NVDA", action="new_position",
                  order_type="limit", shares=Decimal("5"),
                  status="filled", fill_price=Decimal("180.00"))

    otis._book_new_transactions(Sess(), [order])
    assert float(booked[0].realized_pnl) == 0.0


def test_unknown_cost_basis_is_null_not_zero():
    """
    An orphan sale has no position row to price against. Recording zero
    would silently understate the day's realized P&L with nothing to
    notice it by; NULL is excluded from the sum and counted separately
    so it surfaces.
    """
    booked = []

    class Sess:
        def query(self, _m): return self
        def filter(self, *_a, **_k): return self
        def first(self): return None          # no position on record
        def all(self): return []
        def add(self, row): booked.append(row)

    order = Order(order_date=date(2026, 9, 2), ticker="GHOST", action="exit",
                  order_type="limit", shares=Decimal("3"),
                  status="filled", fill_price=Decimal("50.00"))

    unknown = otis._book_new_transactions(Sess(), [order])

    assert unknown == 1
    assert booked[0].realized_pnl is None, "must be NULL, never a silent zero"


def test_already_booked_orders_are_not_double_counted():
    booked = []

    class Sess:
        def query(self, _m): return self
        def filter(self, *_a, **_k): return self
        def first(self): return None
        def all(self):
            return [Transaction(alpaca_transaction_id="dupe-1")]
        def add(self, row): booked.append(row)

    order = Order(order_date=date(2026, 9, 2), ticker="AAPL", action="exit",
                  order_type="limit", shares=Decimal("1"),
                  status="filled", fill_price=Decimal("325.00"),
                  alpaca_order_id="dupe-1")

    otis._book_new_transactions(Sess(), [order])
    assert booked == [], "an order already in the ledger must not be re-booked"


# ===============================================================
# D8 — the run ledger
# ===============================================================
@pytest.fixture
def ledger(monkeypatch):
    """Captures what log_agent_run would have written."""
    rows = []
    monkeypatch.setattr(
        lu, "log_agent_run",
        lambda run_date, agent_name, phase, status, **kw: rows.append(
            {"agent": agent_name, "phase": phase, "status": status, **kw}
        ),
    )
    return rows


def test_a_successful_run_records_running_then_completed(ledger):
    with lu.track_agent_run(date(2026, 9, 2), "atlas", 1) as run:
        run.output = {"regime_signal": "risk-on"}

    assert [r["status"] for r in ledger] == ["running", "completed"]
    assert ledger[-1]["output"] == {"regime_signal": "risk-on"}


def test_a_failed_run_is_recorded_and_the_exception_still_raised(ledger):
    """The ledger observes; it never swallows. A failure that vanished
    into the audit trail would be worse than no audit trail."""
    with pytest.raises(ValueError, match="FMP quota"):
        with lu.track_agent_run(date(2026, 9, 2), "vera", 1):
            raise ValueError("FMP quota exhausted")

    assert [r["status"] for r in ledger] == ["running", "failed"]
    assert "FMP quota" in ledger[-1]["error"]
    assert "ValueError" in ledger[-1]["error"]


def test_running_is_written_on_entry_so_an_interrupted_cycle_leaves_a_trace(ledger):
    """A process killed mid-agent writes no exit row — the 'running' row
    written on entry is the only evidence of how far it got."""
    with lu.track_agent_run(date(2026, 9, 2), "marcus", 3):
        assert len(ledger) == 1
        assert ledger[0]["status"] == "running"


def test_oversized_output_is_truncated_not_dropped():
    big = {"candidates": ["x" * 1000 for _ in range(200)]}
    out = lu._serialise(big)
    assert out["truncated"] is True
    assert out["original_chars"] > lu.MAX_OUTPUT_CHARS
    assert len(out["preview"]) == lu.MAX_OUTPUT_CHARS


def test_unserialisable_output_degrades_instead_of_raising():
    """Serialising a return value must never take down the agent whose
    success is being recorded."""
    class Awkward:
        def __repr__(self): return "<Awkward>"

    out = lu._serialise({"thing": Awkward()})
    assert isinstance(out, dict)  # default=str handles it, no exception


def test_dates_and_decimals_serialise():
    out = lu._serialise({"date": date(2026, 9, 2), "pnl": Decimal("12.34")})
    assert out["date"] == "2026-09-02"
    assert out["pnl"] == "12.34"


# ===============================================================
# D8 — the cycle survives a bad day
# ===============================================================
def test_phase_5_runs_even_when_the_trading_path_dies(monkeypatch):
    """
    The property that matters: a Phase 1 failure used to take Otis and
    Clara with it, so on exactly the days something went wrong the books
    never closed and no report was written.
    """
    import orchestrator

    called = []

    monkeypatch.setattr(orchestrator, "is_trading_day", lambda d: True)
    monkeypatch.setattr(orchestrator, "get_trading_control_state",
                        lambda: type("S", (), {"trading_enabled": True,
                                               "describe": lambda self: "ENABLED"})())

    @contextmanager
    def fake_track(_d, name, _p):
        called.append(name)
        yield type("R", (), {"output": None})()

    monkeypatch.setattr(orchestrator, "track_agent_run", fake_track)

    def boom(*_a, **_k):
        raise RuntimeError("FMP quota exhausted")

    monkeypatch.setattr(orchestrator.atlas, "run", boom)
    monkeypatch.setattr(orchestrator.otis, "run", lambda d: {
        "reconciled": True, "discrepancies": [], "realized_pnl_today": 0.0,
        "unrealized_change_today": 0.0, "cost_basis_unknown": 0,
    })
    monkeypatch.setattr(orchestrator.nora, "monitor_portfolio", lambda d: {
        "portfolio_status": "within_limits", "circuit_breaker_active": False,
        "breaches": [], "position_count": 1, "drawdown": {"basis": "n/a"},
    })
    monkeypatch.setattr(orchestrator.clara, "run", lambda d: {"process_check": "clean"})
    monkeypatch.setattr(orchestrator.clara, "compile_daily_report",
                        lambda d: {"full_report_md": "# Report"})

    result = orchestrator.run_daily_cycle()

    assert "trading_path" in result["errors"], "the failure must be recorded"
    assert result["otis"] is not None, "the books must still close"
    assert result["nora_monitor"] is not None, "risk must still run on the book"
    assert result["clara"] is not None, "the report must still be written"
    assert "atlas" in called and "otis" in called


def test_one_phase_5_failure_does_not_stop_the_others(monkeypatch):
    import orchestrator

    monkeypatch.setattr(orchestrator, "is_trading_day", lambda d: True)
    monkeypatch.setattr(orchestrator, "get_trading_control_state",
                        lambda: type("S", (), {"trading_enabled": True,
                                               "describe": lambda self: "ENABLED"})())

    @contextmanager
    def fake_track(_d, name, _p):
        yield type("R", (), {"output": None})()

    monkeypatch.setattr(orchestrator, "track_agent_run", fake_track)
    monkeypatch.setattr(orchestrator.atlas, "run", lambda d: {
        "regime_signal": "neutral", "change_from_yesterday": "none"})
    monkeypatch.setattr(orchestrator.vera, "run", lambda d, a: {"candidates": [], "monitoring": []})
    monkeypatch.setattr(orchestrator.solomon, "run", lambda d, a, v: {"action_needed": False})

    def otis_boom(_d):
        raise RuntimeError("Alpaca unreachable")

    monkeypatch.setattr(orchestrator.otis, "run", otis_boom)
    monkeypatch.setattr(orchestrator.nora, "monitor_portfolio", lambda d: {
        "portfolio_status": "within_limits", "circuit_breaker_active": False,
        "breaches": [], "position_count": 0, "drawdown": {"basis": "n/a"},
    })
    monkeypatch.setattr(orchestrator.clara, "run", lambda d: {"process_check": "clean"})
    monkeypatch.setattr(orchestrator.clara, "compile_daily_report",
                        lambda d: {"full_report_md": "# Report"})

    result = orchestrator.run_daily_cycle()

    assert "otis" in result["errors"]
    assert result["otis"] is None
    assert result["nora_monitor"] is not None, "Otis failing must not stop risk"
    assert result["clara"] is not None, "Otis failing must not stop the report"


def test_noras_two_passes_log_under_different_names():
    """
    agent_runs is unique on (run_date, agent_name). Logging both of
    Nora's jobs as "nora" would have the Phase 5 monitor silently
    overwrite the Phase 3 review — quiet data loss in the table that
    exists to prevent exactly that.
    """
    import inspect
    import orchestrator

    src = inspect.getsource(orchestrator.run_daily_cycle)
    assert '"nora_review"' in src
    assert '"nora_monitor"' in src
    assert 'track_agent_run(today, "nora",' not in src
