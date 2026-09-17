"""
The schema gate.

WHY THIS IS TESTED AT THIS DEPTH. The desk has lost four sessions to
one mistake — core/models.py ahead of the database — and this module
is the control that turns that from a lost window into a one-line log
entry. A control that is subtly wrong is worse than none, because it
is trusted.

Three things have to hold, and each has a test class:

  1. The COMPARISON must report what is missing and nothing else. A
     database that is AHEAD of the code is normal and safe; a gate
     that refused it would block every ordinary deploy.
  2. The ATTRIBUTION must name the migration that INTRODUCES the
     object, not merely one that mentions it. Pointing at the wrong
     file sends the operator to apply something that changes nothing.
  3. The GATE'S POSITION in run_group must cost no attempt and write
     no cycle row — that is what makes applying a migration
     mid-window take effect on the next tick rather than being
     blocked by a burned retry cap.

No database: the comparison and attribution are pure, and the
orchestrator's gate is exercised with the check stubbed.
"""

import pytest

from core.schema_check import (
    SchemaGap,
    SchemaOutOfDate,
    attribute_migration,
    describe_gaps,
    find_gaps,
)


# =================================================================
# 1. The comparison
# =================================================================
class TestFindGaps:

    def test_a_matching_schema_has_no_gaps(self):
        s = {"macro_briefs": {"id", "brief_date", "narrative"}}
        assert find_gaps(s, s) == []

    def test_a_missing_column_is_a_gap(self):
        # The 17 Sept failure, as arithmetic.
        expected = {"macro_briefs": {"id", "narrative", "read_across"}}
        actual = {"macro_briefs": {"id", "narrative"}}
        assert find_gaps(expected, actual) == [
            SchemaGap(table="macro_briefs", column="read_across")]

    def test_a_missing_table_is_reported_once_not_per_column(self):
        # The 011 failure. thesis_assumptions has a dozen columns;
        # twelve lines about one absent table buries the problem.
        expected = {"thesis_assumptions": {"id", "claim", "status",
                                           "is_load_bearing", "metric"}}
        gaps = find_gaps(expected, {})
        assert gaps == [SchemaGap(table="thesis_assumptions", column=None)]
        assert gaps[0].is_table is True

    def test_A_DATABASE_THAT_IS_AHEAD_IS_NOT_A_GAP(self):
        # Load-bearing. Applying a migration before pulling the code
        # that uses it is a normal, safe order of operations. A gate
        # that refused it would be worse than no gate.
        expected = {"macro_briefs": {"id", "narrative"}}
        actual = {"macro_briefs": {"id", "narrative", "read_across"},
                  "some_future_table": {"id"}}
        assert find_gaps(expected, actual) == []

    def test_several_gaps_are_all_reported(self):
        # The real state on 17 Sept: 011 and 013 both unapplied.
        expected = {"macro_briefs": {"id", "read_across"},
                    "thesis_assumptions": {"id", "claim"},
                    "company_facts": {"id"}}
        actual = {"macro_briefs": {"id"}, "company_facts": {"id"}}
        assert find_gaps(expected, actual) == [
            SchemaGap("macro_briefs", "read_across"),
            SchemaGap("thesis_assumptions", None)]

    def test_output_is_deterministically_ordered(self):
        # A gate whose message reorders between runs is a gate nobody
        # can diff.
        expected = {"z_table": {"a", "b"}, "a_table": {"y", "x"}}
        gaps = find_gaps(expected, {"z_table": set(), "a_table": set()})
        assert [g.label() for g in gaps] == [
            "a_table.x", "a_table.y", "z_table.a", "z_table.b"]

    def test_the_label_distinguishes_a_table_from_a_column(self):
        assert SchemaGap("t").label() == "t (table)"
        assert SchemaGap("t", "c").label() == "t.c"


# =================================================================
# 2. The attribution
# =================================================================
SOURCES = {
    "010_prediction_calibration.sql":
        "ALTER TABLE macro_briefs ADD COLUMN IF NOT EXISTS confidence_pct NUMERIC(5,2);",
    "011_thesis_assumptions.sql":
        "CREATE TABLE IF NOT EXISTS thesis_assumptions (\n"
        "  id BIGSERIAL PRIMARY KEY, claim TEXT NOT NULL,\n"
        "  is_load_bearing BOOLEAN NOT NULL DEFAULT TRUE);",
    "012_company_facts.sql":
        "CREATE TABLE IF NOT EXISTS company_facts (\n"
        "  id BIGSERIAL PRIMARY KEY, period_start DATE);",
    "013_macro_brief_read_across.sql":
        "-- see core/theme.py\n"
        "ALTER TABLE macro_briefs ADD COLUMN IF NOT EXISTS read_across JSONB;",
    "014_unrelated.sql":
        "-- a later migration that merely MENTIONS read_across and\n"
        "-- thesis_assumptions in a comment on macro_briefs\n"
        "COMMENT ON COLUMN macro_briefs.read_across IS 'see 013';",
}


