"""
NYSE trading-calendar helper. Used by the orchestrator to skip
weekends/holidays automatically — never hardcode day-of-week logic,
the calendar library already knows about market holidays.
"""

from datetime import date
import pandas_market_calendars as mcal

NYSE = mcal.get_calendar("NYSE")


def is_trading_day(d: date) -> bool:
    schedule = NYSE.schedule(start_date=d, end_date=d)
    return not schedule.empty
