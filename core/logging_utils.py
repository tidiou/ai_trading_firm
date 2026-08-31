"""
Wraps every agent invocation with a row in agent_runs — this IS the
audit trail Clara's process-compliance checks depend on. Every call
gets logged, success or failure. Never skip this.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


def log_agent_run(session, run_date, agent_name, phase, status, started_at,
                   output=None, error=None):
    # TODO: INSERT/UPDATE into agent_runs table
    raise NotImplementedError
