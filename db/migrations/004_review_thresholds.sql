-- ============================================================
-- Migration 004 — loss/profit review thresholds
--
--   docker exec -i mby-trading-db psql -U mby -d mby_trading \
--       < db/migrations/004_review_thresholds.sql
--
-- Safe to re-run.
--
-- WHY THIS IS ONLY BEING WRITTEN NOW
--
-- These two columns are not new. They were designed and added to
-- schema.sql and models.py during the loss/gain redesign — the one
-- that replaced hard stop-losses with mandatory INVESTIGATION — and
-- the live database was supposed to have been migrated by hand at the
-- time. It wasn't, or it was migrated against a volume that has since
-- been replaced.
--
-- The same un-applied change also explains the missing
-- position_pnl_history table: both came from that redesign, and both
-- were absent from the running database while present in the code that
-- reads them. Neither surfaced until an agent actually touched them.
--
-- This is the argument for `python -m core.check_schema` as a habit
-- rather than a tool you reach for after a crash. Drift between the
-- schema you wrote and the database you're running is invisible until
-- something needs the missing piece, and by then you're mid-cycle.
--
-- WHAT THEY DO (Operating Manual §7.4): these are deliberately NOT
-- stop-loss / take-profit levels. Crossing one triggers a mandatory
-- investigation in Vera's next monitoring pass — is this decline
-- market-wide or company-specific? is this gain sustained or a
-- single-day spike? — and never an automatic exit or sale.
-- ============================================================

BEGIN;

-- NOT NULL with a DEFAULT is safe on a populated table: Postgres fills
-- existing rows with the default value as part of the statement.
ALTER TABLE risk_policy_versions
    ADD COLUMN IF NOT EXISTS loss_review_pct   NUMERIC(5,2) NOT NULL DEFAULT -10.00;

ALTER TABLE risk_policy_versions
    ADD COLUMN IF NOT EXISTS profit_review_pct NUMERIC(5,2) NOT NULL DEFAULT  20.00;

COMMIT;

-- Verify:
--   SELECT effective_date, max_position_pct, max_sector_pct,
--          drawdown_breaker_pct, min_position_count,
--          loss_review_pct, profit_review_pct
--   FROM risk_policy_versions ORDER BY effective_date DESC;
