"""
Thin wrapper around the Alpaca API — paper trading in v1, same
endpoints carry over unchanged when moving to live capital (only
ALPACA_PAPER and the keys in .env would need to change).

Used by: Ada (execution), and later Otis (ground-truth reconciliation).
"""

import os

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest
from dotenv import load_dotenv

load_dotenv()

ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
ALPACA_PAPER = os.environ.get("ALPACA_PAPER", "true").lower() == "true"

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