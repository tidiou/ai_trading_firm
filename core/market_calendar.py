"""
NYSE trading-calendar helper. Used by the orchestrator to skip
weekends/holidays automatically — never hardcode day-of-week logic,
the calendar library already knows about market holidays.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

NYSE = mcal.get_calendar("NYSE")


def is_trading_day(d: date) -> bool:
    schedule = NYSE.schedule(start_date=d, end_date=d)
    return not schedule.empty


def trading_days_between(start: date, end: date) -> int:
    """
    NYSE sessions strictly after `start` and up to and including `end`.

    Used to age the ledger: "Otis last closed the books on Friday, today
    is Monday" is one session, not three days. Counting calendar days
    would flag every Monday as stale and every long weekend as a crisis.

    Returns 0 when end <= start.
    """
    if end <= start:
        return 0
    schedule = NYSE.schedule(start_date=start, end_date=end)
    sessions = [d.date() for d in schedule.index]
    return len([d for d in sessions if d > start])


EASTERN = ZoneInfo("America/New_York")


def market_is_open_now(now: datetime | None = None) -> bool:
    """
    Is the NYSE session live right now?

    Exists for Otis's end-of-day sweep, which cancels orders that did
    not fill. Run at the close that is correct; run at 11am it would
    cancel orders that were still perfectly capable of filling. A
    developer running `python -m agents.otis` to look at the books
    should not thereby kill the desk's live orders — so the sweep asks
    this first and stands down while the session is open.

    Honours real session hours including early closes, since the
    calendar carries them.
    """
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(EASTERN).date()

    schedule = NYSE.schedule(start_date=today, end_date=today)
    if schedule.empty:
        return False

    opens = schedule.iloc[0]["market_open"].to_pydatetime()
    closes = schedule.iloc[0]["market_close"].to_pydatetime()
    return opens <= now <= closes


def now_et() -> datetime:
    """The desk's wall clock. Every schedule in this system is anchored
    to Eastern rather than to the host's timezone, so that a laptop in
    Dakar and a container in Frankfurt agree on what time the desk
    thinks it is."""
    return datetime.now(EASTERN)


def session_bounds(d: date) -> tuple[datetime, datetime] | None:
    """(open, close) for `d` as timezone-aware datetimes, or None if the
    NYSE is not open that day. Honours early closes."""
    schedule = NYSE.schedule(start_date=d, end_date=d)
    if schedule.empty:
        return None
    row = schedule.iloc[0]
    return (row["market_open"].to_pydatetime(), row["market_close"].to_pydatetime())


# =================================================================
# D10 — "has this order had its chance to fill?"
#
# THE DEFECT THIS ANSWERS. `market_is_open_now()` is the wrong question
# for the end-of-day sweep, and the difference only shows up in the one
# case that matters.
#
# The daily cycle was designed to run at 16:30 ET, after the close. Ada
# submits a DAY limit order, which the broker queues for the NEXT
# session's open. Otis then runs in Phase 5 — minutes later, with the
# market shut — so `market_is_open_now()` is False, the sweep proceeds,
# and it cancels an order that has not yet seen a single second of
# trading. The row is marked `expired_unfilled`, which reads as an
# ordinary non-fill: the desk appears to have tried and missed, when in
# fact it cancelled itself before the bell.
#
# It never fired only because no cycle had yet placed an order.
#
# The question the sweep actually wants to ask is not "is the market
# shut right now" but "has the session this order was queued for
# finished". Those coincide for an order placed during a session and
# diverge completely for one placed outside it.
# =================================================================
def order_session_close(submitted_at: datetime) -> Optional[datetime]:
    """
    The close of the session this order was live for.

    An order is live in the first session whose CLOSE falls at or after
    the moment it was submitted:

      - submitted 09:45 Tue (mid-session)  -> Tue 16:00
      - submitted 08:00 Tue (pre-market)   -> Tue 16:00, the open it is
                                              queued for is that morning's
      - submitted 16:30 Fri (after close)  -> Mon 16:00, the next session
      - submitted 16:30 Fri of a long weekend -> Tue 16:00

    That single rule covers all four without special-casing weekends,
    holidays or early closes, because the calendar already knows about
    them. Returns None only if no session can be found within a
    fortnight, which would mean the calendar itself is wrong.
    """
    if submitted_at.tzinfo is None:
        submitted_at = submitted_at.replace(tzinfo=timezone.utc)

    start = submitted_at.astimezone(EASTERN).date()
    schedule = NYSE.schedule(start_date=start, end_date=start + timedelta(days=14))
    for _, row in schedule.iterrows():
        close = row["market_close"].to_pydatetime()
        if close >= submitted_at:
            return close
    return None


def order_session_is_over(submitted_at: datetime, now: datetime | None = None) -> bool:
    """
    Whether the session this order was queued for has finished, i.e.
    whether it has had its chance to fill.

    This is the sweep's guard. False means "leave it alone, it is either
    trading now or waiting for a bell that has not rung yet".
    """
    now = now or datetime.now(timezone.utc)
    close = order_session_close(submitted_at)
    if close is None:
        # No session found ahead of it — refuse to sweep rather than
        # cancel on a calendar we evidently do not understand.
        return False
    return now > close