class TestAttributeMigration:

    def test_an_added_column_is_attributed_to_its_alter(self):
        assert attribute_migration(
            SchemaGap("macro_briefs", "read_across"), SOURCES
        ) == "013_macro_brief_read_across.sql"

    def test_a_missing_table_is_attributed_to_its_create(self):
        assert attribute_migration(
            SchemaGap("thesis_assumptions"), SOURCES
        ) == "011_thesis_assumptions.sql"

    def test_THE_EARLIEST_INTRODUCING_FILE_WINS(self):
        # 014 mentions read_across in a COMMENT. Attributing to 014
        # would have the operator apply a file that adds no column and
        # conclude the gate is broken.
        assert attribute_migration(
            SchemaGap("macro_briefs", "read_across"), SOURCES
        ) != "014_unrelated.sql"

    def test_a_column_from_an_original_create_is_still_found(self):
        # period_start was never ADD COLUMNed — it was in 012's
        # CREATE TABLE. The fallback has to catch that.
        assert attribute_migration(
            SchemaGap("company_facts", "period_start"), SOURCES
        ) == "012_company_facts.sql"

    def test_a_column_requires_its_own_table_to_be_mentioned(self):
        # `claim` appears in 011, but on thesis_assumptions. Asking
        # about macro_briefs.claim must not be attributed to 011 — a
        # bare column-name match would do exactly that.
        assert attribute_migration(SchemaGap("macro_briefs", "claim"),
                                   SOURCES) is None

    def test_nothing_matching_returns_none_rather_than_guessing(self):
        # Informative in itself: the ORM has an object no migration
        # creates, so the fix is a MISSING migration, not an
        # unapplied one.
        assert attribute_migration(SchemaGap("invented_table"), SOURCES) is None

    def test_no_migration_files_at_all_is_handled(self):
        assert attribute_migration(SchemaGap("t", "c"), {}) is None

    def test_a_substring_column_is_not_a_match(self):
        # "confidence" must not match "confidence_pct".
        assert attribute_migration(
            SchemaGap("macro_briefs", "confidence"), SOURCES) is None


# =================================================================
# 3. The message — the thing read at 07:11 on a Tuesday
# =================================================================
class TestDescribeGaps:

    def gaps(self):
        return [SchemaGap("macro_briefs", "read_across",
                          "013_macro_brief_read_across.sql"),
                SchemaGap("thesis_assumptions", None,
                          "011_thesis_assumptions.sql")]

    def test_it_names_every_missing_object(self):
        text = describe_gaps(self.gaps())
        assert "macro_briefs.read_across" in text
        assert "thesis_assumptions (table)" in text

    def test_it_names_the_file_for_each(self):
        text = describe_gaps(self.gaps())
        assert "013_macro_brief_read_across.sql" in text
        assert "011_thesis_assumptions.sql" in text

    def test_it_gives_runnable_commands_in_migration_order(self):
        # THE POINT OF THE WHOLE MODULE: no second question. 011 must
        # be listed before 013, because applying them out of order is
        # its own failure.
        text = describe_gaps(self.gaps())
        cmds = [l for l in text.splitlines() if l.strip().startswith("psql")]
        assert len(cmds) == 2
        assert "011_thesis_assumptions.sql" in cmds[0]
        assert "013_macro_brief_read_across.sql" in cmds[1]

    def test_it_says_nothing_was_spent(self):
        # So the operator knows a schema gap is cheap and does not
        # need to check for a half-run day or a wasted quota.
        assert "cost the tick and nothing else" in describe_gaps(self.gaps())

    def test_an_unattributed_gap_says_the_migration_is_missing(self):
        text = describe_gaps([SchemaGap("orphan_table", None, None)])
        assert "NO MIGRATION FOUND" in text
        assert "missing migration rather than an unapplied one" in text

    def test_no_gaps_reads_as_up_to_date(self):
        assert describe_gaps([]) == "Schema is up to date."

    def test_it_does_not_offer_commands_when_there_are_none(self):
        text = describe_gaps([SchemaGap("orphan_table", None, None)])
        assert "psql" not in text


