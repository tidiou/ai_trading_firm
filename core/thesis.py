"""
Thesis assumptions and the verdict they produce (S4 + S3b).

=====================================================================
WHY A THESIS HAD TO STOP BEING A PARAGRAPH
=====================================================================

`theses.thesis_text` is prose. Vera's monitoring pass read it every
morning and formed a fresh opinion, which has two costs that are easy
to miss:

  It cannot be compared. You cannot diff a news item against a
  paragraph. Asking "does this change the thesis?" of unstructured
  text means re-arguing the whole thesis daily, from scratch, with
  whatever the model happens to weight that morning.

  It cannot be wrong in a useful way. A thesis that says "strong
  franchise, improving margins" is never falsified — it just stops
  being mentioned. Nothing is ever recorded as having been settled.

So a thesis now carries ASSUMPTIONS: named claims, each with the
metric that bears on it, the direction that supports it, and the
threshold that would falsify it. Monitoring checks claims instead of
re-reading prose, which is what makes a daily comparison a matter of
minutes rather than a re-derivation.

=====================================================================
THE VERDICT IS COMPUTED, NOT ASKED FOR
=====================================================================

The model reports what each assumption did. THIS MODULE decides what
that means for the thesis. That division is deliberate and it is the
same one Nora uses for risk limits.

If the model were asked for the verdict directly it could return
"strengthened" while its own per-assumption notes said two load-bearing
claims were under pressure — a flattering summary that contradicts its
own evidence, with nothing to catch it. Here the verdict is a pure
function of the checks, so it cannot disagree with them.

Precedence, highest first:

    broken        a LOAD-BEARING assumption passed its falsifying
                  threshold. The thesis as written is finished.
    weakened      something broke that was not load-bearing, or any
                  assumption is strained (moved against, not yet past
                  its threshold).
    strengthened  at least one assumption improved and nothing is
                  strained or broken.
    unchanged     everything holding, or nothing checked today.

`strengthened` is the one the old vocabulary could not express.
`position_monitoring_log.status` ran intact | at_risk | broken, so
Vera could downgrade or hold and never upgrade — a monitoring loop
that only moves one direction drifts pessimistic over time, and it
could never be the reason Marcus adds to a winner. That asymmetry
quietly capped the desk's upside at whatever it decided on day one.

=====================================================================
LOAD-BEARING, AND WHY IT IS NOT OPTIONAL
=====================================================================

Without it, one broken minor claim sinks a thesis. A six-claim thesis
where the least important assumption fails is WEAKENED, not broken,
and treating those the same would make the verdict useless within a
month — every thesis would read "broken" eventually, for reasons
nobody considered material.

So each assumption declares whether the thesis dies without it. The
model is asked to mark them at open time, when it is reasoning about
the thesis rather than defending it.

=====================================================================
WHAT THIS MODULE REFUSES TO DO
=====================================================================

A thesis with no assumptions gets NO VERDICT — None, with a reason —
never `unchanged`. Those theses exist: every one opened before
migration 011. Calling them "unchanged" would assert that their claims
were checked and held, which is a fabrication of exactly the kind
core/benchmark.py established this codebase does not commit.
"""

from dataclasses import dataclass, field
from typing import Iterable, Optional

# ---------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------
# What one assumption did since it was last looked at.
ASSUMPTION_STATUSES = (
    "holding",    # still true; nothing has moved against it
    "improving",  # evidence moved in its favour beyond what was claimed
    "strained",   # moved against it, but not past the falsifying threshold
    "broken",     # past the falsifying threshold
    "unchecked",  # no information bearing on it arrived
)

# What the thesis as a whole did. Ordered by precedence, worst first —
# `compute_verdict` relies on this order.
VERDICTS = ("broken", "weakened", "strengthened", "unchanged")

_MOVED_AGAINST = {"strained", "broken"}


@dataclass
class AssumptionCheck:
    """One assumption, as of one check.

    `is_load_bearing` is a property of the CLAIM, carried here so the
    verdict can be computed from the checks alone — the alternative is
    a join at verdict time, which would put a database read inside a
    pure function.
    """

    claim: str
    status: str
    is_load_bearing: bool = True
    evidence: str = ""
    assumption_id: Optional[int] = None

    def __post_init__(self):
        if self.status not in ASSUMPTION_STATUSES:
            raise ValueError(
                f"unknown assumption status {self.status!r} — expected one of "
                f"{', '.join(ASSUMPTION_STATUSES)}"
            )


