"""
NYSE trading-calendar helper. Used by the orchestrator to skip
weekends/holidays automatically — never hardcode day-of-week logic,
the calendar library already knows about market holidays.
"""

from datetime import date, datetime, timezone
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
