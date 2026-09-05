-- ============================================================
-- Migration 007 — give a refused order its own evidence
--
--   docker exec -i mby-trading-db psql -U mby -d mby_trading \
--       < db/migrations/007_order_status_detail.sql
--
-- Safe to re-run.
--
-- WHAT WAS WRONG
--
-- Ada records a status on every path — twelve of them, which was the
-- point of D3 and E9. But a status is a verdict, not a reason, and for
-- one of them the difference is expensive.
--
-- On 4 Sept an NVDA allocation came back `rejected_stale_ledger`. The
-- row said that and nothing else: not which ledger, not how stale, not
-- what NAV she saw. And the condition had already been repaired by the
-- time anyone looked, because Otis closes the books in Phase 5 —
-- AFTER Ada trades in Phase 4. So the query that should have confirmed
-- the diagnosis ("when did Otis last close?") returned today's date and
-- appeared to contradict it.
--
-- The evidence was destroyed by the same cycle that produced it.
--
-- Ada's run() does return `ledger` in its result dict, and the
-- orchestrator stores that in agent_runs.raw_output — so the
-- information exists for exactly as long as nobody reruns Ada that day
-- (agent_runs is unique on (run_date, agent_name) and upserted, so a
-- second run overwrites the first). The order row is the durable
-- record, and it is the one that was silent.
--
-- WHAT THIS ADDS
--
--   orders.status_detail — free text, populated alongside the status
--   wherever there is something worth saying: the ledger's date and
--   staleness on a refusal, the arithmetic on a rejected size, the
--   sizing basis on an accepted order.
--
-- Nullable and free-form on purpose. It is for a human reading a row a
-- month later, not for code to branch on — the status field is what
-- code reads, and giving this one any structure would invite someone
-- to parse it.
-- ============================================================

BEGIN;

ALTER TABLE orders ADD COLUMN IF NOT EXISTS status_detail TEXT;

COMMENT ON COLUMN orders.status_detail IS
    'Human-readable context for `status` — the ledger state, the sizing '
    'arithmetic, or the reason a guard fired. Never parsed by code.';

COMMIT;


-- ============================================================
-- VERIFY
--
--   \d orders
--
-- expected: a `status_detail | text` row at the bottom.
--
-- Existing rows keep NULL, correctly: we cannot reconstruct what Ada
-- saw at the time, and inventing it would be worse than the gap. The
-- 4 Sept NVDA refusal stays unexplained on the row — it is explained
-- in this migration instead, which is the honest place for it.
-- ============================================================
