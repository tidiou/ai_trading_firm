#!/bin/bash
# =====================================================================
# run_desk.sh — the one thing the scheduler calls.
#
#     ./scripts/run_desk.sh            # same as `auto`
#     ./scripts/run_desk.sh premarket  # force one group, ignoring the clock
#     ./scripts/run_desk.sh status
#
# It exists because launchd and cron give a job almost no environment:
# no PATH beyond the bare minimum, no shell profile, no virtualenv, and
# a working directory that is not yours. A python command that works
# perfectly in your terminal fails silently under a scheduler for all
# four reasons, and the failure looks exactly like "the desk didn't
# run" — which is the failure mode hardest to notice and most expensive
# to have.
#
# So everything the run needs is established here, explicitly, and the
# output goes somewhere you can read afterwards.
# =====================================================================

set -uo pipefail

# Resolve the project root from this script's own location rather than
# assuming a working directory. Scheduler-safe, and survives the folder
# being moved or renamed.
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT" || exit 1

LOG_DIR="$PROJECT_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/desk-$(date +%Y-%m).log"

# --- the interpreter -------------------------------------------------
# A virtualenv if there is one, otherwise whatever python3 is on PATH.
if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    PYTHON="$PROJECT_ROOT/.venv/bin/python"
elif [ -x "$PROJECT_ROOT/venv/bin/python" ]; then
    PYTHON="$PROJECT_ROOT/venv/bin/python"
else
    PYTHON="$(command -v python3 || true)"
fi

if [ -z "$PYTHON" ]; then
    echo "$(date -u +%FT%TZ) FATAL no python3 found — desk did not run" >> "$LOG_FILE"
    exit 127
fi

# --- the environment -------------------------------------------------
# .env is not read by the shell automatically and python-dotenv only
# helps once the process has started with the right cwd. Sourcing it
# here means the same variables are present whether the run came from
# your terminal or from launchd at 09:45.
if [ -f "$PROJECT_ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$PROJECT_ROOT/.env"
    set +a
fi

# --- the database ----------------------------------------------------
# Postgres runs in Docker. A machine that has just woken up may have
# Docker still starting, and connecting to a database that is not up
# yet fails in a way indistinguishable from a broken cycle. Wait a
# bounded amount for it rather than failing the day over ten seconds.
if command -v docker >/dev/null 2>&1; then
    for _ in $(seq 1 30); do
        if docker inspect -f '{{.State.Running}}' mby-trading-db 2>/dev/null | grep -q true; then
            break
        fi
        docker start mby-trading-db >/dev/null 2>&1 || true
        sleep 2
    done
fi

COMMAND="${1:-auto}"

{
    echo "----------------------------------------------------------------"
    echo "$(date -u +%FT%TZ) [$(date +%H:%M\ %Z)] run_desk.sh $COMMAND"
} >> "$LOG_FILE"

"$PYTHON" -m orchestrator "$COMMAND" >> "$LOG_FILE" 2>&1
STATUS=$?

echo "$(date -u +%FT%TZ) exit $STATUS" >> "$LOG_FILE"
exit $STATUS
