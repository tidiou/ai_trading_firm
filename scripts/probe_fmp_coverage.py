#!/usr/bin/env python
"""
Does FMP's free tier actually cover the bottleneck names?

WHY THIS SCRIPT EXISTS. `FIXED_UNIVERSE` was cut from 16 names to 6
mega-caps to save FMP quota, and nothing on record says WHICH of the
other ten failed or how. The theme universe in core/theme.py is 27
names, 22 of which have never been through the data path. Swapping
Vera's universe before knowing that would not produce errors — it
would produce SILENT GAPS, because fmp_client degrades gracefully by
design: a paywalled ticker comes back as {"error": ...}, gets handed
to Claude as a tool result, and a candidate can be written from the
fragment that still worked.

So this answers one question before any universe change is made: for
each name, do we get a price, a SECTOR, and a valuation figure?

THE CONTROL TICKER IS THE WHOLE DESIGN. FMP's free tier is 250
requests per rolling 24 hours, and an exhausted quota looks exactly
like a paywalled ticker at this layer — both arrive as a failure on a
name. So a known-good mega-cap runs FIRST, and if IT fails the script
aborts immediately rather than burning twenty more requests to
produce a result that cannot be interpreted. A probe that cannot tell
"not covered" from "out of quota" is worse than no probe: it would
retire names that were fine.

WHAT COUNTS AS COVERED, and why not just HTTP 200:

  price      Ada needs it to set a limit price.
  sector     Nora's max_sector_pct reads it. A null sector does not
             raise — it silently exempts the position from the sector
             cap, which is the most dangerous of the three misses.
  valuation  Vera writes it into candidate.valuation_snapshot, and
             the thesis is graded against it later.

A name with a price and no sector is reported as PARTIAL, not as a
pass, because that is the case that breaks a limit rather than a run.

    python -m scripts.probe_fmp_coverage              # control + 6 names, 21 requests
    python -m scripts.probe_fmp_coverage --control-only        # 3 requests
    python -m scripts.probe_fmp_coverage --tickers GEV,VST,MU  # your own list

Exit codes: 0 every name fully covered · 3 at least one gap
            4 INCONCLUSIVE (control failed — quota or auth, not coverage)
"""

import argparse
import sys

# The control: already in FIXED_UNIVERSE, so it is known to work on
# this plan. Its only job is to prove the quota is alive.
CONTROL = "MSFT"

# The bottleneck links from core.theme, ordered roughly by descending
# market cap. Ordered that way on purpose: if the free tier restricts
# by a fixed sample list of large names, the failures will cluster at
# the bottom and the boundary becomes visible rather than a scatter.
DEFAULT_PROBE = ["GEV", "CEG", "VST", "VRT", "CRDO", "MOD"]

REQUESTS_PER_TICKER = 3      # quote=1, profile=1, key-metrics=1


def _err(value) -> str:
    """The error string if this payload is fmp_client's error dict."""
    if isinstance(value, dict) and "error" in value:
        return str(value["error"])
    return ""


def _first_present(d: dict, keys: tuple[str, ...]):
    """FMP renames fields between plan tiers and endpoint versions, so
    a missing key is not evidence of a missing figure. Try the known
    spellings before concluding anything is absent."""
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if v not in (None, "", 0):
            return v
    return None


def probe(ticker: str) -> dict:
    """Three calls for one name, graded on what the desk needs."""
    from core.clients import fmp_client

    out = {"ticker": ticker, "price": None, "sector": None,
           "market_cap": None, "valuation": None, "errors": []}

    quote = fmp_client.get_stock_quote(ticker)
    if e := _err(quote):
        out["errors"].append(f"quote: {e}")
    else:
        out["price"] = _first_present(quote, ("price", "previousClose"))

    snap = fmp_client.get_company_snapshot(ticker)
    profile, metrics = snap.get("profile"), snap.get("key_metrics")

    if e := _err(profile):
        out["errors"].append(f"profile: {e}")
    else:
        out["sector"] = _first_present(profile, ("sector", "industry"))
        out["market_cap"] = _first_present(profile, ("marketCap", "mktCap"))

    if e := _err(metrics):
        out["errors"].append(f"key-metrics: {e}")
    else:
        # Any one of these is enough for a valuation_snapshot; which
        # one is present varies by plan and by name.
        out["valuation"] = _first_present(metrics, (
            "peRatio", "priceToEarningsRatio", "evToSales",
            "enterpriseValueOverEBITDA", "revenuePerShare"))

    missing = [n for n, v in (("price", out["price"]),
                              ("sector", out["sector"]),
                              ("valuation", out["valuation"])) if v is None]
    out["missing"] = missing
    if not missing:
        out["verdict"] = "FULL"
    elif len(missing) == 3:
        out["verdict"] = "NONE"
    else:
        out["verdict"] = "PARTIAL"
    return out


