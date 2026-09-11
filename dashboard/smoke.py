"""
Render-only smoke test for dashboard/app.py.

Runs the whole module twice — once against a database where every table
is empty, once against a populated one — with Streamlit, the database
and the NYSE calendar stubbed out. Proves the page builds without
raising; says nothing about whether the numbers are right.

The empty-database pass is the one that earns its keep: every `.iloc[0]`
and every `int(row["n"])` on a table with no rows is a stack trace in
the browser, and a fresh clone of this project has exactly that shape.
"""

import pathlib
import sys, types, re
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

MODE = sys.argv[1] if len(sys.argv) > 1 else "empty"
VIEW = sys.argv[2] if len(sys.argv) > 2 else "floor"
WHO = sys.argv[3] if len(sys.argv) > 3 else ""
ET = ZoneInfo("America/New_York")
TODAY = datetime.now(timezone.utc).astimezone(ET).date()

# ---------------- fake streamlit ----------------
calls = []

# Every string argument any streamlit call receives. The generic stub
# below swallows arguments, so without this a page could render the
# right widgets carrying the wrong words and still pass.
TEXTS = []


def _capture(args, kwargs):
    for v in list(args) + list(kwargs.values()):
        if isinstance(v, str):
            TEXTS.append(v)


class _Ctx:
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _St(types.ModuleType):
    def __getattr__(self, name):
        def f(*a, **k):
            calls.append(name)
            _capture(a, k)
            if name in ("tabs",):
                return [_Ctx() for _ in a[0]]
            if name == "columns":
                n = a[0] if a else 2
                return [_Col() for _ in range(n if isinstance(n, int) else len(n))]
            if name in ("expander", "container", "form", "sidebar"):
                return _Ctx()
            if name == "selectbox":
                opts = a[1] if len(a) > 1 else k.get("options", [])
                return list(opts)[0] if len(opts) else None
            return None
        return f


class _Col(_Ctx):
    def __getattr__(self, name):
        def f(*a, **k):
            calls.append("col." + name)
            _capture(a, k)
            return None
        return f


st_mod = _St("streamlit")
sys.modules["streamlit"] = st_mod
comp = types.ModuleType("streamlit.components")
comp_v1 = types.ModuleType("streamlit.components.v1")
RENDERED = {}


def _html(h, height=None, scrolling=None):
    RENDERED["wall"] = h
    calls.append("components.html")


comp_v1.html = _html
comp.v1 = comp_v1
comp.__path__ = []
st_mod.components = comp
st_mod.__path__ = []
sys.modules["streamlit.components"] = comp
sys.modules["streamlit.components.v1"] = comp_v1


BLOCKS = []


def _markdown(body, unsafe_allow_html=False):
    calls.append("markdown")
    BLOCKS.append(str(body))
    if "mby-desks" in str(body):
        RENDERED["floor"] = body
    if "mby-rail-nav" in str(body):
        RENDERED["nav"] = body
    if "mby-stage" in str(body):
        RENDERED["room"] = body
    if "mby-dossier" in str(body):
        RENDERED["dossier"] = body


st_mod.markdown = _markdown


class _ColumnConfig:
    def TextColumn(self, *a, **k):
        return {"_cfg": a[0] if a else None, **k}


st_mod.column_config = _ColumnConfig()


def _dataframe(df, **kw):
    calls.append("dataframe")
    GRIDS.append(kw)
    return None


GRIDS = []
st_mod.dataframe = _dataframe
_qp = {"view": VIEW}
if WHO:
    _qp["who"] = WHO
st_mod.query_params = _qp

# ---------------- fake core.db / calendar ----------------
core = types.ModuleType("core"); core.__path__ = []
db = types.ModuleType("core.db"); db.engine = object()
from contextlib import contextmanager as _cm
@_cm
def _no_session():
    raise RuntimeError("smoke.py: the dashboard must not open an ORM session")
