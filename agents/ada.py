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
  [6] MEMORY            -> OTIS'S LEDGER (core/ledger.py) — NAV and
                            current holdings come from the reconciled
                            positions/daily_pnl tables, the same numbers
                            Nora enforced her limits against in Phase 3.
                            Alpaca is still the source for prices and
                            for submission, but no longer for what the
                            firm owns: Operating Manual §4 makes Otis
                            the system of record, and sizing off a live
                            broker call while limits were checked
                            against the ledger meant the book a trade
                            was CHECKED against was not the book it was
                            SIZED against.
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

import logging
from datetime import date

from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.dialects.postgresql import insert as pg_insert

from agents.nora import get_active_risk_policy, is_circuit_breaker_active
from core.clients import alpaca_client
from core.ledger import MAX_LEDGER_STALENESS_SESSIONS, LedgerSnapshot, read_snapshot
from core.trading_control import get_state as get_control_state, is_trading_enabled
from core.clients.claude_client import run_agent_loop, extract_json
from core.db import session_scope
from core.models import Allocation, Order

# A spread wider than this triggers the LLM judgment pass instead of
# the default mechanical path — a real, code-computed threshold, not
# a vibe.
logger = logging.getLogger(__name__)

WIDE_SPREAD_THRESHOLD_PCT = 0.5

# How much of the broker's reported buying power a single order may
# consume. The margin absorbs the gap between the quote we sized from
# and the price at submission — without it, an order sized to exactly
# the available buying power gets rejected by a cent of adverse
# movement, and a rejection tells you far less than a slightly smaller
# fill would have.
BUYING_POWER_SAFETY_MARGIN = 0.98

# =================================================================
# UNITS — allocation.target_size_pct is the RESULTING TOTAL portfolio
# weight for this name after the trade, not the amount to add.
#
# Ada therefore trades the DIFFERENCE between where the name is and
# where Marcus wants it, and converges on the target. She previously
# read the same field as an amount to add and bought that much on top
# of the existing holding, which is how an `add` on a 7% position
# could land at ~12.6% against an 8% limit with every upstream check
# reporting "approved".
#
# One formula now covers buy, trim and exit:
#     delta_dollars = target_weight/100 * NAV - current_market_value
# positive -> buy, negative -> sell, and an exit (target 0) falls out
# of it naturally.
# =================================================================


def _order_result(allocation, status: str, limit_price=None, shares=0,
                  alpaca_order_id=None, detail: str | None = None) -> dict:
    """Shape of a row in the orders table. One builder so the several
    early-return paths below can't drift apart from each other.

    `detail` is the evidence behind the status, in words. A status is a
    verdict — and a verdict alone stops being explainable the moment the
    condition that produced it is repaired. `rejected_stale_ledger` on
    4 Sept 2026 was unreadable within the same cycle, because Otis
    closes the books in Phase 5 and Ada trades in Phase 4: by the time
    anyone queried the ledger it was fresh, and the row looked like it
    was contradicting itself. Never parsed by code (migration 007)."""
    return {
        # The link back to the intent this order came from. It was
        # never populated, so Clara's approval-chain audit ran on
        # ticker/date joins and the end-of-day sweep had no way to say
        # WHICH decision went unfilled. Set here, on every path, so a
        # rejected or halted order carries it too — those are exactly
        # the ones you want to trace back.
        "allocation_id": allocation.id,
        "ticker": allocation.ticker,
        "action": allocation.action,
        "order_type": "limit",
        "limit_price": limit_price,
        "shares": shares,
        "status": status,
        "fill_price": None,      # fill confirmation is Otis's job
        "slippage_bps": None,
        "alpaca_order_id": alpaca_order_id,
        "status_detail": detail,
    }


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


