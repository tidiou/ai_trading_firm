"""
Default risk policy — a starting strawman (Operating Manual §7.4).
The AUTHORITATIVE version lives in the risk_policy_versions table;
this module is only the seed value inserted on first setup, and a
fallback for local dev before the DB is wired up.
"""

DEFAULT_RISK_POLICY = {
    "max_position_pct": 8.0,
    "max_sector_pct": 25.0,
    "drawdown_breaker_pct": -10.0,
    "min_position_count": 12,
}
