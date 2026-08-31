"""
Marcus — Portfolio Manager Agent
Operating Manual §7.5

Mandate: exact position sizing WITHIN Nora's ceiling (never above it).
Uses a deterministic conviction-to-size tier as the starting point,
then LLM judgment adjusts for portfolio-construction nuance
(correlation with existing holdings, cash-constrained prioritization).
"""

# from core.clients import claude_client


def base_size_from_conviction(conviction_score, ceiling_pct):
    """Deterministic conviction-tier formula. No LLM call."""
    # TODO: e.g. conviction 5 -> ceiling, 4 -> 0.7*ceiling, 3 -> starter/none
    raise NotImplementedError


def run(session, today, nora_output, vera_output, solomon_output):
    """
    Returns the Marcus output contract:
    { "date", "allocations": [...], "cash_reserve_pct", "narrative" }
    """
    # TODO: never increase size on a Vera at_risk position, even with idle cash
    # TODO: enforce minimum cash reserve
    # TODO: rank competing same-day proposals by conviction when cash-constrained
    raise NotImplementedError