def execute_allocation(today: date, allocation: Allocation,
                       max_position_pct: float,
                       ledger: LedgerSnapshot) -> dict:
    """
    Executes a single allocation. Returns a dict matching the orders
    table shape. This is where [4] the agentic loop conditionally
    fires — most calls never reach it.

    max_position_pct and `ledger` are both passed in rather than looked
    up here, so every order in a run is sized and checked against ONE
    policy version and ONE portfolio snapshot. Re-reading per order
    would let two orders in the same cycle disagree about what the book
    contains, which is a smaller version of the bug this whole change
    is fixing.

    WHERE THE NUMBERS COME FROM (Operating Manual §4):

      NAV and current holding   -> Otis's ledger, via the snapshot
      price / quote             -> Alpaca (the ledger holds no market data)
      submission and fill       -> Alpaca (that is the broker's business)

    Ada previously read NAV and position size live from Alpaca while
    Nora enforced the position limit against Otis's `positions` table.
    Two sources, two moments — so the book a trade was CHECKED against
    was not necessarily the book it was SIZED against.
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
            return _order_result(allocation, "skipped_wide_spread")
        limit_price = judgment.limit_price
    else:
        # Default mechanical path — no LLM call. Buy at the ask
        # (won't overpay beyond the visible offer); sell at the bid.
        limit_price = quote["ask"] if allocation.action in ("new_position", "add") else quote["bid"]

    if limit_price is None or limit_price <= 0:
        return _order_result(allocation, "rejected_no_price")

    # Buying power is read from the broker, and that is not a D5
    # backslide. It is not a portfolio fact — it is settled-cash
    # availability, which only the broker knows and which changes
    # intraday under T+1 settlement. Same category as a price: the
    # ledger holds no such thing and shouldn't.
    buying_power = alpaca_client.get_buying_power()

    # ---- portfolio state: the ledger, not the broker ----
    if ledger.bootstrap:
        # The one permitted fallback. Otis runs in Phase 5, after Ada,
        # so on a brand-new install the ledger is legitimately empty and
        # a strict rule would make the first cycle unable to trade with
        # no way out. Marked on the order so it is never invisible.
        account = alpaca_client.get_account()
        nav = float(account["equity"])
        broker_position = alpaca_client.get_position(allocation.ticker)
        current_value = float(broker_position["market_value"]) if broker_position else 0.0
        current_qty = float(broker_position["qty"]) if broker_position else 0.0
        logger.warning(
            "Ledger is empty (Otis has never closed the books) — sizing %s from the "
            "broker for this cycle only.", allocation.ticker,
        )
    else:
        nav = float(ledger.nav or 0)
        current_value = ledger.position_value(allocation.ticker)
        current_qty = ledger.position_qty(allocation.ticker)

    if nav <= 0:
        return _order_result(allocation, "rejected_no_nav", limit_price=limit_price,
                             detail=ledger.describe())

    target_weight = float(allocation.target_size_pct)
    target_dollars = nav * (target_weight / 100)
    delta_dollars = target_dollars - current_value

    if allocation.action == "exit":
        # Explicit and total: sell the entire holding, including any
        # fractional remainder. Not derived from the weight arithmetic,
        # because "all of it" must not depend on a rounding step.
        #
        # Quantity comes from the ledger like everything else, but an
        # exit is the one case where the broker's number is the one that
        # matters — you cannot sell shares the broker doesn't think you
        # have. Otis flags any divergence as a discrepancy at the close.
        if current_qty <= 0:
            return _order_result(allocation, "rejected_no_position", limit_price=limit_price,
                                 detail=f"{ledger.describe()} — holds no {allocation.ticker}")
        shares, side = current_qty, "sell"

    elif allocation.action == "trim":
        if current_qty <= 0:
            return _order_result(allocation, "rejected_no_position", limit_price=limit_price,
                                 detail=f"{ledger.describe()} — holds no {allocation.ticker}")
        if delta_dollars >= 0:
            # Already at or below where Marcus wants it — nothing to do.
            return _order_result(
                allocation, "rejected_already_at_target", limit_price=limit_price,
                detail=(f"holding {current_value:,.2f} is already at or below the "
                        f"{target_weight:.2f}% target ({target_dollars:,.2f})"))
        # Sized from MARCUS's target, not a hardcoded 50%. Ada used to
        # recompute qty * 0.5 herself, which happened to agree with
        # Marcus's current rule — and would have silently overridden
        # the PM the moment that rule changed. §7.6: Ada cannot change
        # how much to trade.
        shares = min(current_qty, abs(delta_dollars) / limit_price)
        shares = float(int(shares))  # whole shares, same convention as the buy path
        side = "sell"

    else:  # new_position / add
        if delta_dollars <= 0:
            return _order_result(
                allocation, "rejected_already_at_target", limit_price=limit_price,
                detail=(f"holding {current_value:,.2f} already meets the "
                        f"{target_weight:.2f}% target ({target_dollars:,.2f})"))

        # =========================================================
        # BUYING POWER (D2). get_account() has always returned this
        # and nothing has ever read it — Ada sized purely off NAV.
        #
        # Under T+1 settlement equity and available cash diverge
        # whenever a sale has not settled: the value is real, the money
        # is not there yet. Alpaca simply rejects such an order, and a
        # bare rejection cannot be told apart from any other kind.
        #
        # Clipping instead of rejecting is the deliberate choice: a
        # smaller fill in the right direction beats no fill and an
        # opaque error. When the clip binds it is recorded, so an
        # underweight position has a traceable reason.
        # =========================================================
        affordable = buying_power * BUYING_POWER_SAFETY_MARGIN
        clipped = delta_dollars > affordable
        if clipped:
            logger.warning(
                "%s: sizing %.2f clipped to %.2f by available buying power "
                "(%.2f x %.0f%% margin).",
                allocation.ticker, delta_dollars, affordable,
                buying_power, BUYING_POWER_SAFETY_MARGIN * 100,
            )
            delta_dollars = affordable

        shares = float(int(delta_dollars // limit_price))  # whole shares only
        side = "buy"

        if shares <= 0 and clipped:
            return _order_result(
                allocation, "rejected_insufficient_buying_power", limit_price=limit_price,
                detail=(f"needed {target_dollars - current_value:,.2f}; buying power "
                        f"{buying_power:,.2f} x {BUYING_POWER_SAFETY_MARGIN:.0%} = "
                        f"{affordable:,.2f}, under one share at {limit_price:,.2f}"),
            )

    if shares <= 0:
        return _order_result(
            allocation, "rejected_zero_shares", limit_price=limit_price,
            detail=(f"{target_weight:.2f}% of NAV {nav:,.2f} is {delta_dollars:,.2f} "
                    f"to move, under one share at {limit_price:,.2f}"))

    # =============================================================
    # POST-TRADE ASSERTION — the last wall before money moves.
    #
    # Checked on the LEDGER basis, the same numbers Nora enforced
    # against in Phase 3. That makes this a genuine check of the
    # Nora -> Marcus -> Ada arithmetic rather than a comparison between
    # two sources that were never meant to agree.
    #
    # Then a SECOND check against the broker's live view. It can only
    # ever refuse more, never permit more — so it adds safety without
    # reintroducing the disagreement: if the live position has already
    # drifted past the cap intraday, we decline regardless of what the
    # ledger says. One-directional, which is what makes it safe to mix
    # sources here and nowhere else.
    # =============================================================
    if side == "buy":
        resulting_weight = (current_value + shares * limit_price) / nav * 100
        if resulting_weight > max_position_pct + 1e-9:
            return _order_result(
                allocation, "rejected_post_trade_limit", limit_price=limit_price,
                detail=(f"resulting weight {resulting_weight:.2f}% would breach the "
                        f"{max_position_pct:.2f}% cap, on {ledger.describe()}"))

        live_position = alpaca_client.get_position(allocation.ticker)
        if live_position:
            live_account = alpaca_client.get_account()
            live_nav = float(live_account["equity"])
            if live_nav > 0:
                live_resulting = (
                    (float(live_position["market_value"]) + shares * limit_price)
                    / live_nav * 100
                )
                if live_resulting > max_position_pct + 1e-9:
                    logger.warning(
                        "%s passes the ledger check (%.2f%%) but breaches live (%.2f%%) "
                        "against a %.2f%% cap — declining.",
                        allocation.ticker, resulting_weight, live_resulting, max_position_pct,
                    )
                    return _order_result(
                        allocation, "rejected_live_drift_limit", limit_price=limit_price,
                        detail=(f"ledger basis {resulting_weight:.2f}% passes but the "
                                f"broker's live view is {live_resulting:.2f}% against a "
                                f"{max_position_pct:.2f}% cap"),
                    )

    # =============================================================
    # THE KILL SWITCH — re-read here, on the line before submission,
    # not once at the top of the run.
    #
    # A cycle with several orders in it can span minutes. "Halted" has
    # to mean "as of this order", not "as of whenever this batch
    # started", or the switch does nothing for the orders already
    # queued behind the one you were worried about.
    #
    # This stops sells too, which the drawdown breaker above
    # deliberately does not. Different control, different job: the
    # breaker stops the book growing while you lose money; this stops
    # the system acting at all because you no longer trust it, and a
    # system you don't trust shouldn't be picking exits either.
    # =============================================================
    if not is_trading_enabled():
        return _order_result(allocation, "halted_by_operator", limit_price=limit_price,
                             detail="kill switch flipped between sizing and submission")

    result = alpaca_client.submit_limit_order(allocation.ticker, side, shares, limit_price)
    return _order_result(
        allocation, result["status"],
        limit_price=limit_price, shares=shares,
        alpaca_order_id=result["alpaca_order_id"],
        # The sizing basis, recorded on the order that used it. Otis
        # will have moved the ledger on by the time anyone reads this.
        detail=(f"{shares:g} @ {limit_price:,.2f} to reach {target_weight:.2f}%; "
                f"sized from {ledger.describe()}"),
    )


def run(today: date) -> dict:
    """
    Runs Ada over every one of today's allocations with a non-zero
    target size, SKIPPING any ticker already executed today
    (idempotency-before-submission — see module docstring for why
    this differs from every other agent's persistence pattern).
    """
    with session_scope() as session:
        # target_size_pct > 0 would silently drop every exit, whose
        # target is 0 by definition. Exits are selected explicitly.
        allocations = (
            session.query(Allocation)
            .filter(
                Allocation.allocation_date == today,
                or_(Allocation.target_size_pct > 0, Allocation.action == "exit"),
            )
            .all()
        )
        # ONLY orders that actually reached the broker block a retry.
        #
        # This used to key on ANY order row for the ticker today, which
        # meant a REFUSAL blocked its own retry — and silently, via the
        # `continue` below, leaving no second row and no log line. It
        # bit three times in three days: halted_by_operator, then
        # rejected_zero_shares, then rejected_stale_ledger. Each time
        # the fix was the same manual DELETE, and each time the desk
        # looked like it had simply ignored a valid allocation.
        #
        # A row with no alpaca_order_id never reached the market, so
        # there is nothing to be idempotent about. The guard exists to
        # prevent a DUPLICATE REAL TRADE; it was preventing a retry.
        #
        # A retry adds a second row rather than replacing the first —
        # deliberately. "Refused at 16:31, submitted at 16:44" is the
        # history you want; overwriting it would lose the refusal.
        already_executed = {
            o.ticker for o in session.query(Order).filter(
                Order.order_date == today,
                Order.alpaca_order_id.isnot(None),
            ).all()
        }
        policy = get_active_risk_policy(session, today)
        max_position_pct = float(policy.max_position_pct)

    # ONE snapshot for the whole run — see execute_allocation's docstring.
    ledger = read_snapshot(today)
    logger.info("Ada sizing from %s", ledger.describe())

    if not ledger.usable_for_sizing:
        # Sizing a percentage of a stale NAV produces a confidently
        # wrong number. Refusing is the cheaper failure: the orders are
        # recorded, nothing is submitted, and running Otis unblocks it.
        logger.error(
            "Ledger is not usable for sizing (%s). Refusing to trade — "
            "run `python -m agents.otis` to close the books, then retry.",
            ledger.describe(),
        )

    # DEFENCE IN DEPTH. Nora rejects new risk at the decision point and
    # Marcus zeroes it at sizing; this is the last of the three, and the
    # only one that sits between the decision and the money. Reductions
    # are never frozen — a breaker that stopped you de-risking would be
    # worse than no breaker.
    breaker_active = is_circuit_breaker_active(today)

    # Read once here as well as per-order. This is not the enforcing
    # check — the one before submission is — it just avoids spending
    # quote lookups and a possible LLM call per allocation on a day
    # that was never going to trade. Every order still gets recorded,
    # so a halted day leaves a full record of what it would have done.
    halted = not is_trading_enabled()
    halt_reason = ""
    if halted:
        try:
            _state = get_control_state()
            halt_reason = f"{_state.changed_by}: {_state.reason}"
        except Exception:  # noqa: BLE001
            # The reason is a nicety; never let fetching it stop the
            # recording of the halt itself.
            halt_reason = "reason unavailable"
        logger.warning(
            "TRADING HALTED by operator — recording %d allocation(s) as "
            "halted_by_operator without submitting anything.", len(allocations),
        )

    orders = []
    for allocation in allocations:
        if allocation.ticker in already_executed:
            continue  # already placed today — never resubmit

        if halted:
            order = _order_result(
                allocation, "halted_by_operator",
                detail=f"kill switch set — {halt_reason}")
        elif not ledger.usable_for_sizing:
            order = _order_result(
                allocation, "rejected_stale_ledger",
                detail=(f"{ledger.describe()}; tolerance is "
                        f"{MAX_LEDGER_STALENESS_SESSIONS} session(s). "
                        f"Run `python -m agents.otis` to close the books."))
        elif breaker_active and allocation.action in ("new_position", "add"):
            order = _order_result(
                allocation, "halted_circuit_breaker",
                detail="Nora's drawdown breaker is active — new risk frozen, "
                       "reductions still permitted")
        else:
            order = execute_allocation(today, allocation, max_position_pct, ledger)

        orders.append(order)

        # =========================================================
        # [7] PERSISTENCE — one order at a time, IMMEDIATELY after it
        # is submitted, not batched after the loop.
        #
        # This used to submit every order and then write them all at
        # the end. A crash in between — a rejected order, a dropped
        # connection — lost the record of orders already live at the
        # broker, and the next run's idempotency check, which reads
        # exactly these rows, would happily submit them a second time.
        # Duplicate real trades, from a failure mode as ordinary as a
        # network blip.
        #
        # Its own session per order, so one bad write can't roll back
        # the record of orders that did go through.
        # =========================================================
        with session_scope() as session:
            session.add(Order(order_date=today, **order))

    return {
        "date": today, "orders": orders,
        "circuit_breaker_active": breaker_active,
        "trading_halted": halted,
        "ledger": ledger.describe(),
        "ledger_usable": ledger.usable_for_sizing,
    }


if __name__ == "__main__":
    # Manual smoke test: python -m agents.ada
    #
    # THIS PLACES A REAL ORDER. It is a paper account by default, but
    # ALPACA_PAPER is read from .env at import time, and a .env saying
    # otherwise makes this spend actual money. So it asks first, every
    # time, rather than trusting that the environment is what you
    # assume it is when you run a file to see what it does.
    #
    # It does NOT chain through Atlas/Vera/Solomon/Nora/Marcus —
    # deliberately. Ada doesn't touch FMP at all, so she can be tested
    # independently of the FMP quota pause. This places a small, real
    # (paper) order directly, bypassing the DB chain entirely, purely
    # to verify the Alpaca integration itself works.
    import os

    mode = "PAPER" if alpaca_client.ALPACA_PAPER else "*** LIVE — REAL MONEY ***"
    print(f"SYNTHETIC TEST — this will place a real order. Account mode: {mode}")
    print("(Not going through the agent pipeline — just testing the Alpaca wiring.)\n")

    if os.environ.get("ADA_SMOKE_TEST_CONFIRM") != "yes":
        print("Refusing to trade without explicit confirmation.")
        print("Re-run with:  ADA_SMOKE_TEST_CONFIRM=yes python -m agents.ada")
        raise SystemExit(1)

    from core.trading_control import get_state
    control = get_state()
    if not control.trading_enabled:
        print(f"Trading is {control.describe()}")
        print("Refusing to place a smoke-test order while halted.")
        raise SystemExit(1)

    quote = alpaca_client.get_latest_quote("AAPL")
    print(f"AAPL quote: {quote}")

    account = alpaca_client.get_account()
    print(f"Account: {account}")

    result = alpaca_client.submit_limit_order("AAPL", "buy", 1, quote["ask"])
    print(f"Order submitted: {result}")