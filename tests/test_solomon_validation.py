"""
E5 (the validation layer) and E4 (the corrupted restraint rule).

THE THROUGH-LINE. Solomon is the only agent whose output crosses from
analysis into instruction — Atlas describes the weather, Vera describes
companies, and a defect in either produces a bad opinion. A defect in
Solomon produces an ORDER. His Pydantic contract already stops him
inventing a field; it cannot stop him inventing a ticker, because
"NVDA" is a perfectly valid string. Nora, the next gate, checks SIZE
and not PROVENANCE — she would approve 8% of a name that exists nowhere
in the firm's research, and Ada would place it.

These tests exercise the deterministic layer that closes that gap. No
database, no broker, no model: validate_proposals() is pure, which is
the point. A check that itself depends on an LLM is not a check.
"""

from datetime import date

import pytest

from agents import solomon
from agents.solomon import ProposalOutput, SOLOMON_SYSTEM_PROMPT, validate_proposals


# ---------------------------------------------------------------
# builders — Vera's real output shapes
# ---------------------------------------------------------------
def monitoring(ticker="AAPL", status="intact", trigger="none", conviction=4):
    return {"ticker": ticker, "status": status, "trigger": trigger,
            "reasoning": "r", "conviction_score": conviction}


def candidate(ticker="MSFT", conviction=4, catalyst="Q3 margin inflection"):
    return {"ticker": ticker, "thesis": "t", "catalyst": catalyst,
            "conviction_score": conviction, "key_risks": [], "valuation_snapshot": {}}


def orphan(ticker="TSLA", recommendation="exit"):
    return {"ticker": ticker, "recommendation": recommendation, "reasoning": "r",
            "thesis": None, "catalyst": None, "conviction_score": None}


def vera(monitoring_rows=(), candidates=(), orphans=()):
    return {"monitoring": list(monitoring_rows),
            "candidates": list(candidates),
            "orphan_reviews": list(orphans)}


def proposal(ticker="AAPL", action="new_position", trigger=None, urgency="this_week"):
    return ProposalOutput(
        ticker=ticker,
        action=action,
        linked_trigger=trigger if trigger is not None else f"vera:signal:{ticker}",
        rationale="because",
        urgency=urgency,
    )


def only_reason(kept, rejected):
    assert len(rejected) == 1, f"expected exactly one rejection, got {rejected}"
    assert kept == []
    return rejected[0]["rejection_reason"]


# ===============================================================
# E4 — the corrupted restraint rule
# ===============================================================
class TestPromptIntegrity:
    """
    The rule that was broken is Solomon's most important one, and it
    broke in the worst possible way: not by disappearing, but by ending
    mid-clause while its tail sat stranded at the end of an unrelated
    bullet eleven lines below. The model read a truncated instruction
    and behaved plausibly, which is why nothing surfaced it.
    """

    def test_the_at_risk_rule_is_a_complete_sentence(self):
        assert ('A position sitting at "at_risk" with no new information\n'
                '  since a prior day is NOT sufficient on its own to act.'
                in SOLOMON_SYSTEM_PROMPT), \
            "the at_risk restraint rule is truncated again"

    def test_the_stranded_tail_is_not_left_on_the_sustained_gain_rule(self):
        """The orphaned fragment used to terminate the profit_target
        bullet, where it read as a non-sequitur about prior days."""
        i = SOLOMON_SYSTEM_PROMPT.index("profit_target_sustained")
        j = SOLOMON_SYSTEM_PROMPT.index('- A "new_position" proposal')
        assert "since a prior day is NOT sufficient" not in SOLOMON_SYSTEM_PROMPT[i:j]

    def test_every_hard_rule_bullet_terminates(self):
        """A cheap structural guard against the same class of edit
        damage: no bullet in the hard-rules block may end without
        punctuation."""
        start = SOLOMON_SYSTEM_PROMPT.index("Hard rules")
        end = SOLOMON_SYSTEM_PROMPT.index("EVERY RULE ABOVE")
        block = SOLOMON_SYSTEM_PROMPT[start:end]

        bullets, current = [], None
        for line in block.splitlines():
            if line.startswith("- "):
                if current:
                    bullets.append(current)
                current = line[2:].strip()
            elif line.startswith("  ") and current is not None:
                current += " " + line.strip()
        if current:
            bullets.append(current)

        assert len(bullets) >= 8, f"expected the full rule set, found {len(bullets)}"
        for b in bullets:
            assert b.rstrip().endswith((".", '."')), f"unterminated rule: {b[:70]}..."


