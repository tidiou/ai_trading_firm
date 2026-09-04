"""
The run ledger — a row in agent_runs for every agent invocation.

WHAT THIS IS FOR. Without it there is no record that an agent ran, when,
for how long, or why it stopped. That matters in three specific ways:

  - A cycle that dies halfway leaves no trace of how far it got. You
    find out by noticing that today's report is missing.
  - Clara's process-compliance audit is specified against this table.
    It has been auditing an approval chain whose spine was never
    written.
  - "Did Vera already run today?" currently has to be inferred from the
    artefacts she leaves behind (see core/dev_helpers.py). With a run
    ledger it is a lookup.

Every call is recorded, success or failure. A failed run is the most
valuable row in the table, so failures are written before the exception
is re-raised.

IDENTITY IS (run_date, agent_name), AND NORA RUNS TWICE. Her proposal
review and her daily portfolio monitoring are separate jobs on separate
schedules, so they log under separate names — "nora_review" and
"nora_monitor". Logging both as "nora" would silently overwrite the
first with the second, which is exactly the kind of quiet data loss an
audit table exists to prevent.
"""

import json
import logging
import traceback
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.db import session_scope
from core.models import AgentRun

EASTERN = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)

# raw_output is a debugging aid, not a second copy of the database.
# Vera's full output can run to tens of kilobytes and there is no value
# in carrying all of it here, so oversized payloads are truncated with a
# marker rather than silently dropped or allowed to bloat every row.
MAX_OUTPUT_CHARS = 64_000


def _serialise(output: Any) -> Optional[dict]:
    """
    JSON-safe view of an agent's return value.

    `default=str` handles the dates and Decimals that appear throughout
    these outputs. Serialisation failing must never take down the agent
    whose success we are recording, so anything unexpected degrades to a
    note rather than raising.
    """
    if output is None:
        return None
    try:
        text = json.dumps(output, default=str)
        if len(text) > MAX_OUTPUT_CHARS:
            return {
                "truncated": True,
                "original_chars": len(text),
                "preview": text[:MAX_OUTPUT_CHARS],
            }
        return json.loads(text)
    except Exception as exc:  # noqa: BLE001
        return {"serialisation_failed": str(exc), "repr": repr(output)[:2000]}


def log_agent_run(run_date, agent_name: str, phase: int, status: str,
                  started_at: Optional[datetime] = None,
                  completed_at: Optional[datetime] = None,
                  output: Any = None,
                  error: Optional[str] = None) -> None:
    """
    Upsert this agent's row for the day.

    Opens its own session deliberately: the point of a failure row is
    that it survives, and sharing a session with the work being recorded
    would mean a rollback took the record of the failure with it.

    Best-effort by design. If the ledger write itself fails we log and
    carry on — an audit trail that can abort a trading cycle is worse
    than one with a gap in it.
    """
    try:
        with session_scope() as session:
            stmt = pg_insert(AgentRun).values(
                run_date=run_date,
                agent_name=agent_name,
                phase=phase,
                status=status,
                started_at=started_at,
                completed_at=completed_at,
                raw_output=_serialise(output),
                error_message=error,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["run_date", "agent_name"],
                set_={
                    "phase": stmt.excluded.phase,
                    "status": stmt.excluded.status,
                    "started_at": stmt.excluded.started_at,
                    "completed_at": stmt.excluded.completed_at,
                    "raw_output": stmt.excluded.raw_output,
                    "error_message": stmt.excluded.error_message,
                },
            )
            session.execute(stmt)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not write agent_runs row for %s: %s", agent_name, exc)


class _RunRecorder:
    """Handed to the caller by track_agent_run so it can attach the
    agent's return value to the ledger row."""

    def __init__(self):
        self.output: Any = None


@contextmanager
def track_agent_run(run_date, agent_name: str, phase: int):
    """
    Record one agent invocation.

        with track_agent_run(today, "atlas", 1) as run:
            result = atlas.run(today)
            run.output = result

    Writes `running` on entry so an interrupted cycle leaves evidence of
    where it stopped rather than nothing at all, then `completed` or
    `failed` on exit. The exception is always re-raised — this observes,
    it never swallows.
    """
    started = datetime.now(EASTERN)
    log_agent_run(run_date, agent_name, phase, "running", started_at=started)

    recorder = _RunRecorder()
    try:
        yield recorder
    except Exception as exc:
        log_agent_run(
            run_date, agent_name, phase, "failed",
            started_at=started,
            completed_at=datetime.now(EASTERN),
            error=f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}",
        )
        raise
    else:
        log_agent_run(
            run_date, agent_name, phase, "completed",
            started_at=started,
            completed_at=datetime.now(EASTERN),
            output=recorder.output,
        )


def get_run_status(run_date, agent_name: str) -> Optional[str]:
    """Status of an agent's run for a given date, or None if it has not
    run. The lookup that replaces inferring from artefacts."""
    with session_scope() as session:
        row = (
            session.query(AgentRun)
            .filter(AgentRun.run_date == run_date, AgentRun.agent_name == agent_name)
            .first()
        )
        return row.status if row else None
