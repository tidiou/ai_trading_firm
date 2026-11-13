#!/usr/bin/env python
"""
Can a name enter the desk? Checked against the source that can
actually serve each field.

WHAT THE FIRST VERSION OF THIS SCRIPT GOT WRONG. It graded every
field against FMP and concluded the bottleneck names were
unworkable. The finding was real — FMP's free tier answers /quote
and /key-metrics with HTTP 402 for anything outside a fixed sample
list, and GEV at $252bn fails exactly as hard as MOD at $10.5bn, so
it is an allow-list rather than a size threshold — but the
CONCLUSION was wrong, because FMP is not the only source wired into
this codebase.

    field        FMP free tier      what actually serves it
    ---------    ---------------    ------------------------------
    sector       WORKS (profile)    FMP
    market cap   WORKS (profile)    FMP
    price        402 paywall        Alpaca IEX — every US listing
    valuation    402 paywall        EDGAR XBRL — every SEC filer

Ada already sets every limit price from `alpaca_client
.get_latest_quote`, and `core/fundamentals.py` already reads EDGAR.
Neither has a sample list. So the question this script asks is no
longer "does FMP cover this name" but "can the desk assemble what it
needs for this name from the three sources it already has".

THE THREE CHECKS ARE INDEPENDENT ON PURPOSE. A name can be
researchable but untradeable, or the reverse, and those need
different answers. Collapsing them into one pass/fail is what
produced the misleading verdict the first time.

WHAT EACH FIELD IS FOR, since that is what makes a gap serious or
not:

    sector       Nora's max_sector_pct reads it. A null sector does
                 not raise — it silently exempts the position from
                 the sector cap, which is the worst of the three.
    price        Ada's limit price. Without it the name cannot be
                 traded at all.
    fundamentals Vera's valuation_snapshot, and the basis the thesis
                 is graded against later.

    python -m scripts.probe_fmp_coverage                  # control + 6
    python -m scripts.probe_fmp_coverage --control-only
    python -m scripts.probe_fmp_coverage --tickers GEV,VST,MU
    python -m scripts.probe_fmp_coverage --skip-edgar     # fast, no SEC calls

Exit codes: 0 every name READY · 3 at least one gap
            4 INCONCLUSIVE (the FMP control failed — quota, not coverage)
"""

import argparse
import sys

CONTROL = "MSFT"

# The bottleneck links from core.theme, ordered by descending market
# cap. The ordering is now vestigial — the first run proved coverage
# is not size-correlated — but it is kept so results stay comparable
# with that run.
DEFAULT_PROBE = ["GEV", "CEG", "VST", "VRT", "CRDO", "MOD"]


def _err(value) -> str:
    if isinstance(value, dict) and "error" in value:
        return str(value["error"])
    return ""


def _first_present(d: dict, keys: tuple[str, ...]):
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if v not in (None, "", 0):
            return v
    return None


# =================================================================
# The three sources
# =================================================================
def check_fmp(ticker: str) -> dict:
    """Sector and market cap, from the one FMP endpoint that works
    outside the sample list. Two requests: the paywalled key-metrics
    call is made anyway, deliberately, so the report can show which
    half of the snapshot survives — that distinction is the whole
    finding."""
    from core.clients import fmp_client

    out = {"sector": None, "market_cap": None, "profile_error": "",
           "metrics_error": "", "requests": 2}
    snap = fmp_client.get_company_snapshot(ticker)
    profile, metrics = snap.get("profile"), snap.get("key_metrics")

    if e := _err(profile):
        out["profile_error"] = e
    else:
        out["sector"] = _first_present(profile, ("sector", "industry"))
        out["market_cap"] = _first_present(profile, ("marketCap", "mktCap"))

    out["metrics_error"] = _err(metrics)
    return out


