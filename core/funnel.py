"""
The decision funnel — where the pipeline stops.

=====================================================================
WHY THIS EXISTS
=====================================================================

The desk runs cleanly three times a day and holds almost nothing. Its
book is ~99% cash. Every other metric in the system answers "how well
does the desk trade"; this one answers the question actually in front
of us, which is whether it trades at all.

Something between "Vera found a candidate" and "Ada got a fill" is not
converting. That path crosses seven tables, each of which records its
own stage faithfully, and nothing anywhere joins them up — so the
constriction is invisible despite being fully recorded.

IT IS NOT THE RISK GATE. That was the obvious suspect and it was
checked before this module was written:

  * min_position_count is explicitly NON-BLOCKING. nora.py excludes it
    from hard breaches and treats it as a warning, and at zero
    positions it does not even warn.
  * On a near-empty book, position headroom is the full max_position_pct
    and sector headroom the full max_sector_pct, so neither limit can
    refuse anything.
  * The circuit breaker rejects only while active.

So on the current book Nora approves whatever reaches her. The
constriction is upstream, and this module finds out where.

=====================================================================
WHAT A STAGE COUNT IS, AND IS NOT
=====================================================================

Each stage is a ROW COUNT over the window, not a tracked cohort. The
ratio between two stages is therefore an approximation of conversion,
and it is a good one here for a specific reason: the whole cycle
completes within one trading day. A candidate found in the premarket
run is proposed, reviewed, allocated and submitted the same day. So
same-window counts are effectively cohort counts, with error confined
to the two window edges — one day in, one day out.

If the cycle ever spans days, this assumption breaks and the counts
must become a real cohort join on ids. Written down here so that
change is a decision rather than a surprise.

=====================================================================
THE TWO PLACES ORDERS GO MISSING
=====================================================================

`orders` holds more than orders that were sent. Ada writes a row for
every refusal too — halted_by_operator, rejected_stale_ledger,
rejected_insufficient_buying_power and nine others — which is the
right design (a refusal is a decision worth keeping) and means a raw
count of `orders` overstates what reached the venue.

The test for "reached the broker" is alpaca_order_id IS NOT NULL,
deliberately not a list of local-refusal status names. The id is
EVIDENCE — the broker assigned it — whereas the status vocabulary has
grown twice already and will grow again, and a metric that silently
mis-sorts a status nobody added to a hard-coded list is worse than no
metric. Local refusals are then broken out by status, which is where
the actionable detail lives.

The second place is the venue itself: an order that reached Alpaca and
never filled. Since D10 that is a legitimate resting state rather than
a fault, so it is reported as its own stage and not folded into
anything.

=====================================================================
WHAT THIS MODULE REFUSES TO DO
=====================================================================

It does not compute a conversion rate from a zero denominator, and it
does not report a percentage that would imply precision the counts do
not carry. A stage with no input shows "—", following the convention
core/benchmark.py established: a number that was computed, or None
with a reason, never a plausible-looking placeholder.
"""

import logging
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Mapping, Optional

from sqlalchemy import func

from core.db import session_scope
from core.models import (
    Allocation,
    NewCandidate,
    Order,
    Proposal,
    ProposalReview,
    StrategyDecision,
)

log = logging.getLogger(__name__)

# Default lookback. Thirty calendar days is roughly twenty NYSE
# sessions, which is the smallest window in which a conversion ratio
# says anything — below that a single quiet week reads as a collapse.
DEFAULT_LOOKBACK_DAYS = 30


# =================================================================
# The stages, in pipeline order
#
# key            what is counted
# label          how it prints
# question       what a collapse AT THIS STAGE means — this is the
#                point of the module, so it lives in the data rather
#                than in a comment somewhere else
# =================================================================
STAGES: list[tuple[str, str, str]] = [
    ("candidates", "Candidate found",
     "Research is not producing ideas. Look at Vera's screen and at whether "
     "the universe is too narrow."),
    ("proposals", "Proposed",
     "Ideas exist but Solomon is not converting them into actions. His "
     "action_needed gate is the first thing to read."),
    ("reviewed", "Reviewed",
     "Proposals are written but never reaching Nora — an orchestration or "
     "phase-ordering fault, not a judgment one."),
    ("approved", "Approved",
     "Risk is refusing. On a near-empty book this should be almost "
     "impossible; check whether the circuit breaker is stuck active."),
    ("allocated", "Allocated",
     "Approved proposals are getting no size. Marcus is the place to look, "
     "and conviction_input is the likely input."),
    ("order_rows", "Order attempted",
     "Allocations are not reaching Ada at all — again orchestration rather "
     "than judgment."),
    ("submitted", "Reached the broker",
     "Ada is refusing locally. The refusal breakdown below names which "
     "guard, and every one of them is a deliberate one."),
    ("filled", "Filled",
     "Orders reach Alpaca and never fill. Limit prices are too far from the "
     "market, or the session is ending before they are touched."),
]

