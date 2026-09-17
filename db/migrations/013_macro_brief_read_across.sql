-- ============================================================
-- 013 — macro_briefs.read_across
--
-- Atlas could not name a company. The prohibition was sound: a macro
-- agent that opines on securities becomes a second stock-picker, and
-- Vera receives his brief as context — her screen is only worth
-- having because nobody handed her the answer first.
--
-- But it swept up a category wrongly. "The silicon complex marks
-- against TSMC's monthly revenue" is a claim about where information
-- ENTERS the chain, not a view on whether TSM is worth owning. In a
-- theme book the links are causally chained and information arrives
-- at one name before it arrives at the rest.
--
-- WHY A COLUMN RATHER THAN MORE PROSE IN `narrative`
--
-- Because the point of it is to be JOINED. When a memory position
-- falls 9%, the question Vera's monitoring pass has to answer is
-- whether the thesis broke or the link repriced. If the compute
-- bellwether guided down the night before, the answer is the second
-- one — and without this row she can write a false `strained` verdict
-- against claims that nothing actually contradicted. That verdict
-- then gets graded as though it had been evidence.
--
-- A sentence in `narrative` cannot be joined to a position's move,
-- counted by Clara, or checked for the one thing that must never
-- appear in it: a forecast. A JSONB array of
-- {ticker, event, links_affected, observed_move_pct} can be all
-- three.
--
-- WHAT KEEPS THIS FROM BECOMING A STOCK-TIP CHANNEL
--
-- Not this migration, and not the prompt. Three checks in
-- agents/atlas.py's output contract, all of which fail the run
-- rather than degrade it:
--
--   1. `ticker` must be in core.theme.BELLWETHERS — the model cannot
--      nominate a name.
--   2. `links_affected` must be a subset of the edges that map
--      already assigns to that ticker — the model cannot invent a
--      causal claim.
--   3. `event` must contain no forward-looking or recommendation
--      language. "Guidance raised" passes; "guidance should support
--      the link" does not.
--
-- And the prose fields stay ticker-free, validated. The old
-- prohibition is moved, not lifted: there is now exactly one place in
-- a brief where a company can be named, and everything in it is
-- checkable.
--
-- NULLABLE AND NOT BACKFILLED. Every brief before today has no
-- read-across, and that is the truth about those days rather than
-- something to fill in. An empty array from Atlas means "no
-- bellwether reported and nothing moved enough to attribute", which
-- is different from NULL meaning "this predates the field" — the
-- distinction is worth keeping, so no DEFAULT is set.
-- ============================================================

BEGIN;

ALTER TABLE macro_briefs
    ADD COLUMN IF NOT EXISTS read_across JSONB;

COMMENT ON COLUMN macro_briefs.read_across IS
    'Which theme links repriced off which bellwether, as an array of '
    '{ticker, event, links_affected, observed_move_pct}. The only place '
    'in a brief where a company may be named, and every entry is '
    'validated against core.theme.BELLWETHERS: a ticker outside the map, '
    'an invented causal edge, or forward-looking language fails the run. '
    'Exists to be JOINED to a position''s own move — distinguishing a '
    'link repricing from a thesis breaking. NULL means the brief '
    'predates the field; an empty array means nothing to report.';

COMMIT;


-- ============================================================
-- VERIFY
--
--   \d macro_briefs
--     expected: read_across | jsonb | nullable, no default
--
--   -- old briefs are untouched and still readable:
--   SELECT brief_date, regime_signal, read_across
--   FROM macro_briefs ORDER BY brief_date DESC LIMIT 5;
--     expected: read_across NULL on every pre-013 row. If this query
--     errors, core/models.py is ahead of the database again — the
--     same failure that killed premarket on 2026-09-15.
--
--   -- after the first post-013 premarket run:
--   SELECT brief_date,
--          jsonb_array_length(coalesce(read_across, '[]'::jsonb)) AS entries
--   FROM macro_briefs ORDER BY brief_date DESC LIMIT 3;
--     expected: an integer, frequently 0. Zero is the common case and
--     is correct — most days no bellwether reports.
--
--   -- the guard that matters, checked against stored data:
--   SELECT brief_date, e->>'ticker' AS ticker, e->>'event' AS event
--   FROM macro_briefs, jsonb_array_elements(read_across) AS e
--   WHERE e->>'event' ~* '(should|likely|expect|bullish|cheap|upside)'
--   ORDER BY brief_date DESC;
--     expected: ZERO ROWS, always. A row here means the contract in
--     agents/atlas.py was bypassed — a direct write, or a validator
--     that stopped running. Worth checking after any atlas.py change.
-- ============================================================