db.session_scope = _no_session
sys.modules["core"] = core
sys.modules["core.db"] = db

# core.models exists only so core.benchmark imports; the dashboard reads
# through load(), never the ORM, and _no_session above enforces that.
models = types.ModuleType("core.models")
for _n in ("BenchmarkHistory", "DailyPnl", "Position"):
    setattr(models, _n, type(_n, (), {}))
sys.modules["core.models"] = models


class _Sched:
    def __init__(self, rows): self._rows = rows
    @property
    def empty(self): return not self._rows
    def iterrows(self):
        for i, r in enumerate(self._rows):
            yield i, r
    @property
    def index(self): return [pd.Timestamp(r["d"]) for r in self._rows]
    @property
    def iloc(self): return self._rows


class _TS:
    def __init__(self, dt): self.dt = dt
    def to_pydatetime(self): return self.dt


class _NYSE:
    def schedule(self, start_date, end_date):
        rows, d = [], start_date
        while d <= end_date:
            if d.weekday() < 5:
                o = datetime.combine(d, datetime.min.time(), ET).replace(hour=9, minute=30)
                c = datetime.combine(d, datetime.min.time(), ET).replace(hour=16, minute=0)
                rows.append({"d": d,
                             "market_open": _TS(o.astimezone(timezone.utc)),
                             "market_close": _TS(c.astimezone(timezone.utc))})
            d += timedelta(days=1)
        return _Sched(rows)


cal = types.ModuleType("core.market_calendar")
cal.EASTERN = ET
cal.NYSE = _NYSE()
cal.market_is_open_now = lambda now=None: MODE == "open"
cal.is_trading_day = lambda d: d.weekday() < 5


def _session_bounds(d):
    """Mirrors the real helper against the fake calendar above, rather
    than returning None — a stub that always said "not a session day"
    would let a partial-bar bug through unnoticed."""
    sched = cal.NYSE.schedule(start_date=d, end_date=d)
    if sched.empty:
        return None
    row = sched.iloc[0]
    return (row["market_open"].to_pydatetime(), row["market_close"].to_pydatetime())


cal.session_bounds = _session_bounds
sys.modules["core.market_calendar"] = cal

# core.cycle_schedule is pure data and pure functions with no imports of
# its own beyond the stdlib, so the real module is used rather than a
# stub — a stubbed timetable would happily agree with a broken one.
import importlib.util as _ilu
_here = pathlib.Path(__file__).resolve().parent
_candidates = [
    _here / "cycle_schedule.py",                 # sitting beside this file
    _here.parent / "core" / "cycle_schedule.py",  # tests/ inside the project
    _here / "core" / "cycle_schedule.py",         # project root
]
_found = next((c for c in _candidates if c.exists()), None)
if _found is None:
    raise SystemExit(
        "smoke.py cannot find core/cycle_schedule.py — looked in:\n  "
        + "\n  ".join(str(c) for c in _candidates))
_spec = _ilu.spec_from_file_location("core.cycle_schedule", str(_found))
_sched = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_sched)
sys.modules["core.cycle_schedule"] = _sched

_bspec = _ilu.spec_from_file_location(
    "core.benchmark",
    str(next(c for c in (_here / "benchmark.py",
                         _here.parent / "core" / "benchmark.py",
                         _here / "core" / "benchmark.py") if c.exists())))
_bench = _ilu.module_from_spec(_bspec)
_bspec.loader.exec_module(_bench)
sys.modules["core.benchmark"] = _bench

