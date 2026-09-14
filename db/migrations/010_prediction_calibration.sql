-- ============================================================
-- Migration 010 — making the desk's opinions gradeable
--
--   docker exec -i mby-trading-db psql -U mby -d mby_trading \
--       < db/migrations/010_prediction_calibration.sql
--
-- Safe to re-run.
--
-- WHAT WAS WRONG (I1-I4 of the Measurement Framework)
--
-- The desk records what it DID in forensic detail and what it BELIEVED
-- only in prose. Every table downstream of a decision is complete;
-- every table holding a decision's premise is not:
--
--   theses           thesis_text, catalyst, original_conviction — but
--                    no claim about which way the stock goes, by how
--                    much, or by when. A thesis can therefore be
--                    profitable or unprofitable, never right or wrong,
--                    and those are different facts. This is why
--                    weekly_reports.vera_calibration has been an empty
--                    JSONB column since the schema was first written:
--                    the slot was designed for a score that had no
--                    input.
--
--   macro_briefs     confidence is 'low|medium|high'. Bucketing those
--                    and checking that high-confidence calls land more
--                    often than low ones is worth doing — but a
--                    BRIER SCORE, the proper scoring rule for a
--                    forecast, needs a number. The reason to want a
--                    proper rule is that it cannot be gamed by
--                    hedging: an agent that says 'medium' every day
--                    scores respectably on any loose reading and badly
--                    on Brier, which is the correct verdict.
--
--   proposal_reviews rules_checked holds human-readable sentences.
--                    Excellent as an audit trail, unusable as a metric
--                    input — constraint accounting has to group by
--                    WHICH limit bound, and today that means regular
--                    expressions over prose that changes whenever
--                    someone improves the wording. Nora already
--                    computes the answer (nora.py names the binding
--                    constraint) and then writes it into a sentence
--                    instead of a column.
--
--   theses           closed_date says when a thesis ended and nothing
--                    says why. Whether a position closed because its
--                    catalyst resolved, because the catalyst was
--                    disproved, or for a reason nobody predicted is
--                    the single distinction that separates skill from
--                    luck at the level of one idea — and it is
--                    unrecoverable a month later.
--
-- WHAT THIS ADDS
--
--   theses, new_candidates  expected_direction, expected_move_pct,
--                           horizon_days — a falsifiable prediction
--   theses                  close_reason
--   macro_briefs            confidence_pct
--   proposal_reviews        binding_rule
--
-- DELIBERATELY NOT BACKFILLED, for the same reason migration 008 did
-- not backfill recorded_at. Mapping 'low|medium|high' onto 25/50/75
-- would manufacture probabilities that no agent ever asserted, and a
-- Brier score computed over invented forecasts is worse than no Brier
-- score: it is a number that looks like evidence. Historic rows stay
-- NULL. NULL here means "this row predates the prediction contract",
-- never "no opinion" and never zero.
--
-- NOTHING BREAKS. Every column is nullable and no agent is required to
-- populate one on this migration. The agents' output contracts change
-- separately; until they do, these columns are empty and every
-- existing query, test and dashboard view behaves exactly as before.
-- The migration is deliberately first, because a prediction that was
-- not recorded on the day it was made cannot be reconstructed
-- afterwards — every session between now and the contract change is a
-- session of ungradeable opinions.
-- ============================================================

BEGIN;

-- ============================================================
-- I1 — a thesis states a falsifiable prediction
--
-- SIGN CONVENTION, and it matters: expected_move_pct is a MAGNITUDE
-- and is always positive. expected_direction carries the sign. The
-- predicted return is therefore
--
--     (+1 if up else -1) * expected_move_pct
--
-- rather than a single signed column. Two columns instead of one
-- because a signed magnitude has a silent failure mode: a down-thesis
-- written with a positive number reads as an up-thesis and grades as
-- one, with nothing anywhere to indicate the sign was lost. The CHECK
-- below makes that unrepresentable instead of merely unlikely.
-- ============================================================

ALTER TABLE theses
    ADD COLUMN IF NOT EXISTS expected_direction TEXT
        CHECK (expected_direction IN ('up', 'down'));