@dataclass
class Verdict:
    """The thesis-level answer. Either a verdict with the counts behind
    it, or None with a stated reason — never a plausible default."""

    verdict: Optional[str]
    reason: str
    counts: dict = field(default_factory=dict)
    checked: int = 0
    total: int = 0
    broken_claims: list = field(default_factory=list)
    strained_claims: list = field(default_factory=list)
    improved_claims: list = field(default_factory=list)

    @property
    def measurable(self) -> bool:
        return self.verdict is not None

    def describe(self) -> str:
        if not self.measurable:
            return f"No verdict — {self.reason}"
        head = f"{self.verdict.upper()} — {self.reason}"
        if self.total:
            head += f" ({self.checked} of {self.total} assumption(s) checked)"
        return head


def compute_verdict(checks: Iterable[AssumptionCheck]) -> Verdict:
    """The thesis-level verdict, derived from its assumption checks.

    PURE. No database, no clock, no model. Everything that decides
    what a day's evidence means to a thesis lives in these thirty
    lines, so it is testable and so it cannot be talked out of its
    conclusion by a persuasive narrative.
    """
    checks = list(checks)

    if not checks:
        return Verdict(
            verdict=None,
            reason="this thesis has no assumptions on record, so there is "
                   "nothing to check it against (opened before migration 011)",
        )

    counts = {s: sum(1 for c in checks if c.status == s) for s in ASSUMPTION_STATUSES}
    counts = {k: v for k, v in counts.items() if v}

    broken = [c for c in checks if c.status == "broken"]
    load_bearing_broken = [c for c in broken if c.is_load_bearing]
    strained = [c for c in checks if c.status == "strained"]
    improved = [c for c in checks if c.status == "improving"]
    checked = sum(1 for c in checks if c.status != "unchecked")

    common = dict(
        counts=counts, checked=checked, total=len(checks),
        broken_claims=[c.claim for c in broken],
        strained_claims=[c.claim for c in strained],
        improved_claims=[c.claim for c in improved],
    )

    if load_bearing_broken:
        names = ", ".join(c.claim for c in load_bearing_broken)
        return Verdict(verdict="broken",
                       reason=f"load-bearing assumption failed: {names}",
                       **common)

    if broken or strained:
        bits = []
        if broken:
            bits.append(f"{len(broken)} non-load-bearing assumption(s) failed")
        if strained:
            bits.append(f"{len(strained)} under pressure")
        return Verdict(verdict="weakened", reason="; ".join(bits), **common)

    if improved:
        names = ", ".join(c.claim for c in improved)
        return Verdict(verdict="strengthened",
                       reason=f"assumption(s) improved: {names}",
                       **common)

    if checked == 0:
        # Honest, and different from "we confirmed everything". Nothing
        # arrived that bears on this thesis, so it stands — but the
        # reason says why, so a reader is not misled into thinking it
        # was actively re-verified.
        return Verdict(verdict="unchanged",
                       reason="no information bearing on any assumption arrived",
                       **common)

    return Verdict(verdict="unchanged",
                   reason="every checked assumption is holding",
                   **common)


def verdict_transition(previous: Optional[str], current: Optional[str]) -> Optional[str]:
    """How today's verdict relates to the last one on record.

    Kept here rather than in the dashboard because it is the unit of
    CALIBRATION: a verdict that oscillates strengthened/weakened day to
    day is noise, and a thesis that walked unchanged -> weakened ->
    broken over three weeks is the monitoring pass doing its job with
    lead time. Neither is visible from today's verdict alone.

    Returns None when there is nothing to compare — a first check, or
    either side ungradeable.
    """
    if previous is None or current is None:
        return None
    if previous == current:
        return "held"
    order = {"broken": 0, "weakened": 1, "unchanged": 2, "strengthened": 3}
    if order[current] > order[previous]:
        return "improved"
    return "deteriorated"