# ===============================================================
# E5 — provenance
# ===============================================================
class TestProvenance:

    def test_a_ticker_vera_never_mentioned_is_rejected(self):
        """THE DEFECT THIS EXISTS FOR. Nothing downstream would have
        caught it: Nora checks size, and 8% of a hallucination is a
        perfectly compliant 8%."""
        kept, rejected = validate_proposals(
            [proposal("NVDA", "new_position", "vera:new_candidate:NVDA")],
            vera(candidates=[candidate("MSFT")]),
            held=set(),
        )
        assert "appears nowhere in Vera's output" in only_reason(kept, rejected)

    def test_a_proposal_with_no_linked_trigger_is_rejected(self):
        kept, rejected = validate_proposals(
            [proposal("MSFT", "new_position", trigger="")],
            vera(candidates=[candidate("MSFT")]),
            held=set(),
        )
        assert "no linked_trigger" in only_reason(kept, rejected)

    def test_a_trigger_that_does_not_name_its_ticker_is_rejected(self):
        """Guards the vague-justification case the manual names. A
        trigger that cannot be traced back to a source is decoration."""
        kept, rejected = validate_proposals(
            [proposal("MSFT", "new_position", trigger="vera:looked_good")],
            vera(candidates=[candidate("MSFT")]),
            held=set(),
        )
        assert "does not name the ticker" in only_reason(kept, rejected)

    def test_tickers_are_matched_case_insensitively(self):
        kept, _ = validate_proposals(
            [proposal("msft", "new_position", "vera:new_candidate:msft")],
            vera(candidates=[candidate("MSFT")]),
            held=set(),
        )
        assert len(kept) == 1, "case should not decide whether a trade happens"


# ===============================================================
# E5 — new_position
# ===============================================================
class TestNewPosition:

    def test_conviction_4_with_a_catalyst_is_approved(self):
        kept, rejected = validate_proposals(
            [proposal("MSFT", "new_position", "vera:new_candidate:MSFT")],
            vera(candidates=[candidate("MSFT", conviction=4)]),
            held=set(),
        )
        assert len(kept) == 1 and rejected == []

    @pytest.mark.parametrize("conviction", [1, 2, 3])
    def test_conviction_below_4_is_rejected(self, conviction):
        kept, rejected = validate_proposals(
            [proposal("MSFT", "new_position", "vera:new_candidate:MSFT")],
            vera(candidates=[candidate("MSFT", conviction=conviction)]),
            held=set(),
        )
        assert "below the 4 bar" in only_reason(kept, rejected)

    def test_a_candidate_with_no_catalyst_is_rejected(self):
        """'Looks interesting' is not a trigger — the manual's words."""
        kept, rejected = validate_proposals(
            [proposal("MSFT", "new_position", "vera:new_candidate:MSFT")],
            vera(candidates=[candidate("MSFT", catalyst="   ")]),
            held=set(),
        )
        assert "no named catalyst" in only_reason(kept, rejected)

    def test_new_position_on_a_name_already_held_is_rejected(self):
        kept, rejected = validate_proposals(
            [proposal("MSFT", "new_position", "vera:new_candidate:MSFT")],
            vera(candidates=[candidate("MSFT")]),
            held={"MSFT"},
        )
        assert "already held" in only_reason(kept, rejected)

    def test_a_monitored_name_is_not_a_candidate(self):
        """Being written about is not the same as being recommended.
        Monitoring establishes provenance; it does not supply a
        conviction score or a catalyst."""
        kept, rejected = validate_proposals(
            [proposal("AAPL", "new_position", "vera:new_candidate:AAPL")],
            vera(monitoring_rows=[monitoring("AAPL")]),
            held=set(),
        )
        assert "not among Vera's candidates" in only_reason(kept, rejected)


