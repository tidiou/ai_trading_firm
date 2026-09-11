-- ============================================================
-- AI Trading Firm — Database Schema (v1)
-- Postgres. Every table maps to a specific agent's output
-- contract from the Operating Manual. jsonb columns are used
-- for nested/flexible fields (risks, rules, violations, etc.)
-- so the schema doesn't need to change every time a rubric is
-- tuned.
-- ============================================================
-- IDEMPOTENT BY DESIGN. Every CREATE is IF NOT EXISTS, so running this
-- file against an existing database is safe and creates only what is
-- missing. That matters because this project gets re-downloaded into a
-- fresh folder periodically while the Docker volume persists, and it is
-- easy for a table added late in the build to exist in this file but
-- never in the running database — which surfaces as an UndefinedTable
-- error halfway through an agent, at the worst possible moment.
--
-- WHAT IT DOES NOT DO: add a missing COLUMN to a table that already
-- exists. IF NOT EXISTS skips such a table whole. Columns are the job
-- of db/migrations/*.sql, and `python -m core.check_schema` reports
-- both kinds of drift before you find them at runtime.

-- ---------------------------------------------------------
-- Cross-cutting: one row per agent invocation, every day.
-- This IS the audit trail Clara's process-compliance checks
-- depend on — never delete from this table.
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_runs (
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
CREATE TABLE IF NOT EXISTS macro_briefs (
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
-- Cross-cutting: trading_control (the kill switch)
-- ============================================================

-- APPEND-ONLY. Current state is the LATEST row; halting and resuming
-- both INSERT, so the table is its own audit history and you can
-- always answer "when was it halted, by whom, and why" without a
-- separate log. Never UPDATE or DELETE here.
--
-- An empty table means trading is ENABLED. That is a deliberate
-- bootstrap exception: a fresh install has no rows and must work.
-- The migration seeds an explicit enabled row so the exception is
-- only ever hit on day one.
CREATE TABLE IF NOT EXISTS trading_control (
    id              BIGSERIAL PRIMARY KEY,
    trading_enabled BOOLEAN NOT NULL,
    changed_by      TEXT NOT NULL,
    reason          TEXT NOT NULL,   -- mandatory both ways; a resume
                                     -- without a reason is how a halt
                                     -- gets silently forgotten
    changed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 2. VERA — Equity Research
-- ============================================================

-- A thesis is opened when a position is first proposed and stays
-- open (and gets referenced daily) until the position is closed.
CREATE TABLE IF NOT EXISTS theses (
    id                  BIGSERIAL PRIMARY KEY,
    ticker              TEXT NOT NULL,
    opened_date         DATE NOT NULL,
    closed_date         DATE,
    thesis_text         TEXT NOT NULL,
    catalyst            TEXT,
    original_conviction SMALLINT NOT NULL CHECK (original_conviction BETWEEN 1 AND 5),
    valuation_snapshot  JSONB,
    key_risks           JSONB,
    -- Captured from the FMP profile Vera already fetches at research
    -- time, so the sector limit costs no extra API quota. NULL means
    -- genuinely unknown, and Nora treats an unknown-sector name as its
    -- own single-name bucket rather than pooling it with other unknowns.
    sector              TEXT
);

-- Daily monitoring tag for every currently held position.
CREATE TABLE IF NOT EXISTS position_monitoring_log (
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
CREATE TABLE IF NOT EXISTS new_candidates (
    id                 BIGSERIAL PRIMARY KEY,
    candidate_date     DATE NOT NULL,
    ticker             TEXT NOT NULL,
    thesis             TEXT NOT NULL,
    catalyst           TEXT,
    conviction_score   SMALLINT CHECK (conviction_score BETWEEN 1 AND 5),
    key_risks          JSONB,
    valuation_snapshot JSONB,
    sector             TEXT,           -- from the FMP profile; see theses.sector
    UNIQUE (candidate_date, ticker)
);

-- ============================================================
-- 3. SOLOMON — CIO/Strategy
-- ============================================================
CREATE TABLE IF NOT EXISTS strategy_decisions (
    id             BIGSERIAL PRIMARY KEY,
    decision_date  DATE NOT NULL UNIQUE,
    action_needed  BOOLEAN NOT NULL,
    narrative      TEXT
);

CREATE TABLE IF NOT EXISTS proposals (
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
CREATE TABLE IF NOT EXISTS risk_policy_versions (
    id                    BIGSERIAL PRIMARY KEY,
    effective_date        DATE NOT NULL,
    max_position_pct      NUMERIC(5,2) NOT NULL,   -- e.g. 8.00
    max_sector_pct        NUMERIC(5,2) NOT NULL,   -- e.g. 25.00
    drawdown_breaker_pct  NUMERIC(5,2) NOT NULL,   -- e.g. -10.00
    min_position_count    SMALLINT NOT NULL,
    -- NOT a stop-loss (no automatic exit) — crossing this threshold
    -- triggers mandatory investigation during Vera's daily monitoring
    -- (systematic/market-wide decline vs company-specific problem),
    -- never an automatic action. Same principle for the gain side.
    loss_review_pct       NUMERIC(5,2) NOT NULL DEFAULT -10.00,
    profit_review_pct     NUMERIC(5,2) NOT NULL DEFAULT 20.00,
    notes                 TEXT
);

CREATE TABLE IF NOT EXISTS risk_reviews (
    id                     BIGSERIAL PRIMARY KEY,
    review_date            DATE NOT NULL UNIQUE,
    portfolio_status       TEXT NOT NULL,  -- within_limits|breach_warning|breach_hard
    circuit_breaker_active BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS risk_breaches (
    id              BIGSERIAL PRIMARY KEY,
    risk_review_id  BIGINT NOT NULL REFERENCES risk_reviews(id),
    ticker          TEXT,
    rule_violated   TEXT NOT NULL,
    current_value   NUMERIC,
    limit_value     NUMERIC,
    source          TEXT NOT NULL  -- 'existing_position' | 'proposal'
);

CREATE TABLE IF NOT EXISTS proposal_reviews (
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
CREATE TABLE IF NOT EXISTS allocations (
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
CREATE TABLE IF NOT EXISTS orders (
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
    alpaca_order_id  TEXT,
    -- Human-readable context for `status` (migration 007): the ledger
    -- state on a refusal, the arithmetic on a rejected size, the sizing
    -- basis on an accepted order. A status is a verdict; this is the
    -- reason. Never parsed by code.
    status_detail    TEXT,
    -- When this row was written (migration 008). Otis's end-of-day
    -- sweep reads it to tell an order that has had its session and did
    -- not fill from one queued for a bell that has not rung yet —
    -- without it the sweep cancelled orders minutes after they were
    -- placed and recorded them as ordinary non-fills.
    recorded_at      TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- 7. OTIS — Operations/Reconciliation (system of record)
-- ============================================================

-- Append-only ledger. NEVER UPDATE or DELETE rows here —
-- corrections are inserted as new rows referencing the original.
CREATE TABLE IF NOT EXISTS transactions (
    id                    BIGSERIAL PRIMARY KEY,
    transaction_date      DATE NOT NULL,
    ticker                TEXT NOT NULL,
    action                TEXT NOT NULL,  -- buy|sell|dividend|correction
    shares                NUMERIC(14,4) NOT NULL,
    price                 NUMERIC(12,4) NOT NULL,
    amount                NUMERIC(14,2) NOT NULL,
    alpaca_transaction_id TEXT,
    -- Realized P&L booked ON THIS TRANSACTION: (sale price - average
    -- cost at the moment of sale) x shares. Zero for a buy, NULL when
    -- the cost basis could not be established (a holding with no
    -- position row, e.g. an orphan). Recorded here, at the point of
    -- sale, because average cost is only knowable BEFORE the positions
    -- table is rebuilt to match the broker.
    realized_pnl    NUMERIC(14,2),
    corrects_txn_id       BIGINT REFERENCES transactions(id),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Current state, derived/recomputed from transactions each day —
-- this is the table Nora, Marcus, and Vera should all read from
-- for "current portfolio state," never a raw Alpaca call.
CREATE TABLE IF NOT EXISTS positions (
    ticker           TEXT PRIMARY KEY,
    shares           NUMERIC(14,4) NOT NULL,
    avg_cost         NUMERIC(12,4) NOT NULL,
    market_value     NUMERIC(14,2),
    unrealized_pnl   NUMERIC(14,2),
    weight_pct       NUMERIC(5,2),
    last_updated     DATE NOT NULL,
    -- Carried across by Otis from the open thesis / candidate row.
    -- This is what makes Nora's max_sector_pct limit enforceable.
    sector           TEXT
);

-- DAILY snapshot per held position — one row per ticker per day,
-- unlike `positions` above which holds current state only and is
-- rebuilt each day. Otis UPSERTS today's row, so a same-day rerun
-- refreshes the snapshot rather than failing; earlier days are never
-- touched. (It previously claimed to be append-only, which the code
-- never was — the comment was the thing that was wrong.) Needed so Vera can distinguish a sustained
-- trend from a single-day spike when deciding whether a gain/loss is
-- "real" or noise — a one-row-per-day snapshot is exactly what that
-- judgment needs and `positions` alone can't provide.
CREATE TABLE IF NOT EXISTS position_pnl_history (
    id                BIGSERIAL PRIMARY KEY,
    snapshot_date     DATE NOT NULL,
    ticker            TEXT NOT NULL,
    unrealized_pnl_pct NUMERIC(8,3) NOT NULL,
    market_value      NUMERIC(14,2),
    weight_pct        NUMERIC(5,2),
    UNIQUE (snapshot_date, ticker)
);

-- THE THREE P&L COLUMNS ARE DAILY FLOWS AND THEY RECONCILE:
--     realized_pnl + unrealized_pnl = total_pnl = nav - previous nav
--
-- realized_pnl    P&L crystallised by TODAY'S sales, from the
--                 transaction ledger.
-- unrealized_pnl  the change in mark-to-market on positions still open
--                 — today's movement, NOT the lifetime open gain.
-- open_unrealized_pnl is the lifetime figure, and is a STOCK not a
--                 flow, which is why it sits apart from the three above.
--
-- These were previously conflated: realized was computed as
-- (today's equity delta) - (LIFETIME unrealized across all positions),
-- subtracting a cumulative quantity from a daily one. The result was
-- not an approximation, it was a category error, and it fed Clara's
-- attribution.
CREATE TABLE IF NOT EXISTS daily_pnl (
    pnl_date        DATE PRIMARY KEY,
    realized_pnl    NUMERIC(14,2) NOT NULL,
    unrealized_pnl  NUMERIC(14,2) NOT NULL,
    total_pnl       NUMERIC(14,2) NOT NULL,
    open_unrealized_pnl NUMERIC(14,2),
    cash_balance    NUMERIC(14,2) NOT NULL,
    reconciled      BOOLEAN NOT NULL DEFAULT FALSE,
    -- Total account equity at the close. This is the series the
    -- drawdown circuit breaker runs on: drawdown is (nav - peak_nav)
    -- / peak_nav. It must NOT be computed from total_pnl, which is a
    -- single day's figure rather than an equity curve — taking the
    -- peak of that measures distance from your best DAY, not from the
    -- portfolio's high-water mark.
    nav             NUMERIC(14,2)
);

-- The index the desk is measured against (migration 009). Its own
-- table rather than columns on daily_pnl: the benchmark has sessions
-- the desk does not, and unlike the desk's own history it can be
-- backfilled from Alpaca years later.
CREATE TABLE IF NOT EXISTS benchmark_history (
    id               BIGSERIAL PRIMARY KEY,
    bar_date         DATE NOT NULL,
    ticker           TEXT NOT NULL,
    close            NUMERIC(14,4) NOT NULL,
    -- NULL on the first bar of a series: unknown, not zero.
    daily_return_pct NUMERIC(10,6),
    UNIQUE (bar_date, ticker)
);

CREATE INDEX IF NOT EXISTS idx_benchmark_history_ticker_date
    ON benchmark_history (ticker, bar_date);

CREATE TABLE IF NOT EXISTS discrepancies (
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
CREATE TABLE IF NOT EXISTS attribution (
    id               BIGSERIAL PRIMARY KEY,
    attribution_date DATE NOT NULL,
    ticker           TEXT NOT NULL,
    contribution_pct NUMERIC(6,3),
    thesis_id        BIGINT REFERENCES theses(id),
    thesis_status    TEXT
);

CREATE TABLE IF NOT EXISTS process_checks (
    id             BIGSERIAL PRIMARY KEY,
    check_date     DATE NOT NULL UNIQUE,
    process_check  TEXT NOT NULL,   -- clean|violation_found
    violations     JSONB
);

CREATE TABLE IF NOT EXISTS weekly_reports (
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
CREATE TABLE IF NOT EXISTS daily_reports (
    report_date        DATE PRIMARY KEY,
    executive_summary  TEXT,
    full_report_md     TEXT NOT NULL
);

-- ============================================================
-- APPEND-ONLY ENFORCEMENT
--
-- The two ledgers below say they are append-only. Until migration 006
-- that was a comment and nothing more — and a comment does not stop a
-- bug, a migration, or a late-night psql session from rewriting
-- history. Since realized P&L is derived from `transactions`, an
-- edited row silently changes reported performance.
--
-- The distinction between a ledger and a table is this enforcement.
--
-- Note the TRUNCATE triggers: a row-level trigger does not fire on
-- TRUNCATE, so without them the tables could be emptied straight past
-- the protection. What this still cannot stop is DROP TABLE, or a
-- superuser who means it — the claim is that accidental damage becomes
-- impossible and deliberate damage becomes visible, no more.
--
-- Escape hatch, documented on purpose (see migration 006):
--   ALTER TABLE transactions DISABLE TRIGGER transactions_append_only;
-- ============================================================
CREATE OR REPLACE FUNCTION refuse_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        '% is append-only — % is not permitted. %',
        TG_TABLE_NAME, TG_OP, TG_ARGV[0]
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS transactions_append_only ON transactions;
CREATE TRIGGER transactions_append_only
    BEFORE UPDATE OR DELETE ON transactions
    FOR EACH ROW
    EXECUTE FUNCTION refuse_mutation(
        'Correct an error by INSERTing a new row with corrects_txn_id set to the original.'
    );

DROP TRIGGER IF EXISTS transactions_no_truncate ON transactions;
CREATE TRIGGER transactions_no_truncate
    BEFORE TRUNCATE ON transactions
    FOR EACH STATEMENT
    EXECUTE FUNCTION refuse_mutation('The trade ledger cannot be emptied.');

DROP TRIGGER IF EXISTS trading_control_append_only ON trading_control;
CREATE TRIGGER trading_control_append_only
    BEFORE UPDATE OR DELETE ON trading_control
    FOR EACH ROW
    EXECUTE FUNCTION refuse_mutation(
        'Change the state by INSERTing a new row — the current state is the latest one.'
    );

DROP TRIGGER IF EXISTS trading_control_no_truncate ON trading_control;
CREATE TRIGGER trading_control_no_truncate
    BEFORE TRUNCATE ON trading_control
    FOR EACH STATEMENT
    EXECUTE FUNCTION refuse_mutation('The halt history cannot be emptied.');


-- ============================================================
-- Indexes worth having from day one
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_position_monitoring_date ON position_monitoring_log(log_date);
CREATE INDEX IF NOT EXISTS idx_transactions_ticker_date ON transactions(ticker, transaction_date);
CREATE INDEX IF NOT EXISTS idx_agent_runs_date_status ON agent_runs(run_date, status);