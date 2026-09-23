-- ============================================================
-- 014 — theses.close_reason gains 'never_filled'
--
-- WHY. On 2026-09-17 Marcus opened a thesis for GOOGL at ALLOCATION
-- — before the order existed. Ada then refused the order on a stale
-- ledger (8 sessions against a tolerance of 1), so nothing ever
-- filled. The thesis stayed open. For six days Vera checked its
-- assumptions against a position that did not exist and Clara
-- published "AAPL and GOOGL holdings are intact".
--
-- agents/vera.py now names that state — a PHANTOM THESIS, an open
-- thesis with no position behind it — and refuses to resolve it
-- automatically. Resolving it requires a close_reason, and migration
-- 010 offers four, none of which is true here:
--
--   catalyst_resolved      the thing we predicted happened.  No.
--   catalyst_invalidated   it demonstrably did not.           No.
--   risk_exit              a limit or the breaker closed it.  No.
--   unrelated              closed for something nobody
--                          predicted.                         Closest,
--                          and still wrong — it implies a position
--                          existed and was exited.
--
-- All four describe the end of a POSITION. This one never had one.
-- Forcing it into 'unrelated' would put a row in the calibration
-- record claiming a holding closed for reasons outside its thesis,
-- and 010's own comment says that answer "matters most" — it is the
-- one that detects luck wearing a thesis's clothes. Polluting it with
-- orders that never filled would blunt exactly the signal it exists
-- to carry.
--
-- 'never_filled' is therefore not a tidier synonym. It is the
-- difference between "the thesis was wrong" and "the thesis was never
-- tested", and only the first belongs in a calibration score. Every
-- consumer that grades predictions should skip these rows.
--
-- WHY THE OLD CONSTRAINT IS FOUND RATHER THAN NAMED. Migration 010
-- wrote the CHECK inline on ADD COLUMN, so Postgres generated its
-- name. It is almost certainly theses_close_reason_check, but "almost
-- certainly" is how migration 012 nearly shipped with the wrong
-- unique key. The DO block below looks the constraint up by what it
-- actually constrains and drops it by its real name, then adds a
-- replacement under a name this file chose, so migration 015 will not
-- have to guess either.
--
-- THIS MIGRATION DOES NOT CLOSE ANY THESIS. It widens a vocabulary.
-- Closing GOOGL is a human decision about a real position, not a
-- schema change.
-- ============================================================

BEGIN;

DO $$
DECLARE
    old_name text;
BEGIN
    -- Any CHECK on `theses` whose expression mentions close_reason and
    -- the value list from 010. The needs-close constraint mentions
    -- close_reason too, which is why the value is matched as well.
    SELECT conname INTO old_name
    FROM pg_constraint
    WHERE conrelid = 'theses'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%close_reason%'
      AND pg_get_constraintdef(oid) LIKE '%catalyst_resolved%'
      -- Not the one this file adds, so a re-run is a true no-op
      -- rather than a drop-and-recreate.
      AND conname <> 'theses_close_reason_allowed'
    LIMIT 1;

    IF old_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE theses DROP CONSTRAINT %I', old_name);
        RAISE NOTICE 'Dropped old close_reason CHECK: %', old_name;
    ELSE
        RAISE NOTICE 'No pre-existing close_reason value CHECK found.';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'theses_close_reason_allowed') THEN
        ALTER TABLE theses ADD CONSTRAINT theses_close_reason_allowed
            CHECK (close_reason IN ('catalyst_resolved',
                                    'catalyst_invalidated',
                                    'risk_exit',
                                    'never_filled',
                                    'unrelated'));
    END IF;
END $$;

COMMENT ON COLUMN theses.close_reason IS
    'Why the thesis ended. catalyst_resolved = the thing we predicted '
    'happened. catalyst_invalidated = it demonstrably did not. '
    'risk_exit = closed by a limit or the breaker, verdict unknown. '
    'never_filled = no position ever existed behind it — the order was '
    'refused, cancelled or never placed, so the thesis was never tested; '
    'calibration must SKIP these rows rather than score them. '
    'unrelated = closed for something nobody predicted, which is the '
    'answer that matters most: a position that made money for a reason '
    'other than its thesis is luck wearing a thesis''s clothes, and it '
    'should not be counted as a win.';

COMMIT;


-- ============================================================
-- VERIFY
--
--   -- 1. Exactly one value CHECK, under the name this file chose:
--   SELECT conname, pg_get_constraintdef(oid)
--   FROM pg_constraint
--   WHERE conrelid = 'theses'::regclass AND contype = 'c'
--     AND pg_get_constraintdef(oid) LIKE '%catalyst_resolved%';
--     expected: ONE row, theses_close_reason_allowed, listing five
--     values. Two rows means the DO block did not find 010's
--     constraint — stop and look before closing anything.
--
--   -- 2. The new value is accepted:
--   BEGIN;
--   UPDATE theses SET closed_date = CURRENT_DATE,
--                     close_reason = 'never_filled'
--   WHERE id = (SELECT id FROM theses ORDER BY id LIMIT 1);
--   ROLLBACK;
--     expected: UPDATE 1, then rolled back.
--
--   -- 3. A junk value is still refused:
--   BEGIN;
--   UPDATE theses SET closed_date = CURRENT_DATE, close_reason = 'oops'
--   WHERE id = (SELECT id FROM theses ORDER BY id LIMIT 1);
--   ROLLBACK;
--     expected: ERROR violating theses_close_reason_allowed.
--
--   -- 4. 010's other constraint survived untouched:
--   SELECT conname FROM pg_constraint
--   WHERE conname = 'theses_close_reason_needs_close';
--     expected: one row.
--
--   -- 5. Nothing was closed by this migration:
--   SELECT id, ticker, opened_date, closed_date, close_reason
--   FROM theses ORDER BY id;
--     expected: closed_date and close_reason unchanged on every row.
--     On 2026-09-23 that is AAPL, GOOGL and NVDA, all open.
--
-- THE PHANTOM ITSELF, once you have decided:
--
--   UPDATE theses SET closed_date = CURRENT_DATE,
--                     close_reason = 'never_filled'
--   WHERE id = 2;      -- GOOGL, opened 2026-09-17, never filled
--
-- Run it only if that is the call you want to make. Vera will report
-- the phantom at WARNING every day until something closes it, which
-- is the intended pressure.
-- ============================================================
