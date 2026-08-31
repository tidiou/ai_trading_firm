"""
Thin wrapper around Financial Modeling Prep's current "stable" API
(FMP retired the old /api/v3/ endpoints for any account created
after August 2025 — this project uses /stable/ throughout).

Each function here is "just" an HTTP call — deliberately dumb and
deterministic. This is the code half of the code/LLM split we
established in the Operating Manual: fetching data is never
something we want an LLM "deciding" how to do. The LLM's job starts
only once this data is in hand.
"""

import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

FMP_API_KEY = os.environ["FMP_API_KEY"]
FMP_BASE_URL = "https://financialmodelingprep.com/stable"

# Broad market/macro proxies — indices and the VIX.
MACRO_QUOTE_SYMBOLS = ["^GSPC", "^DJI", "^IXIC", "^VIX"]


def _get(endpoint: str, params: dict) -> dict | list:
    """
    Shared GET helper with graceful degradation on paywall errors,
    and retry-with-backoff on rate limiting.

    We've now hit FMP's per-endpoint (and per-symbol) paywalls twice
    building this project — the free tier restricts some endpoints
    entirely (402/403) and some to a fixed sample list of tickers.
    Rather than letting a single unavailable ticker crash an entire
    agent run, a 402/403 here is caught and returned as a small error
    dict instead of raised — the calling agent can then just note
    "data unavailable" for that one item and carry on, which is a
    much better failure mode for something running unattended every
    day. Any OTHER error (5xx, timeout, bad auth) still raises
    normally — those are real problems, not an expected plan
    limitation.

    A 429 (rate limited) gets a few retries with increasing delay —
    this handles transient per-minute throttling. If it's still 429
    after retries, that's very likely genuine DAILY QUOTA exhaustion
    (free tier: 250 requests/day) rather than a momentary spike, in
    which case retrying further won't help — we raise a clear error
    saying so rather than a bare HTTPError.
    """
    params = {**params, "apikey": FMP_API_KEY}
    max_retries = 3
    for attempt in range(max_retries):
        resp = requests.get(f"{FMP_BASE_URL}/{endpoint}", params=params, timeout=15)
        if resp.status_code in (402, 403):
            return {"error": f"not available on current FMP plan (HTTP {resp.status_code})"}
        if resp.status_code == 429:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # 1s, 2s, then give up
                continue
            raise RuntimeError(
                "FMP returned 429 (rate limited) after retries. This usually means "
                "the free tier's daily request quota (250/day) is exhausted from "
                "repeated testing today — it resets on a rolling 24h basis, not at "
                "midnight. Wait before retrying, or reduce how many tickers/reruns "
                "you're testing with in the meantime."
            )
        resp.raise_for_status()
        return resp.json()


def get_index_quotes() -> list[dict]:
    """
    Current level + daily % change for major US indices and VIX.
    The stable /quote endpoint takes one symbol at a time, so this
    loops rather than batching — fine at 4 calls, and simpler than
    debugging an unconfirmed batch-endpoint parameter name.
    """
    results = []
    for symbol in MACRO_QUOTE_SYMBOLS:
        url = f"{FMP_BASE_URL}/quote"
        resp = requests.get(url, params={"symbol": symbol, "apikey": FMP_API_KEY}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # FMP returns a list even for a single symbol — normalize either way.
        results.extend(data if isinstance(data, list) else [data])
    return results


def get_economic_calendar(from_date: str, to_date: str) -> list[dict]:
    """Scheduled macro releases (Fed, CPI, jobs, etc.) in a date range."""
    url = f"{FMP_BASE_URL}/economic-calendar"
    resp = requests.get(
        url, params={"from": from_date, "to": to_date, "apikey": FMP_API_KEY}, timeout=15
    )
    resp.raise_for_status()
    return resp.json()


def get_macro_news(query: str, limit: int = 10) -> list[dict]:
    """General financial news, filtered client-side by a keyword."""
    url = f"{FMP_BASE_URL}/news/general-latest"
    resp = requests.get(
        url, params={"page": 0, "limit": 50, "apikey": FMP_API_KEY}, timeout=15
    )
    resp.raise_for_status()
    articles = resp.json()
    query_lower = query.lower()
    matches = [a for a in articles if query_lower in a.get("title", "").lower()
               or query_lower in a.get("text", "").lower()]
    return matches[:limit] if matches else articles[:limit]


# ---------------------------------------------------------------
# Vera's data — one stock at a time, not the broad-market proxies
# Atlas uses above.
# ---------------------------------------------------------------
def get_stock_quote(ticker: str) -> dict:
    """Current price, change, volume for a single stock."""
    data = _get("quote", {"symbol": ticker})
    if isinstance(data, list):
        return data[0] if data else {"error": "no data returned"}
    return data


def get_company_snapshot(ticker: str) -> dict:
    """
    Combines company profile (sector, market cap, description) and
    key valuation metrics (P/E, revenue per share, etc.) into one
    result — presented to Claude as a single tool so he isn't
    juggling two near-identical calls for every ticker he checks.
    """
    profile_data = _get("profile", {"symbol": ticker})
    metrics_data = _get("key-metrics", {"symbol": ticker, "limit": 1})

    profile = profile_data[0] if isinstance(profile_data, list) and profile_data else profile_data
    metrics = metrics_data[0] if isinstance(metrics_data, list) and metrics_data else metrics_data

    return {"profile": profile, "key_metrics": metrics}