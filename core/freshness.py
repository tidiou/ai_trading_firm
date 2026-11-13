"""
How old is the portfolio data an agent is reasoning from?

THE GAP. From 2 to 10 Nov 2026 every NVDA monitoring row cited the same
loss, -10.84%, and on 6 Nov Vera concluded that "the entire move was
already in place as of 30 Oct" and so there was no new deterioration. The
figure was frozen: the books had not closed since 30 Oct, and by 10 Nov
the real loss was 26%. Solomon repeated it as a fresh trigger on four
different days. Neither agent was ever told how old its numbers were, so
a stale figure read as a stable one — and "nothing has changed" was
true of the data and false of the stock.

The ledger already knows its own age (`LedgerSnapshot.staleness_sessions`,
used by Ada to refuse stale sizing). This turns that into one line the
LLM agents are shown, and a structured record of it in their output so
it can be audited afterwards.

THIS MODULE IMPORTS NOTHING, and `core.ledger` takes its tolerance from
here, so there is one definition of "stale" rather than two that can
drift: what Ada refuses to size against is exactly what Vera and
Solomon are told to distrust.
"""

from datetime import date
from typing import Optional

# A normal cycle reads yesterday's close: one session. Two or more means
# the close did not run — a failed cycle, a sleeping laptop, a lost DNS
# lookup — and every figure derived from the books has moved on.
STALE_AFTER_SESSIONS = 1


def freshness_stamp(as_of: Optional[date], sessions_old: Optional[int],
                    bootstrap: bool = False, unreadable: bool = False) -> dict:
    """
    One reading of the ledger's age.

    Returns {"as_of", "sessions_old", "stale", "line"}. `stale` is None
    when the age could not be determined — which is NOT the same as
    False, and callers should not collapse the two.
    """
    if unreadable:
        return {"as_of": None, "sessions_old": None, "stale": None,
                "line": ("could not be determined — treat every portfolio "
                         "figure below as unverified.")}
    if bootstrap or as_of is None:
        return {"as_of": None, "sessions_old": None, "stale": False,
                "line": "the ledger has never been written — no portfolio figures exist yet."}

    n = int(sessions_old or 0)
    when = as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of)
    if n > STALE_AFTER_SESSIONS:
        return {"as_of": when, "sessions_old": n, "stale": True,
                "line": (f"as of {when} — {n} sessions old. STALE: every P&L "
                         f"figure, weight and loss percentage below was "
                         f"computed at that close, not at the most recent one.")}
    if n == 0:
        return {"as_of": when, "sessions_old": 0, "stale": False,
                "line": f"as of {when} — current."}
    return {"as_of": when, "sessions_old": n, "stale": False,
            "line": (f"as of {when} — {n} session old. Normal: the book is "
                     f"always one close behind.")}
