"""
D6 — the append-only ledgers, guarded from the application side.

The real enforcement is four database triggers (migration 006), and a
trigger cannot be exercised without a live Postgres. These tests guard
the other half of the problem: that no code in this project ever tries
to update or delete a ledger row in the first place.

That matters because the trigger turns such an attempt into a runtime
exception mid-cycle. Catching it here means the mistake surfaces in the
suite instead of at the close, on a day when something was already
going wrong.

WHAT THESE TESTS CANNOT SEE, stated plainly: they read source text, so
they catch the shapes a mistake actually takes in this codebase — a
`.delete()` on a ledger query, an upsert that becomes an update. They
would not catch raw SQL assembled at runtime, or a mutation routed
through an alias this scan doesn't recognise. The triggers are what
make the guarantee; this is what makes violating it a fast failure.
"""

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The tables whose comments claim append-only AND whose code must honour it.
APPEND_ONLY_MODELS = ["Transaction", "TradingControl"]

# Deliberately NOT in that list:
#   PositionPnlHistory — Otis upserts today's row so a same-day rerun
#                        refreshes rather than fails. Its schema comment
#                        used to claim append-only; the comment was the
#                        thing that was wrong, and it has been corrected.
#   AgentRun           — upserted by design: running -> completed/failed.


def source_files():
    """Every project .py file except the tests themselves."""
    for path in PROJECT_ROOT.rglob("*.py"):
        parts = path.parts
        if "tests" in parts or ".venv" in parts or "__pycache__" in parts:
            continue
        yield path


@pytest.mark.parametrize("model", APPEND_ONLY_MODELS)
def test_no_code_path_deletes_from_an_append_only_ledger(model):
    """
    `session.query(Transaction)...delete()` would raise at the database
    the moment it ran. Catch it here instead.
    """
    offenders = []
    for path in source_files():
        text = path.read_text()
        for match in re.finditer(rf"query\(\s*{model}\s*\)", text):
            # A chained .delete() sits within a statement or two of the query.
            window = text[match.start(): match.start() + 400]
            if ".delete(" in window:
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{line}")

    assert not offenders, (
        f"{model} is append-only — these would raise at the database: {offenders}"
    )


@pytest.mark.parametrize("model", APPEND_ONLY_MODELS)
def test_no_code_path_upserts_an_append_only_ledger(model):
    """
    `pg_insert(Transaction).on_conflict_do_update(...)` is an UPDATE
    wearing an INSERT's clothes, and the trigger treats it as one.
    A correction is a new row with corrects_txn_id set.
    """
    offenders = []
    for path in source_files():
        text = path.read_text()
        for match in re.finditer(rf"pg_insert\(\s*{model}\s*\)", text):
            window = text[match.start(): match.start() + 600]
            if "on_conflict_do_update" in window:
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{line}")

    assert not offenders, (
        f"{model} is append-only — an upsert is an update: {offenders}"
    )


def test_the_triggers_are_in_the_schema():
    """
    schema.sql is what a fresh install runs. If the triggers only ever
    lived in migration 006, every new database would be built without
    them and the guarantee would quietly apply to this machine alone.
    """
    schema = (PROJECT_ROOT / "db" / "schema.sql").read_text()

    assert "CREATE OR REPLACE FUNCTION refuse_mutation()" in schema
    for trigger in (
        "transactions_append_only",
        "transactions_no_truncate",
        "trading_control_append_only",
        "trading_control_no_truncate",
    ):
        assert f"CREATE TRIGGER {trigger}" in schema, f"{trigger} missing from schema.sql"


def test_truncate_is_covered_separately():
    """
    A row-level BEFORE UPDATE OR DELETE trigger does not fire on
    TRUNCATE. Without a statement-level trigger the ledger could be
    emptied straight past the protection — which is exactly the kind of
    gap that looks safe until someone tries it.
    """
    schema = (PROJECT_ROOT / "db" / "schema.sql").read_text()
    for table in ("transactions", "trading_control"):
        assert re.search(
            rf"CREATE TRIGGER {table}_no_truncate\s+BEFORE TRUNCATE ON {table}\s+FOR EACH STATEMENT",
            schema,
        ), f"{table} has no TRUNCATE guard"


def test_the_escape_hatch_is_documented():
    """
    An undocumented lock is one somebody eventually drops permanently
    because they needed it open once. Both the migration and the schema
    say how to open it deliberately, and how to close it again.
    """
    migration = (PROJECT_ROOT / "db" / "migrations" / "006_append_only_ledgers.sql").read_text()
    assert "DISABLE TRIGGER transactions_append_only" in migration
    assert "ENABLE TRIGGER transactions_append_only" in migration


def test_position_pnl_history_is_not_claimed_to_be_append_only():
    """
    It upserts, by design. The old comment said it never gets
    overwritten, which was untrue and would have made a trigger there
    look reasonable — breaking every same-day rerun.
    """
    schema = (PROJECT_ROOT / "db" / "schema.sql").read_text()
    start = schema.index("CREATE TABLE IF NOT EXISTS position_pnl_history")
    preamble = schema[max(0, start - 700): start]

    assert "never gets overwritten" not in preamble
    assert "UPSERTS" in preamble or "upserts" in preamble
    assert "CREATE TRIGGER position_pnl_history" not in schema


# ===============================================================
# SQL validity
# ===============================================================
def test_every_sql_file_parses_as_postgresql():
    """
    Parses schema.sql and every migration with libpg_query — the actual
    PostgreSQL grammar, not an approximation.

    Worth having because a syntax error in a migration is only found by
    running it, and running it means a half-applied change on a live
    database. Especially true of migration 006, whose plpgsql function
    body is the most syntactically involved thing in the project.

    Skipped when pglast isn't installed: it is a development
    convenience, not a runtime dependency, and the suite should not
    require it. `pip install pglast` to enable.
    """
    pglast = pytest.importorskip(
        "pglast", reason="pip install pglast to syntax-check the SQL"
    )

    sql_files = sorted((PROJECT_ROOT / "db").rglob("*.sql"))
    assert sql_files, "no SQL files found — has db/ moved?"

    failures = []
    for path in sql_files:
        try:
            pglast.parse_sql(path.read_text())
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{path.relative_to(PROJECT_ROOT)}: {exc}")

    assert not failures, "SQL that will not parse:\n  " + "\n  ".join(failures)
