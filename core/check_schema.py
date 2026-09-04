"""
Schema drift checker — `python -m core.check_schema`

Compares core/models.py against the database you are actually connected
to, reports what is missing, and can generate or apply the SQL to fix it.

    python -m core.check_schema              # report
    python -m core.check_schema --emit-sql   # print the ALTER statements
    python -m core.check_schema --fix        # apply them (asks first)

WHY THIS EXISTS. models.py and db/schema.sql are kept in sync by hand,
but neither one is the database. This project gets re-downloaded into a
fresh folder while the Docker volume persists; tables and columns get
added late in a design change and the live ALTER gets forgotten, or
runs against a volume that is later replaced. None of it surfaces until
an agent is halfway through a write and Postgres says `relation "..."
does not exist` or `column ... does not exist`.

That is the worst place to find out — mid-cycle, after other agents
have already run and spent API quota, with the work in flight rolled
back. One command beforehand turns it into a list.

WHAT IT CANNOT SEE. Names, not types or constraints: a column that
exists with the wrong type reads as present. Full type-level comparison
is what Alembic is for, and if this project outgrows hand-written
migrations, that is the upgrade to make.
"""

import argparse
import sys

from sqlalchemy import inspect, text
from sqlalchemy.dialects import postgresql

from core.db import engine
from core.models import Base


def find_drift() -> tuple[list[str], list[tuple[str, list[str]]], list[str]]:
    """
    Returns (missing_tables, tables_with_missing_columns, unknown_tables).

    `unknown_tables` are tables in the database that no model describes.
    Reported for information only — they are usually harmless leftovers,
    and this tool will never suggest dropping one.
    """
    inspector = inspect(engine)
    live_tables = set(inspector.get_table_names())

    missing_tables = []
    missing_columns = []

    for name, table in sorted(Base.metadata.tables.items()):
        if name not in live_tables:
            missing_tables.append(name)
            continue
        live_cols = {c["name"] for c in inspector.get_columns(name)}
        absent = sorted({c.name for c in table.columns} - live_cols)
        if absent:
            missing_columns.append((name, absent))

    unknown = sorted(live_tables - set(Base.metadata.tables))
    return missing_tables, missing_columns, unknown


def _column_sql(table_name: str, column_name: str) -> tuple[str, str | None]:
    """
    One ALTER statement for a missing column, plus an optional warning.

    A NOT NULL column with no server-side default cannot be added to a
    populated table — Postgres has no value to put in the existing rows.
    Rather than emit a statement that will fail, this emits the column
    as nullable and says so, leaving you to backfill and tighten it
    deliberately. Guessing a fill value for a risk parameter is exactly
    the kind of silent decision this project avoids elsewhere.
    """
    column = Base.metadata.tables[table_name].columns[column_name]
    col_type = column.type.compile(dialect=postgresql.dialect())

    default = None
    if column.server_default is not None:
        default = str(column.server_default.arg.text
                      if hasattr(column.server_default.arg, "text")
                      else column.server_default.arg)

    warning = None
    if default is not None:
        nullness = "" if column.nullable else " NOT NULL"
        clause = f"{col_type}{nullness} DEFAULT {default}"
    elif column.nullable:
        clause = col_type
    else:
        clause = col_type  # emitted nullable on purpose — see docstring
        warning = (
            f"-- NOTE: {table_name}.{column_name} is NOT NULL in models.py but has no\n"
            f"--       server default, so it is added NULLABLE here. Backfill it, then:\n"
            f"--         ALTER TABLE {table_name} ALTER COLUMN {column_name} SET NOT NULL;"
        )

    stmt = f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} {clause};"
    return stmt, warning


def build_fix_sql(missing_columns: list[tuple[str, list[str]]]) -> list[str]:
    """The ALTER statements needed for every missing column, in order."""
    statements = []
    for table_name, columns in missing_columns:
        for column_name in columns:
            stmt, warning = _column_sql(table_name, column_name)
            if warning:
                statements.append(warning)
            statements.append(stmt)
    return statements


def apply_fix(statements: list[str]) -> int:
    """Execute the generated ALTERs. Comments are skipped."""
    executable = [s for s in statements if not s.lstrip().startswith("--")]
    if not executable:
        print("Nothing to apply.")
        return 0

    applied = 0
    with engine.begin() as conn:
        for stmt in executable:
            print(f"  {stmt}")
            conn.execute(text(stmt))
            applied += 1
    print(f"\nApplied {applied} statement(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.check_schema",
        description="Report and optionally repair drift between models.py and the live database.",
    )
    parser.add_argument("--emit-sql", action="store_true",
                        help="print the ALTER statements instead of running anything")
    parser.add_argument("--fix", action="store_true",
                        help="apply the ALTER statements for missing columns")
    parser.add_argument("--yes", action="store_true",
                        help="with --fix, skip the confirmation prompt")
    args = parser.parse_args(argv)

    try:
        missing_tables, missing_columns, unknown = find_drift()
    except Exception as exc:  # noqa: BLE001 — the message matters more than the type
        print(f"Could not inspect the database: {exc}")
        print("Check DATABASE_URL in .env, and that the Postgres container is running.")
        return 2

    if not missing_tables and not missing_columns:
        if unknown and not args.emit_sql:
            print("Tables in the database with no model (informational, harmless):")
            for t in unknown:
                print(f"    {t}")
            print()
        print("Schema is in sync — every model has its table and every column is present.")
        return 0

    fix_sql = build_fix_sql(missing_columns)

    # --emit-sql prints SQL and nothing else, so it can be piped straight
    # into psql without a grep in between.
    if args.emit_sql:
        if missing_tables:
            print("-- MISSING TABLES — run db/schema.sql instead, it is idempotent:")
            for t in missing_tables:
                print(f"--     {t}")
            print()
        for line in fix_sql:
            print(line)
        return 0

    if unknown:
        print("Tables in the database with no model (informational, harmless):")
        for t in unknown:
            print(f"    {t}")
        print()

    if missing_tables:
        print("MISSING TABLES:")
        for t in missing_tables:
            print(f"    {t}")
        print()
        print("  Fix — schema.sql is idempotent, so this creates only what is absent:")
        print("    docker exec -i mby-trading-db psql -U mby -d mby_trading < db/schema.sql")
        print()

    if missing_columns:
        print("MISSING COLUMNS (the table exists, so schema.sql will NOT fix these):")
        for table, cols in missing_columns:
            print(f"    {table}: {', '.join(cols)}")
        print()

        if args.fix:
            print("About to apply:")
            for line in fix_sql:
                print(f"  {line}")
            if not args.yes:
                answer = input("\nApply these to the database? [y/N] ").strip().lower()
                if answer not in ("y", "yes"):
                    print("Aborted. Nothing was changed.")
                    return 1
            print()
            return apply_fix(fix_sql)

        print("  Fix — either apply the tracked migrations, oldest first:")
        print("    for f in db/migrations/*.sql; do")
        print('      docker exec -i mby-trading-db psql -U mby -d mby_trading < "$f"')
        print("    done")
        print()
        print("  ...or, for drift with no migration written for it yet:")
        print("    python -m core.check_schema --fix")
        print()
        print("  Prefer the migrations when one exists — they carry the intended")
        print("  defaults and constraints, which the generated SQL has to guess at.")

    return 1


if __name__ == "__main__":
    sys.exit(main())
