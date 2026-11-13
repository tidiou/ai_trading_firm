"""
Honest statuses — what a scheduled group actually did.

THE DEFECT. `agent_runs.status` said `completed` for three different
things: a group that did its work, a group that stood down because there
was nothing to do, and a group that finished WITHOUT doing its job. The
close of 22 Oct 2026 was the third kind — Otis raised on a dropped DNS
lookup, the group swallowed it so Clara could still run, and the row
said `completed`. `orchestrator status` printed a success, `auto` never
retried it, and the ledger stayed frozen for the next twenty days.

Everything here is pure: dicts in, outcomes out. No database, no model.
"""

import pytest

from core.run_outcome import (
    CLEAN, FAILED, NOT_RUN, NO_VERDICT, PARTIAL, QUIET, REFUSED, RUNNING, SKIPPED,
    classify_group_row, recorded_attempt, redact,
)


# ===============================================================
# 1. the three kinds of `completed`
# ===============================================================
class TestTheThreeKindsOfCompleted:

    def test_a_group_that_did_its_work_is_clean(self):
        out = classify_group_row("completed", {"errors": {}, "skipped": None}, None)
        assert out.kind == CLEAN
        assert out.label == "completed"
        assert not out.needs_attention and not out.retryable

    def test_standing_down_because_there_is_nothing_to_do_is_not_a_problem(self):
        """Most days. It must not be coloured, flagged or retried."""
        for reason in ("solomon: no action needed", "marcus: no actionable allocations"):
            out = classify_group_row("completed", {"errors": {}, "skipped": reason}, None)
            assert out.kind == QUIET
            assert not out.needs_attention and not out.retryable

    def test_the_quiet_label_says_why(self):
        out = classify_group_row(
            "completed", {"skipped": "solomon: no action needed"}, None)
        assert out.label == "completed — no action needed"

    def test_a_member_that_raised_is_not_a_clean_day(self):
        """THE 22 OCT CASE, exactly as it was stored."""
        summary = {
            "group": "close", "attempt": 1, "skipped": None,
            "errors": {"otis": "ConnectionError: HTTPSConnectionPool("
                               "host='paper-api.alpaca.markets', port=443)"},
        }
        out = classify_group_row("completed", summary, "otis: ConnectionError: ...")
        assert out.kind == PARTIAL
        assert out.names == ("otis",)
        assert "ERRORS" in out.label and "otis" in out.label
        assert out.needs_attention and out.retryable

    def test_every_failed_member_is_named(self):
        out = classify_group_row(
            "completed", {"errors": {"otis": "boom", "clara": "bang"}}, None)
        assert out.names == ("clara", "otis")

    def test_a_refusal_to_trade_is_not_a_success(self):
        """The execution group refusing for want of a morning meeting
        was recorded `completed`, which read the same as having traded."""
        summary = {"skipped": NO_VERDICT,
                   "errors": {"execution_gate": "No completed Solomon run ..."}}
        out = classify_group_row("completed", summary, "execution_gate: ...")
        assert out.kind == REFUSED
        assert out.needs_attention and out.retryable

    def test_a_refusal_is_labelled_with_the_reason_not_the_gate(self):
        """It carries an `errors` entry as well, and read naively that
        would come out as a partial failure named after a gate."""
        summary = {"skipped": NO_VERDICT, "errors": {"execution_gate": "x"}}
        out = classify_group_row("completed", summary, None)
        assert out.kind == REFUSED and "morning meeting" in out.label

    def test_the_gate_error_alone_is_still_a_refusal(self):
        out = classify_group_row(
            "completed", {"errors": {"execution_gate": "x"}}, None)
        assert out.kind == REFUSED

    def test_an_unrecognised_skip_reason_is_surfaced_not_called_clean(self):
        out = classify_group_row("completed", {"skipped": "something new"}, None)
        assert out.kind == SKIPPED and out.needs_attention and not out.retryable

    def test_an_error_message_with_no_structured_errors_is_not_clean(self):
        out = classify_group_row("completed", {"errors": {}}, "something went wrong")
        assert out.kind == PARTIAL and out.detail == "something went wrong"


# ===============================================================
# 2. the other states pass straight through
# ===============================================================
class TestTheOtherStates:

    def test_no_row_means_not_run(self):
        assert classify_group_row(None, None, None).kind == NOT_RUN

    def test_running_is_running(self):
        assert classify_group_row("running", {"attempt": 1}, None).kind == RUNNING

    def test_failed_carries_its_error(self):
        out = classify_group_row("failed", {"attempt": 2}, "ValueError: nope\n\nTraceback...")
        assert out.kind == FAILED and out.detail == "ValueError: nope"
        assert out.needs_attention
        assert not out.retryable, \
            "a FAILED group is retried by the existing path; this must not double it"

    def test_an_unknown_status_is_shown_as_itself(self):
        assert classify_group_row("pending", None, None).kind == "pending"


