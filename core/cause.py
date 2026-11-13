"""
WHY a position moved — the label Vera was never required to give.

THE GAP. Over three weeks in November 2026 the desk logged NVDA as
`at_risk` for nine consecutive sessions. The `trigger` column read
`macro_conflict` on five of them and `fundamental_drift` on four, and the
reports of what actually moved the stock were: a sector-wide AI selloff
(2 Nov, 4 Nov), a policy shock after the midterms (5 Nov, the worst day),
and a company-specific governance investigation (9 Nov). The desk could
tell that NVDA had diverged from the market. It could not say WHY, and
had nowhere to write "I don't know" — so it wrote `fundamental_drift`
on days when no fundamental had been checked, because that was the
nearest word that justified an `at_risk` status.

A trigger names WHAT HAPPENED. This names WHO IT HAPPENED TO, which is
the question the investigate-before-reacting rule actually turns on:
a market-wide move is a correction, a company-specific one may be a
broken thesis, and "unknown" is a legitimate answer that currently has
no column to live in.

    market_wide       the broad index moved the same way, by a
                      comparable (beta-adjusted) amount
    sector_wide       the same link of the chain moved together while
                      the index did not
    company_specific  a named event — a filing, a result, guidance, a
                      dated news item — that the tools can point at
    unknown           none of the above could be confirmed. THE DEFAULT.

THIS MODULE IMPORTS NOTHING, so the vocabulary has one home that the
contract, the schema and the tests can all read.
"""

from typing import Optional

CAUSES = ("market_wide", "sector_wide", "company_specific", "unknown")
DEFAULT_CAUSE = "unknown"

# Triggers that assert something about the COMPANY's own state. A
# `fundamental_drift` trigger with a market-wide cause is a
# contradiction: either the fundamentals drifted, or the market moved,
# and a label that claims the first while the evidence says the second
# is exactly how nine sessions of "drift" were logged for a stock whose
# filings had not been touched.
COMPANY_LEVEL_TRIGGERS = frozenset({"fundamental_drift", "earnings", "news"})


def trigger_cause_mismatch(trigger: str, cause: str) -> Optional[str]:
    """A plain-language reason the pair does not hold together, or None.

    AUDIT, NOT OVERRIDE. This does not change the status Vera assigned
    and does not reject her output — a contract that fails a whole
    monitoring pass over a label would be a worse trade than the label.
    It names the contradiction so it can be logged, carried in her
    output and read by whoever grades her later.
    """
    if trigger in COMPANY_LEVEL_TRIGGERS and cause != "company_specific":
        return (f"trigger '{trigger}' asserts a company-level cause, but the "
                f"cause is '{cause}'")
    if trigger == "macro_conflict" and cause == "company_specific":
        return ("trigger 'macro_conflict' describes a divergence from the "
                "market, but the cause is 'company_specific'")
    return None
