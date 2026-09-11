"""
Thin wrapper around the Alpaca API — paper trading in v1, same
endpoints carry over unchanged when moving to live capital (only
ALPACA_PAPER and the keys in .env would need to change).

Used by: Ada (execution), and later Otis (ground-truth reconciliation).
"""

import os
from datetime import date, datetime, time, timezone

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest, StockLatestQuoteRequest, StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame
from dotenv import load_dotenv

load_dotenv()

ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
ALPACA_PAPER = os.environ.get("ALPACA_PAPER", "true").lower() == "true"

# Which market-data feed historical bars come from. Defaults to IEX
# because that is what the free Alpaca data plan serves — asking for
# SIP on a free key returns HTTP 403 "subscription does not permit
# querying recent SIP data" rather than degrading. Set to "sip" once
# the data subscription is paid for.
ALPACA_DATA_FEED = os.environ.get("ALPACA_DATA_FEED", "iex").lower()

_trading_client = None
_data_client = None


def get_trading_client() -> TradingClient:
    global _trading_client
    if _trading_client is None:
        _trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
    return _trading_client


def get_data_client() -> StockHistoricalDataClient:
    global _data_client
    if _data_client is None:
        _data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    return _data_client


def get_account() -> dict:
    """Cash, equity, buying power for the account — needed to convert
    a target_size_pct into an actual number of shares, and (via
    equity vs last_equity) to compute today's total P&L without
    relying on a portfolio-history endpoint whose availability on
    the standard TradingClient (vs. the separate Broker API client)
    wasn't reliably confirmed — equity/last_equity are stable,
    well-documented Account fields, a safer bet than guessing."""
    account = get_trading_client().get_account()
    return {
        "cash": float(account.cash),
        "equity": float(account.equity),
        "last_equity": float(account.last_equity),
        "buying_power": float(account.buying_power),
    }


def get_buying_power() -> float:
    """
    Settled cash available to trade with, right now.

    Deliberately separate from get_account() rather than a field on it.
    Buying power is one of the few genuinely live broker facts — under
    T+1 settlement it diverges from equity whenever a sale has not
    settled — whereas NAV and holdings come from Otis's ledger
    (Operating Manual §4, core/ledger.py).

    get_account() hands back equity alongside buying power, so calling
    it for one puts the other within arm's reach, which is precisely
    the ambiguity that let Ada size off a live broker call while Nora
    enforced limits against the ledger. A function that can only return
    the one fact cannot be misused for the other.
    """
    return float(get_account()["buying_power"])


def get_order_by_id(order_id: str) -> dict:
    """Current status/fill details for a previously-submitted order —
    used by Otis to reconcile what was submitted against what
    actually happened."""
    order = get_trading_client().get_order_by_id(order_id)
    return {
        "status": str(order.status.value) if hasattr(order.status, "value") else str(order.status),
        "filled_qty": float(order.filled_qty) if order.filled_qty else 0.0,
        "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else None,
    }


def get_position(ticker: str) -> dict | None:
    """Current held quantity for a ticker, or None if not held."""
    positions = get_trading_client().get_all_positions()
    for p in positions:
        if p.symbol == ticker:
            return {"qty": float(p.qty), "market_value": float(p.market_value)}
    return None


def get_all_positions() -> list[dict]:
    """Every currently-held position, in full — used by Otis to
    rebuild the positions table as an exact mirror of Alpaca's real
    state (the actual ground truth for what the firm holds)."""
    positions = get_trading_client().get_all_positions()
    return [
        {
            "ticker": p.symbol,
            "shares": float(p.qty),
            "avg_cost": float(p.avg_entry_price),
            "market_value": float(p.market_value),
            "unrealized_pnl": float(p.unrealized_pl),
        }
        for p in positions
    ]


def get_latest_quote(ticker: str) -> dict:
    """
    Current bid/ask for a ticker — used to set a sane limit price and
    to compute the bid-ask spread as an illiquidity signal.

    Alpaca's free IEX feed doesn't always have a live two-sided quote
    outside active trading hours — a bid or ask of 0 (or missing) is
    a real, observed failure mode, not hypothetical (hit this during
    testing). Rather than hand back a nonsense price (which Alpaca's
    own order API will reject with "limit price must be > 0" anyway),
    fall back to the last actual TRADE price when the live quote
    looks broken — this also means Ada can be tested any time, not
    only during NYSE hours, which matters testing from Europe.
    """
    request = StockLatestQuoteRequest(symbol_or_symbols=[ticker])
    quote = get_data_client().get_stock_latest_quote(request)[ticker]
    bid, ask = float(quote.bid_price), float(quote.ask_price)

    if bid <= 0 or ask <= 0:
        trade_request = StockLatestTradeRequest(symbol_or_symbols=[ticker])
        trade = get_data_client().get_stock_latest_trade(trade_request)[ticker]
        last_price = float(trade.price)
        return {
            "bid": last_price, "ask": last_price, "mid": last_price,
            "spread_pct": 0.0, "used_fallback_trade_price": True,
        }

    mid = (bid + ask) / 2
    spread_pct = ((ask - bid) / mid * 100) if mid else 0.0
    return {
        "bid": bid, "ask": ask, "mid": mid,
        "spread_pct": round(spread_pct, 3), "used_fallback_trade_price": False,
    }


