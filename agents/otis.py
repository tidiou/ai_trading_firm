"""
Otis — Operations/Reconciliation Agent
Operating Manual §7.7

Mandate: SINGLE SOURCE OF TRUTH for current portfolio state. Every
other agent reads positions from here, never from a raw Alpaca call.
Never silently resolves a discrepancy — flags for human review.
Ledger (transactions table) is append-only, always.
"""

# from core.clients import alpaca_client


def reconcile(session, today):
    """
    Compares intended (allocations/orders) vs actual (Alpaca ground truth).
    Pure deterministic dataset-matching. Returns discrepancies list.
    """
    raise NotImplementedError


def run(session, today):
    """
    Returns the Otis output contract:
    { "date", "reconciled", "positions": [...], "cash_balance",
      "realized_pnl_today", "discrepancies": [...], "narrative" }
    """
    # TODO: pull Alpaca ground truth
    # TODO: reconcile() against intended allocations/orders
    # TODO: recompute positions table + daily_pnl
    raise NotImplementedError
