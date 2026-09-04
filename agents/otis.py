"""
Otis — Operations/Reconciliation Agent
Operating Manual §7.7

ANATOMY OF THIS AGENT — same 7 pieces. Otis is the SYSTEM OF RECORD:
every other agent needing "current portfolio state" should read from
the positions table HE maintains, never query Alpaca directly
themselves. That's not a style preference — it's what keeps Nora,
Marcus, and the dashboard all looking at the same facts at the same
moment, instead of each hitting Alpaca independently and risking
subtly different snapshots.

  [1] ROLE / MANDATE   -> OTIS_SYSTEM_PROMPT (narrative only — see below)
  [2] SKILLS / TOOLS    -> NONE for Claude (same pattern as Solomon/
                            Nora/Marcus/Ada) — reconciliation is
                            dataset-matching, which is code, not judgment
  [3] TOOL DISPATCH     -> not needed
  [4] THE AGENTIC LOOP  -> called ONLY if there's something worth
                            narrating (real positions or real
                            discrepancies) — skipped on a genuinely
                            empty, clean day
  [5] OUTPUT CONTRACT   -> ReconciliationNarrative (Pydantic) — text
                            only, no numeric fields. Every number in
                            Otis's output comes from code that already
                            ran; Claude never gets to state a figure
                            that becomes fact, same principle as Nora
  [6] MEMORY            -> Otis's memory IS the ledger: the
                            `transactions` table (append-only,
                            permanent) plus the `positions` table
                            (current state, kept in exact sync with
                            Alpaca's real holdings)
  [7] PERSISTENCE        -> transactions (append-only inserts,
                            idempotent via alpaca_order_id — and
                            reconciliation now selects on "not yet
                            booked" rather than "still open", so an
                            order that filled instantly at submission
                            can no longer slip past unbooked),
                            positions (replace-set to match Alpaca
                            exactly), daily_pnl (upsert per day),
                            discrepancies (insert-if-new)

GUARDRAIL, TAKEN SERIOUSLY: Otis NEVER silently resolves a
discrepancy. If Alpaca's real state doesn't match what was intended,
that gets flagged and left for review — never quietly "corrected" by
assumption. A bookkeeper who guesses is worse than one who says
"I don't know."

P&L DECOMPOSITION — three daily FLOWS that reconcile:

    realized + unrealized_change = total = equity - last_equity

realized is booked per-sale into the transaction ledger at the moment
of sale, when the cost basis is still knowable; unrealized_change is
the residual, i.e. today's mark-to-market on what remains open. The
lifetime open gain is reported separately as open_unrealized_pnl,
because it is a stock and the other three are flows.

This was previously computed the other way round — realized derived as
(today's equity delta) minus (LIFETIME unrealized across all open
positions). That subtracts a cumulative quantity from a daily one; on
any book carrying an open gain both columns came out badly wrong, and
the figures fed Clara's performance attribution.

The remaining assumption is no external cash flows in or out of the
account, which holds for a paper account. Deposits or withdrawals would
break the equity-delta identity and need booking as their own
transaction type.
"""

from datetime import date

from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert

import logging

from sqlalchemy import func

from core.clients import alpaca_client
from core.market_calendar import market_is_open_now
from core.clients.claude_client import run_agent_loop, extract_json
from core.db import session_scope
from core.models import (
    Position, Transaction, DailyPnl, Discrepancy, Order, Allocation, PositionPnlHistory,
    Thesis, NewCandidate,
)

logger = logging.getLogger(__name__)


# =================================================================
# [5] OUTPUT CONTRACT — text only. No numeric field exists here for
# Claude to state as fact — every number in the final return value
# comes from the deterministic reconciliation below, not from this.
# =================================================================
class ReconciliationNarrative(BaseModel):
    narrative: str


OTIS_SYSTEM_PROMPT = """You are Otis, the Operations/Reconciliation agent at MBY-Trading.

You are being shown the result of an ALREADY-COMPLETED reconciliation
— real positions, real P&L figures, and any discrepancies found. Your
only job is to write a clear, plain-language summary of today's
reconciliation for a human reader. You are not verifying or
recomputing anything — that's already done and is authoritative.

If there are discrepancies, mention them plainly and neutrally —
never speculate about blame or silently suggest they're fine to
ignore. A flagged discrepancy is for a human to resolve, not you.

Respond with ONLY a JSON object — your response must start with "{"
as its very first character and contain nothing else:

{"narrative": "2-4 sentences summarizing today's reconciliation"}
"""


