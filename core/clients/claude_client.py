"""
Claude API wrapper — implements the agentic tool-use LOOP shared by
every agent in the firm, not just Atlas.

THE CORE IDEA: this is the difference between "an LLM call with some
pre-fetched context" and "an agent." We don't decide in advance which
tools get called or in what order — we hand Claude the tool
definitions and let IT decide, iteratively:

    1. Send Claude the system prompt, the task, and the available tools
    2. Claude either calls a tool, or gives a final answer
    3. If it called a tool, we execute it (real code, not an LLM)
       and hand the result back to Claude
    4. Repeat until Claude has enough information and stops calling
       tools — that's when we treat its response as final

That loop — perceive tool results, decide, act, repeat — IS the
agentic behavior. Everything else in this codebase (Atlas's system
prompt, his specific tools) is just configuring this loop for his
particular job.
"""

import json
import logging
import os
from typing import Optional

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# The Anthropic SDK has NO default timeout unless one is set here —
# without this, a stalled/hung request (bad network, an unusual API
# stall) blocks forever with no error, no retry, nothing. This is
# what actually happened during real testing: orchestrator.py sat
# silently for 10+ minutes with no output and wouldn't even respond
# to Ctrl+C cleanly. 90s is generous enough for a normal multi-tool-
# call reasoning pass, but bounded — a real hang now fails loudly
# instead of hanging indefinitely.
API_TIMEOUT_SECONDS = 90.0

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], timeout=API_TIMEOUT_SECONDS)

DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_TOOL_ITERATIONS = 12  # raised from Atlas's original 6 — Vera may check 2 tools across many tickers


def run_agent_loop(
    system_prompt: str,
    user_prompt: str,
    tools: list[dict],
    tool_executor,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 2000,
) -> str:
    """
    Runs the tool-use loop described above and returns Claude's final
    TEXT response (once it stops calling tools).

    tool_executor: a function (tool_name: str, tool_input: dict) -> dict
    that actually performs the tool call — e.g. hitting the FMP API.
    Deliberately passed in rather than hardcoded, so this loop is
    reusable by Vera, Solomon, etc. later, each with their own tools.

    max_tokens is deliberately modest (2000) — Atlas's job is a
    focused daily read, not an essay. Keeping this low is also a
    cost/latency control: this loop runs unattended every trading day.
    """
    messages = [{"role": "user", "content": user_prompt}]

    for iteration in range(MAX_TOOL_ITERATIONS):
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "max_tokens":
            # TRUNCATION DOES NOT LOOK LIKE TRUNCATION. A response cut
            # off mid-JSON reaches extract_json as malformed text, and
            # the error that surfaces is "No JSON array or object
            # found" — which reads like a model that ignored its
            # instructions rather than a budget that was too small.
            # Naming it here saves the wrong investigation, and this
            # got more likely the moment agents started returning
            # structured arrays rather than a few short strings.
            raise RuntimeError(
                f"Claude's response hit the {max_tokens}-token limit and was cut "
                f"off before it finished. This is a max_tokens problem, NOT a "
                f"formatting one — raise max_tokens for this agent rather than "
                f"rewording its prompt.")

        if response.stop_reason != "tool_use":
            # Claude decided it has enough — this is the final answer.
            text_blocks = [b.text for b in response.content if b.type == "text"]
            return "".join(text_blocks)

        # Claude wants to call one or more tools before continuing.
        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = tool_executor(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                })
        messages.append({"role": "user", "content": tool_results})

    # Safety valve: if we've looped MAX_TOOL_ITERATIONS times and Claude
    # is STILL calling tools, something is wrong (a bad tool result
    # confusing it, or a genuinely unanswerable request) — fail loudly
    # rather than looping silently forever and burning tokens.
    raise RuntimeError(
        f"Agent loop exceeded {MAX_TOOL_ITERATIONS} tool-use iterations without a final answer."
    )


# =================================================================
# VALIDATION REPAIR
#
# WHY THIS EXISTS. Every agent's output contract is a hard Pydantic
# schema, deliberately: bad data must never reach the database. But
# `run_agent_loop` returns text and the caller validates it, so a
# validation failure was a SINGLE-SHOT KILL — one off-contract
# response and the whole cycle group died with a ValidationError.
#
# That was tolerable while the contracts were four enum fields and a
# paragraph. It stopped being tolerable as the contracts grew: Atlas
# now has four structural rules about where a ticker may appear and
# what may be said about it, and Vera's assumption output has more.
# The likeliest failure is not a wild one — it is a near-miss, like a
# ticker written into the narrative out of habit, which a model can
# fix immediately if it is simply told.
#
# So: one repair round. The contract does not get softer; the model
# gets told precisely what it broke and asked again.
#
# THE REPAIR TURN DELIBERATELY CARRIES NO TOOLS. The model already
# gathered its data in the first pass and its own answer is in
# context, so it needs no new information to correct a format or a
# rule violation. Passing tools would let the loop re-run and spend
# the FMP daily quota a second time for nothing — and on a 250/day
# free tier, a repair that silently doubles the bill is its own
# outage.
#
# ONE ROUND, NOT A LOOP. A contract a model cannot satisfy in two
# tries is a prompt bug, and retrying it four times converts a loud
# bug into a slow, expensive, quiet one.
# =================================================================
MAX_REPAIR_ROUNDS = 1