_STAGE_KEYS = [key for key, _, _ in STAGES]


@dataclass
class Stage:
    key: str
    label: str
    count: int
    # Conversion from the previous stage. None at the first stage, and
    # None wherever the previous stage was zero — a ratio with a zero
    # denominator is undefined, not 0%.
    conversion_pct: Optional[float]
    diagnosis: str


@dataclass
class Funnel:
    """One window's pipeline. Every field is either computed or None
    with a reason — never a placeholder."""

    start: date
    end: date
    stages: list[Stage] = field(default_factory=list)
    # status -> count, for order rows that never reached the broker.
    local_refusals: dict[str, int] = field(default_factory=dict)

    @property
    def measurable(self) -> bool:
        return bool(self.stages) and self.stages[0].count > 0

    @property
    def first_break(self) -> Optional[Stage]:
        """The first stage that received input and produced nothing.
        This is the answer the module exists to give."""
        previous = None
        for stage in self.stages:
            if previous is not None and previous.count > 0 and stage.count == 0:
                return stage
            previous = stage
        return None

    @property
    def end_to_end_pct(self) -> Optional[float]:
        if not self.measurable:
            return None
        return round(100.0 * self.stages[-1].count / self.stages[0].count, 1)

    def describe(self) -> str:
        if not self.measurable:
            return (f"No candidates recorded between {self.start} and "
                    f"{self.end} — the pipeline has no input, so there is "
                    f"no conversion to measure.")
        broken = self.first_break
        head = (f"{self.stages[0].count} candidate(s) -> "
                f"{self.stages[-1].count} fill(s) over "
                f"{self.start} to {self.end} "
                f"({self.end_to_end_pct:.1f}% end to end).")
        if broken is not None:
            return f"{head} Stops at: {broken.label}. {broken.diagnosis}"
        return f"{head} No stage went to zero."


# =================================================================
# PURE — no database, no clock. Everything testable lives here.
# =================================================================
def compute_funnel(counts: Mapping[str, int],
                   start: date,
                   end: date,
                   local_refusals: Optional[Mapping[str, int]] = None) -> Funnel:
    """Build a Funnel from raw stage counts.

    Deliberately takes a plain mapping rather than a session, so the
    whole of the reasoning above is exercisable without a database —
    which is what keeps this project's suite running in two seconds
    and therefore actually run.

    A key missing from `counts` is treated as zero. That is correct
    here and only here: these are COUNTS of rows over a window, and an
    absent key means the query found nothing, which is genuinely zero
    rather than unknown.
    """
    stages: list[Stage] = []
    previous: Optional[int] = None

    for key, label, diagnosis in STAGES:
        count = int(counts.get(key, 0))
        if previous is None:
            conversion = None
        elif previous == 0:
            # Undefined, not zero. Nothing entered this stage, so no
            # statement about its conversion is available.
            conversion = None
        else:
            conversion = round(100.0 * count / previous, 1)
        stages.append(Stage(key=key, label=label, count=count,
                            conversion_pct=conversion, diagnosis=diagnosis))
        previous = count

    return Funnel(start=start, end=end, stages=stages,
                  local_refusals=dict(local_refusals or {}))