def _resolve_sector(session, ticker: str) -> str | None:
    """
    Sector for a held ticker, carried across from research rather than
    re-fetched. Vera receives sector on the FMP profile call she
    already makes, so this costs zero extra API quota — which matters
    on a 250-calls/day free tier where a daily per-position lookup
    would be a poor way to spend it for a value that changes maybe
    once a decade.

    Open thesis first (the documented reason we hold it), then the
    most recent candidate row. Returns None when genuinely unknown —
    Nora treats an unknown-sector name as its own single-name sector
    bucket, so a missing value can only ever over-flag concentration,
    never hide it.
    """
    thesis = (
        session.query(Thesis)
        .filter(Thesis.ticker == ticker, Thesis.closed_date.is_(None))
        .order_by(Thesis.opened_date.desc())
        .first()
    )
    if thesis is not None and thesis.sector:
        return thesis.sector

    candidate = (
        session.query(NewCandidate)
        .filter(NewCandidate.ticker == ticker)
        .order_by(NewCandidate.candidate_date.desc())
        .first()
    )
    if candidate is not None and candidate.sector:
        return candidate.sector

    return None


def _booked_order_ids(session) -> set:
    """Alpaca order ids already present in the transaction ledger."""
    return {
        t.alpaca_transaction_id
        for t in session.query(Transaction).filter(Transaction.alpaca_transaction_id.isnot(None)).all()
    }


def _reconcile_orders(session, today: date, booked_ids: set) -> list[Order]:
    """
    Checks Alpaca for the current state of every one of today's orders
    that has not yet been booked to the ledger, and updates our record.
    Returns the orders that are filled and still unbooked.

    THE SELECTION RULE IS "NOT YET BOOKED", NOT "STILL OPEN".

    This previously selected only orders whose status was pending_new,
    accepted or new. An order that Alpaca reported as `filled` in its
    submission response therefore never came back through here — Ada
    wrote status='filled' and it was skipped forever, so no transaction
    was ever booked for it. A permanent hole in an append-only ledger,
    and one that widened when realized P&L started being derived from
    that ledger: an unbooked sale is a sale whose profit silently never
    existed.

    Keying on "has no transaction row" closes it by construction, and
    subsumes the old condition — an open order has no transaction row
    either. It also backfills fill_price and slippage_bps on orders that
    filled instantly, which Ada leaves as None because confirming a fill
    is Otis's job (§7.7).

    Scoped to today because orders are TimeInForce.DAY: yesterday's
    unfilled orders expired overnight and are not going to fill now.
    Terminal non-fills (rejected, canceled, expired) get re-checked once
    more on their own day, which is cheap and self-limiting.
    """
    candidates = (
        session.query(Order)
        .filter(Order.order_date == today, Order.alpaca_order_id.isnot(None))
        .all()
    )

    newly_filled = []
    for order in candidates:
        if order.alpaca_order_id in booked_ids:
            continue  # already in the ledger — nothing to reconcile

        current = alpaca_client.get_order_by_id(order.alpaca_order_id)
        order.status = current["status"]

        if current["status"] == "filled" and current["filled_avg_price"]:
            order.fill_price = current["filled_avg_price"]
            if order.limit_price:
                order.slippage_bps = round(
                    (float(current["filled_avg_price"]) - float(order.limit_price))
                    / float(order.limit_price) * 10000, 2
                )
            newly_filled.append(order)

    return newly_filled


# Statuses past which an order will not change again. Anything else is
# still live as far as we know, and is what the sweep acts on.
TERMINAL_ORDER_STATUSES = {
    "filled", "canceled", "cancelled", "expired", "rejected", "done_for_day",
    "replaced", "expired_unfilled",
    # Locally-recorded outcomes that never reached the broker at all.
    "halted_by_operator", "halted_circuit_breaker", "skipped_wide_spread",
    "rejected_no_position", "rejected_zero_shares", "rejected_no_price",
    "rejected_no_nav", "rejected_already_at_target", "rejected_post_trade_limit",
    "rejected_live_drift_limit", "rejected_stale_ledger",
    "rejected_insufficient_buying_power",
}


