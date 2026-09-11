"""
The benchmark — what the desk is measured against (D4).

=====================================================================
ABSOLUTE P&L IS NOT A PERFORMANCE MEASURE
=====================================================================

Until this module existed, the desk measured itself against nothing.
Clara computed a contribution per name and a total P&L, and there was
no index anywhere in the codebase. A desk that returns 4% in a month
the index returned 6% has lost money in the only sense that matters,
and the system would have reported a good month, in confident detail,
with a clean audit trail behind it.

It also undermined Clara's own mandate. Her job is to calibrate
whether Vera's conviction scores predict outcomes — but with an
unadjusted return as the outcome variable she was mostly measuring
market beta and crediting it to Vera's stock-picking. A conviction-5
thesis on a name that rose because everything rose would have scored
as a hit, and the calibration would have taught the desk to trust a
signal that never fired.

=====================================================================
THE DECOMPOSITION, AND WHY IT IS NOT JUST "DESK MINUS SPY"
=====================================================================

Active return alone would be misleading on this book, in a specific
and predictable direction. The desk currently holds a few hundred
dollars of stock against roughly a hundred thousand of cash. Compared
to a fully-invested index it will underperform in any rising market —
and that says nothing whatever about whether its stock-picking is any
good. "We held cash" and "our picks were bad" are different findings
with different remedies, and a single number cannot tell them apart.

So the gap is split, using the average invested weight w:

    r_desk                      the desk's return
    r_bench                     the index's return
    w                           mean(invested / NAV) over the period

    selection  = r_desk - w * r_bench
    cash_drag  = (1 - w) * r_bench
    active     = r_desk - r_bench  ==  selection - cash_drag

The identity is exact, and the tests assert it. SELECTION is the
number that answers "were the names any good, given how much we
committed". CASH_DRAG is what being under-invested cost — and it goes
NEGATIVE when the index falls, which is correct: on those days the
cash helped.

This is a simplification of a Brinson attribution, deliberately. A
full one needs per-name benchmark weights, which mean nothing for a
six-name book screened out of a fixed universe.

=====================================================================
WHAT THIS MODULE REFUSES TO REPORT
=====================================================================

Anything requiring a distribution — volatility, tracking error,
information ratio — is withheld until there are enough observations to
compute it without inventing precision. Below the floor the field is
None with a stated reason, which the dashboard renders as an em-dash,
in keeping with the rest of this system: a performance page showing a
Sharpe ratio computed from four days is worse than one admitting it
does not know yet.

And a caution that survives passing the floor: with a handful of
positions held for weeks to months, the desk makes perhaps ten to
thirty real decisions a year. Distinguishing skill from luck at that
rate takes many years, not many sessions. These numbers are a
measurement, not yet evidence, and `describe()` says so out loud.
"""

import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.db import session_scope
from core.market_calendar import EASTERN, session_bounds
from core.models import BenchmarkHistory, DailyPnl, Position

logger = logging.getLogger(__name__)

# SPY rather than ^GSPC: it is what a person could actually have bought
# instead of running this desk, which is the comparison that means
# something. Alpaca serves it from the same data client Ada already
# uses for quotes.
BENCHMARK_TICKER = "SPY"

# Below this many matched sessions, no statistic that needs a
# distribution is reported. Twenty is roughly a trading month — enough
# to compute, nowhere near enough to believe, which describe() states
# rather than leaving to the reader.
MIN_SESSIONS_FOR_RISK_STATS = 20


# =================================================================
# 1. KEEPING THE SERIES
# =================================================================
def drop_incomplete_session(bars: list[dict], now: Optional[datetime] = None) -> list[dict]:
    """
    Discard a bar for a session that has not closed yet.

    A daily bar requested mid-session is a PARTIAL bar: its "close" is
    just the last print so far. Stored as a close it becomes a silent
    lie — a benchmark level for a day that had not ended, which every
    later return calculation would then be measured against.

    Otis syncs after the close so he never sees one. `backfill` run at
    eleven in the morning does, which is exactly the kind of gap
    between two individually-correct components that this project keeps
    finding by using the system rather than reading it.

    The bar is dropped rather than corrected, and it arrives on the
    next sync when it is real. Pure and side-effect free so the rule
    can be tested without a market.
    """
    if not bars:
        return bars
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(EASTERN).date()

    bounds = session_bounds(today)
    if bounds is None:
        return bars           # not a session day; nothing today can be partial
    _, close = bounds
    if now > close:
        return bars           # today's session is over; its bar is final

    kept = [b for b in bars if b["date"] < today]
    if len(kept) != len(bars):
        logger.info("Dropped today's %s bar — the session has not closed yet "
                    "(closes %s).", today, close.astimezone(EASTERN).strftime("%H:%M ET"))
    return kept



