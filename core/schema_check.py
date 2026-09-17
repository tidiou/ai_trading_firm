"""
Does the database have what the code is about to SELECT?

=====================================================================
WHY THIS EXISTS — FOUR FAILURES OF ONE SHAPE
=====================================================================

The desk has now lost a session four times to the same mistake:
core/models.py gained a column, the migration had not been applied,
and the first read of that table died. Each time the symptom was a
SQLAlchemy traceback two hundred lines deep ending in

    psycopg2.errors.UndefinedColumn: column
    macro_briefs.read_across does not exist

which names the problem accurately and says nothing about the fix.

  010  confidence_pct        killed premarket, 15 Sept
  011  thesis_assumptions    UndefinedTable in Vera's monitoring pass
  013  read_across           killed premarket, 17 Sept

Three properties made each one expensive:

  IT FAILED LATE. The traceback arrived after the run had started, so
  the cycle row was already written and the attempt already counted.
  Three ticks later the retry cap engaged and the window shut.

  IT FAILED AFTER SPENDING MONEY. Atlas's model call happens before
  the first read that breaks, so a schema gap cost API tokens and FMP
  quota to discover.

  IT DID NOT NAME THE MIGRATION. The column name is in the error; the
  file that would create it is not. That is the one piece of
  information needed to act.

=====================================================================
WHAT THIS DOES INSTEAD
=====================================================================

One query against information_schema, compared against the ORM's own
metadata, before anything else happens. If the database is behind, the
run refuses with the missing objects, the migration file that appears
to introduce each one, and the commands to apply them in order.

TWO DESIGN DECISIONS WORTH THE WORDS.

1. THE MIGRATION IS FOUND BY SEARCHING, NOT FROM A TABLE OF
   MAPPINGS. A hand-maintained {column -> migration} dict would be a
   second source of truth about the schema, kept in step by hand — the
   exact class of thing that caused the problem this module exists to
   prevent. Instead the migration files are searched for the missing
   object and the earliest one that introduces it is reported. Add a
   migration and attribution keeps working with no bookkeeping.

2. A DATABASE THAT IS AHEAD IS NOT A GAP. Columns present in the
   database but absent from the ORM are ignored, because the ORM only
   ever selects what it knows about. Applying a migration before
   pulling the code that uses it is a normal, safe order of
   operations, and a gate that refused it would be worse than no gate.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"


class SchemaOutOfDate(RuntimeError):
    """The database is behind the code. Carries the gaps for a caller
    that wants to report them structurally rather than as text."""

    def __init__(self, message: str, gaps: list):
        super().__init__(message)
        self.gaps = gaps


@dataclass(frozen=True)
class SchemaGap:
    table: str
    # None means the whole table is missing, which is a different
    # message and a different fix from a missing column.
    column: Optional[str] = None
    migration: Optional[str] = None

    @property
    def is_table(self) -> bool:
        return self.column is None

    def label(self) -> str:
        return f"{self.table} (table)" if self.is_table \
            else f"{self.table}.{self.column}"


# =================================================================
# PURE — the comparison and the attribution
# =================================================================
def find_gaps(expected: dict, actual: dict) -> list[SchemaGap]:
    """What the code expects that the database does not have.

    `expected` and `actual` are both {table_name: {column_name, ...}}.
    A missing table is reported once as a table gap rather than once
    per column — twenty column lines for one absent table buries the
    actual problem.
    """
    gaps: list[SchemaGap] = []
    for table in sorted(expected):
        if table not in actual:
            gaps.append(SchemaGap(table=table))
            continue
        for column in sorted(expected[table] - actual[table]):
            gaps.append(SchemaGap(table=table, column=column))
    return gaps


def _mentions_table(sql: str, table: str) -> bool:
    return re.search(rf'(?<![a-z0-9_]){re.escape(table)}(?![a-z0-9_])',
                     sql, re.IGNORECASE) is not None


def attribute_migration(gap: SchemaGap, sources: dict) -> Optional[str]:
    """Which migration file appears to introduce this object.

    `sources` is {filename: sql_text}. The EARLIEST matching file wins:
    a column added in 013 and referenced again in 015 is introduced by
    013, and pointing at 015 would have the operator apply the wrong
    file and see no change.

    Returns None when nothing matches, which is itself informative —
    it means the ORM has an object no migration creates, so the fix is
    a missing migration rather than an unapplied one.
    """
    for name in sorted(sources):
        sql = sources[name]
        if gap.is_table:
            if re.search(rf'CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?"?'
                         rf'{re.escape(gap.table)}"?(?![a-z0-9_])',
                         sql, re.IGNORECASE):
                return name
            continue

        # A column added later carries an explicit ADD COLUMN, which is
        # the precise signal. Checked first for that reason.
        if re.search(rf'ADD\s+COLUMN\s+(IF\s+NOT\s+EXISTS\s+)?"?'
                     rf'{re.escape(gap.column)}"?(?![a-z0-9_])',
                     sql, re.IGNORECASE):
            return name

        # Otherwise the column was part of the table's original CREATE,
        # so require the file to mention BOTH the table and the column —
        # a bare column-name match would attribute `narrative` to
        # whichever migration happened to mention it in a comment.
        if _mentions_table(sql, gap.table) and \
                re.search(rf'(?<![a-z0-9_]){re.escape(gap.column)}(?![a-z0-9_])',
                          sql, re.IGNORECASE):
            return name
    return None


def describe_gaps(gaps: list[SchemaGap]) -> str:
    """The message the operator reads at 07:11 on a Tuesday.

    It has one job: contain every piece of information needed to fix
    the problem, so that nobody has to come back and ask which file to
    apply.
    """
    if not gaps:
        return "Schema is up to date."

    width = max(len(g.label()) for g in gaps)
    lines = [
        "SCHEMA OUT OF DATE — the desk did not run.",
        "",
        f"core/models.py expects {len(gaps)} thing(s) this database does "
        f"not have:",
        "",
    ]
    for g in gaps:
        where = g.migration or "NO MIGRATION FOUND — see below"
        lines.append(f"    {g.label():<{width}}   ->  {where}")

    files = sorted({g.migration for g in gaps if g.migration})
    if files:
        lines += ["", "Apply them in this order:", ""]
        for f in files:
            lines.append(f'    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 '
                         f'-f db/migrations/{f}')

    if any(g.migration is None for g in gaps):
        lines += [
            "",
            "Something above has NO migration that creates it. That is a "
            "missing migration rather than an unapplied one — the ORM "
            "describes an object nothing in db/migrations/ builds.",
        ]

    lines += [
        "",
        "Nothing ran and no model or data-provider calls were made, so "
        "this cost the tick and nothing else.",
    ]
    return "\n".join(lines)


# =================================================================
# DATABASE
# =================================================================
def expected_schema() -> dict:
    """From the ORM's own metadata — the single source of what the code
    will actually select. Derived rather than declared, so it cannot
    drift from the models the way a checklist would."""
    from core.models import Base
    return {t.name: {c.name for c in t.columns}
            for t in Base.metadata.tables.values()}


def actual_schema(session) -> dict:
    """From information_schema, in one query.

    Scoped to current_schema() rather than hard-coding 'public', so a
    non-default search_path reports on the schema the connection is
    actually using rather than on a different one that happens to look
    correct.
    """
    rows = session.execute(text(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema()"
    )).all()
    out: dict = {}
    for table_name, column_name in rows:
        out.setdefault(table_name, set()).add(column_name)
    return out


def load_migration_sources(directory: Optional[Path] = None) -> dict:
    """{filename: sql} for every .sql in db/migrations."""
    directory = directory or MIGRATIONS_DIR
    if not directory.is_dir():
        logger.warning("No migrations directory at %s — a schema gap will be "
                       "reported without naming a file to apply.", directory)
        return {}
    return {p.name: p.read_text(encoding="utf-8", errors="replace")
            for p in sorted(directory.glob("*.sql"))}


def schema_gaps(session, directory: Optional[Path] = None) -> list[SchemaGap]:
    """The gaps, with each one attributed to a migration where possible."""
    gaps = find_gaps(expected_schema(), actual_schema(session))
    if not gaps:
        return []
    sources = load_migration_sources(directory)
    return [SchemaGap(g.table, g.column, attribute_migration(g, sources))
            for g in gaps]


def require_schema(directory: Optional[Path] = None) -> None:
    """Raise SchemaOutOfDate unless the database has everything the ORM
    expects. Call this BEFORE anything that costs money or writes a
    cycle row — that ordering is the whole point.

    A connection failure is deliberately NOT swallowed. If the database
    is unreachable the desk cannot run either way, and a gate that
    passed quietly on a failed connection would be a gate that only
    works when it is not needed.
    """
    from core.db import session_scope

    with session_scope() as session:
        gaps = schema_gaps(session, directory=directory)

    if gaps:
        message = describe_gaps(gaps)
        logger.error("%s", message)
        raise SchemaOutOfDate(message, gaps)

    logger.info("Schema check passed — %d tables, all expected columns present.",
                len(expected_schema()))


# =================================================================
# CLI — so the check can be run on its own, before a session
# =================================================================
def _main(argv: list[str]) -> int:
    """`python -m core.schema_check`

    Exit 0 when the database matches the code, 3 when it is behind.
    Non-zero-but-distinct so a shell script can tell a schema gap from
    a crash.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        require_schema()
    except SchemaOutOfDate:
        return 3          # already logged, with the fix
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv))