def _sweep_unfilled_orders(session, today: date, booked_ids: set) -> tuple[list[dict], list[Order]]:
    """
    End-of-day sweep (D3). Cancels today's orders that never filled and
    records the outcome. Returns (expired_summaries, filled_during_cancel).

    WHY THIS EXISTS. Every order here is TimeInForce.DAY, so an unfilled
    one lapses overnight on its own. But lapsing silently and being
    cancelled deliberately are different facts, and only the second is a
    decision on the record. Without this, the allocation behind an
    unfilled order was simply forgotten, Otis raised a discrepancy that
    nothing ever actioned, and "we decided to buy it and didn't" was
    indistinguishable from "we never decided" — an intent lost with no
    trace of having existed.

    Marcus rebuilds allocations from scratch each day, so nothing needs
    carrying forward: if the thesis still holds, tomorrow expresses it
    again. What was missing was the record that today's attempt failed
    and why.

    THE GUARD MATTERS. The sweep stands down while the session is open.
    Otis normally runs after the close, but running him mid-session to
    look at the books would otherwise cancel orders still perfectly
    capable of filling — an inspection command with a destructive side
    effect, which is a bad thing to leave lying around.

    Cancelling can also lose a race: an order may fill between the
    status read and the cancel. Those come back as filled and are
    returned for booking rather than treated as an error.
    """
    if market_is_open_now():
        logger.info(
            "Sweep skipped — the NYSE session is still open. Unfilled orders are "
            "left live; run Otis after the close to sweep them."
        )
        return [], []

    candidates = (
        session.query(Order)
        .filter(Order.order_date == today, Order.alpaca_order_id.isnot(None))
        .all()
    )

    expired, filled_during_cancel = [], []
    for order in candidates:
        if order.status in TERMINAL_ORDER_STATUSES or order.alpaca_order_id in booked_ids:
            continue

        outcome = alpaca_client.cancel_order(order.alpaca_order_id)
        current = alpaca_client.get_order_by_id(order.alpaca_order_id)

        if current["status"] == "filled" and current["filled_avg_price"]:
            # Lost the race, in the good direction.
            order.status = "filled"
            order.fill_price = current["filled_avg_price"]
            if order.limit_price:
                order.slippage_bps = round(
                    (float(current["filled_avg_price"]) - float(order.limit_price))
                    / float(order.limit_price) * 10000, 2
                )
            filled_during_cancel.append(order)
            logger.info("%s filled while being cancelled — booking it.", order.ticker)
            continue

        order.status = "expired_unfilled"
        expired.append({
            "ticker": order.ticker,
            "action": order.action,
            "shares": float(order.shares or 0),
            "limit_price": float(order.limit_price) if order.limit_price else None,
            "allocation_id": order.allocation_id,
            "cancel_confirmed": outcome["cancelled"],
            "cancel_error": outcome["error"],
        })

    if expired:
        logger.warning(
            "%d order(s) expired unfilled and were cancelled: %s",
            len(expired), ", ".join(e["ticker"] for e in expired),
        )

    return expired, filled_during_cancel


