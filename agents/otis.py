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
                            idempotent via alpaca_order_id),
                            positions (replace-set to match Alpaca
                            exactly), daily_pnl (upsert per day),
                            discrepancies (insert-if-new)

GUARDRAIL, TAKEN SERIOUSLY: Otis NEVER silently resolves a
discrepancy. If Alpaca's real state doesn't match what was intended,
that gets flagged and left for review — never quietly "corrected" by
assumption. A bookkeeper who guesses is worse than one who says
"I don't know."

HONEST V1 SIMPLIFICATION: realized vs. unrealized P&L is split by
subtraction (total change in equity, from Account.equity vs.
Account.last_equity, minus the sum of current unrealized P&L across
positions) rather than from a true activity-level ledger. This holds
up fine for a paper account with no external cash flows, but is an
approximation worth knowing about, not a precise accounting figure.
"""

from datetime import date

from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.clients import alpaca_client
from core.clients.claude_client import run_agent_loop, extract_json
from core.db import session_scope
from core.models import (
    Position, Transaction, DailyPnl, Discrepancy, Order, Allocation,
)


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


def _reconcile_open_orders(session, today: date) -> list[dict]:
    """
    For any of today's orders not already confirmed filled/rejected/
    canceled at submission time, checks Alpaca for their current
    status and updates our record. Returns newly-filled orders (for
    transaction booking below).
    """
    open_orders = (
        session.query(Order)
        .filter(Order.order_date == today, Order.status.in_(["pending_new", "accepted", "new"]))
        .all()
    )
    newly_filled = []
    for order in open_orders:
        if not order.alpaca_order_id:
            continue
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


def _book_new_transactions(session, newly_filled_orders: list[Order]) -> None:
    """Append-only: books a transaction for any newly-filled order not
    already recorded (idempotent via alpaca_order_id)."""
    already_booked = {
        t.alpaca_transaction_id
        for t in session.query(Transaction).filter(Transaction.alpaca_transaction_id.isnot(None)).all()
    }
    for order in newly_filled_orders:
        if order.alpaca_order_id in already_booked:
            continue
        side_sign = 1 if order.action in ("new_position", "add", "buy") else -1
        session.add(Transaction(
            transaction_date=order.order_date,
            ticker=order.ticker,
            action="buy" if side_sign > 0 else "sell",
            shares=order.shares,
            price=order.fill_price,
            amount=float(order.fill_price) * float(order.shares) * side_sign,
            alpaca_transaction_id=order.alpaca_order_id,
        ))


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
            discrepancies.append({
                "ticker": alloc.ticker,
                "expected": f"filled order for {alloc.target_size_pct}% allocation",
                "actual": "no filled order found today",
                "description": f"Marcus intended a {alloc.action} on {alloc.ticker} today, but no filled order exists.",
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
        newly_filled = _reconcile_open_orders(session, today)
        _book_new_transactions(session, newly_filled)

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
            stmt = pg_insert(Position).values(
                ticker=p["ticker"], shares=p["shares"], avg_cost=p["avg_cost"],
                market_value=p["market_value"], unrealized_pnl=p["unrealized_pnl"],
                weight_pct=weight_pct, last_updated=today,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["ticker"],
                set_={
                    "shares": stmt.excluded.shares, "avg_cost": stmt.excluded.avg_cost,
                    "market_value": stmt.excluded.market_value,
                    "unrealized_pnl": stmt.excluded.unrealized_pnl,
                    "weight_pct": stmt.excluded.weight_pct, "last_updated": stmt.excluded.last_updated,
                },
            )
            session.execute(stmt)

        # ---- P&L (see module docstring re: the realized/unrealized split) ----
        unrealized_total = sum(p["unrealized_pnl"] for p in alpaca_positions)
        total_pnl_today = account["equity"] - account["last_equity"]
        realized_today = total_pnl_today - unrealized_total

        pnl_stmt = pg_insert(DailyPnl).values(
            pnl_date=today, realized_pnl=round(realized_today, 2),
            unrealized_pnl=round(unrealized_total, 2), total_pnl=round(total_pnl_today, 2),
            cash_balance=account["cash"], reconciled=(len(discrepancies) == 0),
        )
        pnl_stmt = pnl_stmt.on_conflict_do_update(
            index_elements=["pnl_date"],
            set_={
                "realized_pnl": pnl_stmt.excluded.realized_pnl,
                "unrealized_pnl": pnl_stmt.excluded.unrealized_pnl,
                "total_pnl": pnl_stmt.excluded.total_pnl,
                "cash_balance": pnl_stmt.excluded.cash_balance,
                "reconciled": pnl_stmt.excluded.reconciled,
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
            f"Realized P&L today: {realized_today:.2f}, Unrealized P&L: {unrealized_total:.2f}\n"
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
        "realized_pnl_today": round(realized_today, 2),
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