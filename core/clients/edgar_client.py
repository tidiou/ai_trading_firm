"""
SEC EDGAR — the primary source for reported fundamentals (S2a).

=====================================================================
WHY EDGAR AND NOT MORE FMP
=====================================================================

Vera scores conviction from an FMP snapshot: one point in time, no
history. She cannot tell whether a margin is the best in a decade or
the worst, whether capex has climbed three years running, or whether
reported earnings ever converted into cash. That is not something a
better prompt fixes — the time series was never fetched.

FMP cannot supply it either. The free tier gates deep fundamentals,
has already paywalled news and the economic calendar on this account,
and caps everything at 250 requests a day, a budget Vera's screening
pass already competes for.

EDGAR is the filing itself rather than a vendor's copy of it. No
authentication, no API key, no daily quota. The obligations are
different in kind:

  IDENTIFY YOURSELF. The SEC requires a User-Agent carrying real
  contact information. There is no default here and no fallback: an
  unset EDGAR_USER_AGENT raises at import, because quietly sending a
  generic agent to a government service on someone's behalf is not a
  decision this module gets to make.

  STAY UNDER TEN REQUESTS A SECOND. Their published ceiling, "carefully
  monitored to preserve equitable access". The throttle below is
  deliberately set well under it — see MIN_INTERVAL.

=====================================================================
WHAT IS HERE AND WHAT IS NOT
=====================================================================

companyfacts returns CONSOLIDATED figures only. There is no
dimensional breakdown, which means NO SEGMENT revenue and no segment
margins. Segment detail requires parsing the filing's own XBRL
instance or its text; management incentives live in the DEF 14A and
are document parsing. Both are real work with a real per-filing cost,
both are out of scope, and neither is blocked by anything here.

Stated plainly so that nobody builds a segment analysis on top of this
module and discovers the gap three layers up.

=====================================================================
THE PARSER IS SEPARATE FROM THE FETCH, ON PURPOSE
=====================================================================

`iter_facts()` takes a payload and yields normalised rows. It touches
no network and no clock, so the shape-handling — which is where this
will actually break — is testable against a fixture.

And it is written to SKIP a malformed fact rather than raise. A
companyfacts payload for a large filer carries tens of thousands of
facts across hundreds of concepts; one unexpected entry should cost
that entry, not the entire company's history. Every skip is counted
and returned, so a degraded parse announces itself instead of looking
like a thin filer.
"""

import json
import logging
import os
import threading
import time
from typing import Iterable, Iterator, Optional

import requests
from dotenv import load_dotenv

load_dotenv()  # same defensive call as core/db.py — import order is not guaranteed

logger = logging.getLogger(__name__)

DATA_BASE = "https://data.sec.gov"
WWW_BASE = "https://www.sec.gov"
TICKER_MAP_URL = f"{WWW_BASE}/files/company_tickers.json"

# The SEC's published ceiling is 10 requests/second. This is set to 5,
# deliberately: the ceiling is per-IP and "carefully monitored", the
# cost of being wrong is losing access for the whole desk, and nothing
# here is latency-sensitive — a research pass that takes twice as long
# is invisible, while a block is not.
MAX_REQUESTS_PER_SECOND = 5
MIN_INTERVAL = 1.0 / MAX_REQUESTS_PER_SECOND

# A contact address is not optional. See the module docstring.
EDGAR_USER_AGENT = os.environ.get("EDGAR_USER_AGENT", "").strip()

TIMEOUT_SECONDS = 30

# The facts every row needs. A fact missing any of these cannot be
# stored or compared, so it is skipped rather than guessed at.
_REQUIRED_FACT_KEYS = ("end", "val", "accn", "filed")


class EdgarNotConfigured(RuntimeError):
    """EDGAR_USER_AGENT is missing. Raised at call time rather than
    import time so the rest of the codebase — and the test suite —
    still imports on a machine that has never fetched a filing."""


def _headers() -> dict:
    if not EDGAR_USER_AGENT:
        raise EdgarNotConfigured(
            "EDGAR_USER_AGENT is not set. The SEC requires automated clients "
            "to identify themselves with real contact information in the "
            "User-Agent header. Add a line to .env, for example:\n\n"
            "    EDGAR_USER_AGENT=MBY-Trading research you@example.com\n\n"
            "There is deliberately no default: sending a generic agent to a "
            "government service under your IP is not a choice this module "
            "makes for you."
        )
    return {
        "User-Agent": EDGAR_USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
        "Accept": "application/json",
    }


