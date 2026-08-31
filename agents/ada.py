"""
Ada — Execution Agent
Operating Manual §7.6

ANATOMY OF THIS AGENT — same 7 pieces. Ada is the most code-heavy
agent on the desk by deliberate design: order mechanics are fully
deterministic, and the LLM layer is reserved for one genuinely
ambiguous judgment call, not decoration.

  [1] ROLE / MANDATE   -> ADA_SYSTEM_PROMPT (used ONLY for the
                            wide-spread edge case — see below)
  [2] SKILLS / TOOLS    -> NONE for Claude (same as Solomon/Nora/
                            Marcus) — Alpaca calls happen directly in
                            code, never mediated through a tool loop,
                            since there's no judgment about WHICH data
                            to fetch here, only mechanical execution
  [3] TOOL DISPATCH     -> not needed
  [4] THE AGENTIC LOOP  -> called ONLY when the bid-ask spread looks
                            unusually wide (an illiquidity signal) —
                            most orders never involve an LLM call at all
  [5] OUTPUT CONTRACT   -> plain dict, built entirely from Alpaca's
                            own response — no Pydantic validation of
                            an LLM output needed for the common path,
                            since Claude usually isn't involved
  [6] MEMORY            -> Alpaca's own account/position state IS the
                            memory here — no separate DB read needed
                            to know what's currently held
  [7] PERSISTENCE        -> orders table — but note this is INSERT-
                            IF-NOT-EXISTS, not upsert (see why below)

HONEST V1 GAP: the Operating Manual's "staleness" guardrail (pause if
price moved materially since MARCUS'S decision) needs Marcus to record
a price snapshot at decision time, which he doesn't do yet — that
would mean a schema change to `allocations`. Rather than fake that
check, Ada instead watches bid-ask SPREAD as a real, implementable
illiquidity signal: a wide spread is a genuine reason to slow down
and get a second opinion, even if it's not exactly the cross-agent
staleness check as originally specified. Flagged here, not hidden —
revisit once Marcus records decision-time pricing.

WHY INSERT-IF-NOT-EXISTS, NOT UPSERT: every other agent's persistence
uses upsert (Atlas, Vera) or delete-then-replace (Solomon, Nora,
Marcus) because re-running them on the same day should refresh
today's analysis. Ada is fundamentally different: she places REAL
(paper) orders. Re-running her must NEVER submit a second order for
a ticker already executed today — that would be a real duplicate
trade, not a stale analysis to refresh. So her idempotency check
happens BEFORE submission (skip if already done), not after (via
upsert). This is the same principle as before, applied correctly to
a different kind of consequence.
"""

from datetime import date

from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.clients import alpaca_client
from core.clients.claude_client import run_agent_loop, extract_json
from core.db import session_scope
from core.models import Allocation, Order

# A spread wider than this triggers the LLM judgment pass instead of
# the default mechanical path — a real, code-computed threshold, not
# a vibe.
WIDE_SPREAD_THRESHOLD_PCT = 0.5


# =================================================================
# [5] OUTPUT CONTRACT — only used for the wide-spread LLM pass.
# =================================================================
class SpreadJudgment(BaseModel):
    proceed: bool
    limit_price: float
    reasoning: str


# =================================================================
# [1] ROLE / MANDATE — only invoked for the wide-spread edge case.
# =================================================================
ADA_SYSTEM_PROMPT = """You are Ada, the Execution agent at MBY-Trading.

You are being consulted only because this ticker's current bid-ask
spread is unusually wide, which can mean thin liquidity — executing
carelessly here risks a bad fill. You are NOT deciding whether to
trade (that's already been decided) or how much (already sized) —
only HOW to execute given this specific liquidity concern.

Given the current bid, ask, and spread, either:
- Recommend proceeding with a specific limit price you judge sensible
  given the spread, or
- Recommend NOT proceeding today if the spread suggests real
  execution risk that isn't worth taking for a weeks-to-months
  holding horizon (there's no urgency here that justifies a bad fill).

Respond with ONLY a JSON object — your response must start with "{"
as its very first character and contain nothing else:

{"proceed": true or false, "limit_price": 0.00, "reasoning": "1-2 sentences"}
"""


