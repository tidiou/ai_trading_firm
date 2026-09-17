-- ============================================================
-- Migration 012 — a decade of reported numbers, from the source
--
--   docker exec -i mby-trading-db psql -U mby -d mby_trading \
--       < db/migrations/012_company_facts.sql
--
-- Safe to re-run.
--
-- WHAT WAS WRONG (S2a of the Research Desk)
--
-- Vera scores conviction 1-5 from an FMP SNAPSHOT — a point-in-time
-- valuation with no history behind it. There is no way for her to know
-- whether a margin is the best in a decade or the worst, whether capex
-- has been climbing for three years, whether the share count is
-- shrinking or diluting, or whether reported earnings have ever
-- converted into cash.
--
-- That is not a prompt problem. No amount of instruction recovers a
-- time series that was never fetched, and it is a large part of why
-- the conviction scores are noisy: she is being asked for a
-- multi-year judgment from one quarter's figures.
--
-- Nor could FMP fix it. The free tier's deep fundamentals are gated,
-- news and the economic calendar are already paywalled, and the whole
-- account is capped at 250 calls a day — a budget Vera's screening
-- pass already competes for.
--
-- WHAT THIS ADDS
--
--   company_facts    every consolidated figure a company has reported,
--                    as filed, from SEC EDGAR's XBRL companyfacts API
--
-- WHY EDGAR. It is the primary source rather than a vendor's copy of
-- it, it is free, and it has no daily cap — only a rate limit and a
-- requirement to identify yourself in the User-Agent. Ten years of
-- revenue, margins, capex, buybacks, share count and debt arrive as
-- structured numbers that core/fundamentals.py turns into series in
-- deterministic code, with no model involved. That is the pattern this
-- codebase is built on: code computes, the model narrates.
--
-- THE BOUNDARY, STATED SO NOBODY PLANS PAST IT. companyfacts returns
-- CONSOLIDATED facts with no dimensional breakdown, so SEGMENT revenue
-- and segment margins are NOT in here. Getting those means parsing the
-- filing's own XBRL instance or its text, which is a different job with
-- a different cost. Management incentives are in the DEF 14A, which is
-- document parsing. Neither is in scope for this migration and neither
-- is blocked by it.
--
-- WHY accession IS PART OF THE KEY
--
-- Companies restate. The same fiscal period can be reported twice with
-- different numbers, and the second filing is not a correction of a
-- typo — it is a different claim about the same past.
--
-- If a restatement overwrote the original, a thesis formed on the
-- original figure would silently lose its own evidence: the reasoning
-- would reference a number the database no longer contains, and the
-- calibration would grade the thesis against facts nobody had at the
-- time. So every filing's version of a period survives, keyed on its
-- accession number, and "the current figure" is a query (latest
-- filed_date) rather than a row that gets mutated.
--
-- Same reasoning as `transactions` being append-only with corrections
-- inserted rather than applied.
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS company_facts (
    id            BIGSERIAL PRIMARY KEY,
    -- Both, deliberately. cik is EDGAR's stable identity and survives
    -- a ticker change; ticker is what every other table in this
    -- schema joins on and what a human reads.
    cik           TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    -- The XBRL concept, e.g. 'Revenues', 'CapitalExpenditures',
    -- 'NetCashProvidedByUsedInOperatingActivities'. Stored as reported
    -- rather than mapped to our own names: a mapping layer here would
    -- be a second source of truth to keep in step with EDGAR's
    -- taxonomy, and the derived series in core/fundamentals.py is the
    -- right place for that translation.
    taxonomy      TEXT NOT NULL DEFAULT 'us-gaap',
    tag           TEXT NOT NULL,
    unit          TEXT NOT NULL,          -- USD | shares | USD/shares
    fiscal_year   INTEGER,
    fiscal_period TEXT,                   -- FY | Q1 | Q2 | Q3 | Q4
    period_start  DATE,
    period_end    DATE NOT NULL,
    value         NUMERIC(24,4) NOT NULL,
    form          TEXT,                   -- 10-K | 10-Q | 8-K | 20-F ...
    filed_date    DATE NOT NULL,
    -- The filing this figure came from. Part of the key so a
    -- restatement lands beside the original instead of erasing it.
    accession     TEXT NOT NULL,
    fetched_at    TIMESTAMPTZ DEFAULT now(),
    -- period_start IS PART OF THE KEY, AND THIS IS NOT COSMETIC.
    --
    -- One 10-Q reports the SAME cash-flow concept twice for the same
    -- period_end: once for the discrete quarter (start = quarter start)
    -- and once cumulatively for the year to date (start = fiscal-year
    -- start). Both come from one accession. Without period_start in the
    -- key those two facts collide, ON CONFLICT DO NOTHING keeps
    -- whichever was inserted first, and the discrete/cumulative
    -- distinction is destroyed before anything can read it — turning a
    -- nine-month figure into a quarter, silently, with no error.
    --
    -- NULLS NOT DISTINCT because period_start is null on instant facts
    -- (a balance-sheet item has a date, not a period), and Postgres's
    -- default treats every null as unique — which would let the same
    -- instant fact insert repeatedly. Requires Postgres 15 or later;
    -- on an older server this migration fails loudly, which is the
    -- correct outcome rather than a quietly duplicating table.
    UNIQUE NULLS NOT DISTINCT
        (cik, taxonomy, tag, unit, period_start, period_end, accession)
);