ALTER TABLE theses
    ADD COLUMN IF NOT EXISTS expected_move_pct NUMERIC(6,2)
        CHECK (expected_move_pct > 0);
ALTER TABLE theses
    ADD COLUMN IF NOT EXISTS horizon_days SMALLINT
        CHECK (horizon_days > 0);

ALTER TABLE new_candidates
    ADD COLUMN IF NOT EXISTS expected_direction TEXT
        CHECK (expected_direction IN ('up', 'down'));
ALTER TABLE new_candidates
    ADD COLUMN IF NOT EXISTS expected_move_pct NUMERIC(6,2)
        CHECK (expected_move_pct > 0);
ALTER TABLE new_candidates
    ADD COLUMN IF NOT EXISTS horizon_days SMALLINT
        CHECK (horizon_days > 0);

-- All three, or none. A half-written prediction is strictly worse than
-- no prediction: it cannot be graded, but it is not empty either, so
-- it survives every "do we have a forecast for this?" filter and then
-- fails silently at scoring time. Added as a table constraint because
-- it spans three columns, and guarded by a catalog lookup because
-- Postgres has no ADD CONSTRAINT IF NOT EXISTS and this file must stay
-- re-runnable.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'theses_prediction_complete') THEN
        ALTER TABLE theses ADD CONSTRAINT theses_prediction_complete CHECK (
            (expected_direction IS NULL
             AND expected_move_pct IS NULL
             AND horizon_days IS NULL)
            OR
            (expected_direction IS NOT NULL
             AND expected_move_pct IS NOT NULL
             AND horizon_days IS NOT NULL)
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'new_candidates_prediction_complete') THEN
        ALTER TABLE new_candidates ADD CONSTRAINT new_candidates_prediction_complete CHECK (
            (expected_direction IS NULL
             AND expected_move_pct IS NULL
             AND horizon_days IS NULL)
            OR
            (expected_direction IS NOT NULL
             AND expected_move_pct IS NOT NULL
             AND horizon_days IS NOT NULL)
        );
    END IF;
END $$;

COMMENT ON COLUMN theses.expected_direction IS
    'Which way this thesis claims the name goes: up|down. Carries the '
    'sign for expected_move_pct, which is always a positive magnitude. '
    'NULL on rows predating migration 010 — no opinion was recorded, '
    'which is not the same as a neutral one.';
COMMENT ON COLUMN theses.expected_move_pct IS
    'The size of the move being claimed, as a POSITIVE percentage. The '
    'direction lives in expected_direction; a signed value here is '
    'rejected by CHECK so a down-thesis can never be graded as an up.';
COMMENT ON COLUMN theses.horizon_days IS
    'Calendar days from opened_date by which the claim should have '
    'played out. Fixed when the thesis is written and NEVER revised '
    'afterwards: choosing a horizon once the outcome is visible is the '
    'cheapest way to manufacture a flattering calibration score, and '
    'it is indistinguishable from honest analysis when read later.';

-- ============================================================
-- I4 — why a thesis ended
--
-- 'still_open' is deliberately NOT one of the values. That state is
-- already expressed by closed_date IS NULL, and encoding the same fact
-- in two columns guarantees they eventually disagree.
-- ============================================================

ALTER TABLE theses
    ADD COLUMN IF NOT EXISTS close_reason TEXT
        CHECK (close_reason IN ('catalyst_resolved',
                                'catalyst_invalidated',
                                'risk_exit',
                                'unrelated'));

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'theses_close_reason_needs_close') THEN
        -- One-directional on purpose. A close_reason without a
        -- closed_date is incoherent and is refused. A closed_date
        -- without a close_reason is every thesis closed before this
        -- migration, and those are allowed to stay unexplained rather
        -- than be assigned a reason nobody recorded.
        ALTER TABLE theses ADD CONSTRAINT theses_close_reason_needs_close
            CHECK (close_reason IS NULL OR closed_date IS NOT NULL);
    END IF;
END $$;

COMMENT ON COLUMN theses.close_reason IS
    'Why the thesis ended. catalyst_resolved = the thing we predicted '
    'happened. catalyst_invalidated = it demonstrably did not. '
    'risk_exit = closed by a limit or the breaker, verdict unknown. '
    'unrelated = closed for something nobody predicted, which is the '
    'answer that matters most: a position that made money for a reason '
    'other than its thesis is luck wearing a thesis''s clothes, and it '
    'will not repeat.';

