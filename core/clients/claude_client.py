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
import os

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

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