def _book_new_transactions(session, newly_filled_orders: list[Order],
                           booked_ids: set | None = None) -> int:
    """
    Append-only: books a transaction for any newly-filled order not
    already recorded (idempotent via alpaca_order_id). Returns the count
    of sales whose cost basis could not be established.

    REALIZED P&L IS COMPUTED HERE, AND THAT IS THE WHOLE POINT.

    A sale's realized P&L is (fill price - average cost) x shares, and
    the average cost is only knowable at this moment: this function runs
    BEFORE the positions table is rebuilt to mirror the broker, so
    `positions` still holds the cost basis as it stood before the sale.
    A full exit removes the position from Alpaca entirely, so a minute
    later there is nothing left to compute it from.

    Booking it onto the transaction row also makes it permanent and
    auditable — the ledger records what each trade actually earned,
    rather than the day's realized figure being re-derived (and
    re-derived differently) later.
    """
    already_booked = booked_ids if booked_ids is not None else _booked_order_ids(session)
    cost_basis_unknown = 0

    for order in newly_filled_orders:
        if order.alpaca_order_id in already_booked:
            continue

        side_sign = 1 if order.action in ("new_position", "add", "buy") else -1
        fill_price = float(order.fill_price)
        shares = float(order.shares)

        if side_sign > 0:
            realized = 0.0  # a purchase crystallises nothing
        else:
            position = session.query(Position).filter(Position.ticker == order.ticker).first()
            if position is not None and position.avg_cost is not None:
                realized = round((fill_price - float(position.avg_cost)) * shares, 2)
            else:
                # An orphan, or a name whose position row never existed.
                # Left as NULL rather than assumed to be zero — a zero
                # here would silently understate the day's realized P&L
                # and there would be nothing to notice it by.
                realized = None
                cost_basis_unknown += 1

        session.add(Transaction(
            transaction_date=order.order_date,
            ticker=order.ticker,
            action="buy" if side_sign > 0 else "sell",
            shares=order.shares,
            price=order.fill_price,
            amount=fill_price * shares * side_sign,
            alpaca_transaction_id=order.alpaca_order_id,
            realized_pnl=realized,
        ))

    return cost_basis_unknown


def _detect_discrepancies(session, today: date, alpaca_positions: list[dict]) -> list[dict]:
    """
    Two real, honest checks — never auto-resolved, only flagged:
    (1) something intended (Marcus's allocation) that never actually
        became a filled order, and (2) something HELD that doesn't
        trace back to any intended allocation on record.
    """
    discrepancies = []
    alpaca_tickers = {p["ticker"] for p in alpaca_positions}

    today_allocations = (
        session.query(Allocation)
        .filter(Allocation.allocation_date == today, Allocation.target_size_pct > 0,
                Allocation.action.in_(["new_position", "add"]))
        .all()
    )
    for alloc in today_allocations:
        matching_order = (
            session.query(Order)
            .filter(Order.order_date == today, Order.ticker == alloc.ticker, Order.status == "filled")
            .first()
        )
        if not matching_order:
            # An order that was placed and expired is a different fact
            # from one that was never placed, and the two used to read
            # identically here. The distinction is the difference
            # between "the market didn't meet our price" and "something
            # in the chain silently dropped the intent".
            attempted = (
                session.query(Order)
                .filter(Order.order_date == today, Order.ticker == alloc.ticker)
                .first()
            )
            if attempted is not None and attempted.status == "expired_unfilled":
                actual = f"order placed at {attempted.limit_price} but expired unfilled"
                description = (
                    f"Marcus intended a {alloc.action} on {alloc.ticker}; the order was "
                    f"placed and cancelled unfilled at the close. Not an error — the market "
                    f"did not reach the limit. Tomorrow's cycle will re-decide."
                )
            elif attempted is not None:
                actual = f"order recorded with status '{attempted.status}'"
                description = (
                    f"Marcus intended a {alloc.action} on {alloc.ticker}; an order exists "
                    f"but did not fill (status '{attempted.status}')."
                )
            else:
                actual = "no order of any kind found today"
                description = (
                    f"Marcus intended a {alloc.action} on {alloc.ticker} today, but no "
                    f"order was ever placed. The intent was dropped somewhere in the chain."
                )
            discrepancies.append({
                "ticker": alloc.ticker,
                "expected": f"filled order for {alloc.target_size_pct}% allocation",
                "actual": actual,
                "description": description,
            })

    known_tickers = {a.ticker for a in session.query(Allocation).all()}
    for ticker in alpaca_tickers - known_tickers:
        discrepancies.append({
            "ticker": ticker,
            "expected": "no position (no matching allocation on record)",
            "actual": f"currently holding {ticker}",
            "description": f"{ticker} is held in Alpaca but doesn't trace to any recorded Marcus allocation.",
        })

    return discrepancies


