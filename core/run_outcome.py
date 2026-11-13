"""
What a scheduled group ACTUALLY did, as opposed to what its status
column says it did.

WHY THIS EXISTS. `agent_runs.status` is a state machine with four
values — running, completed, failed, and absent — and three different
things have been recorded as `completed`:

  1. A run that did its work.
  2. A run that deliberately stood down because Solomon saw no reason
     to trade. Normal, and most days.
  3. A run that finished but could not do its job: the close group
     records `completed` when Otis raised, because it swallows
     per-agent failures so that one cannot stop the others; and the
     execution group records `completed` when it REFUSED to trade for
     want of a morning meeting.

`orchestrator status` and the dashboard printed the same word for all
three. That is how the close of 22 Oct looked like a success while the
books were never written, how Clara called that day "clean" from stale
data, and why `auto` — which skips anything `completed` — never
retried it, although the network was back within minutes.

The orchestrator's own comment already said the intent: "a completed
run with errors, not a failed one — but it must not read as a clean
day." This module is where it stops reading as one.

THE STATUS COLUMN IS NOT CHANGED. `completed` still means "the group
ran to the end", and the readers that depend on exactly that — the
execution gate that needs Solomon's verdict, the attempt counter —
keep working untouched. What is added is a second, derived reading of
the same row: its OUTCOME.

THIS MODULE IMPORTS NOTHING from the project and nothing third-party,
on purpose. The dashboard has to be able to draw a status without an
Anthropic key, a database driver or a broker client, and the logic is
simple enough to test with a plain dict.
"""

import re
from dataclasses import dataclass
from typing import Any, Optional

# The reason the execution group records when it refuses to trade
# because no completed Solomon run exists. Defined here, once, because
# the orchestrator writes it and this module has to recognise it — two
# copies of a string is how a refusal quietly stops being recognised.
NO_VERDICT = "no morning meeting on record"

# Skips that are the NORMAL outcome of a day with nothing to do. These
# are not problems and must not be coloured as one.
QUIET_REASONS = frozenset({
    "solomon: no action needed",
    "marcus: no actionable allocations",
})

# Outcome kinds, as stable strings other code can compare against.
NOT_RUN = "not_run"
RUNNING = "running"
CLEAN = "clean"
QUIET = "quiet"
PARTIAL = "partial"
REFUSED = "refused"
SKIPPED = "skipped"
FAILED = "failed"

# Secrets that have appeared in stored error text. A failed FMP call
# raises with the full request URL, and the URL carries the key — so an
# error message copied into a chat, a log or a screenshot would leak it.
# This is applied to everything DISPLAYED; it does not rewrite what is
# stored (that is a separate, deliberate change).
_SECRET_PARAM = re.compile(
    r"(?i)\b(api[_-]?key|apikey|access[_-]?token|token|secret|password)=[^&\s\"')]+")
_ANTHROPIC_KEY = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")


def redact(text: Any, limit: int = 160) -> str:
    """One short, single-line, secret-free rendering of an error.

    Takes the first line (a traceback's first line is the exception;
    the rest is frames), masks key-shaped values, and caps the length
    so one verbose error cannot push the schedule off the screen.
    """
    if text is None:
        return ""
    line = str(text).strip().splitlines()[0] if str(text).strip() else ""
    line = _SECRET_PARAM.sub(lambda m: f"{m.group(1)}=***", line)
    line = _ANTHROPIC_KEY.sub("sk-ant-***", line)
    if len(line) > limit:
        line = line[: limit - 1].rstrip() + "…"
    return line