# ===============================================================
# 3. it must never raise — it runs inside a status display
# ===============================================================
class TestDefensiveAboutItsInput:

    @pytest.mark.parametrize("payload", [
        None, [], "text", 42, {"errors": None}, {"errors": "string"},
        {"errors": [1, 2]}, {"skipped": 7}, {"truncated": True, "preview": "{...}"},
    ])
    def test_odd_payloads_degrade_to_the_plain_reading(self, payload):
        out = classify_group_row("completed", payload, None)
        assert out.kind in (CLEAN, SKIPPED, PARTIAL, QUIET, REFUSED)

    def test_a_truncated_payload_is_not_parsed(self):
        out = classify_group_row(
            "completed", {"truncated": True, "errors": {"otis": "x"}}, None)
        assert out.kind == CLEAN, \
            "a truncated payload is a text preview, not a summary — it is read as empty"


# ===============================================================
# 4. a retry must be possible for exactly the two outcomes that warrant it
# ===============================================================
class TestWhatIsRetryable:

    @pytest.mark.parametrize("summary,expected", [
        ({"errors": {}, "skipped": None}, False),
        ({"skipped": "solomon: no action needed"}, False),
        ({"skipped": "marcus: no actionable allocations"}, False),
        ({"errors": {"otis": "x"}}, True),
        ({"skipped": NO_VERDICT, "errors": {"execution_gate": "x"}}, True),
    ])
    def test_retryable(self, summary, expected):
        assert classify_group_row("completed", summary, None).retryable is expected


# ===============================================================
# 5. a refusal does not use up an attempt
# ===============================================================
class TestRefusalsAreFree:
    """
    The cap exists to stop a broken group burning model calls failing
    the same way every fifteen minutes. A refusal makes no model call.
    Counted, three refusals before 10:15 would lock execution out for the
    rest of the session even if the morning meeting were caught up at
    10:20.
    """

    def test_a_refusal_does_not_consume_an_attempt(self):
        assert recorded_attempt({"skipped": NO_VERDICT}, 3) == 2

    def test_everything_else_does(self):
        assert recorded_attempt({"skipped": None}, 3) == 3
        assert recorded_attempt({"skipped": "solomon: no action needed"}, 3) == 3
        assert recorded_attempt({"errors": {"otis": "x"}}, 2) == 2

    def test_it_never_goes_negative(self):
        assert recorded_attempt({"skipped": NO_VERDICT}, 0) == 0

    def test_a_non_dict_result_is_counted(self):
        assert recorded_attempt(None, 1) == 1


# ===============================================================
# 6. what is shown must not carry a key
# ===============================================================
class TestRedaction:
    """The 29 Sept error was stored with the full request URL, key
    included. Anything displayed from stored error text goes through
    this, because a screenshot of a status line is how keys leak."""

    def test_an_api_key_in_a_url_is_masked(self):
        text = ("ConnectionError: HTTPSConnectionPool(host='financialmodelingprep.com'): "
                "url: /stable/quote?symbol=%5EGSPC&apikey=cbySkv10s7kEqOHOHSTZHWNdrI5FLqMb")
        out = redact(text)
        assert "cbySkv10s7k" not in out
        assert "apikey=***" in out

    def test_other_key_shaped_params_are_masked_too(self):
        assert "hunter2" not in redact("failed: password=hunter2&x=1")
        assert "abc123" not in redact("GET /x?api_key=abc123")
        assert "abc123" not in redact("GET /x?token=abc123")

    def test_an_anthropic_key_is_masked(self):
        assert "sk-ant-api03" not in redact("auth failed for sk-ant-api03-AbC_dEf")

    def test_only_the_first_line_is_kept(self):
        out = redact("ValueError: boom\n\nTraceback (most recent call last):\n  File ...")
        assert out == "ValueError: boom"

    def test_it_is_capped(self):
        assert len(redact("x" * 1000, limit=50)) <= 50

    def test_none_and_empty_are_empty(self):
        assert redact(None) == "" and redact("") == "" and redact("   ") == ""

    def test_a_partial_outcomes_detail_is_redacted_too(self):
        out = classify_group_row(
            "completed", {"errors": {"atlas": "GET /q?apikey=SECRETVALUE123 timed out"}}, None)
        assert "SECRETVALUE123" not in out.detail
