"""
The validation repair round, and the truncation detector.

WHY THIS IS WORTH TESTING CAREFULLY. Every agent's output contract is
a hard schema on purpose — bad data must never reach the database.
But until now a validation failure was a SINGLE-SHOT KILL: one
off-contract response and the cycle group died. As the contracts grew
(Atlas now has four structural rules about tickers alone) the chance
of a near-miss on any given morning stopped being negligible.

The repair round is the fix, and it has three properties that all
have to hold at once, because getting any of them wrong turns a loud
failure into a quiet one:

  1. A repair must NOT soften the contract. The same validator runs
     on the corrected answer.
  2. A repair must NOT spend the FMP quota again. The corrective turn
     carries no tools, so the agent loop cannot re-run — on a 250/day
     free tier a retry that silently doubles the bill is its own
     outage.
  3. A repair must be LOUD. A contract the model fails every morning
     is a prompt bug, and a silent retry would hide exactly that
     while appearing to work.

The Anthropic client is stubbed, so nothing here makes a network call
and a bug in these tests cannot spend money or quota.
"""

import logging

import pytest
from pydantic import BaseModel, ValidationError, field_validator

import core.clients.claude_client as cc


# ---------------------------------------------------------------
# a stub Anthropic client that returns scripted responses
# ---------------------------------------------------------------
class Block:
    def __init__(self, text=None, name=None, input=None, id=None):
        self.type = "text" if text is not None else "tool_use"
        self.text = text
        self.name = name
        self.input = input or {}
        self.id = id or "tu-1"


class Response:
    def __init__(self, blocks, stop_reason="end_turn"):
        self.content = blocks
        self.stop_reason = stop_reason


