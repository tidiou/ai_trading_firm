"""
WHY a position moved — the `cause` label on Vera's monitoring output.

THE GAP. NVDA was logged `at_risk` for nine consecutive sessions in
November 2026 with a trigger alternating between `macro_conflict` and
`fundamental_drift`. The reports of what actually moved it were a sector
selloff, a policy shock after the midterms and one company-specific
governance event — and on the days labelled `fundamental_drift` no
fundamental had been checked. The desk could see a divergence from the
market and had nowhere to write "I could not tell".

Sections 1-2 are pure (no pydantic, no database). Sections 3-5 read the
contract, the schema and the prompt, and need the project's packages.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from core.cause import (
    CAUSES, COMPANY_LEVEL_TRIGGERS, DEFAULT_CAUSE, trigger_cause_mismatch,
)


# ===============================================================
# 1. the vocabulary
# ===============================================================
class TestTheVocabulary:

    def test_unknown_is_a_member_and_the_default(self):
        """The whole point: an honest "I could not tell" needs a value."""
        assert "unknown" in CAUSES and DEFAULT_CAUSE == "unknown"

    def test_there_are_exactly_four(self):
        assert set(CAUSES) == {"market_wide", "sector_wide", "company_specific", "unknown"}

    def test_the_company_level_triggers_are_the_ones_that_claim_something_about_the_company(self):
        assert COMPANY_LEVEL_TRIGGERS == {"fundamental_drift", "earnings", "news"}


# ===============================================================
# 2. a trigger and a cause that contradict each other
# ===============================================================
class TestTriggerCauseConsistency:

    @pytest.mark.parametrize("trigger", ["fundamental_drift", "earnings", "news"])
    @pytest.mark.parametrize("cause", ["market_wide", "sector_wide", "unknown"])
    def test_a_company_level_trigger_needs_a_company_level_cause(self, trigger, cause):
        """THE NOVEMBER NVDA CASE: `fundamental_drift` logged four times
        for a stock whose filings had not been touched."""
        assert trigger_cause_mismatch(trigger, cause) is not None

    @pytest.mark.parametrize("trigger", ["fundamental_drift", "earnings", "news"])
    def test_a_company_trigger_with_a_company_cause_is_consistent(self, trigger):
        assert trigger_cause_mismatch(trigger, "company_specific") is None

    @pytest.mark.parametrize("cause", ["market_wide", "sector_wide", "unknown"])
    def test_a_macro_divergence_is_consistent_with_a_non_company_cause(self, cause):
        assert trigger_cause_mismatch("macro_conflict", cause) is None

    def test_a_macro_divergence_claiming_a_company_cause_is_flagged(self):
        assert trigger_cause_mismatch("macro_conflict", "company_specific") is not None

    @pytest.mark.parametrize("cause", list(CAUSES))
    def test_no_trigger_is_always_consistent(self, cause):
        assert trigger_cause_mismatch("none", cause) is None
        assert trigger_cause_mismatch("profit_target_sustained", cause) is None

    def test_the_message_names_both_halves(self):
        msg = trigger_cause_mismatch("fundamental_drift", "unknown")
        assert "fundamental_drift" in msg and "unknown" in msg


# ===============================================================
# 3. the contract
# ===============================================================
class TestTheContract:

    def _base(self, **extra):
        return {"ticker": "NVDA", "status": "at_risk", "trigger": "macro_conflict",
                "reasoning": "r", "conviction_score": 2, **extra}

    def test_an_absent_cause_defaults_to_unknown(self):
        """An older prompt, or a model that forgets, must still validate:
        failing a whole monitoring pass over a label would cost more than
        the label is worth."""
        from agents.vera import PositionMonitoring
        assert PositionMonitoring.model_validate(self._base()).cause == "unknown"

    @pytest.mark.parametrize("cause", list(CAUSES))
    def test_every_value_in_the_vocabulary_is_accepted(self, cause):
        from agents.vera import PositionMonitoring
        assert PositionMonitoring.model_validate(self._base(cause=cause)).cause == cause

    def test_a_value_outside_the_vocabulary_is_rejected(self):
        from pydantic import ValidationError
        from agents.vera import PositionMonitoring
        with pytest.raises(ValidationError):
            PositionMonitoring.model_validate(self._base(cause="the_fed"))

    def test_the_contract_and_the_vocabulary_cannot_drift_apart(self):
        """The Literal is spelled out in the model (type checkers read it);
        this is what stops it disagreeing with core/cause.py."""
        import typing
        from agents.vera import PositionMonitoring
        annotation = PositionMonitoring.model_fields["cause"].annotation
        assert set(typing.get_args(annotation)) == set(CAUSES)

    def test_the_cause_reaches_the_output(self):
        from agents.vera import PositionMonitoring
        dumped = PositionMonitoring.model_validate(self._base(cause="sector_wide")).model_dump()
        assert dumped["cause"] == "sector_wide"


# ===============================================================
# 4. the schema, the migration and the model say the same thing
# ===============================================================
class TestSchemaMigrationAndModelAgree:

    def _values(self, sql: str) -> set:
        m = re.search(r"cause\s+IN\s*\(([^)]*)\)", sql, re.IGNORECASE | re.DOTALL)
        assert m, "no `cause IN (...)` constraint found"
        return set(re.findall(r"'([a-z_]+)'", m.group(1)))

    def test_the_migration_allows_exactly_the_vocabulary(self):
        sql = (ROOT / "db" / "migrations" / "015_monitoring_cause.sql").read_text()
        assert self._values(sql) == set(CAUSES)

    def test_the_schema_allows_exactly_the_vocabulary(self):
        sql = (ROOT / "db" / "schema.sql").read_text()
        table = sql[sql.index("CREATE TABLE IF NOT EXISTS position_monitoring_log"):]
        table = table[:table.index(");")]
        assert self._values(table) == set(CAUSES)

    def test_the_constraint_has_the_same_name_either_way(self):
        """A database built from schema.sql and one that went through the
        migration must end up identical, name included — otherwise the
        next migration that has to drop it has to guess."""
        name = "position_monitoring_log_cause_check"
        assert name in (ROOT / "db" / "migrations" / "015_monitoring_cause.sql").read_text()
        assert name in (ROOT / "db" / "schema.sql").read_text()

    def test_the_column_is_nullable_so_history_is_not_invented(self):
        sql = (ROOT / "db" / "migrations" / "015_monitoring_cause.sql").read_text()
        assert "ADD COLUMN IF NOT EXISTS cause TEXT" in sql
        assert "NOT NULL" not in sql.split("ADD COLUMN")[1].split(";")[0]
        statements = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
        assert "UPDATE" not in statements.upper(), \
            "a backfill would record a cause assigned with hindsight as though observed"

    def test_the_model_has_the_column(self):
        from core.models import PositionMonitoringLog
        assert "cause" in PositionMonitoringLog.__table__.columns
        assert PositionMonitoringLog.__table__.columns["cause"].nullable


# ===============================================================
# 5. what she is told, and what is persisted
# ===============================================================
class TestThePrompt:

    def test_the_prompt_defines_every_cause(self):
        from agents.vera import MONITORING_SYSTEM_PROMPT as P
        for cause in CAUSES:
            assert cause in P

    def test_unknown_is_presented_as_the_default_and_a_complete_answer(self):
        from agents.vera import MONITORING_SYSTEM_PROMPT as P
        assert "THE DEFAULT" in P and "honest answer" in P

    def test_a_price_move_is_not_allowed_to_explain_itself(self):
        from agents.vera import MONITORING_SYSTEM_PROMPT as P
        assert "never evidence" in P

    def test_the_company_level_triggers_are_tied_to_a_company_cause(self):
        from agents.vera import MONITORING_SYSTEM_PROMPT as P
        for trig in COMPANY_LEVEL_TRIGGERS:
            assert trig in P
        assert "REQUIRE" in P

    def test_the_output_shape_in_the_prompt_includes_the_field(self):
        from agents.vera import MONITORING_SYSTEM_PROMPT as P
        assert '"cause": "market_wide | sector_wide | company_specific | unknown"' in P


class TestPersistence:

    def test_the_monitoring_upsert_writes_and_updates_the_cause(self):
        """Structural, in the style of the neighbouring tests: both the
        insert and the on-conflict update must carry it, or a same-day
        rerun would leave a stale cause beside a fresh status."""
        import inspect
        import agents.vera as vera
        src = inspect.getsource(vera.run)
        assert "cause=m.cause" in src
        assert '"cause": stmt.excluded.cause' in src

    def test_a_contradictory_pair_is_carried_in_her_output(self):
        import inspect
        import agents.vera as vera
        src = inspect.getsource(vera.run)
        assert "trigger_cause_mismatch" in src and '"cause_flags"' in src