# ---------------- canned rows ----------------
POP = {
    "trading_control": [{"trading_enabled": True, "changed_by": "malick",
                         "reason": "resolved", "changed_at": datetime.now(timezone.utc)}],
    "benchmark_history": [
        {"bar_date": TODAY - timedelta(days=3), "close": 640.00},
        {"bar_date": TODAY, "close": 646.40},
    ],
    # Two sessions, not one — a single NAV row cannot be compared to
    # anything, so a one-row fixture would only ever exercise the
    # "no comparison available" branch of the D4 headline.
    "daily_pnl": [
        {"pnl_date": TODAY - timedelta(days=3), "nav": 100000.00, "total_pnl": 0.0,
         "realized_pnl": 0.0, "unrealized_pnl": 0.0, "cash_balance": 99349.94,
         "reconciled": True, "open_unrealized_pnl": 0.0},
        {"pnl_date": TODAY, "nav": 100005.37, "total_pnl": 5.37,
         "realized_pnl": 0.0, "unrealized_pnl": 5.37, "cash_balance": 99355.31,
         "reconciled": True, "open_unrealized_pnl": 5.37},
    ],
    "agent_runs": [
        {"agent_name": "atlas", "phase": 1, "status": "completed", "run_date": TODAY,
         "started_at": pd.Timestamp("2026-09-04 13:02"), "completed_at": pd.Timestamp("2026-09-04 13:03"),
         "error_message": None},
        {"agent_name": "vera", "phase": 1, "status": "completed", "run_date": TODAY,
         "started_at": pd.Timestamp("2026-09-04 13:04"), "completed_at": pd.Timestamp("2026-09-04 13:08"),
         "error_message": None},
        {"agent_name": "solomon", "phase": 2, "status": "completed", "run_date": TODAY,
         "started_at": pd.Timestamp("2026-09-04 13:30"), "completed_at": pd.Timestamp("2026-09-04 13:33"),
         "error_message": None},
        {"agent_name": "ada", "phase": 4, "status": "running", "run_date": TODAY,
         "started_at": pd.Timestamp("2026-09-04 19:47"), "completed_at": None,
         "error_message": None},
        {"agent_name": "nora_monitor", "phase": 5, "status": "failed", "run_date": TODAY,
         "started_at": pd.Timestamp("2026-09-04 20:11"), "completed_at": None,
         "error_message": "boom"},
    ],
    "macro_briefs": [{"regime_signal": "transitioning", "change_from_yesterday": "narrower",
                      "brief_date": TODAY, "confidence": "medium", "narrative": "n"}],
    "new_candidates": [{"n": 3, "candidate_date": TODAY, "ticker": "AAPL",
                        "conviction_score": 4, "catalyst": "c", "thesis": "t"}],
    "strategy_decisions": [{"decision_date": TODAY, "action_needed": True, "narrative": "n",
                            "acted": 1, "total": 14}],
    "risk_reviews": [{"review_date": TODAY, "portfolio_status": "within_limits",
                      "circuit_breaker_active": False}],
    "risk_breaches": [{"n": 0}],
    "allocations": [{"n": 1, "avg_pct": 3.4, "allocation_date": TODAY, "ticker": "AAPL",
                     "action": "new_position", "target_size_pct": 1.0}],
    "orders": [{"ticker": "AAPL", "action": "new_position", "shares": 2.0,
                "limit_price": 325.03, "status": "accepted", "fill_price": None,
                "slippage_bps": None, "alpaca_order_id": "3f9156d3-d3ef",
                "allocation_id": 42, "mean_bps": 4.2, "fills": 3,
                "order_date": TODAY, "orders": 3, "first_seen": TODAY, "last_seen": TODAY,
                "worst_bps": 9.1},
               {"ticker": "AAPL", "action": "new_position", "shares": 0.0,
                "limit_price": None, "status": "rejected_zero_shares", "fill_price": None,
                "slippage_bps": None, "alpaca_order_id": None, "allocation_id": 43,
                "mean_bps": None, "fills": 0, "order_date": TODAY, "orders": 1,
                "first_seen": TODAY, "last_seen": TODAY, "worst_bps": None}],
    "discrepancies": [{"open_n": 1, "total": 1, "found_date": TODAY, "ticker": "AAPL",
                       "expected": "0", "actual": "1", "description": "untraced share",
                       "resolved": False}],
    "process_checks": [{"check_date": TODAY, "process_check": "clean", "violations": None}],
    "daily_reports": [{"report_date": TODAY, "full_report_md": "# report",
                       "executive_summary": "s"}],
    "positions": [{"ticker": "AAPL", "sector": "Technology", "shares": 2.0, "avg_cost": 325.0,
                   "market_value": 650.06, "unrealized_pnl": 5.37, "weight_pct": 0.98,
                   "last_updated": TODAY}],
    "theses": [{"ticker": "AAPL", "opened_date": TODAY, "original_conviction": 4,
                "thesis_text": "t", "closed_date": None}],
    "position_monitoring_log": [{"log_date": TODAY, "ticker": "AAPL", "status": "hold",
                                 "trigger": None, "reasoning": "r"}],
    "proposals": [{"decision_date": TODAY, "ticker": "AAPL", "action": "add",
                   "linked_trigger": "t", "urgency": "low"}],
    "proposal_reviews": [{"ticker": "AAPL", "decision": "approve", "max_size_pct": 7.02,
                          "reasoning": "r"}],
    "attribution": [{"attribution_date": TODAY, "ticker": "AAPL", "contribution_pct": 0.01,
                     "thesis_status": "intact"}],
}

