"""
The ledger — read-only access to Otis's system of record.

Operating Manual §4: every agent that needs portfolio state reads it
from Otis's reconciled ledger, never from a raw broker query. This
module is that read path, so "the ledger" is one importable thing
rather than a convention people remember to follow.

WHY IT MATTERS, CONCRETELY. Ada used to size trades off a live Alpaca
call while Nora enforced the position limit against Otis's `positions`
table. Two sources, two moments, no reconciliation between them — so
the number a trade was checked against and the number it was sized
against could differ, and the limit that was satisfied in Phase 3 was
not necessarily the limit the order respected in Phase 4.

Institutional desks keep this separation deliberately: books-and-records
is the firm's own ledger, the broker is external ground truth, and the
two are reconciled on a schedule. What they never do is let one system
size trades off one source while another polices limits off the other.

WHAT STILL COMES FROM ALPACA, CORRECTLY:

  - Prices and quotes. The ledger holds no market data and shouldn't.
  - Order submission and fill status. That IS the broker's business.
  - Otis's own reconciliation, whose entire job is comparing the
    ledger against the broker. He must query both; that is the point.

THE LEDGER IS ALWAYS ONE SESSION BEHIND, AND THAT IS INTENDED. Otis
closes the books in Phase 5; Ada trades in Phase 4. So a cycle sizes
against the previous close — which is exactly the basis Nora used when
she computed headroom in Phase 3. Consistency between the limit check
and the sizing is the point of this module; being current is what the
staleness guard below is for.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from core.db import session_scope
from core.market_calendar import trading_days_between
from core.models import DailyPnl, Position

# How old the ledger may be before it stops being usable for sizing.
#
# A normal cycle reads yesterday's close: one session. Two or more means
# Otis has not run — a failed cycle, a stopped container, a weekend of
# nobody looking — and NAV may have moved materially since. Sizing a
# percentage of a stale NAV produces a confidently wrong number, which
# is worse than refusing, so past this the ledger reports itself
# unusable and Ada declines to trade rather than guessing.
MAX_LEDGER_STALENESS_SESSIONS = 1


@dataclass(frozen=True)
class LedgerSnapshot:
    """Portfolio state as of the ledger's last close."""

    nav: Optional[float]
    as_of: Optional[date]
    staleness_sessions: int
    positions: dict[str, dict] = field(default_factory=dict)
    bootstrap: bool = False  # the ledger has never been written

    @property
    def usable_for_sizing(self) -> bool:
        """
        Whether a trade may be sized against this snapshot.

        The bootstrap case passes deliberately. On a brand-new install
        Otis has never run — and he runs in Phase 5, after Ada — so a
        strict rule would make the first cycle unable to trade, forever,
        with no way out except running Otis by hand. Callers fall back
        to the broker in that one case and record that they did.
        """
        if self.bootstrap:
            return True
        if self.nav is None or self.nav <= 0:
            return False
        return self.staleness_sessions <= MAX_LEDGER_STALENESS_SESSIONS

    def describe(self) -> str:
        if self.bootstrap:
            return "ledger empty — Otis has never closed the books"
        if self.nav is None:
            return "ledger has no NAV recorded"
        return (
            f"ledger as of {self.as_of} "
            f"({self.staleness_sessions} session(s) old), NAV {self.nav:,.2f}, "
            f"{len(self.positions)} position(s)"
        )

    def position_value(self, ticker: str) -> float:
        """Market value of a holding at the last close, 0.0 if not held."""
        row = self.positions.get(ticker)
        return float(row["market_value"] or 0) if row else 0.0

    def position_qty(self, ticker: str) -> float:
        row = self.positions.get(ticker)
        return float(row["shares"] or 0) if row else 0.0

    def position_weight(self, ticker: str) -> float:
        row = self.positions.get(ticker)
        return float(row["weight_pct"] or 0) if row else 0.0


def read_snapshot(today: date) -> LedgerSnapshot:
    """
    One read of the whole portfolio state, rather than a query per
    lookup — so every number in a sizing decision comes from the same
    moment. Mixing two reads taken seconds apart is a smaller version of
    the same bug this module exists to fix.
    """
    with session_scope() as session:
        latest = (
            session.query(DailyPnl)
            .filter(DailyPnl.nav.isnot(None))
            .order_by(DailyPnl.pnl_date.desc())
            .first()
        )

        positions = {
            p.ticker: {
                "ticker": p.ticker,
                "shares": float(p.shares or 0),
                "avg_cost": float(p.avg_cost or 0),
                "market_value": float(p.market_value or 0),
                "weight_pct": float(p.weight_pct or 0),
                "sector": p.sector,
            }
            for p in session.query(Position).all()
        }

        if latest is None:
            return LedgerSnapshot(
                nav=None, as_of=None, staleness_sessions=0,
                positions=positions, bootstrap=True,
            )

        return LedgerSnapshot(
            nav=float(latest.nav),
            as_of=latest.pnl_date,
            staleness_sessions=trading_days_between(latest.pnl_date, today),
            positions=positions,
        )
