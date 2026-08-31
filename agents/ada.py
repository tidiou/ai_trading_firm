"""
Ada — Execution Agent
Operating Manual §7.6

Mandate: HOW to execute what Marcus already decided — never what or
how much. Most code-heavy agent on the desk. Key guardrail: a
staleness check that pauses and escalates rather than blindly
executing if price moved materially since Marcus's decision.
"""

# from core.clients import alpaca_client


def is_stale(ticker, decision_time_price, current_price, threshold_pct=2.0):
    """Deterministic staleness check. No LLM call."""
    raise NotImplementedError


def run(session, today, marcus_output):
    """
    Returns the Ada output contract:
    { "date", "orders": [...], "narrative" }
    """
    # TODO: for each allocation -> check staleness -> place limit order via Alpaca
    # TODO: idempotency guard (one execution per allocation, ever)
    raise NotImplementedError
