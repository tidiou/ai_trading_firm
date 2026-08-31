-- ============================================================
-- AI Trading Firm — Database Schema (v1)
-- Postgres. Every table maps to a specific agent's output
-- contract from the Operating Manual. jsonb columns are used
-- for nested/flexible fields (risks, rules, violations, etc.)
-- so the schema doesn't need to change every time a rubric is
-- tuned.
-- ============================================================

-- ---------------------------------------------------------
-- Cross-cutting: one row per agent invocation, every day.
-- This IS the audit trail Clara's process-compliance checks
-- depend on — never delete from this table.
-- ---------------------------------------------------------
CREATE TABLE agent_runs (
    id              BIGSERIAL PRIMARY KEY,
    run_date        DATE NOT NULL,
    agent_name      TEXT NOT NULL,          -- 'atlas' | 'vera' | 'solomon' | ...
    phase           SMALLINT NOT NULL,      -- 1-5, matches Operating Manual §6
    status          TEXT NOT NULL DEFAULT 'pending', -- pending|running|completed|failed
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    raw_output      JSONB,                  -- full structured output, as produced
    error_message   TEXT,
    UNIQUE (run_date, agent_name)
);

-- ============================================================
-- 1. ATLAS — Macro/Market Intelligence
-- ============================================================
CREATE TABLE macro_briefs (
    id                    BIGSERIAL PRIMARY KEY,
    brief_date            DATE NOT NULL UNIQUE,
    regime_signal         TEXT NOT NULL,   -- risk-on|risk-off|neutral|transitioning
    change_from_yesterday TEXT NOT NULL,   -- none|minor|material
    confidence            TEXT NOT NULL,   -- low|medium|high
    key_events            JSONB,
    notable_moves         JSONB,
    narrative             TEXT
);

-- ============================================================
-- 2. VERA — Equity Research
-- ============================================================

-- A thesis is opened when a position is first proposed and stays
-- open (and gets referenced daily) until the position is closed.
CREATE TABLE theses (
    id                  BIGSERIAL PRIMARY KEY,
    ticker              TEXT NOT NULL,
    opened_date         DATE NOT NULL,
    closed_date         DATE,
    thesis_text         TEXT NOT NULL,
    catalyst            TEXT,
    original_conviction SMALLINT NOT NULL CHECK (original_conviction BETWEEN 1 AND 5),
    valuation_snapshot  JSONB,
    key_risks           JSONB
);

-- Daily monitoring tag for every currently held position.
CREATE TABLE position_monitoring_log (
    id                BIGSERIAL PRIMARY KEY,
    log_date          DATE NOT NULL,
    thesis_id         BIGINT NOT NULL REFERENCES theses(id),
    ticker            TEXT NOT NULL,
    status            TEXT NOT NULL,   -- intact|at_risk|broken
    trigger           TEXT,            -- none|earnings|news|fundamental_drift|macro_conflict
    reasoning         TEXT,
    conviction_score  SMALLINT CHECK (conviction_score BETWEEN 1 AND 5),
    UNIQUE (log_date, thesis_id)
);

-- New candidates surfaced that day (zero rows is a normal day).
CREATE TABLE new_candidates (
    id                 BIGSERIAL PRIMARY KEY,
    candidate_date     DATE NOT NULL,
    ticker             TEXT NOT NULL,
    thesis             TEXT NOT NULL,
    catalyst           TEXT,
    conviction_score   SMALLINT CHECK (conviction_score BETWEEN 1 AND 5),
    key_risks          JSONB,
    valuation_snapshot JSONB,
    UNIQUE (candidate_date, ticker)
);

-- ============================================================
-- 3. SOLOMON — CIO/Strategy
-- ============================================================
CREATE TABLE strategy_decisions (
    id             BIGSERIAL PRIMARY KEY,
    decision_date  DATE NOT NULL UNIQUE,
    action_needed  BOOLEAN NOT NULL,
    narrative      TEXT
);

CREATE TABLE proposals (
    id                BIGSERIAL PRIMARY KEY,
    strategy_decision_id BIGINT NOT NULL REFERENCES strategy_decisions(id),
    ticker            TEXT NOT NULL,
    action            TEXT NOT NULL,    -- exit|trim|add|new_position
    linked_trigger    TEXT NOT NULL,    -- e.g. 'vera:thesis_broken', 'atlas:regime_change'
    rationale         TEXT,
    urgency           TEXT              -- same_day|this_week
);

-- ============================================================
-- 4. NORA — Risk Manager
-- ============================================================

-- Versioned, human-editable risk policy. Code reads the row
-- with the latest effective_date <= today. Never overwritten —
-- a new version is inserted when limits are tuned.
CREATE TABLE risk_policy_versions (
    id                    BIGSERIAL PRIMARY KEY,
    effective_date        DATE NOT NULL,
    max_position_pct      NUMERIC(5,2) NOT NULL,   -- e.g. 8.00
    max_sector_pct        NUMERIC(5,2) NOT NULL,   -- e.g. 25.00
    drawdown_breaker_pct  NUMERIC(5,2) NOT NULL,   -- e.g. -10.00
    min_position_count    SMALLINT NOT NULL,
    notes                 TEXT
);

