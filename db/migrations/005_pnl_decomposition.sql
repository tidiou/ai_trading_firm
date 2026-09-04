-- ============================================================
-- Migration 005 — correct P&L decomposition
--
--   docker exec -i mby-trading-db psql -U mby -d mby_trading \
--       < db/migrations/005_pnl_decomposition.sql
--
-- Safe to re-run.
--
-- WHAT WAS WRONG
--
-- Otis computed the day's realized P&L as:
--
--     realized = (equity - last_equity) - SUM(unrealized_pnl)
--
-- The first term is TODAY'S change. The second is the LIFETIME open
-- gain across every held position. Subtracting a cumulative stock from
-- a daily flow is not an approximation, it is a category error, and on
-- any book carrying an open gain both the realized and unrealized
-- columns came out badly wrong. Those figures feed Clara's performance
-- attribution.
--
-- WHAT IT IS NOW
--
-- Three daily flows that reconcile:
--
--     realized_pnl + unrealized_pnl = total_pnl = nav - previous nav
--
--   realized_pnl    crystallised by TODAY'S sales, summed from the
--                   transaction ledger
--   unrealized_pnl  the residual: today's mark-to-market on positions
--                   still open
--
-- plus one stock, kept deliberately apart from the flows so the two can
-- never be confused again:
--
--   open_unrealized_pnl   lifetime open gain across held positions
--
-- Realized P&L is booked PER TRANSACTION, at the moment of sale, because
-- that is the only moment the cost basis is knowable: Otis books
-- transactions before rebuilding `positions` to mirror the broker, and a
-- full exit removes the position from the broker entirely.
-- ============================================================

BEGIN;

-- NULL, not 0, when cost basis could not be established (an orphan
-- holding with no position row). Zero would silently understate the
-- day's realized P&L with nothing to notice it by.
ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS realized_pnl NUMERIC(14,2);

ALTER TABLE daily_pnl
    ADD COLUMN IF NOT EXISTS open_unrealized_pnl NUMERIC(14,2);

COMMIT;


-- ============================================================
-- HISTORIC ROWS ARE LEFT ALONE, ON PURPOSE.
--
-- Existing daily_pnl rows keep their old realized/unrealized split,
-- and it is wrong. It cannot be recomputed: the correct realized figure
-- needs the cost basis as it stood at each past sale, and that is not
-- recoverable from what was stored.
--
-- Overwriting them with a fresh guess would replace known-wrong numbers
-- with unknown-wrong ones, which is worse — at least these are wrong in
-- a way you now understand. From the next Otis run forward the figures
-- are correct and reconcile.
--
-- If you want the break marked in the data:
--
--   UPDATE daily_pnl SET open_unrealized_pnl = NULL
--   WHERE pnl_date < CURRENT_DATE;
--
-- To check the identity holds on new rows:
--
--   SELECT pnl_date, realized_pnl, unrealized_pnl, total_pnl,
--          realized_pnl + unrealized_pnl - total_pnl AS should_be_zero
--   FROM daily_pnl ORDER BY pnl_date DESC LIMIT 10;
-- ============================================================