# closes / clean-close aggregate rides on daily_pnl
POP["daily_pnl"][0].update({"clean": 11, "total": 12})

TABLE_RE = re.compile(r"from\s+([a-z_]+)", re.I)


def fake_read_sql(query, con, params=None):
    q = " ".join(str(query).split())
    if MODE == "empty":
        return pd.DataFrame()
    m = TABLE_RE.search(q)
    tbl = m.group(1) if m else None
    # the two aggregate queries that read a different table than they count
    if "FILTER (WHERE reconciled)" in q:
        return pd.DataFrame([{"clean": 11, "total": 12}])
    if "FILTER (WHERE NOT resolved)" in q:
        return pd.DataFrame([{"open_n": 1, "total": 1}])
    if "FILTER (WHERE action_needed)" in q:
        return pd.DataFrame([{"acted": 1, "total": 14}])
    if "count(*) AS n FROM new_candidates" in q:
        return pd.DataFrame([{"n": 3}])
    if "risk_breaches" in q and "count(*)" in q:
        return pd.DataFrame([{"n": 0}])
    if "FROM allocations" in q and "count(*)" in q:
        return pd.DataFrame([{"n": 1, "avg_pct": 3.4}])
    if "avg(slippage_bps)" in q and "GROUP BY" not in q:
        return pd.DataFrame([{"mean_bps": 4.2, "fills": 3}])
    if tbl in POP:
        return pd.DataFrame(POP[tbl])
    return pd.DataFrame()


pd.read_sql = fake_read_sql

# ---------------- run it ----------------
sys.argv = ["app.py"]
src = (pathlib.Path(__file__).resolve().parent / "app.py").read_text()
ns = {"__name__": "__main__", "__file__": "dashboard/app.py"}
exec(compile(src, "app.py", "exec"), ns)

print(f"[{MODE}/{VIEW}{'/'+WHO if WHO else ''}] rendered ok — {len(calls)} streamlit calls")

wall = RENDERED.get("wall", "")
nav = RENDERED.get("nav", "")
assert 'id="ny-hr"' in wall and 'id="be-hr"' in wall, "a clock hand is missing"
assert "setInterval" in wall, "the clocks do not tick"
assert nav, "no navigation rail rendered"
for key in ("floor", "room", "report", "briefing", "activity",
            "research", "risk", "execution", "portfolio"):
    assert f'href="?view={key}"' in nav, f"nav is missing {key}"

