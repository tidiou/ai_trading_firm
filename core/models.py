"""
SQLAlchemy ORM models mirroring db/schema.sql exactly.

schema.sql is the source of truth — these models describe tables
that already exist (created by running schema.sql against Postgres),
they do not create the schema themselves. If you change a column in
schema.sql, update the matching model here in the same commit, or
the two will silently drift apart.

Typed 2.0-style SQLAlchemy (Mapped[...] / mapped_column) is used
deliberately — it gives real autocomplete and type-checking in
PyCharm for every column and catches a typo'd column name before
the code ever runs.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import ForeignKey, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ============================================================
# Cross-cutting: agent_runs (the audit trail)
# ============================================================
class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (UniqueConstraint("run_date", "agent_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_date: Mapped[date]
    agent_name: Mapped[str]
    phase: Mapped[int]
    status: Mapped[str] = mapped_column(default="pending")
    started_at: Mapped[Optional[datetime]]
    completed_at: Mapped[Optional[datetime]]
    raw_output: Mapped[Optional[dict]] = mapped_column(JSONB)
    error_message: Mapped[Optional[str]]


# ============================================================
# Cross-cutting: trading_control (the kill switch)
# ============================================================
class TradingControl(Base):
    """
    APPEND-ONLY. Current state is the latest row by id — halting and
    resuming both insert, so the table doubles as the audit history.
    Never update or delete a row here.
    """
    __tablename__ = "trading_control"

    id: Mapped[int] = mapped_column(primary_key=True)
    trading_enabled: Mapped[bool]
    changed_by: Mapped[str]
    reason: Mapped[str]
    changed_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


# ============================================================
# 1. Atlas
# ============================================================
class MacroBrief(Base):
    __tablename__ = "macro_briefs"

    id: Mapped[int] = mapped_column(primary_key=True)
    brief_date: Mapped[date] = mapped_column(unique=True)
    regime_signal: Mapped[str]
    change_from_yesterday: Mapped[str]
    confidence: Mapped[str]
    key_events: Mapped[Optional[dict]] = mapped_column(JSONB)
    notable_moves: Mapped[Optional[dict]] = mapped_column(JSONB)
    narrative: Mapped[Optional[str]]


# ============================================================
# 2. Vera
# ============================================================
class Thesis(Base):
    __tablename__ = "theses"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str]
    opened_date: Mapped[date]
    closed_date: Mapped[Optional[date]]
    thesis_text: Mapped[str]
    catalyst: Mapped[Optional[str]]
    original_conviction: Mapped[int]
    valuation_snapshot: Mapped[Optional[dict]] = mapped_column(JSONB)
    key_risks: Mapped[Optional[dict]] = mapped_column(JSONB)
    # Captured from the FMP profile Vera already fetches, so the sector
    # limit costs no extra quota. None = genuinely unknown; Nora treats
    # an unknown-sector name as its own bucket rather than pooling it.
    sector: Mapped[Optional[str]]


class PositionMonitoringLog(Base):
    __tablename__ = "position_monitoring_log"
    __table_args__ = (UniqueConstraint("log_date", "thesis_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    log_date: Mapped[date]
    thesis_id: Mapped[int] = mapped_column(ForeignKey("theses.id"))
    ticker: Mapped[str]
    status: Mapped[str]
    trigger: Mapped[Optional[str]]
    reasoning: Mapped[Optional[str]]
    conviction_score: Mapped[Optional[int]]


class NewCandidate(Base):
    __tablename__ = "new_candidates"
    __table_args__ = (UniqueConstraint("candidate_date", "ticker"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_date: Mapped[date]
    ticker: Mapped[str]
    thesis: Mapped[str]
    catalyst: Mapped[Optional[str]]
    conviction_score: Mapped[Optional[int]]
    key_risks: Mapped[Optional[dict]] = mapped_column(JSONB)
    valuation_snapshot: Mapped[Optional[dict]] = mapped_column(JSONB)
    sector: Mapped[Optional[str]]  # from the FMP profile; see Thesis.sector


# ============================================================
# 3. Solomon
# ============================================================
class StrategyDecision(Base):
    __tablename__ = "strategy_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    decision_date: Mapped[date] = mapped_column(unique=True)
    action_needed: Mapped[bool]
    narrative: Mapped[Optional[str]]


class Proposal(Base):
    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_decision_id: Mapped[int] = mapped_column(ForeignKey("strategy_decisions.id"))
    ticker: Mapped[str]
    action: Mapped[str]
    linked_trigger: Mapped[str]
    rationale: Mapped[Optional[str]]
    urgency: Mapped[Optional[str]]


# ============================================================
# 4. Nora
# ============================================================
class RiskPolicyVersion(Base):
    __tablename__ = "risk_policy_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    effective_date: Mapped[date]
    max_position_pct: Mapped[Decimal]
    max_sector_pct: Mapped[Decimal]
    drawdown_breaker_pct: Mapped[Decimal]
    min_position_count: Mapped[int]
    # Deliberately NOT a stop-loss (no automatic exit) — a loss beyond
    # this threshold triggers mandatory INVESTIGATION during Vera's
    # monitoring pass (is it systematic/market-wide or company-
    # specific?), never an automatic action. Same principle for gains.
    loss_review_pct: Mapped[Decimal] = mapped_column(default=-10.0)
    profit_review_pct: Mapped[Decimal] = mapped_column(default=20.0)
    notes: Mapped[Optional[str]]


class RiskReview(Base):
    __tablename__ = "risk_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    review_date: Mapped[date] = mapped_column(unique=True)
    portfolio_status: Mapped[str]
    circuit_breaker_active: Mapped[bool] = mapped_column(default=False)


class RiskBreach(Base):
    __tablename__ = "risk_breaches"

    id: Mapped[int] = mapped_column(primary_key=True)
    risk_review_id: Mapped[int] = mapped_column(ForeignKey("risk_reviews.id"))
    ticker: Mapped[Optional[str]]
    rule_violated: Mapped[str]
    current_value: Mapped[Optional[Decimal]]
    limit_value: Mapped[Optional[Decimal]]
    source: Mapped[str]  # 'existing_position' | 'proposal'


class ProposalReview(Base):
    __tablename__ = "proposal_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposals.id"))
    decision: Mapped[str]
    max_size_pct: Mapped[Optional[Decimal]]
    rules_checked: Mapped[Optional[dict]] = mapped_column(JSONB)
    reasoning: Mapped[Optional[str]]


# ============================================================
# 5. Marcus
# ============================================================
class Allocation(Base):
    __tablename__ = "allocations"

    id: Mapped[int] = mapped_column(primary_key=True)
    allocation_date: Mapped[date]
    proposal_review_id: Mapped[Optional[int]] = mapped_column(ForeignKey("proposal_reviews.id"))
    ticker: Mapped[str]
    action: Mapped[str]
    target_size_pct: Mapped[Decimal]
    conviction_input: Mapped[Optional[int]]
    rationale: Mapped[Optional[str]]
    priority: Mapped[Optional[int]]


# ============================================================
# 6. Ada
# ============================================================
class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    allocation_id: Mapped[Optional[int]] = mapped_column(ForeignKey("allocations.id"))
    order_date: Mapped[date]
    ticker: Mapped[str]
    action: Mapped[str]
    order_type: Mapped[str]
    limit_price: Mapped[Optional[Decimal]]
    shares: Mapped[Decimal]
    status: Mapped[str]
    fill_price: Mapped[Optional[Decimal]]
    slippage_bps: Mapped[Optional[Decimal]]
    alpaca_order_id: Mapped[Optional[str]]
    # Why this status, in words — see migration 007. A status is a
    # verdict; a verdict with no evidence stops being explainable the
    # moment the condition that caused it is repaired.
    status_detail: Mapped[Optional[str]]
    # When the row was written — see migration 008. Otis's sweep needs
    # it to tell "this order has had its session and did not fill" from
    # "this order is queued for a bell that has not rung yet". Set by
    # the database so no return path in Ada can forget it. NULL on rows
    # predating 008: unknown, not zero.
    recorded_at: Mapped[Optional[datetime]] = mapped_column(
        server_default=text("now()"))


# ============================================================
# 7. Otis (system of record)
# ============================================================
class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_date: Mapped[date]
    ticker: Mapped[str]
    action: Mapped[str]  # buy|sell|dividend|correction
    shares: Mapped[Decimal]
    price: Mapped[Decimal]
    amount: Mapped[Decimal]
    alpaca_transaction_id: Mapped[Optional[str]]
    # Realized on THIS transaction: (sale price - avg cost at sale) x
    # shares. 0 for a buy, None when cost basis was unknown.
    realized_pnl: Mapped[Optional[Decimal]]
    corrects_txn_id: Mapped[Optional[int]] = mapped_column(ForeignKey("transactions.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class Position(Base):
    __tablename__ = "positions"

    ticker: Mapped[str] = mapped_column(primary_key=True)
    shares: Mapped[Decimal]
    avg_cost: Mapped[Decimal]
    market_value: Mapped[Optional[Decimal]]
    unrealized_pnl: Mapped[Optional[Decimal]]
    weight_pct: Mapped[Optional[Decimal]]
    last_updated: Mapped[date]
    # Carried across by Otis from the open thesis / candidate row.
    # This is what makes Nora's max_sector_pct limit enforceable.
    sector: Mapped[Optional[str]]


class PositionPnlHistory(Base):
    __tablename__ = "position_pnl_history"
    __table_args__ = (UniqueConstraint("snapshot_date", "ticker"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_date: Mapped[date]
    ticker: Mapped[str]
    unrealized_pnl_pct: Mapped[Decimal]
    market_value: Mapped[Optional[Decimal]]
    weight_pct: Mapped[Optional[Decimal]]


class DailyPnl(Base):
    __tablename__ = "daily_pnl"

    pnl_date: Mapped[date] = mapped_column(primary_key=True)
    # Daily FLOWS, and they reconcile:
    #   realized_pnl + unrealized_pnl = total_pnl = nav - previous nav
    realized_pnl: Mapped[Decimal]     # crystallised by today's sales
    unrealized_pnl: Mapped[Decimal]   # today's mark-to-market change
    total_pnl: Mapped[Decimal]
    # A STOCK, not a flow: lifetime open gain across held positions.
    open_unrealized_pnl: Mapped[Optional[Decimal]]
    cash_balance: Mapped[Decimal]
    reconciled: Mapped[bool] = mapped_column(default=False)
    # Account equity at the close — the series the drawdown circuit
    # breaker runs on. Deliberately NOT derived from total_pnl, which
    # is one day's figure rather than an equity curve.
    nav: Mapped[Optional[Decimal]]


class BenchmarkHistory(Base):
    """
    The index the desk is measured against — see migration 009.

    ITS OWN TABLE, NOT A COLUMN ON daily_pnl, for two reasons. The
    benchmark exists on sessions the desk did not run, so bolting it
    onto the desk's own series would make those days unrecordable. And
    it is a market fact rather than a desk fact: different provenance,
    different lifecycle, and fully backfillable from Alpaca long after
    the fact, which is exactly what daily_pnl is not.
    """
    __tablename__ = "benchmark_history"
    __table_args__ = (UniqueConstraint("bar_date", "ticker"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bar_date: Mapped[date]
    ticker: Mapped[str]
    close: Mapped[Decimal]
    # Session-over-session return, in percent. NULL on the first bar of
    # a series — there is no prior close to compare against, and a zero
    # there would read as "the market was flat that day".
    daily_return_pct: Mapped[Optional[Decimal]]


class Discrepancy(Base):
    __tablename__ = "discrepancies"

    id: Mapped[int] = mapped_column(primary_key=True)
    found_date: Mapped[date]
    ticker: Mapped[Optional[str]]
    expected: Mapped[Optional[str]]
    actual: Mapped[Optional[str]]
    description: Mapped[str]
    resolved: Mapped[bool] = mapped_column(default=False)
    resolution_note: Mapped[Optional[str]]


# ============================================================
# 8. Clara
# ============================================================
class Attribution(Base):
    __tablename__ = "attribution"

    id: Mapped[int] = mapped_column(primary_key=True)
    attribution_date: Mapped[date]
    ticker: Mapped[str]
    contribution_pct: Mapped[Optional[Decimal]]
    thesis_id: Mapped[Optional[int]] = mapped_column(ForeignKey("theses.id"))
    thesis_status: Mapped[Optional[str]]


class ProcessCheck(Base):
    __tablename__ = "process_checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    check_date: Mapped[date] = mapped_column(unique=True)
    process_check: Mapped[str]  # clean|violation_found
    violations: Mapped[Optional[dict]] = mapped_column(JSONB)


class WeeklyReport(Base):
    __tablename__ = "weekly_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    week_of: Mapped[date] = mapped_column(unique=True)
    portfolio_return_pct: Mapped[Optional[Decimal]]
    win_rate_pct: Mapped[Optional[Decimal]]
    vera_calibration: Mapped[Optional[dict]] = mapped_column(JSONB)
    solomon_calibration: Mapped[Optional[dict]] = mapped_column(JSONB)
    risk_events: Mapped[Optional[dict]] = mapped_column(JSONB)
    recommendations: Mapped[Optional[dict]] = mapped_column(JSONB)


class DailyReport(Base):
    __tablename__ = "daily_reports"

    report_date: Mapped[date] = mapped_column(primary_key=True)
    executive_summary: Mapped[Optional[str]]
    full_report_md: Mapped[str]