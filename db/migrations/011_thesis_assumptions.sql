-- ============================================================
-- Migration 011 — a thesis you can check, and a verdict you can act on
--
--   docker exec -i mby-trading-db psql -U mby -d mby_trading \
--       < db/migrations/011_thesis_assumptions.sql
--
-- Safe to re-run.
--
-- WHAT WAS WRONG (S4 + S3b of the Research Desk)
--
-- Two problems, and they turn out to be one problem.
--
--   A THESIS WAS A PARAGRAPH. theses.thesis_text is prose, so the
--   monitoring pass re-formed an opinion every morning by re-reading
--   it. You cannot diff a news item against a paragraph — asking
--   "does this change the thesis?" of unstructured text means
--   re-arguing the whole thesis daily with whatever the model happens
--   to weight that day. And a prose thesis is never falsified; it just
--   stops being mentioned. Nothing is ever settled.
--
--   MONITORING COULD ONLY MOVE ONE DIRECTION.
--   position_monitoring_log.status runs intact | at_risk | broken.
--   There is no value meaning "this got BETTER". Vera could downgrade
--   or hold and never upgrade, so the loop drifts pessimistic over
--   time and can never be the reason Marcus adds to a winner. That
--   asymmetry quietly capped the desk's upside at whatever it decided
--   on the day the position was opened.
--
-- WHAT THIS ADDS
--
--   thesis_assumptions                  the named claims a thesis rests on
--   position_monitoring_log.verdict     strengthened|unchanged|weakened|broken
--   position_monitoring_log.assumption_checks
--                                       what each claim did today (JSONB)
--
-- WHY assumption_checks IS JSONB ON THE LOG RATHER THAN A THIRD TABLE.
-- The log is already unique on (log_date, thesis_id), so it is already
-- the per-day record — putting the day's checks there gives dated
-- history for free. A separate assumption_checks table would hold one
-- row per claim per day per thesis and earn nothing: nothing queries a
-- single claim's status on a single past day, whereas "what did this
-- thesis look like on the 14th" is asked constantly. The durable claim
-- lives in thesis_assumptions; the daily observation lives with the
-- day.
--
-- WHY status AND verdict BOTH SURVIVE. A status is a STATE (where the
-- thesis stands) and a verdict is a MOVEMENT (what today did to it).
-- They answer different questions and they can legitimately disagree —
-- a thesis can be `at_risk` and `strengthened` in the same breath if
-- it was in trouble and something improved. Replacing one with the
-- other would also make every row written before today unreadable.
--
-- NOT BACKFILLED, same principle as 008 and 010. Theses opened before
-- this migration have no assumptions, and core/thesis.py returns NO
-- VERDICT for them rather than `unchanged` — calling them unchanged
-- would assert their claims were checked and held, which never
-- happened.
-- ============================================================

BEGIN;

-- ============================================================
-- The claims a thesis rests on
--
-- falsify_threshold IS TEXT, NOT NUMERIC, and that is deliberate. The
-- claims that matter are not all numeric: "management keeps buying
-- back stock below intrinsic value" has no threshold expressible as a
-- number, and forcing one would either exclude the claim or invite a
-- fabricated figure. What the field must do is be SPECIFIC enough that
-- a later reader can tell whether it happened — that is a prompt
-- requirement, enforced by review, not a type constraint.
-- ============================================================
CREATE TABLE IF NOT EXISTS thesis_assumptions (
    id                BIGSERIAL PRIMARY KEY,
    thesis_id         BIGINT NOT NULL REFERENCES theses(id),
    claim             TEXT NOT NULL,
    -- What to watch. A line item, a ratio, a disclosure — whatever
    -- bears on the claim.
    metric            TEXT,
    -- Which way supports the claim.
    direction         TEXT CHECK (direction IN ('up', 'down', 'stable')),
    -- What would settle this against us. Prose, but specific.
    falsify_threshold TEXT,
    -- Does the thesis die without this claim? Without this flag one
    -- broken minor assumption sinks a thesis, and within a month every
    -- thesis reads `broken` for reasons nobody considered material.
    is_load_bearing   BOOLEAN NOT NULL DEFAULT TRUE,
    -- Current state, carried forward between checks.
    status            TEXT NOT NULL DEFAULT 'holding'
                      CHECK (status IN ('holding', 'improving', 'strained',
                                        'broken', 'unchecked')),
    evidence          TEXT,
    opened_date       DATE NOT NULL,
    last_checked      DATE,
    -- A claim is retired, never deleted: it is the record of what the
    -- desk believed, and deleting it would erase the reason a position
    -- was opened.
    retired_date      DATE,
    UNIQUE (thesis_id, claim)
);