def check_alpaca(ticker: str) -> dict:
    """A tradeable price, from the feed Ada already executes against.

    Alpaca's free plan serves IEX — one venue rather than the
    consolidated tape, so the mid is close to but not identical to the
    official print. That is the same quote Ada has always used to set
    limit prices, so a name that passes here is no less tradeable than
    the six mega-caps already in the universe.
    """
    from core.clients import alpaca_client

    out = {"price": None, "spread_pct": None, "fallback": None, "error": "",
           "requests": 1}
    try:
        q = alpaca_client.get_latest_quote(ticker)
        out["price"] = q.get("mid")
        out["spread_pct"] = q.get("spread_pct")
        out["fallback"] = q.get("used_fallback_trade_price")
    except Exception as exc:                      # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def check_edgar(ticker: str) -> dict:
    """Does the SEC have enough to build a valuation from?

    One companyfacts request per name rather than one per tag: the
    payload is large for a mega-cap but it is a single call, and this
    is a diagnostic rather than the sync path.

    Resolving `revenue` specifically, because it is the concept with
    the most tag variation across filers — if the candidate list
    resolves for a name, the narrower concepts generally will too.
    """
    from core.clients.edgar_client import (
        EdgarNotConfigured, cik_for, company_facts, iter_facts)
    from core.fundamentals import TRACKED_TAGS, annual_rows, resolve_concept

    out = {"cik": None, "tag": None, "years": 0, "error": "", "requests": 0}
    try:
        cik = cik_for(ticker)
        out["requests"] += 1
        if not cik:
            out["error"] = "no CIK — not an SEC filer under this ticker"
            return out
        out["cik"] = cik

        payload = company_facts(cik)
        out["requests"] += 1
        rows, _stats = iter_facts(payload, ticker, TRACKED_TAGS)
        series = resolve_concept(annual_rows(rows), "revenue")
        if series.measurable:
            out["tag"] = series.tag
            out["years"] = len(series.points)
        else:
            out["error"] = series.unavailable or "no annual revenue resolved"
    except EdgarNotConfigured as exc:
        out["error"] = f"EDGAR not configured: {exc}".split("\n")[0]
    except Exception as exc:                      # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


# =================================================================
# Grading
# =================================================================
def grade(fmp: dict, alpaca: dict, edgar: dict, skip_edgar: bool) -> tuple[str, list]:
    """READY / RESEARCH_ONLY / TRADE_ONLY / BLOCKED, and what is missing.

    Sector is treated as mandatory for every verdict above BLOCKED,
    because a name with no sector is a name Nora's concentration cap
    cannot see — and that fails silently rather than loudly, which
    makes it worse than an absent price.
    """
    missing = []
    if not fmp["sector"]:
        missing.append("sector")
    if alpaca["price"] is None:
        missing.append("price")
    if not skip_edgar and not edgar["years"]:
        missing.append("fundamentals")

    if not fmp["sector"]:
        return "BLOCKED", missing
    if not missing:
        return "READY", missing
    if missing == ["price"]:
        return "RESEARCH_ONLY", missing
    if missing == ["fundamentals"]:
        return "TRADE_ONLY", missing
    return "BLOCKED", missing


def _cap(cap) -> str:
    if not isinstance(cap, (int, float)) or cap <= 0:
        return "        —"
    return f"{cap / 1e9:7.1f}bn"


def _price(p) -> str:
    return "      —" if p is None else f"{p:7.2f}"


def line(r: dict) -> str:
    return (f"  {r['ticker']:<6} {r['verdict']:<14} cap {_cap(r['fmp']['market_cap'])}  "
            f"px {_price(r['alpaca']['price'])}  "
            f"{str(r['fmp']['sector'] or '—')[:14]:<14} "
            f"edgar {r['edgar']['years'] or '—':>2}y  "
            f"missing: {','.join(r['missing']) or '-'}")