def sync_benchmark(start: date, end: date, ticker: str = BENCHMARK_TICKER) -> int:
    """
    Fetch and store daily closes for a date range. Idempotent — an
    existing bar is updated rather than duplicated, so re-running over
    an overlapping window is free.

    Returns the number of bars written. Imports the Alpaca client
    lazily so that everything below this point stays testable without
    API keys in the environment.
    """
    from core.clients import alpaca_client

    bars = drop_incomplete_session(alpaca_client.get_daily_bars(ticker, start, end))
    if not bars:
        logger.info("No completed %s sessions between %s and %s.", ticker, start, end)
        return 0

    with session_scope() as session:
        # The close immediately BEFORE this batch, so the first bar of
        # the batch gets a real return rather than a NULL every time
        # the series is extended. Only the very first bar ever stored
        # should carry NULL.
        prior = (
            session.query(BenchmarkHistory)
            .filter(BenchmarkHistory.ticker == ticker,
                    BenchmarkHistory.bar_date < bars[0]["date"])
            .order_by(BenchmarkHistory.bar_date.desc())
            .first()
        )
        previous_close = float(prior.close) if prior else None

        for bar in bars:
            ret = None
            if previous_close:
                ret = round((bar["close"] / previous_close - 1) * 100, 6)

            stmt = pg_insert(BenchmarkHistory).values(
                bar_date=bar["date"], ticker=ticker,
                close=round(bar["close"], 4),
                daily_return_pct=ret,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["bar_date", "ticker"],
                set_={"close": stmt.excluded.close,
                      "daily_return_pct": stmt.excluded.daily_return_pct},
            )
            session.execute(stmt)
            previous_close = bar["close"]

    logger.info("Stored %d %s bar(s), %s to %s.",
                len(bars), ticker, bars[0]["date"], bars[-1]["date"])
    return len(bars)


def sync_to_date(today: date, ticker: str = BENCHMARK_TICKER) -> int:
    """
    Bring the series up to `today`, fetching only what is missing.

    Called by Otis at the close. Asks for a small overlapping window
    rather than just today, so a session missed because the desk did
    not run is filled in on the next close instead of leaving a
    permanent hole in the series.
    """
    with session_scope() as session:
        latest = (
            session.query(BenchmarkHistory)
            .filter(BenchmarkHistory.ticker == ticker)
            .order_by(BenchmarkHistory.bar_date.desc())
            .first()
        )
        last_date = latest.bar_date if latest else None

    if last_date is None:
        return backfill(ticker=ticker)

    if last_date >= today:
        return 0
    return sync_benchmark(last_date, today, ticker=ticker)


def backfill(start: Optional[date] = None, ticker: str = BENCHMARK_TICKER) -> int:
    """
    Fill the series from the beginning of the desk's own history.

    THE REASON D4 COULD WAIT. The benchmark is recoverable in a way the
    desk's own book is not: nobody can reconstruct what the portfolio
    was worth on a day nobody closed it, but the index's close that day
    is a permanent public fact. So every session the desk has already
    traded can still be given its comparison, after the fact.

    Defaults to the first session with a NAV on record — a daily_pnl
    row without a NAV predates the equity series and has nothing to
    compare.
    """
    if start is None:
        with session_scope() as session:
            first = (
                session.query(DailyPnl)
                .filter(DailyPnl.nav.isnot(None))
                .order_by(DailyPnl.pnl_date.asc())
                .first()
            )
        if first is None:
            logger.warning("No NAV on record — nothing to backfill against.")
            return 0
        # One session of lead-in, so the first day of the desk's history
        # has a prior close and therefore a return of its own.
        start = first.pnl_date - timedelta(days=7)

    return sync_benchmark(start, date.today(), ticker=ticker)