CREATE TABLE risk_reviews (
    id                     BIGSERIAL PRIMARY KEY,
    review_date            DATE NOT NULL UNIQUE,
    portfolio_status       TEXT NOT NULL,  -- within_limits|breach_warning|breach_hard
    circuit_breaker_active BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE risk_breaches (
    id              BIGSERIAL PRIMARY KEY,
    risk_review_id  BIGINT NOT NULL REFERENCES risk_reviews(id),
    ticker          TEXT,
    rule_violated   TEXT NOT NULL,
    current_value   NUMERIC,
    limit_value     NUMERIC,
    source          TEXT NOT NULL  -- 'existing_position' | 'proposal'
);

CREATE TABLE proposal_reviews (
    id             BIGSERIAL PRIMARY KEY,
    proposal_id    BIGINT NOT NULL REFERENCES proposals(id),
    decision       TEXT NOT NULL,   -- approved|rejected
    max_size_pct   NUMERIC(5,2),
    rules_checked  JSONB,
    reasoning      TEXT
);

-- ============================================================
-- 5. MARCUS — Portfolio Manager
-- ============================================================
CREATE TABLE allocations (
    id                 BIGSERIAL PRIMARY KEY,
    allocation_date    DATE NOT NULL,
    proposal_review_id BIGINT REFERENCES proposal_reviews(id),
    ticker             TEXT NOT NULL,
    action             TEXT NOT NULL,   -- buy|trim|exit
    target_size_pct    NUMERIC(5,2) NOT NULL,
    conviction_input   SMALLINT,
    rationale          TEXT,
    priority           SMALLINT
);

-- ============================================================
-- 6. ADA — Execution
-- ============================================================
CREATE TABLE orders (
    id               BIGSERIAL PRIMARY KEY,
    allocation_id    BIGINT REFERENCES allocations(id),
    order_date       DATE NOT NULL,
    ticker           TEXT NOT NULL,
    action           TEXT NOT NULL,
    order_type       TEXT NOT NULL,      -- market|limit
    limit_price      NUMERIC(12,4),
    shares           NUMERIC(14,4) NOT NULL,
    status           TEXT NOT NULL,      -- filled|partial|pending|rejected
    fill_price       NUMERIC(12,4),
    slippage_bps     NUMERIC(8,2),
    alpaca_order_id  TEXT
);

-- ============================================================
-- 7. OTIS — Operations/Reconciliation (system of record)
-- ============================================================

-- Append-only ledger. NEVER UPDATE or DELETE rows here —
-- corrections are inserted as new rows referencing the original.
CREATE TABLE transactions (
    id                    BIGSERIAL PRIMARY KEY,
    transaction_date      DATE NOT NULL,
    ticker                TEXT NOT NULL,
    action                TEXT NOT NULL,  -- buy|sell|dividend|correction
    shares                NUMERIC(14,4) NOT NULL,
    price                 NUMERIC(12,4) NOT NULL,
    amount                NUMERIC(14,2) NOT NULL,
    alpaca_transaction_id TEXT,
    corrects_txn_id       BIGINT REFERENCES transactions(id),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Current state, derived/recomputed from transactions each day —
-- this is the table Nora, Marcus, and Vera should all read from
-- for "current portfolio state," never a raw Alpaca call.
CREATE TABLE positions (
    ticker           TEXT PRIMARY KEY,
    shares           NUMERIC(14,4) NOT NULL,
    avg_cost         NUMERIC(12,4) NOT NULL,
    market_value     NUMERIC(14,2),
    unrealized_pnl   NUMERIC(14,2),
    weight_pct       NUMERIC(5,2),
    last_updated     DATE NOT NULL
);

CREATE TABLE daily_pnl (
    pnl_date        DATE PRIMARY KEY,
    realized_pnl    NUMERIC(14,2) NOT NULL,
    unrealized_pnl  NUMERIC(14,2) NOT NULL,
    total_pnl       NUMERIC(14,2) NOT NULL,
    cash_balance    NUMERIC(14,2) NOT NULL,
    reconciled      BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE discrepancies (
    id             BIGSERIAL PRIMARY KEY,
    found_date     DATE NOT NULL,
    ticker         TEXT,
    expected       TEXT,
    actual         TEXT,
    description    TEXT NOT NULL,
    resolved       BOOLEAN NOT NULL DEFAULT FALSE,
    resolution_note TEXT
);

-- ============================================================
-- 8. CLARA — Performance/Compliance
-- ============================================================
CREATE TABLE attribution (
    id               BIGSERIAL PRIMARY KEY,
    attribution_date DATE NOT NULL,
    ticker           TEXT NOT NULL,
    contribution_pct NUMERIC(6,3),
    thesis_id        BIGINT REFERENCES theses(id),
    thesis_status    TEXT
);

CREATE TABLE process_checks (
    id             BIGSERIAL PRIMARY KEY,
    check_date     DATE NOT NULL UNIQUE,
    process_check  TEXT NOT NULL,   -- clean|violation_found
    violations     JSONB
);

CREATE TABLE weekly_reports (
    id                     BIGSERIAL PRIMARY KEY,
    week_of                DATE NOT NULL UNIQUE,
    portfolio_return_pct   NUMERIC(6,3),
    win_rate_pct           NUMERIC(5,2),
    vera_calibration       JSONB,   -- { conviction_vs_outcome_correlation: ... }
    solomon_calibration    JSONB,   -- { escalation_precision: ..., escalation_recall: ... }
    risk_events            JSONB,
    recommendations        JSONB    -- surfaced to human, never auto-applied
);

-- Compiled once per day by Clara (Operating Manual §7.8).
CREATE TABLE daily_reports (
    report_date        DATE PRIMARY KEY,
    executive_summary  TEXT,
    full_report_md     TEXT NOT NULL
);

-- ============================================================
-- Indexes worth having from day one
-- ============================================================
CREATE INDEX idx_position_monitoring_date ON position_monitoring_log(log_date);
CREATE INDEX idx_transactions_ticker_date ON transactions(ticker, transaction_date);
CREATE INDEX idx_agent_runs_date_status ON agent_runs(run_date, status);