-- ============================================================
-- I2 — confidence as a probability
--
-- The prose column stays. It is what the model actually writes and it
-- is what a human reads in the daily report. The number is added
-- BESIDE it rather than replacing it, so nothing has to be rewritten
-- and a disagreement between the two is itself visible.
-- ============================================================

ALTER TABLE macro_briefs
    ADD COLUMN IF NOT EXISTS confidence_pct NUMERIC(5,2)
        CHECK (confidence_pct >= 0 AND confidence_pct <= 100);

COMMENT ON COLUMN macro_briefs.confidence_pct IS
    'The probability Atlas is actually asserting for its regime call, '
    '0-100. Enables a Brier score, which is a PROPER scoring rule: '
    'unlike hit rate it cannot be improved by hedging, so a brief that '
    'says 50% every day scores exactly as badly as it deserves. NULL '
    'on rows predating migration 010 and deliberately not derived from '
    'the prose confidence — inventing probabilities nobody asserted '
    'would produce a Brier score that is worse than none, because it '
    'would look like evidence.';

-- ============================================================
-- I3 — which limit actually bound
--
-- Note this records which limit set the CEILING, not only which one
-- refused. On an approval it names the tighter of the position and
-- sector headrooms — which is exactly what constraint accounting needs
-- in order to price a trim as well as a rejection. Whether the trade
-- was refused is already in `decision`; these are different questions
-- and get different columns.
--
-- NULL means the row predates this migration. Every row written from
-- here on gets a value, including reductions, so NULL never has to do
-- double duty as a category.
-- ============================================================

ALTER TABLE proposal_reviews
    ADD COLUMN IF NOT EXISTS binding_rule TEXT
        CHECK (binding_rule IN ('circuit_breaker',
                                'max_position_pct',
                                'max_sector_pct',
                                'reduction'));

COMMENT ON COLUMN proposal_reviews.binding_rule IS
    'Which limit set the ceiling for this proposal — not merely which '
    'refused it. circuit_breaker = frozen, no new risk. '
    'max_position_pct / max_sector_pct = the tighter of the two '
    'headrooms, whether the outcome was a rejection or a trim. '
    'reduction = an exit or trim, which skips the limit checks because '
    'de-risking is always permitted. Nora already computes this; '
    'before migration 010 it was written into a rules_checked sentence '
    'and could only be recovered by parsing prose. NULL on rows '
    'predating this migration.';

COMMIT;


-- ============================================================
-- VERIFY
--
--   \d theses
--     expected: expected_direction | text
--               expected_move_pct  | numeric(6,2)
--               horizon_days       | smallint
--               close_reason       | text
--
--   SELECT conname FROM pg_constraint
--   WHERE conname IN ('theses_prediction_complete',
--                     'new_candidates_prediction_complete',
--                     'theses_close_reason_needs_close');
--     expected: three rows.
--
--   -- the completeness guard actually refuses a half-prediction:
--   BEGIN;
--   UPDATE theses SET expected_direction = 'up' WHERE id = (
--       SELECT id FROM theses ORDER BY id LIMIT 1);
--     expected: ERROR, new row violates check constraint
--               "theses_prediction_complete"
--   ROLLBACK;
--
--   SELECT count(*) FROM theses WHERE expected_direction IS NOT NULL;
--     expected: 0. Nothing was backfilled, and nothing should be.
--
-- AFTER THIS MIGRATION, the columns exist and stay empty until the
-- agent output contracts are changed to fill them:
--
--   Vera    every thesis and candidate gains expected_direction,
--           expected_move_pct and horizon_days as REQUIRED fields, and
--           Vera sets close_reason when she closes a thesis.
--   Atlas   the brief gains confidence_pct beside its prose confidence.
--   Nora    review_proposal already knows the binding constraint (it
--           names it in rules_checked); it returns it as a field and
--           the caller persists it.
--
-- Until then this migration is inert — which is the point. The columns
-- have to exist before the first prediction worth grading is made.
-- ============================================================