# ---------------------------------------------------------------
# Throttle
#
# Module-level and lock-guarded because the limit is per IP, not per
# caller. Two agents fetching concurrently would each keep to their own
# pace and together breach the ceiling, so the interval is enforced
# once for the whole process.
# ---------------------------------------------------------------
_throttle_lock = threading.Lock()
_last_request_at = 0.0


def _throttled_get(url: str, *, max_retries: int = 3) -> dict:
    """GET with the SEC's rate limit respected and backoff on 429/5xx.

    A 404 is returned as an empty dict rather than raised: a company
    with no XBRL facts under a given concept is an ordinary answer, not
    an error, and the caller decides what an absence means.
    """
    global _last_request_at
    headers = _headers()

    for attempt in range(max_retries):
        with _throttle_lock:
            wait = MIN_INTERVAL - (time.monotonic() - _last_request_at)
            if wait > 0:
                time.sleep(wait)
            _last_request_at = time.monotonic()

        resp = requests.get(url, headers=headers, timeout=TIMEOUT_SECONDS)

        if resp.status_code == 404:
            logger.info("EDGAR 404 for %s — no data under that path.", url)
            return {}

        if resp.status_code == 403:
            # Almost always the User-Agent, and the message says so
            # rather than leaving someone to guess at a bare 403.
            raise RuntimeError(
                f"EDGAR returned 403 for {url}. This is usually the "
                f"User-Agent: the SEC rejects clients that do not identify "
                f"themselves with contact information. Current value: "
                f"{EDGAR_USER_AGENT!r}"
            )

        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt < max_retries - 1:
                delay = 2 ** attempt  # 1s, 2s
                logger.warning("EDGAR %s for %s — retrying in %ss.",
                               resp.status_code, url, delay)
                time.sleep(delay)
                continue
            raise RuntimeError(
                f"EDGAR returned {resp.status_code} for {url} after "
                f"{max_retries} attempts. If this is a 429, the 10 req/s "
                f"ceiling is being breached somewhere — check for a second "
                f"process fetching at the same time, since the limit is "
                f"per-IP rather than per-client."
            )

        resp.raise_for_status()
        return resp.json()

    return {}  # unreachable; keeps the type honest


# ---------------------------------------------------------------
# Identity
# ---------------------------------------------------------------
_ticker_map_cache: Optional[dict] = None


def cik_for(ticker: str) -> Optional[str]:
    """The zero-padded 10-digit CIK for a ticker, or None.

    Cached for the process: the mapping file is a few hundred KB and
    covers every filer, so fetching it once and answering from memory
    is both faster and better manners than a request per lookup.
    """
    global _ticker_map_cache
    if _ticker_map_cache is None:
        payload = _throttled_get(TICKER_MAP_URL)
        # Shape is {"0": {"cik_str": 320193, "ticker": "AAPL", "title": ...}}
        _ticker_map_cache = {
            str(row["ticker"]).upper(): f"{int(row['cik_str']):010d}"
            for row in payload.values()
            if isinstance(row, dict) and row.get("ticker") and row.get("cik_str")
        }
        logger.info("Loaded %s ticker→CIK mappings from EDGAR.",
                    len(_ticker_map_cache))
    return _ticker_map_cache.get(ticker.upper())


def _cik_path(cik: str) -> str:
    """EDGAR wants CIK zero-padded to ten digits, prefixed with CIK."""
    return f"CIK{int(cik):010d}"


# ---------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------
def company_facts(cik: str) -> dict:
    """Every XBRL concept a filer has reported. One large payload —
    tens of MB for a mega-cap — so prefer `company_concept` when only
    a handful of concepts are wanted."""
    return _throttled_get(
        f"{DATA_BASE}/api/xbrl/companyfacts/{_cik_path(cik)}.json")


def company_concept(cik: str, tag: str, taxonomy: str = "us-gaap") -> dict:
    """One concept's full history for one filer. Far cheaper than
    companyfacts and shaped so `iter_facts` handles both."""
    return _throttled_get(
        f"{DATA_BASE}/api/xbrl/companyconcept/{_cik_path(cik)}/{taxonomy}/{tag}.json")


def submissions(cik: str) -> dict:
    """Filing history — form types, dates, accession numbers. This is
    what answers "what changed between the 2021 and 2024 10-K" at the
    level of which documents exist; reading them is a separate job."""
    return _throttled_get(f"{DATA_BASE}/submissions/{_cik_path(cik)}.json")


