-- ============================================================
-- Migration 002 — sector limits + NAV-based drawdown
--
-- Run against your existing database BEFORE running the updated
-- agents. schema.sql already carries these columns for a fresh
-- install; this file is for the database you already have data in.
--
--   docker exec -i mby-trading-db psql -U mby -d mby_trading \
--       < db/migrations/002_sector_and_nav.sql
--
-- Every statement is IF NOT EXISTS, so re-running is harmless.
--
-- WHY THESE FOUR COLUMNS
--
-- Three of the four risk limits in §7.4 could not be enforced without
-- them:
--
--   max_sector_pct (25%)       needs a sector per position. Vera
--                              already receives it on the FMP profile
--                              call she makes at research time, so
--                              capturing it there costs no extra API
--                              quota — which matters on a 250/day
--                              free tier.
--
--   drawdown_breaker (-10%)    needs an equity curve. It was being
--                              computed from daily_pnl.total_pnl,
--                              which is a SINGLE DAY's figure — so
--                              "peak" meant "your best day", and a
--                              flat day after a good one read as a
--                              -100% drawdown. nav fixes the input.
--
-- min_position_count needs no new column; it is a count of rows.
-- ============================================================

BEGIN;

-- ---- sector, captured at research time and carried to positions ----
ALTER TABLE theses          ADD COLUMN IF NOT EXISTS sector TEXT;
ALTER TABLE new_candidates  ADD COLUMN IF NOT EXISTS sector TEXT;
ALTER TABLE positions       ADD COLUMN IF NOT EXISTS sector TEXT;

-- ---- account equity at the close, for the drawdown breaker ----
ALTER TABLE daily_pnl       ADD COLUMN IF NOT EXISTS nav NUMERIC(14,2);

COMMIT;


-- ============================================================
-- BACKFILL — deliberately NOT done automatically.
--
-- nav is left NULL on existing rows. It is NOT reconstructible from
-- the columns you have: total_pnl is a daily delta and cash_balance
-- is only part of equity, so any formula here would be a guess
-- dressed up as history — and the circuit breaker would then be
-- measuring an invented equity curve.
--
-- check_drawdown_circuit_breaker() skips NULL-nav rows and reports
-- "insufficient NAV history — not evaluated" until at least two real
-- sessions have been recorded by Otis. That is the honest behaviour:
-- the breaker stays quiet rather than firing on fabricated data, and
-- starts working on its own after two closes.
--
-- If you want the breaker live sooner and you know a historical
-- equity figure to be correct, set it explicitly, e.g.:
--
--   UPDATE daily_pnl SET nav = 100000.00 WHERE pnl_date = '2026-08-28';
--
-- sector back-fills itself: Otis writes it on every position at the
-- next reconciliation, from the open thesis or candidate row. Any
-- name still without one is treated by Nora as its own single-name
-- sector bucket, which can only ever over-flag, never under-flag.
-- ============================================================