# =================================================================
# DATABASE
# =================================================================
def funnel_counts(session, start: date, end: date) -> dict[str, int]:
    """One count per stage over [start, end] inclusive."""

    candidates = session.query(func.count(NewCandidate.id)).filter(
        NewCandidate.candidate_date.between(start, end)).scalar() or 0

    # Proposals carry no date of their own — they hang off the day's
    # strategy_decisions row, which does.
    proposals = session.query(func.count(Proposal.id)).join(
        StrategyDecision, Proposal.strategy_decision_id == StrategyDecision.id
    ).filter(StrategyDecision.decision_date.between(start, end)).scalar() or 0

    reviewed = session.query(func.count(ProposalReview.id)).join(
        Proposal, ProposalReview.proposal_id == Proposal.id
    ).join(
        StrategyDecision, Proposal.strategy_decision_id == StrategyDecision.id
    ).filter(StrategyDecision.decision_date.between(start, end)).scalar() or 0

    approved = session.query(func.count(ProposalReview.id)).join(
        Proposal, ProposalReview.proposal_id == Proposal.id
    ).join(
        StrategyDecision, Proposal.strategy_decision_id == StrategyDecision.id
    ).filter(
        StrategyDecision.decision_date.between(start, end),
        ProposalReview.decision == "approved",
    ).scalar() or 0

    allocated = session.query(func.count(Allocation.id)).filter(
        Allocation.allocation_date.between(start, end)).scalar() or 0

    order_rows = session.query(func.count(Order.id)).filter(
        Order.order_date.between(start, end)).scalar() or 0

    # Evidence the broker saw it, rather than a hard-coded list of
    # local-refusal status names that will drift — see the module
    # docstring.
    submitted = session.query(func.count(Order.id)).filter(
        Order.order_date.between(start, end),
        Order.alpaca_order_id.isnot(None),
    ).scalar() or 0

    filled = session.query(func.count(Order.id)).filter(
        Order.order_date.between(start, end),
        Order.status == "filled",
    ).scalar() or 0

    return {
        "candidates": candidates,
        "proposals": proposals,
        "reviewed": reviewed,
        "approved": approved,
        "allocated": allocated,
        "order_rows": order_rows,
        "submitted": submitted,
        "filled": filled,
    }


def local_refusal_counts(session, start: date, end: date) -> dict[str, int]:
    """Order rows that never reached the broker, grouped by status.

    This is where the actionable detail is when `submitted` collapses:
    every one of these statuses is a guard somebody added on purpose,
    and knowing WHICH guard fired is the difference between a fix and a
    guess.
    """
    rows = session.query(Order.status, func.count(Order.id)).filter(
        Order.order_date.between(start, end),
        Order.alpaca_order_id.is_(None),
    ).group_by(Order.status).all()
    return {status: int(count) for status, count in rows}


def funnel_over(days: int = DEFAULT_LOOKBACK_DAYS,
                today: Optional[date] = None) -> Funnel:
    """The funnel over the last `days` calendar days, ending today."""
    end = today or date.today()
    start = end - timedelta(days=days - 1)
    with session_scope() as session:
        counts = funnel_counts(session, start, end)
        refusals = local_refusal_counts(session, start, end)
    return compute_funnel(counts, start, end, refusals)


# =================================================================
# CLI
# =================================================================
_USAGE = """usage: python -m core.funnel [report] [--days N]

  report     Print the decision funnel — candidates through fills —
             and name the stage where it stops. Default 30 days.

  --days N   Lookback in calendar days (default 30, ~20 sessions).
"""


def _render(result: Funnel) -> str:
    lines = [f"Decision funnel  {result.start} to {result.end}", ""]
    width = max(len(label) for _, label, _ in STAGES)
    for stage in result.stages:
        conv = (f"{stage.conversion_pct:5.1f}%"
                if stage.conversion_pct is not None else "    — ")
        lines.append(f"  {stage.label:<{width}}  {stage.count:>6}   {conv}")

    if result.local_refusals:
        lines += ["", "  Never reached the broker:"]
        for status, count in sorted(result.local_refusals.items(),
                                    key=lambda kv: -kv[1]):
            lines.append(f"    {status:<36} {count:>5}")

    lines += ["", "  " + result.describe()]
    return "\n".join(lines)


def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    args = [a for a in argv[1:] if a != "report"]
    days = DEFAULT_LOOKBACK_DAYS
    if "--days" in args:
        try:
            days = int(args[args.index("--days") + 1])
            if days < 1:
                raise ValueError
        except (IndexError, ValueError):
            print("--days needs a positive integer.", file=sys.stderr)
            return 2
    elif args:
        print(_USAGE, file=sys.stderr)
        return 2

    print(_render(funnel_over(days)))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