CREATE INDEX IF NOT EXISTS idx_thesis_assumptions_open
    ON thesis_assumptions (thesis_id) WHERE retired_date IS NULL;

COMMENT ON TABLE thesis_assumptions IS
    'The named claims a thesis rests on — what makes it checkable '
    'rather than re-arguable. One row per claim; the daily observation '
    'lives in position_monitoring_log.assumption_checks.';
COMMENT ON COLUMN thesis_assumptions.is_load_bearing IS
    'Does the thesis die without this claim? Only a load-bearing '
    'failure produces a `broken` verdict; anything else is `weakened`. '
    'Marked at open time, while the model is reasoning about the '
    'thesis rather than defending it.';
COMMENT ON COLUMN thesis_assumptions.falsify_threshold IS
    'What would settle this claim against us. TEXT rather than numeric '
    'because the claims that matter are not all numeric — the '
    'requirement is specificity a later reader can judge, not a type.';

-- ============================================================
-- The verdict, and the evidence behind it
-- ============================================================
ALTER TABLE position_monitoring_log
    ADD COLUMN IF NOT EXISTS verdict TEXT
        CHECK (verdict IN ('strengthened', 'unchanged', 'weakened', 'broken'));

ALTER TABLE position_monitoring_log
    ADD COLUMN IF NOT EXISTS assumption_checks JSONB;

COMMENT ON COLUMN position_monitoring_log.verdict IS
    'What today did to the thesis. COMPUTED by core.thesis.compute_verdict '
    'from assumption_checks, never asked of the model — otherwise it '
    'could return `strengthened` while its own per-claim notes said two '
    'load-bearing claims were failing, and nothing would catch it. '
    'NULL on rows with no assumptions on record (theses predating '
    'migration 011): no verdict, deliberately not `unchanged`.';
COMMENT ON COLUMN position_monitoring_log.assumption_checks IS
    'What each assumption did on this date: claim, status, evidence, '
    'is_load_bearing. The verdict is derivable from this, so a stored '
    'verdict can always be re-checked against its own inputs.';

COMMIT;


-- ============================================================
-- VERIFY
--
--   \d thesis_assumptions
--     expected: 12 columns, two CHECK constraints, UNIQUE
--               (thesis_id, claim)
--
--   \d position_monitoring_log
--     expected: verdict | text, assumption_checks | jsonb
--
--   -- the verdict vocabulary is enforced:
--   BEGIN;
--   UPDATE position_monitoring_log SET verdict = 'improved'
--   WHERE id = (SELECT id FROM position_monitoring_log LIMIT 1);
--     expected: ERROR, violates check constraint
--   ROLLBACK;
--
--   -- nothing was backfilled:
--   SELECT count(*) FROM thesis_assumptions;
--     expected: 0
--   SELECT count(*) FROM position_monitoring_log WHERE verdict IS NOT NULL;
--     expected: 0
--
-- AFTER THIS MIGRATION, Vera's output contract carries the claims:
-- every new thesis and candidate states its assumptions, and the
-- monitoring pass returns a status per open assumption instead of one
-- holistic tag. Theses already open have no assumptions and will
-- report no verdict until they are given some — which is the honest
-- state, not a gap to paper over.
--
-- DELIBERATELY NOT IN THIS MIGRATION: any change to Solomon. He
-- escalates on thesis_broken and at_risk-with-a-trigger today, and
-- `strengthened` is the obvious new trigger to add to a position. But
-- wiring the money path to a signal with zero observations would be
-- tuning on noise — the same argument that sets a 30-observation floor
-- on constraint accounting. Record verdicts for a few weeks first,
-- then decide.
-- ============================================================