-- The query this table exists to serve: one company's history of one
-- concept, newest filing first.
CREATE INDEX IF NOT EXISTS idx_company_facts_series
    ON company_facts (ticker, tag, period_end DESC, filed_date DESC);

-- And the reverse: what did this filing tell us?
CREATE INDEX IF NOT EXISTS idx_company_facts_accession
    ON company_facts (accession);

COMMENT ON TABLE company_facts IS
    'Consolidated figures as reported to the SEC, from EDGAR XBRL '
    'companyfacts. Append-only in effect: a restated period is a new '
    'row under a new accession, never an update, so a thesis keeps the '
    'evidence it was actually formed on. NO segment detail — '
    'companyfacts carries no dimensional breakdown.';
COMMENT ON COLUMN company_facts.accession IS
    'The filing this figure came from, and part of the unique key. '
    'Companies restate, and a restatement is a different claim about '
    'the same past rather than a typo fix — overwriting would make a '
    'thesis reference a number the database no longer holds.';
COMMENT ON COLUMN company_facts.tag IS
    'The XBRL concept as EDGAR reports it, unmapped. Translation to '
    'the desk''s own vocabulary belongs in core/fundamentals.py, not '
    'in the store — a mapping here would be a second source of truth '
    'to keep in step with a taxonomy we do not control.';

COMMIT;


-- ============================================================
-- VERIFY
--
--   \d company_facts
--     expected: 16 columns, UNIQUE NULLS NOT DISTINCT (cik, taxonomy,
--               tag, unit, period_start, period_end, accession),
--               two indexes
--
--   -- the collision the key exists to prevent, after syncing MSFT:
--   SELECT period_start, period_end, value/1e9 AS bn
--   FROM company_facts
--   WHERE ticker = 'MSFT'
--     AND tag = 'NetCashProvidedByUsedInOperatingActivities'
--     AND period_end = (SELECT max(period_end) FROM company_facts
--                       WHERE ticker = 'MSFT' AND tag =
--                       'NetCashProvidedByUsedInOperatingActivities')
--   ORDER BY period_start;
--     expected: MORE THAN ONE ROW for that period_end — the discrete
--     quarter and the cumulative year to date. One row means the two
--     collided and core/theme.py is reading a cumulative figure as a
--     quarter.
--
--   -- after `python -m core.fundamentals sync AAPL`:
--   SELECT tag, count(*) AS periods, min(period_end), max(period_end)
--   FROM company_facts WHERE ticker = 'AAPL'
--   GROUP BY tag ORDER BY periods DESC LIMIT 10;
--     expected: several tags with ~40+ periods each, spanning a decade
--
--   -- a restatement keeps both versions:
--   SELECT period_end, value, filed_date, accession
--   FROM company_facts
--   WHERE ticker = 'AAPL' AND tag = 'Revenues'
--   ORDER BY period_end DESC, filed_date DESC LIMIT 6;
--     expected: where a period appears twice, two DIFFERENT accessions
--     and filed_dates — that is correct, not a duplicate
-- ============================================================