class StubMessages:
    """Replays a script of responses and records every call it got."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("the stub ran out of scripted responses — "
                                 "the code under test made more calls than "
                                 "this test expected")
        return self.script.pop(0)


class StubClient:
    def __init__(self, script):
        self.messages = StubMessages(script)


@pytest.fixture
def stub(monkeypatch):
    def install(script):
        c = StubClient(script)
        monkeypatch.setattr(cc, "client", c)
        return c
    return install


def text(payload, stop_reason="end_turn"):
    return Response([Block(text=payload)], stop_reason=stop_reason)


# ---------------------------------------------------------------
# a contract with the same shape as a real agent's
# ---------------------------------------------------------------
class Brief(BaseModel):
    signal: str
    narrative: str

    @field_validator("narrative")
    @classmethod
    def _no_tickers(cls, v):
        if "NVDA" in v:
            raise ValueError("ticker NVDA in the narrative; tickers belong in "
                             "read_across")
        return v


GOOD = '{"signal": "neutral", "narrative": "Breadth narrowed."}'
BAD = '{"signal": "neutral", "narrative": "NVDA led the move."}'
STILL_BAD = '{"signal": "neutral", "narrative": "NVDA again."}'


def run(**over):
    kwargs = dict(system_prompt="SYS", user_prompt="do the brief",
                  tools=[], tool_executor=lambda n, i: {},
                  validate=Brief.model_validate, agent_name="Atlas")
    kwargs.update(over)
    return cc.run_validated_agent_loop(**kwargs)


# ===============================================================
# The ordinary case — nothing to repair
# ===============================================================
class TestNoRepairNeeded:

    def test_a_valid_answer_returns_the_validated_object(self, stub):
        c = stub([text(GOOD)])
        out = run()
        assert isinstance(out, Brief)
        assert out.narrative == "Breadth narrowed."

    def test_and_makes_exactly_one_api_call(self, stub):
        c = stub([text(GOOD)])
        run()
        assert len(c.messages.calls) == 1, "a passing answer must not be retried"

    def test_nothing_is_logged_when_nothing_went_wrong(self, stub, caplog):
        stub([text(GOOD)])
        with caplog.at_level(logging.WARNING):
            run()
        assert caplog.text == ""


# ===============================================================
# The repair
# ===============================================================
class TestOneRepairRound:

    def test_a_near_miss_is_corrected_rather_than_fatal(self, stub):
        # THE CASE THIS WHOLE MECHANISM EXISTS FOR: a ticker written
        # into the narrative out of habit. Before the repair round
        # this killed the entire premarket group.
        stub([text(BAD), text(GOOD)])
        out = run()
        assert out.narrative == "Breadth narrowed."

    def test_the_repair_turn_is_told_what_it_broke(self, stub):
        c = stub([text(BAD), text(GOOD)])
        run()
        repair = c.messages.calls[1]
        sent = repair["messages"][-1]["content"]
        assert "ticker NVDA in the narrative" in sent, \
            "the validator's own message must reach the model — that is why " \
            "the agents' errors name what IS permitted"
        assert "REJECTED" in sent
        assert "was not stored" in sent

    def test_the_repair_turn_carries_the_rejected_answer_for_context(self, stub):
        c = stub([text(BAD), text(GOOD)])
        run()
        roles = [m["role"] for m in c.messages.calls[1]["messages"]]
        assert roles == ["user", "assistant", "user"]
        assert c.messages.calls[1]["messages"][1]["content"] == BAD

    def test_the_repair_turn_keeps_the_same_system_prompt(self, stub):
        # The rules have to still be in front of the model, or it
        # fixes the named error and breaks a different rule.
        c = stub([text(BAD), text(GOOD)])
        run()
        assert c.messages.calls[1]["system"] == "SYS"

    def test_THE_REPAIR_TURN_HAS_NO_TOOLS(self, stub):
        # Load-bearing. With tools attached the agent loop could run
        # again and spend the FMP daily quota a second time for an
        # answer it already has in context.
        c = stub([text(BAD), text(GOOD)])
        run()
        assert "tools" not in c.messages.calls[1] or not c.messages.calls[1]["tools"]

    def test_the_tool_executor_is_not_invoked_during_a_repair(self, stub):
        calls = []
        stub([text(BAD), text(GOOD)])
        run(tool_executor=lambda n, i: calls.append(n) or {})
        assert calls == []

    def test_a_repair_is_logged_loudly(self, stub, caplog):
        # A contract the model fails every morning is a prompt bug. A
        # quiet retry would hide it behind a green run.
        stub([text(BAD), text(GOOD)])
        with caplog.at_level(logging.WARNING):
            run()
        assert "Atlas" in caplog.text
        assert "repaired on retry" in caplog.text
        assert "a repair is a defect, not a" in caplog.text

    def test_the_contract_is_not_softened_by_the_retry(self, stub):
        # The same validator runs on the corrected answer. A repair
        # that accepted anything would be worse than no repair.
        stub([text(BAD), text(STILL_BAD)])
        with pytest.raises(RuntimeError):
            run()


# ===============================================================
# Two failures
# ===============================================================
class TestSecondFailureRaises:

    def test_it_raises_rather_than_retrying_forever(self, stub):
        c = stub([text(BAD), text(STILL_BAD)])
        with pytest.raises(RuntimeError):
            run()
        assert len(c.messages.calls) == 2, "exactly one repair, never a loop"

    def test_the_error_carries_both_attempts(self, stub):
        # Needed to tell "the model misunderstood once" from "the
        # contract cannot be satisfied as written".
        stub([text(BAD), text(STILL_BAD)])
        with pytest.raises(RuntimeError) as e:
            run()
        msg = str(e.value)
        assert "FIRST attempt" in msg
        assert "AFTER being told the error" in msg
        assert "failed its output contract twice" in msg

    def test_the_error_points_at_the_prompt_not_the_model(self, stub):
        stub([text(BAD), text(STILL_BAD)])
        with pytest.raises(RuntimeError) as e:
            run()
        assert "check the prompt" in str(e.value)

    def test_the_agent_is_named_so_the_log_says_who(self, stub):
        stub([text(BAD), text(STILL_BAD)])
        with pytest.raises(RuntimeError) as e:
            run(agent_name="Vera")
        assert "Vera" in str(e.value)


# ===============================================================
# Malformed JSON goes down the same path
# ===============================================================
class TestMalformedJson:

    def test_unparseable_output_gets_a_repair_round_too(self, stub):
        # A truncated or prose-wrapped answer is the same class of
        # problem as a rule violation: recoverable by being told.
        stub([text("I think the regime is neutral, honestly."), text(GOOD)])
        assert run().signal == "neutral"

    def test_a_fenced_block_never_needed_repairing(self, stub):
        # extract_json already strips fences. Worth pinning so a
        # future change to the repair path does not start burning a
        # round on something already handled.
        c = stub([text("```json\n" + GOOD + "\n```")])
        run()
        assert len(c.messages.calls) == 1


# ===============================================================
# Truncation, named rather than guessed at
# ===============================================================
class TestTruncationDetector:

    def test_a_cut_off_response_says_so(self, stub):
        # Before this, truncation surfaced as "No JSON array or object
        # found" — which reads as a model ignoring instructions and
        # sends you to reword the prompt instead of raising the
        # budget. The wrong investigation, every time.
        stub([text('{"signal": "neu', stop_reason="max_tokens")])
        with pytest.raises(RuntimeError) as e:
            run()
        assert "max_tokens problem, NOT a" in str(e.value)

    def test_the_error_names_the_limit_that_was_hit(self, stub):
        stub([text("{", stop_reason="max_tokens")])
        with pytest.raises(RuntimeError) as e:
            run(max_tokens=3000)
        assert "3000-token limit" in str(e.value)

    def test_truncation_is_not_repaired(self, stub):
        # A repair at the same budget would truncate again. Raising is
        # the correct response.
        c = stub([text("{", stop_reason="max_tokens")])
        with pytest.raises(RuntimeError):
            run()
        assert len(c.messages.calls) == 1

    def test_a_truncated_repair_also_says_so(self, stub):
        stub([text(BAD), text("{", stop_reason="max_tokens")])
        with pytest.raises(RuntimeError) as e:
            run()
        assert "correction attempt hit the" in str(e.value)


# ===============================================================
# The tool loop still works as it did
# ===============================================================
class TestToolLoopUnchanged:

    def test_tools_are_still_executed_on_the_first_pass(self, stub):
        executed = []
        c = stub([
            Response([Block(name="get_index_quotes", id="t1")], stop_reason="tool_use"),
            text(GOOD),
        ])
        out = run(tools=[{"name": "get_index_quotes"}],
                  tool_executor=lambda n, i: executed.append(n) or {"vix": 14.2})
        assert executed == ["get_index_quotes"]
        assert out.signal == "neutral"

    def test_a_tool_pass_followed_by_a_bad_answer_still_repairs(self, stub):
        # The realistic Atlas failure: he calls both tools, then writes
        # a ticker into the narrative.
        c = stub([
            Response([Block(name="get_bellwether_quotes", id="t1")], stop_reason="tool_use"),
            text(BAD),
            text(GOOD),
        ])
        out = run(tools=[{"name": "get_bellwether_quotes"}],
                  tool_executor=lambda n, i: [{"ticker": "NVDA", "move_pct": -7.0}])
        assert out.narrative == "Breadth narrowed."
        # three calls: tool pass, bad answer, repair — and the repair
        # is the only one without tools attached
        assert len(c.messages.calls) == 3
        assert not c.messages.calls[2].get("tools")
