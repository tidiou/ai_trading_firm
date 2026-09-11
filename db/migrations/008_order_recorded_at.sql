    -- ============================================================
-- Migration 008 — when the order was actually placed
--
--   docker exec -i mby-trading-db psql -U mby -d mby_trading \
--       < db/migrations/008_order_recorded_at.sql
--
-- Safe to re-run.
--
-- WHAT WAS WRONG (D10)
--
-- The orders table records WHICH DAY an order was placed and nothing
-- finer. For the end-of-day sweep that is not enough information to
-- act safely, and the gap is not academic:
--
--   The daily cycle was designed to run at 16:30 ET, after the close.
--   Ada submits a DAY limit order in Phase 4; the broker queues it for
--   the next session's open. Otis then runs in Phase 5 — minutes
--   later, market shut — and his sweep cancels every unfilled order of
--   the day. Including the one submitted ninety seconds earlier that
--   has not yet seen a single second of trading.
--
--   The row is then marked `expired_unfilled`, which reads as an
--   ordinary non-fill. "We tried to buy it and the limit was never
--   touched" and "we cancelled our own order before the bell" are
--   different facts, and the second one is a defect.
--
-- The sweep's guard asked `market_is_open_now()`. That is the right
-- question for an order placed DURING a session and the wrong one for
-- an order placed outside it — which, on the old schedule, was every
-- order the desk would ever place.
--
-- To ask the right question — "has the session this order was queued
-- for finished?" — the row has to know when it was written.
--
-- WHAT THIS ADDS
--
--   orders.recorded_at — set by the database, so it cannot be
--   forgotten on one of Ada's twelve return paths.
--
-- DELIBERATELY NOT BACKFILLED. Adding the column and its default in
-- one statement would stamp every historic row with the migration's
-- own timestamp — a fabricated fact, and precisely the fabricated fact
-- the sweep would then act on. Existing rows stay NULL, the sweep
-- recognises NULL as "unknown" and falls back to its old behaviour for
-- them, and nothing pretends to know something it does not.
-- ============================================================

BEGIN;

-- Two statements, not one, so existing rows are not backfilled with
-- now(). Postgres backfills when a DEFAULT is present at ADD COLUMN
-- time; setting it afterwards applies to future rows only.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMPTZ;
ALTER TABLE orders ALTER COLUMN recorded_at SET DEFAULT now();

COMMENT ON COLUMN orders.recorded_at IS
    'When this order row was written — within a second of submission for '
    'an order that reached the broker, and the moment of the refusal for '
    'one that did not. Read by Otis''s end-of-day sweep to decide whether '
    'the session this order was queued for has closed. NULL on rows '
    'predating migration 008: unknown, not zero.';

COMMIT;


-- ============================================================
-- VERIFY
--
--   \d orders
--     expected: recorded_at | timestamp with time zone | default now()
--
--   SELECT order_date, ticker, status, recorded_at FROM orders
--   ORDER BY id DESC LIMIT 5;
--     expected: recorded_at NULL on every existing row, populated on
--     everything Ada writes from here on.
-- ============================================================