# =================================================================
# 2. THE COMPARISON
# =================================================================
@dataclass(frozen=True)
class Performance:
    """One period's comparison. Every field is either a number that was
    computed or None with a reason — never a plausible-looking
    placeholder."""

    start: Optional[date]
    end: Optional[date]
    sessions: int
    nav_start: Optional[float]
    nav_end: Optional[float]
    desk_return_pct: Optional[float]
    benchmark_return_pct: Optional[float]
    active_return_pct: Optional[float]
    avg_invested_pct: Optional[float]
    selection_pct: Optional[float]
    cash_drag_pct: Optional[float]
    tracking_error_pct: Optional[float]
    unavailable: str = ""

    @property
    def measurable(self) -> bool:
        return self.active_return_pct is not None

    def describe(self) -> str:
        if not self.measurable:
            return f"No comparison available — {self.unavailable}"
        verb = "ahead of" if self.active_return_pct >= 0 else "behind"
        text = (
            f"{self.desk_return_pct:+.2f}% against {BENCHMARK_TICKER} "
            f"{self.benchmark_return_pct:+.2f}% — "
            f"{abs(self.active_return_pct):.2f}pp {verb} the index over "
            f"{self.sessions} session(s), at {self.avg_invested_pct:.1f}% "
            f"average invested. Selection {self.selection_pct:+.2f}pp, "
            f"cash drag {self.cash_drag_pct:+.2f}pp."
        )
        if self.sessions < MIN_SESSIONS_FOR_RISK_STATS:
            text += (f" Too few sessions for risk statistics "
                     f"(need {MIN_SESSIONS_FOR_RISK_STATS}).")
        return text


def _unavailable(reason: str, sessions: int = 0) -> Performance:
    return Performance(
        start=None, end=None, sessions=sessions,
        nav_start=None, nav_end=None,
        desk_return_pct=None, benchmark_return_pct=None,
        active_return_pct=None, avg_invested_pct=None,
        selection_pct=None, cash_drag_pct=None,
        tracking_error_pct=None, unavailable=reason,
    )


def compute_performance(nav_series: list[tuple[date, float]],
                        bench_series: dict[date, float],
                        invested_weights: Optional[list[float]] = None) -> Performance:
    """
    Compare two series over their common span. PURE — no database, no
    network, so the arithmetic is testable on hand-built numbers.

    `nav_series` is (date, nav) ascending. `bench_series` maps date to
    close. `invested_weights` are per-session invested fractions of NAV
    (0.0-1.0); absent, exposure is assumed full and the decomposition
    collapses to plain active return, which is the honest fallback when
    the exposure history is not known.

    Only dates present in BOTH series are used. Comparing endpoints
    that are not the same two sessions would silently attribute a
    weekend's market move to the desk.
    """
    if len(nav_series) < 2:
        return _unavailable(
            "fewer than two sessions with a NAV on record", len(nav_series))
    if not bench_series:
        return _unavailable(
            f"no {BENCHMARK_TICKER} history stored — "
            f"run `python -m core.benchmark backfill`")

    matched = [(d, nav) for d, nav in nav_series if d in bench_series]
    if len(matched) < 2:
        return _unavailable(
            f"only {len(matched)} session(s) present in both the desk's "
            f"history and the {BENCHMARK_TICKER} series", len(matched))

    start, nav_start = matched[0]
    end, nav_end = matched[-1]
    if not nav_start:
        return _unavailable("the first matched session has no usable NAV",
                            len(matched))

    desk = (nav_end / nav_start - 1) * 100
    bench = (bench_series[end] / bench_series[start] - 1) * 100

    # Average exposure over the period. Trimmed to the matched sessions
    # so the weight describes the same window as the returns.
    if invested_weights:
        weights = invested_weights[:len(matched)]
        w = sum(weights) / len(weights)
    else:
        w = 1.0

    selection = desk - w * bench
    cash_drag = (1 - w) * bench

    # Risk statistics need a distribution, not two endpoints.
    tracking_error = None
    if len(matched) >= MIN_SESSIONS_FOR_RISK_STATS:
        diffs = []
        for (d0, n0), (d1, n1) in zip(matched, matched[1:]):
            if not n0 or not bench_series.get(d0):
                continue
            diffs.append((n1 / n0 - 1) * 100 - (bench_series[d1] / bench_series[d0] - 1) * 100)
        if len(diffs) >= 2:
            mean = sum(diffs) / len(diffs)
            var = sum((x - mean) ** 2 for x in diffs) / (len(diffs) - 1)
            tracking_error = round(var ** 0.5, 4)

    return Performance(
        start=start, end=end, sessions=len(matched),
        nav_start=round(nav_start, 2), nav_end=round(nav_end, 2),
        desk_return_pct=round(desk, 4),
        benchmark_return_pct=round(bench, 4),
        active_return_pct=round(desk - bench, 4),
        avg_invested_pct=round(w * 100, 2),
        selection_pct=round(selection, 4),
        cash_drag_pct=round(cash_drag, 4),
        tracking_error_pct=tracking_error,
    )