@dataclass(frozen=True)
class GroupOutcome:
    """The derived reading of one group's row for one day."""

    kind: str
    detail: str = ""        # what went wrong, or why it stood down
    names: tuple = ()       # which members raised, for `partial`

    @property
    def retryable(self) -> bool:
        """Whether `auto` should run this group again even though its
        status says `completed`.

        `partial` — a member failed, and the thing that stopped it
        (a dropped connection, a rate limit) is usually gone by the
        next tick. `refused` — the group could not start for want of an
        input, and the input may have arrived since (a premarket run
        caught up by hand).

        A group that did its work, or deliberately stood down, is never
        retried: that is the whole reason `completed` stops `auto`.
        """
        return self.kind in (PARTIAL, REFUSED)

    @property
    def needs_attention(self) -> bool:
        """Whether a human should look. Quiet days do not."""
        return self.kind in (PARTIAL, REFUSED, FAILED, SKIPPED)

    @property
    def label(self) -> str:
        """What `orchestrator status` and the dashboard print."""
        if self.kind == PARTIAL:
            who = ", ".join(self.names)
            return f"completed WITH ERRORS ({who})" if who else "completed WITH ERRORS"
        if self.kind == REFUSED:
            return "REFUSED (no morning meeting)"
        if self.kind == QUIET:
            # "solomon: no action needed" -> "no action needed"
            return f"completed — {self.detail.split(': ', 1)[-1]}"
        if self.kind == SKIPPED:
            return "completed — skipped"
        return {
            NOT_RUN: "not run",
            RUNNING: "running",
            CLEAN: "completed",
            FAILED: "failed",
        }.get(self.kind, self.kind)


def classify_group_row(status: Optional[str], output: Optional[dict],
                       error: Optional[str]) -> GroupOutcome:
    """
    Turn a cycle row (status, raw_output summary, error_message) into
    what the group really did.

    Defensive about its inputs on purpose: it is called from a status
    command and a dashboard, and a status display that raises is worse
    than one that says less. An unrecognisable payload degrades to the
    plain reading of the status column.
    """
    if status is None:
        return GroupOutcome(NOT_RUN)
    if status == "running":
        return GroupOutcome(RUNNING)
    if status == "failed":
        return GroupOutcome(FAILED, detail=redact(error))
    if status != "completed":
        # An unknown status is shown as itself rather than guessed at.
        return GroupOutcome(status, detail=redact(error))

    # `completed`. A truncated payload is a text preview, not a
    # summary — read it as empty rather than parse it.
    summary = output if isinstance(output, dict) and not output.get("truncated") else {}
    errors = summary.get("errors")
    errors = errors if isinstance(errors, dict) else {}
    skipped = summary.get("skipped")
    skipped = skipped if isinstance(skipped, str) else None

    # The refusal is checked FIRST. It carries an `errors` entry too
    # (the gate's explanation), and read as a plain partial failure it
    # would be labelled with the name of a gate rather than the reason.
    if skipped == NO_VERDICT or "execution_gate" in errors:
        return GroupOutcome(REFUSED, detail=NO_VERDICT)

    if errors:
        names = tuple(sorted(str(k) for k in errors))
        detail = "; ".join(f"{k}: {redact(v, 110)}" for k, v in sorted(errors.items()))
        return GroupOutcome(PARTIAL, detail=detail, names=names)

    # `completed` with an error_message and no structured errors is a
    # row someone wrote by hand or an older version of the code wrote.
    # Better to surface it than to call it clean.
    if error and str(error).strip():
        return GroupOutcome(PARTIAL, detail=redact(error), names=())

    if skipped in QUIET_REASONS:
        return GroupOutcome(QUIET, detail=skipped)
    if skipped:
        return GroupOutcome(SKIPPED, detail=redact(skipped))
    return GroupOutcome(CLEAN)


def recorded_attempt(result: dict, attempt: int) -> int:
    """
    The attempt number to WRITE on the cycle row.

    A refusal does not use up an attempt. The cap exists to stop a
    broken group from burning model calls failing the same way every
    fifteen minutes; a refusal makes no model call, costs a database
    read, and may succeed on the very next tick if the morning meeting
    has since been caught up. Counting it would let three early-morning
    refusals lock execution out for the whole session.
    """
    if isinstance(result, dict) and result.get("skipped") == NO_VERDICT:
        return max(attempt - 1, 0)
    return attempt