# ===============================================================
# E5 — trim and exit, which is where the repaired E4 rule lands
# ===============================================================
class TestTrimAndExit:

    def test_broken_thesis_permits_an_exit(self):
        kept, rejected = validate_proposals(
            [proposal("AAPL", "exit", "vera:thesis_broken:AAPL")],
            vera(monitoring_rows=[monitoring("AAPL", status="broken", trigger="news")]),
            held={"AAPL"},
        )
        assert len(kept) == 1 and rejected == []

    def test_at_risk_with_a_named_trigger_permits_a_trim(self):
        kept, rejected = validate_proposals(
            [proposal("AAPL", "trim", "vera:at_risk:AAPL")],
            vera(monitoring_rows=[monitoring("AAPL", status="at_risk",
                                             trigger="fundamental_drift")]),
            held={"AAPL"},
        )
        assert len(kept) == 1 and rejected == []

    def test_at_risk_with_no_new_information_is_refused(self):
        """
        E4 AND E5 MEETING IN ONE ASSERTION. This is the exact case the
        truncated sentence was supposed to forbid — and forbidding it in
        the prompt alone would only ever have been a request. Here it is
        a refusal.
        """
        kept, rejected = validate_proposals(
            [proposal("AAPL", "trim", "vera:at_risk:AAPL")],
            vera(monitoring_rows=[monitoring("AAPL", status="at_risk", trigger="none")]),
            held={"AAPL"},
        )
        assert "no newly named deteriorating trigger" in only_reason(kept, rejected)

    def test_an_intact_thesis_does_not_permit_a_trim(self):
        kept, rejected = validate_proposals(
            [proposal("AAPL", "trim", "vera:intact:AAPL")],
            vera(monitoring_rows=[monitoring("AAPL", status="intact", trigger="none")]),
            held={"AAPL"},
        )
        assert "requires 'broken'" in only_reason(kept, rejected)

    def test_a_sustained_gain_permits_a_trim(self):
        kept, rejected = validate_proposals(
            [proposal("AAPL", "trim", "vera:profit_target_sustained:AAPL")],
            vera(monitoring_rows=[monitoring("AAPL", status="intact",
                                             trigger="profit_target_sustained")]),
            held={"AAPL"},
        )
        assert len(kept) == 1 and rejected == []

    def test_a_sustained_gain_does_not_permit_a_full_exit(self):
        """The manual calls it a valid TRIM escalation. Taking something
        off the table after a run is not the same as closing a position
        whose thesis is intact."""
        kept, rejected = validate_proposals(
            [proposal("AAPL", "exit", "vera:profit_target_sustained:AAPL")],
            vera(monitoring_rows=[monitoring("AAPL", status="intact",
                                             trigger="profit_target_sustained")]),
            held={"AAPL"},
        )
        assert "justifies a trim, not a full exit" in only_reason(kept, rejected)

    def test_an_undocumented_position_marked_exit_stands_on_its_own(self):
        """Nothing was ever documented to change FROM, so the usual
        'what changed' test cannot apply to it."""
        kept, rejected = validate_proposals(
            [proposal("TSLA", "exit", "vera:orphan_review:TSLA")],
            vera(orphans=[orphan("TSLA", "exit")]),
            held={"TSLA"},
        )
        assert len(kept) == 1 and rejected == []

    def test_an_undocumented_position_marked_adopt_does_not(self):
        kept, rejected = validate_proposals(
            [proposal("TSLA", "exit", "vera:orphan_review:TSLA")],
            vera(orphans=[orphan("TSLA", "adopt")]),
            held={"TSLA"},
        )
        assert "no monitoring entry" in only_reason(kept, rejected)

    def test_exit_on_a_name_not_held_is_rejected(self):
        kept, rejected = validate_proposals(
            [proposal("AAPL", "exit", "vera:thesis_broken:AAPL")],
            vera(monitoring_rows=[monitoring("AAPL", status="broken", trigger="news")]),
            held=set(),
        )
        assert "not currently held" in only_reason(kept, rejected)


# ===============================================================
# E5 — add
# ===============================================================
class TestAdd:

    def test_add_to_an_intact_monitored_position_is_approved(self):
        kept, rejected = validate_proposals(
            [proposal("AAPL", "add", "vera:intact:AAPL")],
            vera(monitoring_rows=[monitoring("AAPL", status="intact")]),
            held={"AAPL"},
        )
        assert len(kept) == 1 and rejected == []

    @pytest.mark.parametrize("status", ["at_risk", "broken"])
    def test_add_to_a_deteriorating_thesis_is_refused(self, status):
        """Mirrors Marcus's existing guardrail one step earlier in the
        chain, so the desk refuses at the point of decision rather than
        at the point of sizing. A deliberate extension of the manual,
        not a transcribed rule."""
        kept, rejected = validate_proposals(
            [proposal("AAPL", "add", "vera:signal:AAPL")],
            vera(monitoring_rows=[monitoring("AAPL", status=status, trigger="news")]),
            held={"AAPL"},
        )
        assert "does not add to a deteriorating thesis" in only_reason(kept, rejected)


# ===============================================================
# Mixed batches, and what survives
# ===============================================================
class TestBatchBehaviour:

    def test_a_bad_proposal_does_not_take_the_good_ones_with_it(self):
        kept, rejected = validate_proposals(
            [
                proposal("MSFT", "new_position", "vera:new_candidate:MSFT"),
                proposal("NVDA", "new_position", "vera:new_candidate:NVDA"),
                proposal("AAPL", "exit", "vera:thesis_broken:AAPL"),
            ],
            vera(monitoring_rows=[monitoring("AAPL", status="broken", trigger="news")],
                 candidates=[candidate("MSFT")]),
            held={"AAPL"},
        )
        assert {p.ticker for p in kept} == {"MSFT", "AAPL"}
        assert len(rejected) == 1 and rejected[0]["ticker"] == "NVDA"

    def test_a_rejection_carries_the_whole_proposal_not_just_a_flag(self):
        """It lands in agent_runs.raw_output. A reason with no proposal
        attached is unreadable a week later."""
        _, rejected = validate_proposals(
            [proposal("NVDA", "new_position", "vera:new_candidate:NVDA")],
            vera(), held=set(),
        )
        r = rejected[0]
        for field in ("ticker", "action", "linked_trigger", "rationale",
                      "urgency", "rejection_reason"):
            assert field in r, f"{field} missing from the rejection record"

    def test_empty_vera_output_rejects_everything(self):
        """The degraded-run case. If Vera produced nothing, there is no
        universe, and Solomon has none of his own."""
        kept, rejected = validate_proposals(
            [proposal("AAPL", "exit", "vera:thesis_broken:AAPL"),
             proposal("MSFT", "new_position", "vera:new_candidate:MSFT")],
            {}, held={"AAPL"},
        )
        assert kept == [] and len(rejected) == 2

    def test_no_proposals_is_not_an_error(self):
        assert validate_proposals([], vera(), set()) == ([], [])
