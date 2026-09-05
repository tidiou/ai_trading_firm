"""
Thin wrapper around Financial Modeling Prep's current "stable" API
(FMP retired the old /api/v3/ endpoints for any account created
after August 2025 — this project uses /stable/ throughout).

Each function here is "just" an HTTP call — deliberately dumb and
deterministic. This is the code half of the code/LLM split we
established in the Operating Manual: fetching data is never
something we want an LLM "deciding" how to do. The LLM's job starts
only once this data is in hand.

D7 — WHY THIS MODULE COUNTS ITS OWN FAILURES

Graceful degradation was the right call and is kept: one paywalled
ticker should not crash a run that is otherwise fine. But degrading
gracefully and degrading SILENTLY are different things, and this
module was doing the second. A 402 returned `{"error": ...}`, that
dict was handed to Claude as a tool result, and nothing anywhere
counted how many times it happened. Vera could screen six names,
receive real fundamentals for two, and produce confident candidates
from the remainder — with the run looking identical to a clean one.

A desk with a broken data feed does not trade on the fragment that
still works. It declares the run degraded and stands down.

So `_get()` — the single place where degradation is actually
detectable — now records every request against the symbol it was
for. Every caller below is a consumer that may or may not notice an
error dict; instrumenting here rather than in each of them is what
makes the count trustworthy.
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


# =================================================================
# DATA COVERAGE  (D7)
# =================================================================
class DataCoverage:
    """
    How much of what we asked for we actually got, this run.

    Keyed by SYMBOL rather than by request, because the question that
    matters downstream is not "how many HTTP calls succeeded" but "for
    how many of the names we looked at do we have a complete picture".
    `get_company_snapshot` makes two calls per ticker; a ticker whose
    profile arrived and whose key-metrics were paywalled is not a
    covered ticker, and a request-level count would score it 50% and
    move on.

    Endpoints with no symbol (the macro calendar, general news) are
    tracked separately so they cannot distort ticker coverage.

    Deliberately a plain module-level singleton reset at the top of a
    run, not something threaded through every call signature. This
    project runs one sequential cycle per process; a coverage object
    passed down through four layers of tool dispatch would be more
    correct in the abstract and worse to read, and the reset makes the
    lifetime explicit at the one place it matters.
    """

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        """Start a fresh count. Called at the top of an agent's run so
        one agent's degradation is never attributed to the next."""
        self._by_symbol: dict[str, dict[str, bool]] = {}
        self._symbolless: dict[str, bool] = {}

    def record(self, endpoint: str, symbol: str | None, ok: bool) -> None:
        if symbol:
            self._by_symbol.setdefault(str(symbol).upper(), {})[endpoint] = ok
        else:
            self._symbolless[endpoint] = ok

    def summary(self) -> dict:
        attempted = len(self._by_symbol)
        complete = sum(1 for calls in self._by_symbol.values() if all(calls.values()))
        degraded = {
            sym: sorted(e for e, ok in calls.items() if not ok)
            for sym, calls in self._by_symbol.items()
            if not all(calls.values())
        }
        request_outcomes = [ok for calls in self._by_symbol.values() for ok in calls.values()]
        request_outcomes += list(self._symbolless.values())

        return {
            "tickers_attempted": attempted,
            "tickers_complete": complete,
            # None, not 0.0, when nothing was requested. "We asked for
            # nothing" and "we asked and got nothing" are different
            # claims, and a gate that cannot tell them apart would stand
            # the desk down on a day it simply held no positions.
            "coverage_pct": round(complete / attempted * 100, 1) if attempted else None,
            "degraded_tickers": degraded,
            "requests_attempted": len(request_outcomes),
            "requests_degraded": sum(1 for ok in request_outcomes if not ok),
        }


coverage = DataCoverage()


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

    Every outcome is recorded against `coverage` (D7) so the run can
    afterwards say how much of what it asked for it actually got.
    """
    symbol = params.get("symbol")
    params = {**params, "apikey": FMP_API_KEY}
    max_retries = 3
    for attempt in range(max_retries):
        resp = requests.get(f"{FMP_BASE_URL}/{endpoint}", params=params, timeout=15)
        if resp.status_code in (402, 403):
            coverage.record(endpoint, symbol, ok=False)
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
        coverage.record(endpoint, symbol, ok=True)
        return resp.json()


def get_index_quotes() -> list[dict]:
    """
    Current level + daily % change for major US indices and VIX.
    The stable /quote endpoint takes one symbol at a time, so this
    loops rather than batching — fine at 4 calls, and simpler than
    debugging an unconfirmed batch-endpoint parameter name.

    Routes through the shared _get() helper (same as every other
    function below) — this was NOT true earlier: this function
    predates _get()'s retry-with-backoff logic and was still making
    a raw, unprotected request, so it had no retry/backoff on a 429
    while every function built after it did. Fixed here.

    The `continue` below is the exact silent-skip D7 is about: a run
    that got none of the four indices looked the same as one that got
    all four. It still skips — Atlas surviving on three indices is
    correct — but `coverage` has now already recorded the miss.
    """
    results = []
    for symbol in MACRO_QUOTE_SYMBOLS:
        data = _get("quote", {"symbol": symbol})
        if isinstance(data, dict) and "error" in data:
            continue  # plan/paywall issue for this symbol — skip, don't crash the whole call
        results.extend(data if isinstance(data, list) else [data])
    return results


def get_economic_calendar(from_date: str, to_date: str) -> list[dict]:
    """Scheduled macro releases (Fed, CPI, jobs, etc.) in a date range.
    Currently unused (dropped from Atlas's toolset — paid FMP tier
    only) but fixed to route through _get() for when it's reinstated."""
    return _get("economic-calendar", {"from": from_date, "to": to_date})


def get_macro_news(query: str, limit: int = 10) -> list[dict]:
    """General financial news, filtered client-side by a keyword.
    Currently unused (dropped from Atlas's toolset — paid FMP tier
    only) but fixed to route through _get() for when it's reinstated."""
    articles = _get("news/general-latest", {"page": 0, "limit": 50})
    if isinstance(articles, dict) and "error" in articles:
        return []
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
        if data:
            return data[0]
        # A 200 with an empty body is still no data. _get() recorded
        # this as a success because HTTP-wise it was one; the verdict
        # is corrected here, where the payload is actually inspected.
        coverage.record("quote", ticker, ok=False)
        return {"error": "no data returned"}
    return data


def get_company_snapshot(ticker: str) -> dict:
    """
    Combines company profile (sector, market cap, description) and
    key valuation metrics (P/E, revenue per share, etc.) into one
    result — presented to Claude as a single tool so he isn't
    juggling two near-identical calls for every ticker he checks.

    Two requests for one ticker, which is why coverage is tracked per
    symbol: a name whose profile arrived and whose valuation metrics
    were paywalled has not been researched, whatever the request count
    says.
    """
    profile_data = _get("profile", {"symbol": ticker})
    metrics_data = _get("key-metrics", {"symbol": ticker, "limit": 1})

    profile = profile_data[0] if isinstance(profile_data, list) and profile_data else profile_data
    metrics = metrics_data[0] if isinstance(metrics_data, list) and metrics_data else metrics_data

    return {"profile": profile, "key_metrics": metrics}
