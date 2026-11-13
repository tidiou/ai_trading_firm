-- ============================================================
-- 015 — position_monitoring_log.cause
--
-- WHY. A monitoring row says what Vera concluded (`status`) and what
-- she says triggered it (`trigger`). It does not say WHY the position
-- moved, and that is the distinction your own rule turns on: is this a
-- market correction, or has something gone wrong with the business?
--
-- Over nine sessions in November the same stock was logged `at_risk`
-- with a trigger alternating between `macro_conflict` and
-- `fundamental_drift`, while the record of what actually moved it was
-- a sector selloff, a policy shock and one company-specific event. The
-- desk had nowhere to write "I could not tell", so it wrote the nearest
-- word that justified the status.
--
-- The vocabulary is in core/cause.py:
--     market_wide | sector_wide | company_specific | unknown
-- `unknown` is a valid, expected answer and the default.
--
-- NO BACKFILL. Every existing row stays NULL. A cause assigned to a
-- past day now would be a guess made with hindsight and recorded as
-- though it had been observed, which is the thing the calibration work
-- exists to avoid (same reasoning as 008 and 010). NULL means "this row
-- predates the question", which is true; `unknown` would mean "the
-- question was asked and not answered", which is not.
--
-- ORDER OF APPLICATION MATTERS. core/models.py now expects this column
-- and the schema gate refuses to run ANY group against a database that
-- lacks one — so apply this BEFORE deploying the code, not after. It is
-- additive and nullable, so the old code runs against it unchanged and
-- applying it early costs nothing.
--
-- RE-RUNNABLE. Both statements are idempotent.
-- ============================================================

BEGIN;

ALTER TABLE position_monitoring_log
    ADD COLUMN IF NOT EXISTS cause TEXT;

-- Named explicitly, and dropped-then-added rather than guarded with a
-- lookup, so that a database built from schema.sql and one that went
-- through this file end up with the SAME constraint under the SAME name.
ALTER TABLE position_monitoring_log
    DROP CONSTRAINT IF EXISTS position_monitoring_log_cause_check;

ALTER TABLE position_monitoring_log
    ADD CONSTRAINT position_monitoring_log_cause_check
    CHECK (cause IS NULL OR cause IN
           ('market_wide', 'sector_wide', 'company_specific', 'unknown'));

COMMIT;