def execute_allocation(today: date, allocation: Allocation) -> dict:
    """
    Executes a single allocation. Returns a dict matching the orders
    table shape. This is where [4] the agentic loop conditionally
    fires — most calls never reach it.
    """
    quote = alpaca_client.get_latest_quote(allocation.ticker)

    if quote["spread_pct"] > WIDE_SPREAD_THRESHOLD_PCT:
        # =========================================================
        # [4] THE AGENTIC LOOP — the one case Ada actually consults
        # Claude for.
        # =========================================================
        user_prompt = (
            f"Ticker: {allocation.ticker}\n"
            f"Bid: {quote['bid']}, Ask: {quote['ask']}, Spread: {quote['spread_pct']}%\n"
            f"Intended action: {allocation.action}, target size: {allocation.target_size_pct}%"
        )
        raw = run_agent_loop(
            system_prompt=ADA_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            tools=[],
            tool_executor=lambda name, inp: (_ for _ in ()).throw(
                RuntimeError(f"Ada has no tools, but one was called: {name}")
            ),
            max_tokens=500,
        )
        judgment = SpreadJudgment.model_validate(extract_json(raw))
        if not judgment.proceed:
            return {
                "ticker": allocation.ticker, "action": allocation.action,
                "order_type": "limit", "limit_price": None, "shares": 0,
                "status": "skipped_wide_spread", "fill_price": None,
                "slippage_bps": None, "alpaca_order_id": None,
            }
        limit_price = judgment.limit_price
    else:
        # Default mechanical path — no LLM call. Buy at the ask
        # (won't overpay beyond the visible offer); sell at the bid.
        limit_price = quote["ask"] if allocation.action in ("new_position", "add") else quote["bid"]

    account = alpaca_client.get_account()

    if allocation.action in ("new_position", "add"):
        target_dollars = account["equity"] * (float(allocation.target_size_pct) / 100)
        shares = int(target_dollars // limit_price)  # whole shares only
        side = "buy"
    else:  # exit / trim
        position = alpaca_client.get_position(allocation.ticker)
        if not position:
            return {
                "ticker": allocation.ticker, "action": allocation.action,
                "order_type": "limit", "limit_price": limit_price, "shares": 0,
                "status": "rejected_no_position", "fill_price": None,
                "slippage_bps": None, "alpaca_order_id": None,
            }
        if allocation.action == "exit":
            shares = position["qty"]
        else:  # trim — v1 always targets a 50% reduction, matching
               # Marcus's current logic (see marcus.py's simplification note)
            shares = position["qty"] * 0.5
        side = "sell"

    if shares <= 0:
        return {
            "ticker": allocation.ticker, "action": allocation.action,
            "order_type": "limit", "limit_price": limit_price, "shares": 0,
            "status": "rejected_zero_shares", "fill_price": None,
            "slippage_bps": None, "alpaca_order_id": None,
        }

    result = alpaca_client.submit_limit_order(allocation.ticker, side, shares, limit_price)
    return {
        "ticker": allocation.ticker, "action": allocation.action,
        "order_type": "limit", "limit_price": limit_price, "shares": shares,
        "status": result["status"], "fill_price": None,  # fill confirmation is Otis's job
        "slippage_bps": None, "alpaca_order_id": result["alpaca_order_id"],
    }


def run(today: date) -> dict:
    """
    Runs Ada over every one of today's allocations with a non-zero
    target size, SKIPPING any ticker already executed today
    (idempotency-before-submission — see module docstring for why
    this differs from every other agent's persistence pattern).
    """
    with session_scope() as session:
        allocations = (
            session.query(Allocation)
            .filter(Allocation.allocation_date == today, Allocation.target_size_pct > 0)
            .all()
        )
        already_executed = {
            o.ticker for o in session.query(Order).filter(Order.order_date == today).all()
        }

    orders = []
    for allocation in allocations:
        if allocation.ticker in already_executed:
            continue  # already placed today — never resubmit
        orders.append(execute_allocation(today, allocation))

    # =============================================================
    # [7] PERSISTENCE — plain inserts. Idempotency was already
    # handled above, BEFORE submission — see module docstring.
    # =============================================================
    with session_scope() as session:
        for o in orders:
            session.add(Order(order_date=today, **o))

    return {"date": today, "orders": orders}


if __name__ == "__main__":
    # Manual smoke test: python -m agents.ada
    #
    # This does NOT chain through Atlas/Vera/Solomon/Nora/Marcus —
    # deliberately. Ada doesn't touch FMP at all, so she can be
    # tested independently of the FMP quota pause. This places a
    # small, real (paper) order directly, bypassing the DB chain
    # entirely, purely to verify the Alpaca integration itself works.
    print("SYNTHETIC TEST — placing a small real paper order via Alpaca directly.")
    print("(Not going through the agent pipeline — just testing the Alpaca wiring.)\n")

    quote = alpaca_client.get_latest_quote("AAPL")
    print(f"AAPL quote: {quote}")

    account = alpaca_client.get_account()
    print(f"Account: {account}")

    result = alpaca_client.submit_limit_order("AAPL", "buy", 1, quote["ask"])
    print(f"Order submitted: {result}")