def _fmt_cap(cap) -> str:
    if not isinstance(cap, (int, float)) or cap <= 0:
        return "        —"          # 9 chars, matching the figure below
    return f"{cap / 1e9:7.1f}bn"


def _line(r: dict) -> str:
    gap = ",".join(r["missing"]) or "-"
    return (f"  {r['ticker']:<6} {r['verdict']:<8} cap {_fmt_cap(r['market_cap'])}  "
            f"sector {str(r['sector'] or '—')[:22]:<22} missing: {gap}")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tickers", help="comma-separated list to probe instead of the default")
    p.add_argument("--control-only", action="store_true",
                   help="just check the quota is alive (3 requests)")
    args = p.parse_args(argv[1:])

    names = ([t.strip().upper() for t in args.tickers.split(",") if t.strip()]
             if args.tickers else list(DEFAULT_PROBE))
    if args.control_only:
        names = []

    budget = (1 + len(names)) * REQUESTS_PER_TICKER
    print(f"FMP coverage probe — control {CONTROL} + {len(names)} name(s)")
    print(f"Budget: {budget} requests of the free tier's 250 per rolling 24h.\n")

    # ---- the control, first and alone ----
    control = probe(CONTROL)
    print("CONTROL")
    print(_line(control))
    if control["verdict"] != "FULL":
        print("\n" + "=" * 68)
        print("INCONCLUSIVE — the control name failed, so this tells us")
        print("nothing about coverage of the others. Either the daily quota")
        print("is exhausted or the key/plan changed. Errors reported:")
        for e in control["errors"] or ["(none — a 200 with an empty body)"]:
            print(f"  - {e}")
        print("\nNo further requests were made. Wait for the rolling 24h")
        print("window to clear, then run again.")
        print("=" * 68)
        return 4
    print("  -> quota is alive and the plan covers a mega-cap. Proceeding.\n")

    if not names:
        return 0

    results = [probe(t) for t in names]
    print("PROBE")
    for r in results:
        print(_line(r))

    full = [r for r in results if r["verdict"] == "FULL"]
    partial = [r for r in results if r["verdict"] == "PARTIAL"]
    none = [r for r in results if r["verdict"] == "NONE"]

    print(f"\n{len(full)} of {len(results)} fully covered · "
          f"{len(partial)} partial · {len(none)} no data")

    if partial or none:
        print("\nWHICH FIELD IS MISSING, AND WHAT IT BREAKS")
        for field, breaks in (
                ("sector", "Nora's max_sector_pct silently stops applying to this name"),
                ("valuation", "Vera writes a candidate with an empty valuation_snapshot"),
                ("price", "Ada cannot set a limit price — the name is untradeable")):
            hit = [r["ticker"] for r in results if field in r["missing"]]
            if hit:
                print(f"  {field:<10} {', '.join(hit)}")
                print(f"  {'':<10} -> {breaks}")

    errs = {e for r in results for e in r["errors"]}
    if errs:
        print("\nRAW ERRORS")
        for e in sorted(errs):
            print(f"  - {e}")

    from core.clients.fmp_client import coverage
    s = coverage.summary()
    print(f"\nfmp_client's own count: {s['requests_attempted']} requests, "
          f"{s['requests_degraded']} degraded · "
          f"{s['tickers_complete']}/{s['tickers_attempted']} tickers complete")

    print("\nVERDICT")
    if not partial and not none:
        print("  Every probed name came back complete. A universe swap to the")
        print("  bottleneck links is feasible on this plan — but note the")
        print("  QUOTA arithmetic is a separate question from coverage:")
        print(f"  {REQUESTS_PER_TICKER} requests per name means 250/day caps a screening")
        print(f"  pass at ~{250 // REQUESTS_PER_TICKER} names before anything else runs.")
        return 0
    print("  Do NOT swap the universe yet. The names above would enter")
    print("  Vera's screen and come back partly empty, which this codebase")
    print("  turns into a confident candidate rather than an error.")
    return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