def performance_since_inception(today: Optional[date] = None,
                                ticker: str = BENCHMARK_TICKER) -> Performance:
    """The headline figure: the desk against the index over its whole
    recorded life, read from the database."""
    today = today or date.today()

    with session_scope() as session:
        nav_rows = (
            session.query(DailyPnl)
            .filter(DailyPnl.nav.isnot(None), DailyPnl.pnl_date <= today)
            .order_by(DailyPnl.pnl_date.asc())
            .all()
        )
        nav_series = [(r.pnl_date, float(r.nav)) for r in nav_rows]

        bench_rows = (
            session.query(BenchmarkHistory)
            .filter(BenchmarkHistory.ticker == ticker,
                    BenchmarkHistory.bar_date <= today)
            .all()
        )
        bench_series = {r.bar_date: float(r.close) for r in bench_rows}

        # EXPOSURE IS MEASURED FROM TODAY'S BOOK AND HELD FLAT, WHICH IS
        # AN APPROXIMATION AND IS LABELLED AS ONE. positions is rebuilt
        # each close rather than kept as a history, so there is no
        # per-session invested weight to read. position_pnl_history
        # could give one; wiring that is a separate change, and
        # inventing a series here would be worse than approximating
        # openly.
        invested = sum(float(p.market_value or 0) for p in session.query(Position).all())

    if not nav_series:
        return _unavailable("no NAV recorded yet — Otis has never closed the books")

    latest_nav = nav_series[-1][1]
    weight = min(invested / latest_nav, 1.0) if latest_nav else 0.0
    return compute_performance(nav_series, bench_series,
                               [weight] * len(nav_series))


# =================================================================
# CLI
# =================================================================
_USAGE = """usage: python -m core.benchmark <command>

  backfill   Fetch SPY from the first session with a NAV on record to
             today. Run once after migration 009; safe to re-run.
  sync       Bring the series up to today only.
  report     Print the desk against the index since inception.
"""


def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    command = argv[1] if len(argv) > 1 else "report"

    if command == "backfill":
        print(f"Stored {backfill()} bar(s).")
    elif command == "sync":
        print(f"Stored {sync_to_date(date.today())} bar(s).")
    elif command == "report":
        result = performance_since_inception()
        print(result.describe())
        if result.measurable:
            print(f"\n  window        {result.start} to {result.end}"
                  f"  ({result.sessions} matched session(s))")
            print(f"  NAV           {result.nav_start:,.2f} -> {result.nav_end:,.2f}")
            print(f"  desk          {result.desk_return_pct:+.2f}%")
            print(f"  {BENCHMARK_TICKER:<13} {result.benchmark_return_pct:+.2f}%")
            print(f"  active        {result.active_return_pct:+.2f}pp")
            print(f"  selection     {result.selection_pct:+.2f}pp")
            print(f"  cash drag     {result.cash_drag_pct:+.2f}pp")
            te = (f"{result.tracking_error_pct:.2f}%"
                  if result.tracking_error_pct is not None
                  else f"— (needs {MIN_SESSIONS_FOR_RISK_STATS} sessions)")
            print(f"  tracking err  {te}")
    else:
        print(_USAGE, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
