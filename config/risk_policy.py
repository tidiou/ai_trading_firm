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
    # NOT a stop-loss/take-profit — these only trigger mandatory
    # investigation during Vera's monitoring pass, never an automatic
    # exit or sale. See agents/nora.py and agents/vera.py.
    "loss_review_pct": -10.0,
    "profit_review_pct": 20.0,
}