_REPAIR_INSTRUCTION = """Your previous response did not satisfy the output contract and was REJECTED. It was not stored.

The validation error was:

{error}

Correct it and return ONLY the corrected JSON object — no markdown fences, no explanation, no apology. Keep every factual value from your previous answer that the error did not complain about; change only what the error names. Do not call any tools.
"""


def run_validated_agent_loop(
    system_prompt: str,
    user_prompt: str,
    tools: list[dict],
    tool_executor,
    validate,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 2000,
    agent_name: str = "agent",
):
    """Run the agent loop, then validate — with one repair round.

    `validate` is any callable that takes the parsed dict and either
    returns the validated object or raises. In practice this is a
    Pydantic model's `.model_validate`, so the exception text already
    names the offending field and value; the agents' validators are
    written with that in mind, listing what IS permitted rather than
    only what was wrong.

    Returns whatever `validate` returns. Raises the SECOND failure,
    with the first attached, when the repair also fails — both are
    needed to tell "the model misunderstood once" from "the contract
    is unsatisfiable".
    """
    raw = run_agent_loop(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        tools=tools,
        tool_executor=tool_executor,
        model=model,
        max_tokens=max_tokens,
    )

    first_error: Optional[Exception] = None
    for attempt in range(MAX_REPAIR_ROUNDS + 1):
        try:
            validated = validate(extract_json(raw))
            if attempt:
                # Loud on purpose. A repair that happens every single
                # morning is a prompt that needs fixing, and a quiet
                # retry would hide exactly that.
                logger.warning(
                    "%s failed its output contract and was repaired on retry. "
                    "The run succeeded, but a repair is a defect, not a "
                    "feature — if this recurs daily the prompt is wrong, not "
                    "the model. Original error: %s", agent_name, first_error)
            return validated
        except Exception as exc:            # ValidationError or a JSON failure
            if attempt >= MAX_REPAIR_ROUNDS:
                if first_error is not None:
                    raise RuntimeError(
                        f"{agent_name} failed its output contract twice.\n\n"
                        f"FIRST attempt: {first_error}\n\n"
                        f"AFTER being told the error: {exc}\n\n"
                        f"Two failures in a row usually means the contract "
                        f"cannot be satisfied as written — check the prompt "
                        f"and the validator agree before blaming the model."
                    ) from exc
                raise
            first_error = exc
            logger.warning("%s output rejected; requesting one correction. %s",
                           agent_name, exc)
            raw = _repair_round(system_prompt, user_prompt, raw, exc,
                                model=model, max_tokens=max_tokens)

    raise AssertionError("unreachable")     # pragma: no cover


def _repair_round(system_prompt: str, user_prompt: str, bad_answer: str,
                  error: Exception, *, model: str, max_tokens: int) -> str:
    """One corrective turn. No tools — see the note above."""
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": bad_answer},
            {"role": "user", "content": _REPAIR_INSTRUCTION.format(error=error)},
        ],
    )
    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            f"The correction attempt hit the {max_tokens}-token limit and was "
            f"cut off. Raise max_tokens — the contract may be fine.")
    return "".join(b.text for b in response.content if b.type == "text")


def _find_json_span(text: str) -> str:
    """
    Finds the first complete, balanced JSON array/object within a
    larger string — used when Claude adds prose or scratch notes
    before the actual JSON despite being told not to. Tracks bracket
    depth and string state (so brackets inside quoted strings don't
    confuse the scan) to find exactly where the JSON structure ends.
    """
    start = None
    for i, ch in enumerate(text):
        if ch in "[{":
            start = i
            break
    if start is None:
        raise ValueError("No JSON array or object found anywhere in the response.")

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError("Found the start of a JSON structure but it was never closed.")


def extract_json(raw_text: str) -> dict:
    """
    Claude sometimes wraps JSON in ```json ... ``` fences, or adds
    prose/reasoning before the JSON, even when told not to. Strip
    fences first, then try a direct parse, then fall back to
    scanning for a balanced JSON span within the text — rather than
    trusting the model's formatting discipline every single time.
    """
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()
    if not text:
        raise ValueError(
            "Claude's final response was empty — this usually means max_tokens "
            "was too low for the task and the response got cut off before any "
            "output was produced. Try increasing max_tokens for this call."
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass  # fall through to the defensive scan below

    try:
        return json.loads(_find_json_span(text))
    except (ValueError, json.JSONDecodeError) as e:
        preview = text[:500] + ("..." if len(text) > 500 else "")
        raise ValueError(
            f"Failed to parse Claude's response as JSON: {e}\n"
            f"Raw response (first 500 chars): {preview}"
        ) from e