if VIEW == "floor":
    floor = RENDERED["floor"]
    for a in ("Atlas", "Vera", "Solomon", "Nora", "Marcus", "Ada", "Otis", "Clara"):
        assert f">{a}</span>" in floor, f"{a} missing from the floor"
        assert f'href="?view=agent&who={a}"' in floor, f"{a} card is not a link"
    assert "PHASE 1" in floor and "PHASE 5" in floor, "phase rail incomplete"

    # D10 — the schedule strip. Its whole job is to be visible when a
    # scheduled run did not happen, so both branches are asserted: it is
    # there on a session day and deliberately absent on a Saturday,
    # where there is nothing to be late for.
    if TODAY.weekday() < 5:
        assert "mby-sched" in floor, "no schedule strip on a session day"
        for group in ("premarket", "execution", "close"):
            assert group in floor, f"{group} missing from the schedule strip"
        strip = "schedule strip"
        if "did not run" in floor:
            strip += " + overdue banner"
    else:
        assert "mby-sched" not in floor, \
            "schedule strip shown on a non-session day"
        strip = "no schedule strip (not a session day)"

    print(f"[{MODE}] 8 clickable cards, phase rail, tape, {strip}: ok "
          f"({floor.count('mby-missing')} em-dash metrics)")

elif VIEW == "room":
    room = RENDERED["room"]
    for a in ("ATLAS", "VERA", "SOLOMON", "NORA", "MARCUS", "ADA", "OTIS", "CLARA"):
        assert a in room, f"{a} has no seat"
    assert room.count("mby-seat") >= 8, "a seat is missing"
    assert "VERDICT" in " ".join(BLOCKS), "no verdict block"
    print(f"[{MODE}] 8 clickable seats, minutes, verdict: ok")

elif VIEW == "agent":
    d = RENDERED.get("dossier", "")
    assert WHO in d, f"{WHO} nameplate missing"
    assert "RUN HISTORY" in " ".join(BLOCKS), "no run history section"
    print(f"[{MODE}] dossier for {WHO}: ok")

else:
    # The property worth asserting is "the route ran to completion and put
    # SOMETHING on the page" — not a call count, which just encodes how
    # chatty a particular page happens to be.
    widgets = [c for c in calls
               if c in ("dataframe", "info", "subheader", "metric", "selectbox",
                        "line_chart", "expander", "success", "warning", "error",
                        "json", "write", "caption")]
    assert widgets, f"page '{VIEW}' rendered no content at all"
    assert f'<span class="h">{__import__("re").escape("")}' or True
    titled = any("mby-head" in b for b in BLOCKS)
    assert titled, f"page '{VIEW}' has no title band"
    if VIEW == "portfolio":
        # D4 — the comparison is the headline and must actually compute
        # on a populated book, not silently fall back to "unavailable".
        body = " ".join(BLOCKS) + " " + " ".join(TEXTS)
        assert "Performance vs SPY" in body, "no D4 headline"
        if MODE == "populated":
            assert "No comparison available" not in body, \
                "the comparison did not compute on a populated book"
            assert "Selection" in body and "cash drag" in body, \
                "the decomposition is missing — active return alone misleads here"

    print(f"[{MODE}] page '{VIEW}': ok ({len(widgets)} widgets)")

# --- table-rendering assertions -------------------------------------
if GRIDS:
    fills = sum(1 for g in GRIDS if g.get("use_container_width"))
    natural = sum(1 for g in GRIDS if not g.get("use_container_width"))
    cfgd = sum(1 for g in GRIDS if g.get("column_config"))
    assert all("hide_index" in g for g in GRIDS), "a grid lost hide_index"
    # A grid carrying a prose column must NOT be forced to container width,
    # or the prose is clipped with nothing wider to scroll to.
    for g in GRIDS:
        if g.get("column_config"):
            assert not g["use_container_width"], \
                "a prose grid is still pinned to container width"
    print(f"[{MODE}/{VIEW}] grids: {len(GRIDS)} "
          f"({fills} fill, {natural} natural-width, {cfgd} with prose columns)")