def run(today: date) -> dict:
    """
    Runs Otis: reconciles open orders, books newly-filled
    transactions, rebuilds the positions table to exactly match
    Alpaca, computes today's P&L, detects discrepancies, and (only
    if there's something worth narrating) gets a plain-language
    summary from Claude.
    """
    with session_scope() as session:
        # Read once, used by both: the reconciliation decides what to
        # look at, the booking decides what to write, and they must agree
        # on what is already in the ledger.
        booked_ids = _booked_order_ids(session)
        newly_filled = _reconcile_orders(session, today, booked_ids)

        # Sweep AFTER reconciling, so statuses are current before we
        # decide what is still live. A cancel can itself reveal a fill,
        # which then joins the booking list below.
        expired_orders, filled_late = _sweep_unfilled_orders(session, today, booked_ids)
        newly_filled.extend(filled_late)

        cost_basis_unknown = _book_new_transactions(session, newly_filled, booked_ids)

        alpaca_positions = alpaca_client.get_all_positions()
        account = alpaca_client.get_account()
        discrepancies = _detect_discrepancies(session, today, alpaca_positions)

        # ---- rebuild positions table to exactly match Alpaca ----
        alpaca_tickers = {p["ticker"] for p in alpaca_positions}
        session.query(Position).filter(Position.ticker.notin_(alpaca_tickers or [""])).delete(
            synchronize_session=False
        )
        for p in alpaca_positions:
            weight_pct = round(p["market_value"] / account["equity"] * 100, 2) if account["equity"] else 0.0
            sector = _resolve_sector(session, p["ticker"])
            stmt = pg_insert(Position).values(
                ticker=p["ticker"], shares=p["shares"], avg_cost=p["avg_cost"],
                market_value=p["market_value"], unrealized_pnl=p["unrealized_pnl"],
                weight_pct=weight_pct, last_updated=today, sector=sector,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["ticker"],
                set_={
                    "shares": stmt.excluded.shares, "avg_cost": stmt.excluded.avg_cost,
                    "market_value": stmt.excluded.market_value,
                    "unrealized_pnl": stmt.excluded.unrealized_pnl,
                    "weight_pct": stmt.excluded.weight_pct, "last_updated": stmt.excluded.last_updated,
                    # COALESCE, not overwrite: if this run couldn't resolve a
                    # sector (thesis closed, candidate row aged out) we keep
                    # the one already on record rather than blanking it and
                    # silently dropping the name out of its sector bucket.
                    "sector": func.coalesce(stmt.excluded.sector, Position.__table__.c.sector),
                },
            )
            session.execute(stmt)

            # ---- ALSO append to the permanent daily history — this
            # is what lets Vera later tell a sustained trend from a
            # single-day spike, which `positions` alone (overwritten
            # daily) can never support.
            unrealized_pnl_pct = (
                round(float(p["unrealized_pnl"]) / (float(p["market_value"]) - float(p["unrealized_pnl"])) * 100, 3)
                if (float(p["market_value"]) - float(p["unrealized_pnl"])) != 0 else 0.0
            )
            history_stmt = pg_insert(PositionPnlHistory).values(
                snapshot_date=today, ticker=p["ticker"], unrealized_pnl_pct=unrealized_pnl_pct,
                market_value=p["market_value"], weight_pct=weight_pct,
            )
            history_stmt = history_stmt.on_conflict_do_update(
                index_elements=["snapshot_date", "ticker"],
                set_={
                    "unrealized_pnl_pct": history_stmt.excluded.unrealized_pnl_pct,
                    "market_value": history_stmt.excluded.market_value,
                    "weight_pct": history_stmt.excluded.weight_pct,
                },
            )
            session.execute(history_stmt)

        # =========================================================
        # P&L — three daily FLOWS that reconcile to the equity move:
        #
        #     realized + unrealized_change = total = equity - last_equity
        #
        # realized comes from the ledger (booked at the point of sale,
        # see _book_new_transactions). unrealized_change is the residual
        # — today's mark-to-market on whatever is still open.
        #
        # This used to be the other way round: realized was derived as
        # (today's equity delta) - (LIFETIME unrealized across every
        # open position), subtracting a cumulative stock from a daily
        # flow. On a book carrying any open gain the result was wildly
        # wrong in both columns, and it fed Clara's attribution.
        # =========================================================
        total_pnl_today = account["equity"] - account["last_equity"]

        realized_rows = (
            session.query(Transaction)
            .filter(Transaction.transaction_date == today,
                    Transaction.realized_pnl.isnot(None))
            .all()
        )
        realized_today = float(sum(float(t.realized_pnl) for t in realized_rows))
        unrealized_change_today = total_pnl_today - realized_today

        # A STOCK, not a flow: lifetime open gain across held positions.
        # Reported separately so it can never again be mistaken for a
        # daily figure.
        open_unrealized = sum(p["unrealized_pnl"] for p in alpaca_positions)

        # nav is account equity at the close. It is recorded as its own
        # series because the drawdown circuit breaker needs an equity
        # curve — total_pnl is a single day's figure, and taking the
        # peak of THAT measures distance from your best day rather than
        # from the portfolio's high-water mark.
        pnl_stmt = pg_insert(DailyPnl).values(
            pnl_date=today, realized_pnl=round(realized_today, 2),
            unrealized_pnl=round(unrealized_change_today, 2),
            total_pnl=round(total_pnl_today, 2),
            open_unrealized_pnl=round(open_unrealized, 2),
            cash_balance=account["cash"], reconciled=(len(discrepancies) == 0),
            nav=round(account["equity"], 2),
        )
        pnl_stmt = pnl_stmt.on_conflict_do_update(
            index_elements=["pnl_date"],
            set_={
                "realized_pnl": pnl_stmt.excluded.realized_pnl,
                "unrealized_pnl": pnl_stmt.excluded.unrealized_pnl,
                "total_pnl": pnl_stmt.excluded.total_pnl,
                "open_unrealized_pnl": pnl_stmt.excluded.open_unrealized_pnl,
                "cash_balance": pnl_stmt.excluded.cash_balance,
                "reconciled": pnl_stmt.excluded.reconciled,
                "nav": pnl_stmt.excluded.nav,
            },
        )
        session.execute(pnl_stmt)

        # ---- discrepancies: insert only genuinely new ones ----
        for d in discrepancies:
            exists = (
                session.query(Discrepancy)
                .filter(Discrepancy.found_date == today, Discrepancy.ticker == d["ticker"],
                        Discrepancy.description == d["description"])
                .first()
            )
            if not exists:
                session.add(Discrepancy(found_date=today, **d))

    # =============================================================
    # [4] THE AGENTIC LOOP — only if there's something worth saying.
    # =============================================================
    narrative = "Portfolio is empty and there is nothing to reconcile today."
    if alpaca_positions or discrepancies:
        summary = (
            f"Positions: {alpaca_positions}\n"
            f"Realized P&L today: {realized_today:.2f}, "
            f"unrealized change today: {unrealized_change_today:.2f}, "
            f"total: {total_pnl_today:.2f}\n"
            f"Open (lifetime) unrealized across held positions: {open_unrealized:.2f}\n"
            f"Discrepancies found: {discrepancies or 'none'}"
        )
        raw = run_agent_loop(
            system_prompt=OTIS_SYSTEM_PROMPT,
            user_prompt=summary,
            tools=[],
            tool_executor=lambda name, inp: (_ for _ in ()).throw(
                RuntimeError(f"Otis has no tools, but one was called: {name}")
            ),
            max_tokens=500,
        )
        narrative = ReconciliationNarrative.model_validate(extract_json(raw)).narrative

    return {
        "date": today,
        "reconciled": len(discrepancies) == 0,
        "positions": alpaca_positions,
        "cash_balance": account["cash"],
        "nav": round(account["equity"], 2),
        "realized_pnl_today": round(realized_today, 2),
        "unrealized_change_today": round(unrealized_change_today, 2),
        "total_pnl_today": round(total_pnl_today, 2),
        "open_unrealized_pnl": round(open_unrealized, 2),
        "cost_basis_unknown": cost_basis_unknown,
        "expired_unfilled": expired_orders,
        "discrepancies": discrepancies,
        "narrative": narrative,
    }


if __name__ == "__main__":
    # Manual smoke test: python -m agents.otis
    # Does NOT chain through the other agents — Otis reads Alpaca's
    # real state directly, which already exists from Ada's earlier
    # test. This should surface a genuine discrepancy: the AAPL
    # share Ada placed synthetically has no matching Marcus
    # allocation on record.
    result = run(date.today())
    print(result)