-- ============================================================
-- Migration 003 — the kill switch
--
--   docker exec -i mby-trading-db psql -U mby -d mby_trading \
--       < db/migrations/003_trading_control.sql
--
-- Safe to re-run: the table is IF NOT EXISTS and the seed row only
-- inserts when the table is empty.
--
-- WHAT THIS IS FOR
--
-- A way for a human to stop the desk trading, immediately, without
-- killing a process mid-cycle. That last part matters here: Ada
-- submits an order and then records it, and killing the process
-- between those two steps is exactly the state that causes a
-- duplicate order on the next run. "Ctrl-C" is not a safe stop.
--
-- It is NOT the drawdown circuit breaker. The breaker is automatic,
-- triggers on a measured condition, and permits de-risking. This is
-- manual, triggers on your judgement, and stops everything including
-- exits — because you reach for it when you don't trust the system,
-- and a system you don't trust shouldn't be choosing what to sell
-- either. Unwind by hand while halted if you need to.
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS trading_control (
    id              BIGSERIAL PRIMARY KEY,
    trading_enabled BOOLEAN NOT NULL,
    changed_by      TEXT NOT NULL,
    reason          TEXT NOT NULL,
    changed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed an explicit enabled row. Reading an empty table is treated as
-- "enabled" so a fresh install works, but that bootstrap exception
-- should never be the state you actually run in — an explicit row
-- means the log has a beginning, and `status` has something to show.
INSERT INTO trading_control (trading_enabled, changed_by, reason)
SELECT TRUE, 'migration:003', 'Initial state — trading enabled at kill-switch install.'
WHERE NOT EXISTS (SELECT 1 FROM trading_control);

COMMIT;


-- ============================================================
-- USAGE
--
--   python -m core.trading_control status
--   python -m core.trading_control halt   --reason "investigating fills"
--   python -m core.trading_control resume --reason "root cause fixed, verified"
--
-- Or straight from psql if Python is the thing you don't trust:
--
--   INSERT INTO trading_control (trading_enabled, changed_by, reason)
--   VALUES (FALSE, 'malick', 'reason here');
--
-- Current state is always the latest row:
--
--   SELECT * FROM trading_control ORDER BY id DESC LIMIT 1;
-- ============================================================