def get_daily_bars(ticker: str, start: date, end: date) -> list[dict]:
    """
    Daily closes for one symbol over a date range (D4).

    A narrow accessor in the same spirit as get_buying_power(): the
    caller gets the two fields it needs, not an Alpaca object graph, so
    the SDK's shape stays behind this module.

    HISTORICAL, NOT LIVE, AND THAT IS THE POINT. The benchmark is
    backfillable — you can ask for bars from before the desk existed —
    which is why D4 could be deferred without losing anything.

    TWO REQUEST OPTIONS THAT ARE NOT COSMETIC:

    `feed` — the free Alpaca data plan serves IEX and refuses recent
    SIP outright ("subscription does not permit querying recent SIP
    data", HTTP 403), which is what the first backfill hit. IEX is one
    venue rather than the consolidated tape, so its daily close is the
    last IEX print rather than the official closing auction — close but
    not identical. For a multi-session RETURN comparison on a name as
    liquid as SPY that difference is immaterial, and it is named here
    rather than hidden. Set ALPACA_DATA_FEED=sip on a paid plan to get
    the consolidated series instead.

    `adjustment=ALL` — split AND dividend adjusted, which makes the
    series a TOTAL-RETURN proxy. This one is a correctness fix, not a
    nicety: the desk's NAV already includes dividends it received, so
    comparing it against a price-only index would flatter the desk by
    roughly the index's yield every year, quietly and in one direction.
    A benchmark that is systematically 1.3% a year too low is exactly
    the kind of silent bias the rest of this system exists to refuse.

    Returns [{"date": date, "close": float}] ascending, empty on a
    range containing no sessions. Bars come back timezone-aware; a US
    equity daily bar is stamped at the session date, so the date part
    is taken directly rather than converted.
    """
    request = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TimeFrame.Day,
        start=datetime.combine(start, time.min, tzinfo=timezone.utc),
        end=datetime.combine(end, time.max, tzinfo=timezone.utc),
        feed=DataFeed(ALPACA_DATA_FEED),
        adjustment=Adjustment.ALL,
    )

    try:
        bars = get_data_client().get_stock_bars(request)
    except APIError as exc:
        # The subscription errors are worth translating. Raw, this
        # surfaces as a 403 with a message about SIP that says nothing
        # about which knob to turn — the same reasoning as the FMP
        # client distinguishing quota exhaustion from throttling.
        message = str(exc)
        if "subscription" in message.lower() or "sip" in message.lower():
            raise RuntimeError(
                f"Alpaca refused {ALPACA_DATA_FEED!r} market data for {ticker}: "
                f"{message}. The free data plan serves the IEX feed only — set "
                f"ALPACA_DATA_FEED=iex in .env (the default), or upgrade the "
                f"Alpaca data subscription to use sip."
            ) from exc
        raise

    rows = []
    for bar in bars.data.get(ticker, []):
        stamp = bar.timestamp
        rows.append({
            "date": stamp.date() if hasattr(stamp, "date") else stamp,
            "close": float(bar.close),
        })
    return sorted(rows, key=lambda r: r["date"])


def cancel_order(order_id: str) -> dict:
    """
    Cancels an order that has not filled.

    Needed for the end-of-day sweep (Operating Manual §7.7). Every order
    this desk places is TimeInForce.DAY, so an unfilled one expires on
    its own overnight — but "expired on its own" and "we cancelled it
    deliberately" are different facts, and only one of them is a
    decision on the record.

    Alpaca rejects a cancel on an order that has already reached a
    terminal state, which is a race we can lose legitimately: it may
    have filled between the status check and this call. That is not an
    error worth failing a reconciliation over, so it is caught and
    reported rather than raised — the subsequent status read is what
    settles what actually happened.
    """
    try:
        get_trading_client().cancel_order_by_id(order_id)
        return {"cancelled": True, "error": None}
    except Exception as exc:  # noqa: BLE001 — see docstring
        return {"cancelled": False, "error": str(exc)}


def submit_limit_order(ticker: str, side: str, qty: float, limit_price: float) -> dict:
    """
    Submits a limit order. side is 'buy' or 'sell'. Returns the
    immediate response from Alpaca — this is the ORDER STATUS AT
    SUBMISSION (e.g. 'accepted'), not necessarily filled yet.
    Confirming the actual fill is Otis's job (Operating Manual §7.7):
    Ada submits, Otis later reconciles what actually happened against
    what was intended. Deliberately not polling for a fill here.
    """
    order_data = LimitOrderRequest(
        symbol=ticker,
        qty=qty,
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        limit_price=round(limit_price, 2),
    )
    order = get_trading_client().submit_order(order_data=order_data)
    return {
        "alpaca_order_id": str(order.id),
        "status": str(order.status.value) if hasattr(order.status, "value") else str(order.status),
    }