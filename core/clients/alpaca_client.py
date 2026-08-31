"""
Thin wrapper around the Alpaca API — paper trading in v1, same
endpoints carry over unchanged when moving to live capital.
Used by: Ada (execution/orders), Otis (ground-truth reconciliation).
"""

import os
# from alpaca.trading.client import TradingClient
# from alpaca.data.historical import StockHistoricalDataClient

ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
ALPACA_PAPER = os.environ.get("ALPACA_PAPER", "true").lower() == "true"

# TODO: instantiate TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