def probe(ticker: str, skip_edgar: bool) -> dict:
    fmp = check_fmp(ticker)
    alpaca = check_alpaca(ticker)
    edgar = ({"cik": None, "tag": None, "years": 0, "error": "skipped", "requests": 0}
             if skip_edgar else check_edgar(ticker))
    verdict, missing = grade(fmp, alpaca, edgar, skip_edgar)
    return {"ticker": ticker, "fmp": fmp, "alpaca": alpaca, "edgar": edgar,
            "verdict": verdict, "missing": missing}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tickers")
    p.add_argument("--control-only", action="store_true")
    p.add_argument("--skip-edgar", action="store_true",
                   help="skip the SEC calls — faster, and useful when only "
                        "tradeability is in question")
    args = p.parse_args(argv[1:])

    names = ([t.strip().upper() for t in args.tickers.split(",") if t.strip()]
             if args.tickers else list(DEFAULT_PROBE))
    if args.control_only:
        names = []

    print(f"Coverage probe — control {CONTROL} + {len(names)} name(s)")
    print("Each field is checked against the source that can serve it: "
          "sector from FMP,")
    print("price from Alpaca IEX, fundamentals from EDGAR XBRL.\n")

    # ---- the control, first and alone ----
    #
    # Only the FMP half needs a control. Alpaca and EDGAR have no
    # daily quota to exhaust, so a failure there is a real failure
    # rather than an ambiguous one.
    control = probe(CONTROL, args.skip_edgar)
    print("CONTROL")
    print(line(control))
    if not control["fmp"]["sector"]:
        print("\n" + "=" * 70)
        print("INCONCLUSIVE — FMP could not return a sector for the control.")
        print("Either the daily quota is exhausted or the key changed, and")
        print("either way this says nothing about the other names.")
        for e in [control["fmp"]["profile_error"]] or []:
            if e:
                print(f"  - {e}")
        print("=" * 70)
        return 4
    print("  -> FMP quota is alive. Proceeding.\n")

    if not names:
        return 0

    results = [probe(t, args.skip_edgar) for t in names]
    print("PROBE")
    for r in results:
        print(line(r))

    counts = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print("\n" + " · ".join(f"{n} {v}" for v, n in sorted(counts.items())))

    # ---- what is missing, and what it costs ----
    consequences = {
        "sector": "Nora's max_sector_pct silently stops applying to this name",
        "price": "Ada cannot set a limit price — the name is untradeable",
        "fundamentals": "Vera writes a candidate with an empty valuation_snapshot",
    }
    gaps = {f: [r["ticker"] for r in results if f in r["missing"]]
            for f in ("sector", "price", "fundamentals")}
    if any(gaps.values()):
        print("\nWHICH FIELD IS MISSING, AND WHAT IT BREAKS")
        for field, hit in gaps.items():
            if hit:
                print(f"  {field:<13} {', '.join(hit)}")
                print(f"  {'':<13} -> {consequences[field]}")

    # ---- the FMP paywall, reported rather than buried ----
    paywalled = [r["ticker"] for r in results if r["fmp"]["metrics_error"]]
    if paywalled:
        print(f"\nFMP key-metrics paywalled for: {', '.join(paywalled)}")
        print("  Expected, and no longer load-bearing — valuation now comes "
              "from EDGAR.")

    errs = sorted({r["alpaca"]["error"] for r in results if r["alpaca"]["error"]}
                  | {r["edgar"]["error"] for r in results
                     if r["edgar"]["error"] and r["edgar"]["error"] != "skipped"})
    if errs:
        print("\nRAW ERRORS (Alpaca / EDGAR)")
        for e in errs:
            print(f"  - {e}")

    spend = {
        "FMP": sum(r["fmp"]["requests"] for r in results) + control["fmp"]["requests"],
        "Alpaca": sum(r["alpaca"]["requests"] for r in results) + control["alpaca"]["requests"],
        "EDGAR": sum(r["edgar"]["requests"] for r in results) + control["edgar"]["requests"],
    }
    print("\nREQUESTS SPENT  " + "   ".join(f"{k} {v}" for k, v in spend.items()))
    print("  FMP is the only one with a daily cap (250 on the free tier). "
          "Alpaca and")
    print("  EDGAR are rate-limited per second, not per day.")

    print("\nVERDICT")
    ready = counts.get("READY", 0)
    if ready == len(results):
        print("  Every probed name can be researched AND traded with the")
        print("  sources already wired in. The universe can be widened; FMP's")
        print("  sample list is not the constraint it appeared to be.")
        return 0
    if counts.get("RESEARCH_ONLY"):
        print("  Some names are researchable but have no tradeable price.")
        print("  Vera could screen them; Ada could not act on them — which is")
        print("  a worse state than excluding them, because the work gets done")
        print("  and then discarded at the last step.")
    if counts.get("BLOCKED"):
        print("  Some names are missing a SECTOR, which is the one gap that")
        print("  fails silently: Nora's concentration cap cannot see a")
        print("  position it has no sector for.")
    return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