# =================================================================
# 4. The gate's POSITION in run_group
# =================================================================
class TestTheGateCostsNothing:
    """What made 17 Sept expensive was not the missing column — it was
    that three attempts burned before anyone saw it, after which the
    retry cap refused the group for the rest of the day. These tests
    pin the ordering that prevents that."""

    @pytest.fixture
    def broken(self, monkeypatch):
        import orchestrator as orch

        calls = {"runs_logged": [], "attempts_read": 0,
                 "control_announced": 0, "group_ran": 0}

        def boom(*a, **k):
            raise SchemaOutOfDate(
                "SCHEMA OUT OF DATE — the desk did not run.",
                [SchemaGap("macro_briefs", "read_across",
                           "013_macro_brief_read_across.sql")])

        monkeypatch.setattr(orch, "require_schema", boom)
        monkeypatch.setattr(orch, "log_agent_run",
                            lambda *a, **k: calls["runs_logged"].append(a))
        monkeypatch.setattr(orch, "_attempts_so_far",
                            lambda *a, **k: calls.__setitem__(
                                "attempts_read", calls["attempts_read"] + 1) or 0)
        monkeypatch.setattr(orch, "_announce_control_state",
                            lambda *a, **k: calls.__setitem__(
                                "control_announced",
                                calls["control_announced"] + 1) or True)
        monkeypatch.setitem(
            orch.GROUP_RUNNERS, "premarket",
            lambda today: calls.__setitem__("group_ran",
                                            calls["group_ran"] + 1) or {})
        return orch, calls

    def test_it_refuses_rather_than_raising(self, broken):
        orch, _ = broken
        result = orch.run_group("premarket")
        assert result["action"] == "refused"
        assert result["reason"] == "schema out of date"

    def test_NO_ATTEMPT_IS_RECORDED(self, broken):
        # The property that makes a mid-window fix work: with no
        # attempt burned, the next tick runs normally instead of
        # hitting the cap.
        orch, calls = broken
        orch.run_group("premarket")
        assert calls["attempts_read"] == 0
        assert calls["runs_logged"] == [], "no cycle row may be written"

    def test_no_agent_is_run_so_nothing_is_spent(self, broken):
        orch, calls = broken
        orch.run_group("premarket")
        assert calls["group_ran"] == 0

    def test_it_gates_above_the_control_state_read(self, broken):
        # _announce_control_state reads the database too, so it must
        # not run first — on a broken schema it could be the thing
        # that raises, with a worse message.
        orch, calls = broken
        orch.run_group("premarket")
        assert calls["control_announced"] == 0

    def test_the_refusal_carries_the_gaps_and_the_fix(self, broken):
        orch, _ = broken
        result = orch.run_group("premarket")
        assert result["gaps"] == ["macro_briefs.read_across"]
        assert "SCHEMA OUT OF DATE" in result["errors"][0]

    def test_the_refusal_makes_the_process_exit_non_zero(self, broken):
        # A scheduler must be able to tell a refused day from a quiet
        # one. `errors` is what _main() checks.
        orch, _ = broken
        assert orch.run_group("premarket")["errors"]

    def test_an_unknown_group_still_raises_before_the_gate(self, broken):
        # The argument check is a programming error, not an
        # operational condition, and must not be masked by a schema
        # refusal.
        orch, _ = broken
        with pytest.raises(ValueError):
            orch.run_group("not_a_group")


class TestAGoodSchemaChangesNothing:

    def test_run_group_proceeds_normally_when_the_schema_is_fine(self, monkeypatch):
        import orchestrator as orch

        ran = {"n": 0}
        monkeypatch.setattr(orch, "require_schema", lambda *a, **k: None)
        monkeypatch.setattr(orch, "log_agent_run", lambda *a, **k: None)
        monkeypatch.setattr(orch, "_attempts_so_far", lambda *a, **k: 0)
        monkeypatch.setattr(orch, "_announce_control_state", lambda *a, **k: True)
        monkeypatch.setitem(
            orch.GROUP_RUNNERS, "premarket",
            lambda today: ran.__setitem__("n", 1) or {"phases": {}})

        result = orch.run_group("premarket")
        assert ran["n"] == 1
        assert result["attempt"] == 1
        assert result.get("action") != "refused"
