-- ============================================================
-- Migration 009 — the number the desk is measured against
--
--   docker exec -i mby-trading-db psql -U mby -d mby_trading \
--       < db/migrations/009_benchmark_history.sql
--
-- Safe to re-run.
--
-- WHAT WAS WRONG (D4)
--
-- The desk measured its performance against nothing at all. Clara
-- computed contribution_pct per name and a total P&L, and there was no
-- index anywhere in the codebase — no SPY, no active return, no
-- separation of alpha from beta.
--
-- Absolute P&L is not a performance measure. A desk that returned 4%
-- in a month the index returned 6% lost money in the only sense that
-- matters, and this system would have reported a good month in
-- confident detail.
--
-- It also quietly undermined Clara's actual mandate. Her job is to
-- calibrate whether Vera's conviction scores predict outcomes — but
-- with an unadjusted return as the outcome variable, she was mostly
-- measuring market beta and crediting it to Vera's stock-picking. A
-- conviction-5 thesis on a name that rose with everything else would
-- score as a hit.
--
-- WHY A TABLE AND NOT COLUMNS ON daily_pnl
--
-- Two reasons, both about lifecycle.
--
--   The benchmark exists on sessions the desk did not run. Weekends
--   aside, the desk has already missed days — 3 Sept has no daily_pnl
--   row. Bolting the index onto the desk's own series would make those
--   sessions unrecordable, and the gap would silently distort any
--   cumulative comparison spanning it.
--
--   It is a market fact, not a desk fact. It is fully backfillable
--   from Alpaca years after the event, which is precisely what
--   daily_pnl is not — nobody can reconstruct what the book was worth
--   on a day nobody closed it. Mixing a recoverable series into an
--   unrecoverable one loses that distinction.
--
-- WHAT THIS ADDS
--
--   benchmark_history — one row per (session, ticker), carrying the
--   close and the session-over-session return.
--
-- daily_return_pct is NULLABLE ON PURPOSE. The first bar of a series
-- has no prior close to compare against, and a 0.00 there would read
-- as "the market was flat that day" rather than "we do not know".
-- Same principle as orders.recorded_at in migration 008: unknown is
-- not zero.
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS benchmark_history (
    id               BIGSERIAL PRIMARY KEY,
    bar_date         DATE NOT NULL,
    ticker           TEXT NOT NULL,
    close            NUMERIC(14,4) NOT NULL,
    daily_return_pct NUMERIC(10,6),
    UNIQUE (bar_date, ticker)
);

CREATE INDEX IF NOT EXISTS idx_benchmark_history_ticker_date
    ON benchmark_history (ticker, bar_date);

COMMENT ON TABLE benchmark_history IS
    'Daily closes for the comparison index (SPY). Backfillable from '
    'Alpaca, and deliberately separate from daily_pnl: the index has '
    'sessions the desk does not.';

COMMENT ON COLUMN benchmark_history.daily_return_pct IS
    'Session-over-session return in percent. NULL on the first bar of '
    'a series — unknown, not zero.';

COMMIT;


-- ============================================================
-- FILL IT
--
--   python -m core.benchmark backfill
--
-- pulls SPY from the first session with a NAV on record to today, so
-- the whole existing track record gets a comparison rather than only
-- future days. Otis then keeps it current at each close.
--
-- VERIFY
--
--   SELECT count(*), min(bar_date), max(bar_date) FROM benchmark_history;
--   python -m core.benchmark report
-- ============================================================
