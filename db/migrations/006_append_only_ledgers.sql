-- ============================================================
-- Migration 006 — make the append-only ledgers actually append-only
--
--   docker exec -i mby-trading-db psql -U mby -d mby_trading \
--       < db/migrations/006_append_only_ledgers.sql
--
-- Safe to re-run.
--
-- WHAT WAS WRONG
--
-- schema.sql said, of `transactions`:
--
--     -- Append-only ledger. NEVER UPDATE or DELETE rows here
--
-- and nothing enforced it. That is a comment, not a constraint. Any
-- bug, any migration, any late-night psql session could rewrite the
-- transaction history — and since E3 that history is where realized
-- P&L comes from, so a silently edited row changes reported
-- performance with nothing to notice it by.
--
-- The distinction between a ledger and a table IS this enforcement.
--
-- WHAT IT COVERS
--
--   transactions      the trade ledger, and the source of realized P&L
--   trading_control   the halt/resume history — a tamper-evident record
--                     of who stopped the desk and why
--
-- NOT position_pnl_history, despite its comment claiming it is never
-- overwritten. Otis genuinely upserts it, so that a same-day rerun
-- refreshes today's snapshot rather than failing. The code is right and
-- the comment was wrong; schema.sql is corrected rather than the
-- behaviour, because a trigger there would break reruns.
--
-- NOT agent_runs, which is upserted by design: running -> completed.
-- ============================================================

BEGIN;

CREATE OR REPLACE FUNCTION refuse_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        '% is append-only — % is not permitted. %',
        TG_TABLE_NAME, TG_OP, TG_ARGV[0]
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;


-- ---- transactions ----
DROP TRIGGER IF EXISTS transactions_append_only ON transactions;
CREATE TRIGGER transactions_append_only
    BEFORE UPDATE OR DELETE ON transactions
    FOR EACH ROW
    EXECUTE FUNCTION refuse_mutation(
        'Correct an error by INSERTing a new row with corrects_txn_id set to the original.'
    );

-- A row-level trigger does NOT fire on TRUNCATE, so without this one
-- `TRUNCATE transactions` would empty the ledger straight past the
-- protection above.
DROP TRIGGER IF EXISTS transactions_no_truncate ON transactions;
CREATE TRIGGER transactions_no_truncate
    BEFORE TRUNCATE ON transactions
    FOR EACH STATEMENT
    EXECUTE FUNCTION refuse_mutation('The trade ledger cannot be emptied.');


-- ---- trading_control ----
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

COMMIT;


-- ============================================================
-- VERIFY IT WORKS
--
-- 1. Are the triggers there at all? Expect FOUR rows.
--
--   SELECT c.relname AS tbl, t.tgname AS trg
--   FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
--   WHERE NOT t.tgisinternal
--     AND c.relname IN ('transactions', 'trading_control');
--
-- 2. Try to empty the ledger. This is the test to trust, because a
--    STATEMENT-level trigger fires whether or not the table has rows:
--
--   BEGIN; TRUNCATE transactions; ROLLBACK;
--
--   expected:
--     ERROR:  transactions is append-only — TRUNCATE is not permitted.
--             The trade ledger cannot be emptied.
--
-- 3. Only once the table HAS rows, the row-level guard:
--
--   UPDATE transactions SET price = price;
--
--   expected:
--     ERROR:  transactions is append-only — UPDATE is not permitted.
--             Correct an error by INSERTing a new row with corrects_txn_id
--             set to the original.
--
-- A WARNING ABOUT TEST 3, learned the hard way. A row-level BEFORE
-- trigger only fires for rows that actually match. On an empty table,
-- or with a WHERE clause matching nothing, Postgres reports `UPDATE 0`
-- and no error — so the check passes identically whether the trigger
-- exists or not. A verification that cannot fail is not a
-- verification. Use tests 1 and 2 first.
--
-- THE ESCAPE HATCH, documented deliberately.
--
-- An undocumented lock is one somebody eventually drops permanently
-- because they needed it open once. If you genuinely must repair a row:
--
--   ALTER TABLE transactions DISABLE TRIGGER transactions_append_only;
--   -- make the repair, and write down why
--   ALTER TABLE transactions ENABLE TRIGGER transactions_append_only;
--
-- Re-enabling is the part that gets forgotten. `python -m core.check_schema`
-- does not check trigger state; if you disable one, set yourself a
-- reminder.
--
-- WHAT THIS STILL CANNOT STOP: DROP TABLE, and a superuser who means it.
-- Triggers make accidental damage impossible and deliberate damage
-- visible. That is the whole claim — no more than that.
-- ============================================================
