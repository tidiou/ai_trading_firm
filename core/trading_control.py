"""
The kill switch — a way for a human to stop the desk trading, now,
without killing a process mid-cycle.

WHY NOT JUST CTRL-C. Ada submits an order to Alpaca and then records
it in the database. Killing the process between those two steps leaves
a live order at the broker with no local record of it, and the next
run's idempotency check — which reads exactly those records — will
happily submit it again. So the one thing you reach for when something
looks wrong is the one thing that can cause a duplicate real trade.
This gives you a stop that takes effect at a safe point instead.

HOW IT DIFFERS FROM THE CIRCUIT BREAKER. Nora's drawdown breaker is
automatic, fires on a measured condition, freezes new risk, and
deliberately still permits exits and trims — its job is to stop the
book growing while you're losing money, and a breaker that stopped you
de-risking would be worse than none.

This is the opposite kind of control. It is manual, fires on your
judgement, and stops everything including exits. The reasoning: you
reach for a kill switch when you no longer trust the system, and a
system you don't trust should not be choosing what to sell either. If
you need out of a position while halted, unwind it by hand — that is
the intended workflow, not a gap.

WHAT IT DOES NOT STOP. Analysis. Atlas, Vera, Solomon, Nora, Marcus,
Otis and Clara all run normally while halted; only Ada's submissions
are blocked, and each blocked order is still recorded with status
`halted_by_operator`. So a halted day still closes its books, still
reconciles, still produces a daily report, and still shows you exactly
what the desk *would* have done — which is the diagnostic you actually
want during an incident. A halt that also blinded you would be a
strange kind of safety feature.

STORAGE. Append-only: halting and resuming both INSERT, and the
current state is the latest row. The table is therefore its own audit
history — "when was it halted, by whom, and why" is answerable without
a separate log, and no row is ever mutated.

READING IT IS CHEAP AND HAPPENS OFTEN. Ada re-reads immediately before
every single submission rather than once at the top of her run,
because "halted" needs to mean "as of this order", not "as of whenever
this batch started". A cycle with four orders in it can span minutes.
"""

import argparse
import getpass
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from core.db import session_scope
from core.models import TradingControl


@dataclass(frozen=True)
class ControlState:
    trading_enabled: bool
    changed_by: Optional[str]
    reason: Optional[str]
    changed_at: Optional[datetime]
    bootstrap: bool = False  # True when no row exists yet

    def describe(self) -> str:
        if self.bootstrap:
            return "ENABLED (no control row on record — bootstrap default)"
        state = "ENABLED" if self.trading_enabled else "HALTED"
        when = self.changed_at.strftime("%Y-%m-%d %H:%M:%S %Z").strip() if self.changed_at else "unknown time"
        return f'{state} — set by {self.changed_by} at {when}: "{self.reason}"'


def get_state() -> ControlState:
    """
    Current control state: the latest row.

    An empty table reads as ENABLED. That is a deliberate bootstrap
    exception, not a fail-open policy — a fresh install has no rows and
    has to work, and migration 003 seeds an explicit row so this branch
    is only ever hit before the migration runs.

    There is no fail-open path beyond that. If the database is
    unreachable this raises, and every caller is inside a cycle that
    dies with it — so an unreadable control state stops trading rather
    than defaulting to permitted, which is the behaviour you want from
    a safety control.
    """
    with session_scope() as session:
        row = session.query(TradingControl).order_by(TradingControl.id.desc()).first()
        if row is None:
            return ControlState(True, None, None, None, bootstrap=True)
        return ControlState(
            trading_enabled=bool(row.trading_enabled),
            changed_by=row.changed_by,
            reason=row.reason,
            changed_at=row.changed_at,
        )


def is_trading_enabled() -> bool:
    """Convenience read for the hot path."""
    return get_state().trading_enabled


def set_state(trading_enabled: bool, reason: str, changed_by: Optional[str] = None) -> ControlState:
    """
    Append a new control row.

    `reason` is mandatory in BOTH directions, and that is not
    box-ticking. A halt without a reason is unreadable to whoever finds
    it tomorrow; a *resume* without a reason is how a halt gets lifted
    because it was in the way rather than because it was resolved.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("A reason is required to halt or resume trading.")

    changed_by = (changed_by or getpass.getuser() or "unknown").strip()

    with session_scope() as session:
        session.add(TradingControl(
            trading_enabled=trading_enabled,
            changed_by=changed_by,
            reason=reason,
        ))

    return get_state()


def halt(reason: str, changed_by: Optional[str] = None) -> ControlState:
    return set_state(False, reason, changed_by)


def resume(reason: str, changed_by: Optional[str] = None) -> ControlState:
    return set_state(True, reason, changed_by)


def history(limit: int = 20) -> list[TradingControl]:
    with session_scope() as session:
        rows = (
            session.query(TradingControl)
            .order_by(TradingControl.id.desc())
            .limit(limit)
            .all()
        )
        # expire_on_commit=False, so these stay readable after the session closes.
        return rows


# =================================================================
# CLI — `python -m core.trading_control ...`
#
# Matches the `python -m agents.x` convention used everywhere else,
# and deliberately has no dependency on the agents package: if the
# thing you are halting is an agent, you do not want importing it to
# be a prerequisite for stopping it.
# =================================================================
def _print_state(state: ControlState) -> None:
    marker = "  " if state.trading_enabled else ">>"
    print(f"{marker} Trading is {state.describe()}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.trading_control",
        description="Halt or resume all order submission (the kill switch).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show current state")

    p_halt = sub.add_parser("halt", help="stop ALL order submission, including exits")
    p_halt.add_argument("--reason", required=True, help="why you are halting (recorded)")
    p_halt.add_argument("--by", default=None, help="who is halting (defaults to the OS user)")

    p_resume = sub.add_parser("resume", help="re-enable order submission")
    p_resume.add_argument("--reason", required=True, help="why it is safe to resume (recorded)")
    p_resume.add_argument("--by", default=None, help="who is resuming (defaults to the OS user)")

    p_hist = sub.add_parser("history", help="recent halt/resume history")
    p_hist.add_argument("--limit", type=int, default=20)

    args = parser.parse_args(argv)

    if args.command == "status":
        _print_state(get_state())
        return 0

    if args.command == "halt":
        state = halt(args.reason, args.by)
        print("Trading HALTED. Ada will submit nothing — buys or sells — until resumed.")
        print("Analysis, reconciliation and reporting continue as normal.")
        _print_state(state)
        return 0

    if args.command == "resume":
        state = resume(args.reason, args.by)
        print("Trading RESUMED.")
        _print_state(state)
        return 0

    if args.command == "history":
        rows = history(args.limit)
        if not rows:
            print("No control rows on record — trading is enabled by bootstrap default.")
            return 0
        for r in rows:
            state = "ENABLED" if r.trading_enabled else "HALTED "
            when = r.changed_at.strftime("%Y-%m-%d %H:%M:%S") if r.changed_at else "?"
            print(f"  {when}  {state}  {r.changed_by:<16} {r.reason}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
