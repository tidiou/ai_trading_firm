"""
The desk's daily timetable — one definition, two readers.

The orchestrator uses this to decide what to run. The dashboard uses it
to decide what is overdue. They must agree, and the only reliable way
to make two things agree is to give them one thing to read.

This module deliberately imports NOTHING from `agents`. The dashboard
needs the timetable and must not need an Anthropic key, an Alpaca key
and an FMP key to draw a banner — importing the orchestrator would drag
all eight agents and every client in with it.


=====================================================================
WHY THE DAY IS SPLIT AT ALL (D10)
=====================================================================

The cycle used to be one process at 16:30 ET, after the close. Ada
submits DAY limit orders; submitted after the close, the broker queues
them for the NEXT session's open. So the desk sized against this
evening's numbers and executed against tomorrow's opening print, with a
night of news in between.

And Otis's end-of-day sweep ran minutes later in the same process,
cancelling those orders before the bell they were queued for and
recording them as `expired_unfilled` — the desk cancelling its own
orders and filing it as the market not reaching the limit.

So the day is split along the seams the Operating Manual already
describes, each group running when a desk would actually do it.


=====================================================================
WHY THE WINDOWS ARE WIDE
=====================================================================

Because the scheduler is dumb on purpose. `orchestrator auto` is meant
to be called every fifteen minutes and to do nothing almost every time:
it reads the Eastern clock itself rather than trusting a cron entry
written in local time, which is wrong for several weeks a year in both
directions and catastrophically wrong on the day the US and Europe
change clocks a fortnight apart.

Wide windows also mean a tick missed because the machine was asleep is
picked up by the next one. A run that starts at 09:47 instead of 09:45
is fine. A run that never starts is not.
"""

from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class PhaseGroup:
    key: str
    label: str
    opens: time       # earliest ET time `auto` will start this group
    closes: time      # latest — past this, starting it would do harm
    due_by: time      # by when it should have run, for "is it overdue"
    phases: str
    agents: tuple[str, ...]

    def is_in_window(self, t: time) -> bool:
        return self.opens <= t <= self.closes


# Each window closes before it would do harm rather than at a tidy
# round number:
#
#   premarket closes at 09:25 — five minutes before the bell, so the
#   morning meeting is never held on the wrong side of the open.
#
#   execution closes at 15:30 — thirty minutes before the close, so an
#   order always has half an hour of session left to fill in. Later
#   than that and we are back to placing orders nobody can fill, which
#   is the defect this whole split exists to remove.
#
#   close opens at 16:15, fifteen minutes after the bell, to let the
#   broker settle the last fills before Otis reconciles against them.
PHASE_GROUPS = {
    "premarket": PhaseGroup(
        "premarket", "pre-market review & morning meeting",
        opens=time(7, 0), closes=time(9, 25), due_by=time(9, 25),
        phases="1-2", agents=("atlas", "vera", "solomon")),
    "execution": PhaseGroup(
        "execution", "risk, sizing & execution",
        opens=time(9, 45), closes=time(15, 30), due_by=time(10, 30),
        phases="3-4", agents=("nora_review", "marcus", "ada")),
    "close": PhaseGroup(
        "close", "books, risk monitoring & report",
        opens=time(16, 15), closes=time(22, 0), due_by=time(17, 30),
        phases="5", agents=("otis", "nora_monitor", "clara")),
}

GROUP_ORDER = ["premarket", "execution", "close"]

# A group that fails is retried on the next tick — a dropped connection
# or a rate-limited API is exactly the case worth retrying. Three is the
# cap: past that it is not transient, and retrying every fifteen minutes
# until the window shuts spends real money on model calls to fail the
# same way twenty times.
MAX_ATTEMPTS_PER_GROUP = 3

# agent_runs is keyed on (run_date, agent_name), so the groups get their
# own namespaced rows alongside the agents. One mechanism answers three
# questions: has this group already run (idempotency), did it fail
# (retry), and did the desk run at all today (the dashboard's banner).
CYCLE_ROW_PREFIX = "cycle:"


def cycle_row_name(group: str) -> str:
    return f"{CYCLE_ROW_PREFIX}{group}"


def is_cycle_row(agent_name: str) -> bool:
    """The dashboard counts agents, not groups. Without this the cycle
    rows would inflate "6 of 9 completed" into "6 of 12"."""
    return agent_name.startswith(CYCLE_ROW_PREFIX)


def now_et() -> datetime:
    """The desk's wall clock. Every schedule here is anchored to Eastern
    rather than to the host's timezone, so a laptop in Dakar and a
    container in Frankfurt agree on what time the desk thinks it is."""
    return datetime.now(EASTERN)


def due_group(now: Optional[datetime] = None) -> Optional[str]:
    """Which group's window contains this moment, if any. The windows do
    not overlap, so at most one."""
    now = now or now_et()
    t = now.astimezone(EASTERN).time()
    for key in GROUP_ORDER:
        if PHASE_GROUPS[key].is_in_window(t):
            return key
    return None
