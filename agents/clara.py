"""
Clara — Performance/Compliance Agent
Operating Manual §7.8

Two jobs: (1) performance attribution tied to original theses,
(2) process-compliance audit — did every trade follow the full
approval chain? Process violations are flagged regardless of
profitability. NEVER auto-modifies another agent's rubric/parameters —
calibration recommendations go to human review only.

Also compiles the Daily Closing Report (expanded responsibility,
Operating Manual §7.8 "Expanded responsibility").
"""

# from core.clients import claude_client


def audit_process_chain(session, today):
    """
    Deterministic check: does every trade today trace to a full valid
    chain (Vera -> Solomon -> Nora -> Marcus -> Ada -> Otis)?
    No LLM call needed — this is a lookup against agent_runs/proposals/orders.
    """
    raise NotImplementedError


def compile_daily_report(session, today, all_agent_outputs):
    """
    Compiles each agent's narrative/structured output into the Daily
    Closing Report. Mostly deterministic formatting; one light LLM
    pass for the executive summary.
    """
    raise NotImplementedError


def run(session, today):
    """
    Returns the Clara output contract (daily):
    { "date", "daily_pnl", "attribution": [...], "process_check",
      "violations": [...], "narrative" }
    Also writes a row to daily_reports.
    """
    raise NotImplementedError