# ---------------------------------------------------------------
# Parse — pure, no network, no clock
# ---------------------------------------------------------------
def iter_facts(payload: dict, ticker: str,
               tags: Optional[Iterable[str]] = None) -> tuple[list[dict], dict]:
    """Normalise a companyfacts OR companyconcept payload into rows.

    Returns (rows, stats). `stats` carries what was skipped and why, so
    a degraded parse is visible rather than looking like a filer with
    little history.

    Handles both payload shapes because they differ only in nesting:

        companyfacts    {cik, entityName, facts: {taxonomy: {tag: {units: {...}}}}}
        companyconcept  {cik, taxonomy, tag, units: {...}}

    Each fact looks like:

        {"start": "2023-01-01", "end": "2023-03-31", "val": 1234,
         "accn": "0000320193-23-000064", "fy": 2023, "fp": "Q2",
         "form": "10-Q", "filed": "2023-05-05", "frame": "CY2023Q1"}

    `start` is absent on instant facts (a balance-sheet item has a date,
    not a period) and `frame` is absent on anything the SEC has not
    assigned to a calendar frame. Both are optional here. The four in
    _REQUIRED_FACT_KEYS are not: a fact without them cannot be stored
    or compared, so it is skipped and counted.
    """
    cik_raw = payload.get("cik")
    cik = f"{int(cik_raw):010d}" if cik_raw is not None else None

    rows: list[dict] = []
    skipped_missing_keys = 0
    skipped_unparsable = 0
    concepts_seen = 0

    # Flatten both shapes into (taxonomy, tag, unit, facts)
    def _blocks():
        if "facts" in payload:                       # companyfacts
            for taxonomy, concepts in (payload.get("facts") or {}).items():
                for tag, body in (concepts or {}).items():
                    for unit, facts in ((body or {}).get("units") or {}).items():
                        yield taxonomy, tag, unit, facts
        elif "units" in payload:                     # companyconcept
            taxonomy = payload.get("taxonomy", "us-gaap")
            tag = payload.get("tag")
            for unit, facts in (payload.get("units") or {}).items():
                yield taxonomy, tag, unit, facts

    wanted = set(tags) if tags else None

    for taxonomy, tag, unit, facts in _blocks():
        if wanted is not None and tag not in wanted:
            continue
        concepts_seen += 1
        for fact in facts or []:
            if not isinstance(fact, dict):
                skipped_unparsable += 1
                continue
            if any(fact.get(k) is None for k in _REQUIRED_FACT_KEYS):
                skipped_missing_keys += 1
                continue
            try:
                rows.append({
                    "cik": cik,
                    "ticker": ticker.upper(),
                    "taxonomy": taxonomy,
                    "tag": tag,
                    "unit": unit,
                    "fiscal_year": fact.get("fy"),
                    "fiscal_period": fact.get("fp"),
                    "period_start": fact.get("start"),
                    "period_end": fact["end"],
                    "value": float(fact["val"]),
                    "form": fact.get("form"),
                    "filed_date": fact["filed"],
                    "accession": fact["accn"],
                })
            except (TypeError, ValueError):
                # A non-numeric val, or a date that isn't one. One bad
                # fact, not one bad company.
                skipped_unparsable += 1

    stats = {
        "concepts": concepts_seen,
        "rows": len(rows),
        "skipped_missing_keys": skipped_missing_keys,
        "skipped_unparsable": skipped_unparsable,
    }
    if skipped_missing_keys or skipped_unparsable:
        logger.warning("EDGAR parse for %s skipped %s fact(s): %s missing "
                       "required keys, %s unparsable.", ticker,
                       skipped_missing_keys + skipped_unparsable,
                       skipped_missing_keys, skipped_unparsable)
    return rows, stats


def latest_per_period(rows: list[dict]) -> list[dict]:
    """One row per (tag, unit, period_end) — the most recently FILED.

    Restatements are kept in the database on purpose, so reading back
    needs a rule for which version is current. That rule is filing
    date, applied here rather than in SQL so it is testable and so the
    reasoning sits next to the reason.

    Note this does NOT delete anything. The superseded figure stays on
    record because a thesis formed on it must keep its evidence.
    """
    best: dict[tuple, dict] = {}
    for r in rows:
        key = (r["tag"], r["unit"], r["period_end"])
        incumbent = best.get(key)
        if incumbent is None or str(r["filed_date"]) > str(incumbent["filed_date"]):
            best[key] = r
    return sorted(best.values(), key=lambda r: (r["tag"], str(r["period_end"])))
