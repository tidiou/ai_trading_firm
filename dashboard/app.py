"""
MBY-Trading — CEO Dashboard

Streamlit app reading directly from the shared Postgres database —
the SAME single source of truth every agent reads and writes to
(via core.db.engine). Pure read-only view; never writes anything.

Run with (from the project root):
    streamlit run dashboard/app.py

v4 — a router, not a tab strip.

  THE WALL     persistent chrome on every page: the two clocks the desk
               runs on (NYSE governs, Berlin is where you sit), the
               book, and the halt state. You do not stop being able to
               see the clock because you walked to another desk.

  THE RAIL     navigation, between the Wall and whatever you are
               looking at. "The Floor" is always the way home.

  THE FLOOR    the opening page and nothing else: phase rail, eight
               agent cards, the tape. Every card is a link to that
               agent's dossier.

Everything else — the seven v2 views, the per-agent dossier, and the
conference room — is a page of its own, reached through ?view= in the
URL. That means every destination is bookmarkable and the browser's
own back button works, which st.tabs never gave you.

A NOTE ON MISSING NUMBERS. Some metrics a real desk would show have
no table behind them yet — per-agent calibration in particular.
Those render as a dotted em-dash with the reason on hover, never as a
plausible-looking number. A dashboard on a trading system that
displays a figure it did not compute is worse than one that admits a
gap, so the gaps are deliberately visible.
"""

import html as _html
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from core.db import engine
from core.market_calendar import EASTERN, NYSE, market_is_open_now

BERLIN = ZoneInfo("Europe/Berlin")

st.set_page_config(page_title="MBY-Trading", layout="wide", page_icon="🕰️")


# =================================================================
# PALETTE
#
# One place, so no two parts of the page can drift apart. Named for
# what they mean rather than for the colour, because the point of
# oxblood here is "a number you do not want to see", not "red".
# =================================================================
C = {
    "ground": "#1A1815",
    "panel": "#232019",
    "panel2": "#2C2820",
    "sunk": "#1E1B16",
    "tape": "#16140F",
    "rule": "#3A342A",
    "rule_strong": "#4A4235",
    "ink": "#F2EDE3",
    "muted": "#A69C89",
    "faint": "#6F6656",
    "brass": "#C9A227",
    "brass_dim": "#8A6F1C",
    "green": "#6E9C6B",
    "amber": "#D08C2A",
    "oxblood": "#A63A2E",
}

FONTS = ("https://fonts.googleapis.com/css2?"
         "family=Libre+Baskerville:ital,wght@0,400;0,700;1,400"
         "&family=Archivo:wght@400;500;600;700"
         "&family=JetBrains+Mono:wght@400;500;700&display=swap")


# =================================================================
# HELPERS
# =================================================================
def load(query: str, params: tuple | None = None) -> pd.DataFrame:
    return pd.read_sql(query, engine, params=params)


def one(query: str, params: tuple | None = None):
    """First row of a query, or None. Saves a `.empty` check at every
    call site — there are a lot of them on the Floor."""
    df = load(query, params)
    return None if df.empty else df.iloc[0]


def missing(reason: str) -> str:
    """
    A metric with nothing behind it. Renders as an em-dash carrying the
    reason on hover.

    Deliberately NOT a zero, a blank, or a hidden row: "0 breaches" and
    "we do not measure breaches" are completely different claims, and a
    dashboard that cannot tell them apart will eventually be believed
    about the wrong one.
    """
    return f'<span class="mby-missing" title="{_html.escape(reason)}">&mdash;</span>'


def fmt_pct(v, digits=2, sign=False) -> str:
    if v is None or pd.isna(v):
        return "&mdash;"
    return f"{float(v):{'+' if sign else ''}.{digits}f}%"


def fmt_money(v, digits=2, sign=False) -> str:
    if v is None or pd.isna(v):
        return "&mdash;"
    return f"{float(v):{'+' if sign else ''},.{digits}f}"


def col(text: str, colour: str) -> str:
    return f'<span style="color:{colour}">{text}</span>'


def esc(v) -> str:
    return _html.escape(str(v))


# =================================================================
# TABLES
#
# st.dataframe(use_container_width=True) gives every column an equal
# slice of the container, so a 300-word thesis and a 4-character ticker
# get the same room — the thesis is clipped to "The compa…" and there is
# nothing wider to scroll to, because nothing is wider.
#
# So: prose columns are declared, given a large width, and the grid is
# left at its natural size, which is what makes it scroll sideways.
# Tables with no prose still fill the container, since stretching six
# short columns across the page is fine and looks better.
#
# The grid is still a grid, though — no column width makes a paragraph
# readable in a cell. Any table holding prose therefore also gets a
# "read a row in full" panel underneath, which is the escape hatch that
# works regardless of what the grid does with widths.
# =================================================================
PROSE_COLUMNS = {
    "narrative", "thesis", "thesis_text", "reasoning", "rationale", "detail",
    "description", "error_message", "catalyst", "executive_summary",
    "resolution_note", "trigger", "linked_trigger", "activity", "violations",
    "expected", "actual", "key_risks", "notes",
}

_table_seq = [0]


def records(df: pd.DataFrame, title_cols: list[str], body_col: str,
            tone: str = "warn") -> None:
    """
    Render rows as readable text blocks instead of grid cells.

    For the handful of tables whose whole point is a sentence — an open
    discrepancy, a compliance violation — a grid is the wrong container.
    st.column_config.TextColumn(width="large") caps a column at roughly
    400px and truncates past it; it does not widen to fit. And once the
    grid fits inside the container there is no overflow, so there is
    nothing to scroll sideways to either. The text is simply gone.

    A paragraph belongs in a paragraph.
    """
    if df is None or df.empty:
        return
    edge = {"warn": C["oxblood"], "note": C["amber"], "ok": C["green"]}.get(tone, C["rule"])
    for _, row in df.iterrows():
        head = " · ".join(
            str(row[c]) for c in title_cols
            if c in df.columns and row[c] is not None and str(row[c]).strip()
        )
        body = str(row[body_col]) if body_col in df.columns and row[body_col] else ""
        extras = "".join(
            f'<div class="mby-rec-kv"><span>{_html.escape(c)}</span>'
            f'<span>{_html.escape(str(row[c]))}</span></div>'
            for c in df.columns
            if c not in title_cols + [body_col]
            and row[c] is not None and str(row[c]).strip()
            and not isinstance(row[c], (bool,))
        )
        st.markdown(
            f'<div class="mby-rec" style="border-left-color:{edge}">'
            f'<div class="mby-rec-head">{_html.escape(head)}</div>'
            f'<div class="mby-rec-body">{_html.escape(body)}</div>'
            f'{extras}</div>',
            unsafe_allow_html=True,
        )


def _row_label(df: pd.DataFrame, i: int) -> str:
    """A one-line name for a row, built from its shortest identifying
    columns — a date and a ticker beat 'row 3'."""
    row = df.iloc[i]
    bits = []
    for c in df.columns:
        if c in PROSE_COLUMNS or len(bits) >= 3:
            continue
        v = row[c]
        if v is None or (not isinstance(v, (list, dict)) and pd.isna(v)):
            continue
        t = str(v)
        if len(t) <= 24:
            bits.append(t)
    return " · ".join(bits) if bits else f"row {i + 1}"


def table(df: pd.DataFrame, height: int | None = None,
          prose: set[str] | None = None, full: bool = True) -> None:
    if df is None or df.empty:
        return

    cols = PROSE_COLUMNS | set(prose or ())
    long_cols = [c for c in df.columns if c in cols]

    cfg = None
    try:
        if long_cols:
            cfg = {c: st.column_config.TextColumn(c, width="large")
                   for c in long_cols}
    except Exception:
        # column_config arrived in Streamlit 1.23; an older one still
        # gets the natural-width grid below, which is the important half.
        cfg = None

    kwargs = {"hide_index": True}
    if cfg:
        kwargs["column_config"] = cfg
    if height is not None:
        kwargs["height"] = height
    # Natural width (and therefore a horizontal scrollbar) whenever a
    # column holds prose; fill the container when nothing does.
    kwargs["use_container_width"] = not long_cols

    st.dataframe(df, **kwargs)

    if full and long_cols:
        _table_seq[0] += 1
        n = len(df)
        with st.expander(f"Read a row in full — {n} row{'s' if n != 1 else ''}",
                         expanded=n <= 5):
            i = st.selectbox(
                "Row", range(n), format_func=lambda k: _row_label(df, k),
                key=f"mby-row-{_table_seq[0]}", label_visibility="collapsed")
            row = df.iloc[i]
            for c in df.columns:
                v = row[c]
                if v is None or (not isinstance(v, (list, dict)) and pd.isna(v)):
                    continue
                st.markdown(f"**{c}**")
                if isinstance(v, (list, dict)):
                    st.json(v)
                else:
                    st.text(str(v))


# =================================================================
# ROUTING
#
# The whole app hangs off one query parameter. Anchors rather than
# st.button for two reasons: a button cannot be the whole surface of a
# styled card without wrecking the grid, and a URL is bookmarkable
# while a button press is not — the browser's back button working is
# most of what was wrong with the tab strip.
# =================================================================
def read_params() -> dict:
    """st.query_params is 1.30+; fall back to the older API rather than
    crashing on an older Streamlit."""
    try:
        return {k: v for k, v in st.query_params.items()}
    except Exception:
        try:
            raw = st.experimental_get_query_params()
            return {k: (v[0] if isinstance(v, list) and v else v)
                    for k, v in raw.items()}
        except Exception:
            return {}


PARAMS = read_params()
VIEW = str(PARAMS.get("view", "floor") or "floor")
WHO = str(PARAMS.get("who", "") or "")


def href(view: str, **kw) -> str:
    q = f"?view={quote(view)}"
    for k, v in kw.items():
        q += f"&{quote(str(k))}={quote(str(v))}"
    return q


# view key, rail label, page title
PAGES = [
    ("floor",     "The Floor",          "The Floor"),
    ("room",      "Conference Room",    "The Morning Meeting"),
    ("report",    "Closing Report",     "Daily Closing Report"),
    ("briefing",  "Briefing",           "Today's Briefing"),
    ("activity",  "Activity Log",       "Agent Activity Log"),
    ("research",  "Research",           "Research — Vera"),
    ("risk",      "Risk & Compliance",  "Risk, Strategy & Compliance"),
    ("execution", "Execution",          "Execution — Ada"),
    ("portfolio", "Portfolio",          "Portfolio"),
]
PAGE_TITLES = {k: t for k, _, t in PAGES}


# =================================================================
# TIME
#
# The desk is anchored to America/New_York — "today" for every agent
# is the ET date, not the date on the machine running this. Getting
# that wrong is how a European operator ends up looking at an empty
# dashboard at 00:30 local and concluding the cycle failed.
# =================================================================
now_utc = datetime.now(timezone.utc)
et_now = now_utc.astimezone(EASTERN)
et_today = et_now.date()


def next_session_boundary() -> tuple[str, datetime | None, str]:
    """
    (label, when, state) for the NYSE session — the next boundary the
    operator cares about: the close while the session is live,
    otherwise the next open. The countdown to it runs client-side so
    the clock ticks without a Streamlit rerun.
    """
    schedule = NYSE.schedule(start_date=et_today - timedelta(days=1),
                             end_date=et_today + timedelta(days=10))
    if schedule.empty:
        return ("SESSION UNKNOWN", None, "closed")

    if market_is_open_now(now_utc):
        closes = [r["market_close"].to_pydatetime() for _, r in schedule.iterrows()]
        return ("CLOSES IN", next((c for c in closes if c > now_utc), None), "open")

    opens = [r["market_open"].to_pydatetime() for _, r in schedule.iterrows()]
    return ("OPENS IN", next((o for o in opens if o > now_utc), None), "closed")


boundary_label, boundary_at, session_state = next_session_boundary()


# =================================================================
# THE WALL
#
# Rendered in an iframe rather than as markdown, for one reason: it
# is the only part of this app that needs script. A wall clock that
# only moves when you click something is not a clock, and Streamlit
# strips <script> from markdown. Nothing in here navigates, so the
# iframe costs us nothing — the rail below it does the linking.
# =================================================================
control_row = one(
    "SELECT trading_enabled, changed_by, reason, changed_at "
    "FROM trading_control ORDER BY id DESC LIMIT 1"
)
halted = control_row is not None and not bool(control_row["trading_enabled"])

pnl_row = one(
    "SELECT pnl_date, nav, total_pnl, realized_pnl, unrealized_pnl, cash_balance "
    "FROM daily_pnl ORDER BY pnl_date DESC LIMIT 1"
)

WALL_CSS = """
<style>
  @import url('""" + FONTS + """');
  * { box-sizing: border-box; }
  /* overflow-x is the last line of defence: below the narrowest media
     query the strip scrolls rather than being clipped by the iframe. */
  body { margin: 0; background: """ + C["ground"] + """; overflow-x: auto;
         overflow-y: hidden;
         font-family: Archivo, 'Helvetica Neue', Arial, sans-serif;
         color: """ + C["ink"] + """; }
  .wall { display: flex; align-items: stretch; height: 200px; min-width: 0;
          border-bottom: 2px solid """ + C["rule"] + """; }
  /* Everything sizes off flex-basis with min-width:0 rather than a hard
     width. The old fixed 372 + 420 + 178 needed ~1370px before the two
     figures had anywhere to go, so a narrower window cut the book and
     the halt switch off the right-hand edge. */
  .plate { flex: 1 1 300px; min-width: 220px; max-width: 380px;
           padding: 22px 24px;
           border-right: 1px solid """ + C["rule"] + """;
           display: flex; flex-direction: column; justify-content: space-between; }
  .brand { font-family: 'Libre Baskerville', Georgia, serif; font-size: 30px;
           letter-spacing: 0.02em; }
  .brand em { font-style: normal; color: """ + C["brass"] + """; }
  .sub { margin-top: 5px; font-family: 'JetBrains Mono', monospace;
         font-size: 10.5px; letter-spacing: 0.2em; color: """ + C["faint"] + """; }
  .state { display: flex; align-items: center; gap: 10px; }
  .lamp { width: 8px; height: 8px; border-radius: 50%; }
  .state .txt { font-family: 'JetBrains Mono', monospace; font-size: 11.5px;
                letter-spacing: 0.14em; }
  .state .note { font-size: 11.5px; color: """ + C["faint"] + """; }
  .clocks { flex: 0 0 auto; border-right: 1px solid """ + C["rule"] + """;
            background: """ + C["sunk"] + """; display: flex; align-items: center;
            justify-content: center; gap: 40px; padding: 0 30px; }
  .clock { display: flex; flex-direction: column; align-items: center;
           justify-content: center; gap: 8px; }
  .face { position: relative; width: 92px; height: 92px; border-radius: 50%;
          background: """ + C["panel2"] + """;
          box-shadow: inset 0 2px 10px rgba(0,0,0,0.55); }
  .face.gov { border: 2px solid """ + C["brass_dim"] + """; }
  .face.loc { border: 2px solid """ + C["rule_strong"] + """; }
  .face .inner { position: absolute; inset: 8px; border-radius: 50%;
                 border: 1px solid """ + C["rule"] + """; }
  .tick { position: absolute; background: """ + C["rule_strong"] + """; }
  .t12 { left: 50%; top: 9px; width: 1.5px; height: 7px; transform: translateX(-50%); }
  .t6  { left: 50%; bottom: 9px; width: 1.5px; height: 7px; transform: translateX(-50%); }
  .t9  { top: 50%; left: 9px; width: 7px; height: 1.5px; transform: translateY(-50%); }
  .t3  { top: 50%; right: 9px; width: 7px; height: 1.5px; transform: translateY(-50%); }
  .gov .t12 { background: """ + C["brass_dim"] + """; }
  .hand { position: absolute; left: 50%; top: 50%; transform-origin: 50% 100%;
          border-radius: 2px; }
  .hr { width: 3px; height: 26px; }
  .mn { width: 2px; height: 36px; }
  .sc { width: 1px; height: 38px; background: """ + C["brass"] + """; opacity: 0.75; }
  .gov .hr, .gov .mn { background: """ + C["ink"] + """; }
  .loc .hr, .loc .mn { background: """ + C["muted"] + """; }
  .pin { position: absolute; left: 50%; top: 50%; width: 6px; height: 6px;
         border-radius: 50%; transform: translate(-50%, -50%); }
  .gov .pin { background: """ + C["brass"] + """; }
  .loc .pin { background: """ + C["brass_dim"] + """; }
  .czone { font-family: 'JetBrains Mono', monospace; font-size: 10px;
           letter-spacing: 0.18em; }
  .cdig { font-family: 'JetBrains Mono', monospace; font-size: 17px;
          font-variant-numeric: tabular-nums; }
  .cnote { font-family: 'JetBrains Mono', monospace; font-size: 10px;
           letter-spacing: 0.08em; }
  .book { flex: 2 1 0; min-width: 0; display: flex; align-items: stretch; }
  .fig { flex: 1 1 0; min-width: 0; padding: 22px 20px; display: flex;
         flex-direction: column; justify-content: center; gap: 6px;
         border-right: 1px solid """ + C["rule"] + """; }
  .cap { font-family: 'JetBrains Mono', monospace; font-size: 10px;
         letter-spacing: 0.16em; color: """ + C["faint"] + """; }
  .big { font-family: 'JetBrains Mono', monospace;
         font-size: clamp(19px, 2.1vw, 30px);
         font-variant-numeric: tabular-nums; letter-spacing: -0.01em;
         white-space: nowrap; }
  /* A note that outgrows its column ellipsises instead of pushing the
     next panel off the strip. */
  .sml, .cap, .state .note, .cnote, .czone { white-space: nowrap;
         overflow: hidden; text-overflow: ellipsis; }
  .sml { font-family: 'JetBrains Mono', monospace; font-size: 11.5px;
         color: """ + C["faint"] + """; }
  .sml b { font-weight: 400; }
  .ctrl { flex: 0 0 auto; width: 168px; padding: 20px 16px;
          background: """ + C["sunk"] + """;
          display: flex; flex-direction: column; justify-content: center;
          align-items: center; gap: 11px; }
  .switch { width: 100%; border-radius: 2px; padding: 12px 0; text-align: center;
            font-family: 'JetBrains Mono', monospace; font-size: 13px;
            letter-spacing: 0.16em; }
  .ctrl .hint { font-size: 10.5px; color: """ + C["faint"] + """; text-align: center;
                line-height: 1.4; }

  /* Shed the least load-bearing thing first, in order. The Berlin analog
     face goes before the New York one because Berlin is only where you
     are sitting; New York governs the session. The digital readouts and
     the session countdown never go. */
  @media (max-width: 1280px) {
    .face.loc { display: none; }
    .clocks { gap: 30px; padding: 0 22px; }
    .plate { padding: 20px; }
  }
  @media (max-width: 1080px) {
    .face { display: none; }
    .clocks { gap: 26px; padding: 0 18px; }
    .plate { max-width: 300px; min-width: 200px; }
    .ctrl { width: 146px; }
    .fig { padding: 18px 14px; }
  }
  @media (max-width: 900px) {
    .plate .sub, .ctrl .hint, .fig .sml { display: none; }
    .brand { font-size: 24px; }
  }
</style>
"""

WALL_JS = """
<script>
(function () {
  var boundary = BOUNDARY_ISO ? new Date(BOUNDARY_ISO) : null;

  function partsIn(d, tz) {
    var f = new Intl.DateTimeFormat('en-GB', {
      timeZone: tz, hour: '2-digit', minute: '2-digit',
      second: '2-digit', hour12: false
    }).formatToParts(d);
    var o = {};
    f.forEach(function (p) { o[p.type] = p.value; });
    return { h: +o.hour % 24, m: +o.minute, s: +o.second };
  }

  function paint(prefix, tz) {
    var d = new Date(), p = partsIn(d, tz);
    document.getElementById(prefix + '-dig').textContent =
      String(p.h).padStart(2, '0') + ':' + String(p.m).padStart(2, '0') +
      ':' + String(p.s).padStart(2, '0');
    // Hour hand advances continuously through the hour, as a real one
    // does — a hand that jumps on the hour reads as broken.
    var hAng = ((p.h % 12) + p.m / 60) * 30;
    var mAng = (p.m + p.s / 60) * 6;
    document.getElementById(prefix + '-hr').style.transform =
      'translate(-50%,-100%) rotate(' + hAng + 'deg)';
    document.getElementById(prefix + '-mn').style.transform =
      'translate(-50%,-100%) rotate(' + mAng + 'deg)';
    var sec = document.getElementById(prefix + '-sc');
    if (sec) sec.style.transform =
      'translate(-50%,-100%) rotate(' + (p.s * 6) + 'deg)';
  }

  function countdown() {
    var el = document.getElementById('boundary');
    if (!el || !boundary) return;
    var ms = boundary - new Date();
    if (ms <= 0) { el.textContent = 'NOW'; return; }
    var t = Math.floor(ms / 1000);
    el.textContent = String(Math.floor(t / 3600)).padStart(2, '0') + ':' +
                     String(Math.floor((t % 3600) / 60)).padStart(2, '0') + ':' +
                     String(t % 60).padStart(2, '0');
  }

  function tick() {
    paint('ny', 'America/New_York');
    paint('be', 'Europe/Berlin');
    countdown();
  }
  tick();
  setInterval(tick, 1000);
})();
</script>
"""


def clock_markup(prefix: str, klass: str, zone_label: str, zone_colour: str,
                 note_html: str, with_second: bool) -> str:
    sec = f'<span class="hand sc" id="{prefix}-sc"></span>' if with_second else ""
    return f"""
      <div class="clock">
        <div class="face {klass}">
          <div class="inner"></div>
          <span class="tick t12"></span><span class="tick t6"></span>
          <span class="tick t9"></span><span class="tick t3"></span>
          <span class="hand hr" id="{prefix}-hr"></span>
          <span class="hand mn" id="{prefix}-mn"></span>
          {sec}
          <span class="pin"></span>
        </div>
        <div class="czone" style="color:{zone_colour}">{zone_label}</div>
        <div class="cdig" id="{prefix}-dig">&nbsp;</div>
        <div class="cnote">{note_html}</div>
      </div>"""


def render_wall() -> None:
    if halted:
        lamp = lamp_col = C["oxblood"]
        lamp_txt = "TRADING HALTED"
        lamp_note = f"by {esc(control_row['changed_by'])}"
        switch_style = (f"border:1.5px solid {C['green']};background:{C['panel2']};"
                        f"color:{C['green']}")
        switch_txt = "RESUME DESK"
    else:
        lamp = lamp_col = C["green"]
        lamp_txt = "TRADING ENABLED"
        lamp_note = ("&mdash; no halt on record" if control_row is not None
                     else "&mdash; bootstrap default")
        switch_style = (f"border:1.5px solid {C['brass_dim']};background:{C['panel2']};"
                        f"color:{C['brass']}")
        switch_txt = "HALT DESK"

    if pnl_row is not None:
        nav_txt = fmt_money(pnl_row["nav"]) if pd.notna(pnl_row["nav"]) else "&mdash;"
        nav_note = f"marked from the ledger &middot; {pnl_row['pnl_date']}"
        total = pnl_row["total_pnl"]
        pnl_txt = fmt_money(total, sign=True)
        pnl_col = C["green"] if (pd.notna(total) and float(total) >= 0) else C["oxblood"]
        pnl_note = (f'<b style="color:{C["muted"]}">realized</b> '
                    f'{fmt_money(pnl_row["realized_pnl"], sign=True)} &nbsp; '
                    f'<b style="color:{C["muted"]}">unrealized</b> '
                    f'{fmt_money(pnl_row["unrealized_pnl"], sign=True)}')
    else:
        nav_txt, pnl_txt, pnl_col = "&mdash;", "&mdash;", C["faint"]
        nav_note = "Otis has not closed a session yet"
        pnl_note = "no reconciled day on record"

    session_colour = C["green"] if session_state == "open" else C["oxblood"]
    session_word = "OPEN" if session_state == "open" else "CLOSED"
    ny_note = (f'<span style="color:{session_colour}">{session_word}</span> '
               f'&middot; {boundary_label} <span id="boundary">&nbsp;</span>')
    be_note = f'<span style="color:{C["faint"]}">YOUR DESK</span>'

    ny_clock = clock_markup("ny", "gov", "NEW YORK", C["brass"], ny_note, True)
    be_clock = clock_markup("be", "loc", "BERLIN", C["faint"], be_note, False)

    body = f"""
<div class="wall">
  <div class="plate">
    <div>
      <div class="brand">MBY <em>Trading</em></div>
      <div class="sub">SYSTEMATIC EQUITY DESK</div>
    </div>
    <div class="state">
      <span class="lamp" style="background:{lamp};box-shadow:0 0 8px {lamp}88"></span>
      <span class="txt" style="color:{lamp_col}">{lamp_txt}</span>
      <span class="note">{lamp_note}</span>
    </div>
  </div>

  <div class="clocks">{ny_clock}{be_clock}</div>

  <div class="book">
    <div class="fig">
      <div class="cap">NET ASSET VALUE</div>
      <div class="big" style="color:{C['ink']}">{nav_txt}</div>
      <div class="sml">{nav_note}</div>
    </div>
    <div class="fig">
      <div class="cap">DAY P&amp;L</div>
      <div class="big" style="color:{pnl_col}">{pnl_txt}</div>
      <div class="sml">{pnl_note}</div>
    </div>
    <div class="ctrl">
      <div class="cap">DESK CONTROL</div>
      <div class="switch" style="{switch_style}">{switch_txt}</div>
      <div class="hint">python -m core.trading_control<br>reason required</div>
    </div>
  </div>
</div>
"""
    js = WALL_JS.replace(
        "BOUNDARY_ISO",
        f'"{boundary_at.isoformat()}"' if boundary_at else "null")
    components.html(WALL_CSS + body + js, height=214, scrolling=False)


# =================================================================
# SHARED CSS
#
# Everything below the Wall is server-rendered markdown, so it shares
# one stylesheet injected once per run.
# =================================================================
PAGE_CSS = """
<style>
  @import url('""" + FONTS + """');

  /* --- navigation rail ------------------------------------ */
  .mby-rail-nav { display: flex; flex-wrap: wrap; align-items: stretch;
    background: """ + C["rule"] + """; gap: 1px;
    border-left: 1px solid """ + C["rule"] + """;
    border-right: 1px solid """ + C["rule"] + """;
    border-bottom: 1px solid """ + C["rule_strong"] + """;
    margin-bottom: 14px; font-family: 'JetBrains Mono', monospace; }
  .mby-rail-nav a { flex: 1 1 auto; text-align: center; padding: 10px 14px;
    background: """ + C["panel"] + """; color: """ + C["muted"] + """;
    text-decoration: none; font-size: 11px; letter-spacing: 0.12em;
    text-transform: uppercase; white-space: nowrap; transition: all .12s; }
  .mby-rail-nav a:hover { background: """ + C["panel2"] + """;
    color: """ + C["ink"] + """; }
  .mby-rail-nav a.home { flex: 0 0 auto; color: """ + C["brass"] + """;
    background: """ + C["sunk"] + """; }
  .mby-rail-nav a.on { background: """ + C["ground"] + """;
    color: """ + C["brass"] + """; box-shadow: inset 0 -2px 0 """ + C["brass"] + """; }

  /* --- generic page furniture ------------------------------ */
  .mby-page { background: """ + C["ground"] + """; color: """ + C["ink"] + """;
    font-family: Archivo, 'Helvetica Neue', Arial, sans-serif;
    border: 1px solid """ + C["rule"] + """; margin-bottom: 12px; }
  .mby-head { display: flex; align-items: baseline; justify-content: space-between;
    padding: 18px 28px; border-bottom: 1px solid """ + C["rule"] + """; }
  .mby-head .h { font-family: 'Libre Baskerville', Georgia, serif; font-size: 23px; }
  .mby-mono { font-family: 'JetBrains Mono', monospace;
    font-variant-numeric: tabular-nums; }
  .mby-sec { font-family: 'JetBrains Mono', monospace; font-size: 10px;
    letter-spacing: 0.18em; color: """ + C["brass"] + """; }
  .mby-rec { background: """ + C["panel"] + """;
    border: 1px solid """ + C["rule"] + """;
    border-left: 3px solid """ + C["oxblood"] + """;
    border-radius: 2px; padding: 12px 16px; margin-bottom: 8px; }
  .mby-rec-head { font-family: 'JetBrains Mono', monospace; font-size: 11px;
    letter-spacing: .1em; color: """ + C["brass"] + """;
    text-transform: uppercase; margin-bottom: 6px; }
  .mby-rec-body { font-size: 13.5px; line-height: 1.6;
    color: """ + C["ink"] + """;
    overflow-wrap: anywhere; white-space: pre-wrap; }
  .mby-rec-kv { display: flex; gap: 10px; margin-top: 6px;
    font-family: 'JetBrains Mono', monospace; font-size: 11px;
    color: """ + C["faint"] + """; }
  .mby-rec-kv span:last-child { color: """ + C["muted"] + """;
    overflow-wrap: anywhere; }
  .mby-missing { color: """ + C["faint"] + """;
    border-bottom: 1px dotted """ + C["rule_strong"] + """; cursor: help; }

  /* --- phase rail ------------------------------------------ */
  .mby-rail { display: flex; gap: 1px; background: """ + C["rule"] + """;
    border-bottom: 1px solid """ + C["rule"] + """; }
  .mby-ph { flex: 1; padding: 10px 16px; display: flex; align-items: center;
    gap: 9px; background: """ + C["panel"] + """; }
  .mby-ph.down { background: """ + C["sunk"] + """; }
  .mby-dot { width: 7px; height: 7px; border-radius: 50%; flex: none; }
  .mby-ph .n { font-family: 'JetBrains Mono', monospace; font-size: 10px;
    letter-spacing: 0.12em; color: """ + C["faint"] + """; }
  .mby-ph .l { font-size: 12.5px; }
  .mby-ph .t { margin-left: auto; font-family: 'JetBrains Mono', monospace;
    font-size: 10px; color: """ + C["faint"] + """; }
  .mby-rail { flex-wrap: wrap; }
  .mby-ph { min-width: 210px; }

  /* --- the desks ------------------------------------------- */
  .mby-desks { display: grid; grid-template-columns: repeat(3, minmax(0,1fr));
    gap: 1px; background: """ + C["rule"] + """; }
  .mby-desk { background: """ + C["ground"] + """; padding: 20px 24px;
    display: flex; flex-direction: column; gap: 14px; min-width: 0; }
  @media (max-width: 1250px) {
    .mby-desks { grid-template-columns: repeat(2, minmax(0,1fr)); }
  }
  @media (max-width: 820px) { .mby-desks { grid-template-columns: 1fr; } }
  .mby-met .k, .mby-met .v { overflow-wrap: anywhere; }
  .mby-role, .mby-name { overflow-wrap: anywhere; }
  .mby-deskhd { display: flex; align-items: center; gap: 10px; }
  .mby-deskhd span { font-family: 'JetBrains Mono', monospace; font-size: 10.5px;
    letter-spacing: 0.16em; color: """ + C["brass"] + """; }
  .mby-deskhd i { flex: 1; height: 1px; background: """ + C["rule"] + """; }
  .mby-cardlink { display: block; text-decoration: none; color: inherit; }
  .mby-card { background: """ + C["panel"] + """;
    border: 1px solid """ + C["rule"] + """; border-radius: 2px;
    padding: 14px 18px; display: flex; flex-direction: column; gap: 10px;
    transition: border-color .12s, background .12s; }
  .mby-cardlink:hover .mby-card { border-color: """ + C["brass_dim"] + """;
    background: """ + C["panel2"] + """; }
  .mby-card.down { background: """ + C["sunk"] + """; }
  .mby-who { display: flex; align-items: center; gap: 14px; }
  .mby-av { position: relative; width: 44px; height: 44px; flex: none; }
  .mby-av .disc { width: 44px; height: 44px; border-radius: 50%;
    border: 1.5px solid """ + C["brass_dim"] + """; background: """ + C["panel2"] + """;
    display: flex; align-items: center; justify-content: center;
    font-family: 'Libre Baskerville', Georgia, serif; font-size: 17px;
    color: """ + C["brass"] + """; }
  .mby-card.down .mby-av .disc { border-color: """ + C["rule_strong"] + """;
    background: """ + C["sunk"] + """; color: """ + C["faint"] + """; }
  .mby-av .lamp { position: absolute; right: -1px; bottom: -1px; width: 11px;
    height: 11px; border-radius: 50%; border: 2px solid """ + C["ground"] + """; }
  .mby-name { font-family: 'Libre Baskerville', Georgia, serif; font-size: 16px;
    color: """ + C["ink"] + """; }
  .mby-card.down .mby-name { color: """ + C["muted"] + """; }
  .mby-ext { font-family: 'JetBrains Mono', monospace; font-size: 11px;
    color: """ + C["faint"] + """; }
  .mby-role { font-size: 12.5px; color: """ + C["muted"] + """; }
  .mby-card.down .mby-role { color: """ + C["faint"] + """; }
  .mby-met { display: flex; align-items: baseline; justify-content: space-between; }
  .mby-met.first { border-top: 1px solid """ + C["panel2"] + """; padding-top: 9px; }
  .mby-met .k { font-size: 12px; color: """ + C["faint"] + """; }
  .mby-met .v { font-family: 'JetBrains Mono', monospace; font-size: 13px;
    font-variant-numeric: tabular-nums; color: """ + C["ink"] + """; }
  .mby-open { font-family: 'JetBrains Mono', monospace; font-size: 9.5px;
    letter-spacing: 0.14em; color: """ + C["rule_strong"] + """; text-align: right; }
  .mby-cardlink:hover .mby-open { color: """ + C["brass"] + """; }

  /* --- the tape -------------------------------------------- */
  .mby-tape { border-top: 1px solid """ + C["rule"] + """;
    background: """ + C["tape"] + """; padding: 0 28px; height: 44px;
    display: flex; align-items: center; gap: 24px; overflow: hidden; }
  .mby-tape .lbl { font-family: 'JetBrains Mono', monospace; font-size: 10px;
    letter-spacing: 0.16em; color: """ + C["brass"] + """; flex: none; }
  .mby-tape .items { display: flex; align-items: center; gap: 22px;
    font-family: 'JetBrains Mono', monospace; font-size: 11.5px;
    color: """ + C["muted"] + """; white-space: nowrap; }
  .mby-tape .sep { color: """ + C["rule"] + """; }
  .mby-tape .ts { color: """ + C["faint"] + """; }
  /* The tape is one long line by nature — let it scroll rather than
     silently dropping the events past the right edge. */
  .mby-tape { overflow-x: auto; }

  /* --- dossier --------------------------------------------- */
  .mby-dossier { display: flex; align-items: center; gap: 18px; padding: 22px 28px;
    background: """ + C["sunk"] + """;
    border-bottom: 1px solid """ + C["rule"] + """; }
  .mby-dossier .big { position: relative; width: 68px; height: 68px; flex: none; }
  .mby-dossier .big .disc { width: 68px; height: 68px; border-radius: 50%;
    border: 2px solid """ + C["brass_dim"] + """; background: """ + C["panel2"] + """;
    display: flex; align-items: center; justify-content: center;
    font-family: 'Libre Baskerville', Georgia, serif; font-size: 28px;
    color: """ + C["brass"] + """; }
  .mby-dossier .big .lamp { position: absolute; right: 1px; bottom: 1px;
    width: 14px; height: 14px; border-radius: 50%;
    border: 2.5px solid """ + C["sunk"] + """; }
  .mby-quote { padding: 16px 28px; border-bottom: 1px solid """ + C["panel2"] + """;
    font-family: 'Libre Baskerville', Georgia, serif; font-style: italic;
    font-size: 13px; color: """ + C["muted"] + """; line-height: 1.55; }
  .mby-kv { display: grid; grid-template-columns: 1fr auto; gap: 0; }
  .mby-kv .k { padding: 7px 28px; font-size: 12.5px; color: """ + C["faint"] + """;
    border-bottom: 1px solid """ + C["panel2"] + """; }
  .mby-kv .v { padding: 7px 28px; font-family: 'JetBrains Mono', monospace;
    font-size: 13px; font-variant-numeric: tabular-nums; text-align: right;
    color: """ + C["ink"] + """; border-bottom: 1px solid """ + C["panel2"] + """;
    overflow-wrap: anywhere; }
  .mby-kv .k { overflow-wrap: anywhere; }
  .mby-quote, .mby-say .body { overflow-wrap: anywhere; }
  .mby-band { padding: 7px 28px; background: """ + C["panel2"] + """;
    border-top: 1px solid """ + C["rule"] + """;
    border-bottom: 1px solid """ + C["rule"] + """; }
  .mby-limits { padding: 16px 28px; display: flex; flex-direction: column; gap: 13px; }
  .mby-bar { height: 6px; background: """ + C["panel2"] + """; }
  .mby-bar div { height: 100%; }

  /* --- conference room ------------------------------------- */
  .mby-room { display: flex; justify-content: center; overflow-x: auto;
    background: """ + C["sunk"] + """;
    border-bottom: 1px solid """ + C["rule"] + """; }
  /* Fixed-width stage inside a centring band: the seats are placed in
     absolute px, so they and the centred table need one shared origin.
     Without this the table centres on a 1900px screen and the seats
     stay bunched at the left. */
  .mby-stage { position: relative; width: 704px; height: 280px; flex: none; }
  .mby-table { position: absolute; left: 50%; top: 50%;
    transform: translate(-50%,-50%); width: 320px; height: 128px;
    border-radius: 64px;
    background: linear-gradient(160deg,#3A3225 0%,#2A2419 60%,#241F16 100%);
    border: 1.5px solid """ + C["rule_strong"] + """;
    box-shadow: 0 10px 26px rgba(0,0,0,.45), inset 0 1px 0 rgba(201,162,39,.14); }
  .mby-tablbl { position: absolute; left: 50%; top: 50%;
    transform: translate(-50%,-50%); text-align: center; }
  .mby-seat { position: absolute; width: 40px; text-align: center;
    text-decoration: none; }
  .mby-seat .d { width: 40px; height: 40px; border-radius: 50%;
    border: 1.5px solid """ + C["brass_dim"] + """; background: """ + C["panel2"] + """;
    display: flex; align-items: center; justify-content: center;
    font-family: 'Libre Baskerville', Georgia, serif; font-size: 16px;
    color: """ + C["brass"] + """; }
  .mby-seat.off .d { border-color: """ + C["rule_strong"] + """;
    background: """ + C["sunk"] + """; color: """ + C["faint"] + """; }
  .mby-seat .n { font-family: 'JetBrains Mono', monospace; font-size: 9px;
    letter-spacing: .08em; color: """ + C["muted"] + """; margin-top: 5px; }
  .mby-seat.off .n { color: """ + C["rule_strong"] + """; }
  .mby-seat:hover .d { border-color: """ + C["brass"] + """; }
  .mby-say { display: flex; gap: 14px; padding: 0 28px 16px 28px; }
  .mby-say .av { flex: none; width: 34px; height: 34px; border-radius: 50%;
    border: 1.5px solid """ + C["brass_dim"] + """; background: """ + C["panel2"] + """;
    display: flex; align-items: center; justify-content: center;
    font-family: 'Libre Baskerville', Georgia, serif; font-size: 14px;
    color: """ + C["brass"] + """; }
  .mby-say .body { font-size: 13px; color: """ + C["muted"] + """; line-height: 1.55; }
  .mby-verdict { margin: 0 28px 22px 28px; border: 1px solid """ + C["rule"] + """;
    border-left: 3px solid """ + C["brass"] + """; background: """ + C["panel"] + """;
    padding: 15px 20px; display: flex; align-items: center;
    justify-content: space-between; }
</style>
"""


def render_nav() -> None:
    items = ""
    for key, label, _ in PAGES:
        cls = "home" if key == "floor" else ""
        if VIEW == key or (VIEW == "agent" and key == "floor"):
            cls += " on"
        arrow = "&#9664; " if key == "floor" else ""
        items += f'<a class="{cls.strip()}" target="_self" href="{href(key)}">{arrow}{label}</a>'
    st.markdown(f'<div class="mby-rail-nav">{items}</div>', unsafe_allow_html=True)


# =================================================================
# THE FLOOR
# =================================================================
# The run ledger keys Nora twice (nora_review in Phase 3, nora_monitor
# in Phase 5), so each agent carries the list of keys that count as it.
AGENTS = [
    ("Atlas",   "A", "x101", "Macro &amp; Market Intelligence", "research",  ["atlas"]),
    ("Vera",    "V", "x102", "Equity Research",                 "research",  ["vera"]),
    ("Solomon", "S", "x103", "CIO &amp; Strategy",              "strategy",  ["solomon"]),
    ("Nora",    "N", "x104", "Risk",                            "strategy",
     ["nora_review", "nora_monitor", "nora"]),
    ("Marcus",  "M", "x105", "Portfolio Manager",               "strategy",  ["marcus"]),
    ("Ada",     "A", "x106", "Execution",                       "execution", ["ada"]),
    ("Otis",    "O", "x107", "Operations &amp; Reconciliation", "execution", ["otis"]),
    ("Clara",   "C", "x108", "Performance &amp; Compliance",    "execution", ["clara"]),
]
BY_NAME = {a[0]: a for a in AGENTS}

DESKS = [("research", "RESEARCH DESK"),
         ("strategy", "STRATEGY &amp; RISK"),
         ("execution", "EXECUTION &amp; OPS")]

MANDATES = {
    "Atlas": "Reads the macro backdrop before the open and calls the regime. "
             "Never proposes a trade — the regime is context every other desk works inside.",
    "Vera": "Screens for names and monitors the ones already held. A thesis she "
            "opened is a thesis she has to keep defending or close.",
    "Solomon": "Chairs the morning meeting and decides whether the desk acts at all. "
               "Most days the answer is no, and that is the job working.",
    "Nora": "Holds the four numeric limits. Reviews every proposal before it reaches "
            "Ada, and re-reads the book after the close whether or not anyone traded.",
    "Marcus": "Turns an approved proposal into a size. Called only when Solomon "
              "escalates, which is why he stands down on most days.",
    "Ada": "Places the order and nothing else. Re-reads the kill switch immediately "
           "before every submission, and refuses rather than guessing.",
    "Otis": "Keeps the books. The ledger he writes is what Ada sizes from, so a "
            "reconciliation he does not finish is a day the desk cannot trade on.",
    "Clara": "Audits the process independent of the outcome and files the closing "
             "report. A profitable trade that skipped a step is still a violation.",
}

PHASES = [(1, "Pre-market scan"), (2, "Morning meeting"), (3, "Risk review"),
          (4, "Allocation"), (5, "Close &amp; reconciliation")]


class Desk:
    """Every figure the Floor and the dossiers show, loaded once."""

    def __init__(self):
        self.runs_today = load(
            "SELECT agent_name, phase, status, started_at, completed_at "
            "FROM agent_runs WHERE run_date = %s", (et_today,))
        self.macro = one("SELECT regime_signal, change_from_yesterday, confidence, "
                         "narrative, brief_date FROM macro_briefs "
                         "ORDER BY brief_date DESC LIMIT 1")
        self.cands_today = one("SELECT count(*) AS n FROM new_candidates "
                               "WHERE candidate_date = %s", (et_today,))
        self.strat = one("SELECT decision_date, action_needed, narrative "
                         "FROM strategy_decisions ORDER BY decision_date DESC LIMIT 1")
        self.escal = one("SELECT count(*) FILTER (WHERE action_needed) AS acted, "
                         "count(*) AS total FROM strategy_decisions")
        self.risk = one("SELECT review_date, portfolio_status, circuit_breaker_active "
                        "FROM risk_reviews ORDER BY review_date DESC LIMIT 1")
        self.breach_n = one("""
            SELECT count(*) AS n FROM risk_breaches rb
            JOIN risk_reviews rr ON rb.risk_review_id = rr.id
            WHERE rr.review_date = %s""", (et_today,))
        self.alloc_today = one("SELECT count(*) AS n FROM allocations "
                               "WHERE allocation_date = %s", (et_today,))
        self.alloc_all = one("SELECT count(*) AS n, avg(target_size_pct) AS avg_pct "
                             "FROM allocations")
        self.orders_today = load("""
            SELECT ticker, action, shares, limit_price, status, fill_price,
                   slippage_bps, alpaca_order_id
            FROM orders WHERE order_date = %s ORDER BY id DESC""", (et_today,))
        self.slip = one("SELECT round(avg(slippage_bps), 1) AS mean_bps, "
                        "count(*) AS fills FROM orders WHERE slippage_bps IS NOT NULL")
        self.disc = one("SELECT count(*) FILTER (WHERE NOT resolved) AS open_n, "
                        "count(*) AS total FROM discrepancies")
        self.closes = one("SELECT count(*) FILTER (WHERE reconciled) AS clean, "
                          "count(*) AS total FROM daily_pnl")
        self.proc = one("SELECT check_date, process_check FROM process_checks "
                        "ORDER BY check_date DESC LIMIT 1")
        self.last_report = one("SELECT report_date FROM daily_reports "
                               "ORDER BY report_date DESC LIMIT 1")

        terminal = {"filled", "expired_unfilled", "canceled", "cancelled", "rejected"}
        if self.orders_today.empty:
            self.working_n = 0
        else:
            reached = self.orders_today["alpaca_order_id"].notna()
            self.working_n = int(
                (reached & ~self.orders_today["status"].isin(terminal)).sum())

    def status(self, keys: list[str]) -> tuple[str, str]:
        """(lamp colour, plain-language state) for one agent today.

        Grey is 'not called', which on this desk is the normal outcome —
        phases 3 and 4 stand down on any day Solomon sees no reason to
        act. Deliberately not the same colour as a failure.
        """
        if self.runs_today.empty:
            return C["rule_strong"], "not called"
        rows = self.runs_today[self.runs_today["agent_name"].isin(keys)]
        if rows.empty:
            return C["rule_strong"], "not called"
        if (rows["status"] == "failed").any():
            return C["oxblood"], "failed"
        if (rows["status"] == "running").any():
            return C["amber"], "running"
        if (rows["status"] == "completed").any():
            return C["green"], "completed"
        return C["rule_strong"], str(rows.iloc[0]["status"])

    # -- the two headline numbers on each card ---------------------
    # Every one is either read from a table or an honest em-dash.
    # Nothing here is a constant dressed as a measurement.
    def metrics(self, name: str) -> list[tuple[str, str]]:
        if name == "Atlas":
            regime = (col(esc(self.macro["regime_signal"]), C["amber"])
                      if self.macro is not None
                      else missing("Atlas has not produced a brief yet."))
            return [("Regime read", regime),
                    ("Change called correctly",
                     missing("No table scores a called regime change against what "
                             "happened. Needs an outcome check on macro_briefs."))]

        if name == "Vera":
            n = int(self.cands_today["n"]) if self.cands_today is not None else 0
            return [("Candidates surfaced today", str(n)),
                    ("Conviction-5 hit rate",
                     missing("Needs weekly_reports.vera_calibration, which Clara does "
                             "not populate yet — the column exists and is empty."))]

        if name == "Solomon":
            if self.strat is not None and self.strat["decision_date"] == et_today:
                verdict = (col("action needed", C["amber"]) if self.strat["action_needed"]
                           else col("no action", C["muted"]))
            else:
                verdict = missing("Solomon has not decided today.")
            rate = (f'{int(self.escal["acted"])} / {int(self.escal["total"])}'
                    if self.escal is not None and int(self.escal["total"])
                    else missing("No strategy decisions on record."))
            return [("Verdict today", verdict), ("Escalation rate", rate)]

        if name == "Nora":
            if self.risk is not None:
                s = str(self.risk["portfolio_status"])
                tone = {"within_limits": C["green"], "breach_warning": C["amber"],
                        "breach_hard": C["oxblood"]}.get(s, C["muted"])
                txt = (col("BREAKER ACTIVE", C["oxblood"])
                       if self.risk["circuit_breaker_active"]
                       else col(s.replace("_", " "), tone))
            else:
                txt = missing("Nora has not reviewed the book yet.")
            n = int(self.breach_n["n"]) if self.breach_n is not None else 0
            br = col("0", C["green"]) if n == 0 else col(str(n), C["oxblood"])
            return [("Portfolio status", txt), ("Breaches today", br)]

        if name == "Marcus":
            n = int(self.alloc_today["n"]) if self.alloc_today is not None else 0
            first = (("Not called today", col("stood down", C["rule_strong"]))
                     if n == 0 else ("Allocations sized", str(n)))
            avg = self.alloc_all["avg_pct"] if self.alloc_all is not None else None
            second = ("Mean target size",
                      fmt_pct(avg) if avg is not None and pd.notna(avg)
                      else missing("Marcus has never sized a position."))
            return [first, second]

        if name == "Ada":
            w = (col(f"{self.working_n} order" + ("s" if self.working_n != 1 else ""),
                     C["amber"]) if self.working_n else col("none", C["muted"]))
            if (self.slip is not None and pd.notna(self.slip["mean_bps"])
                    and int(self.slip["fills"])):
                s = f'{float(self.slip["mean_bps"]):+.1f} bps'
            else:
                s = missing("No fill has a recorded limit price yet — "
                            "nothing to measure.")
            return [("Working at broker", w), ("Mean slippage", s)]

        if name == "Otis":
            if self.disc is not None and int(self.disc["total"]):
                n = int(self.disc["open_n"])
                d = (col("none open", C["green"]) if n == 0
                     else col(f"{n} discrepanc" + ("y" if n == 1 else "ies"),
                              C["oxblood"]))
            else:
                d = col("none open", C["green"])
            c = (f'{int(self.closes["clean"])} / {int(self.closes["total"])}'
                 if self.closes is not None and int(self.closes["total"])
                 else missing("Otis has not closed a session yet."))
            return [("Books", d), ("Clean closes", c)]

        if name == "Clara":
            if self.proc is not None:
                p = (col("clean", C["green"]) if self.proc["process_check"] == "clean"
                     else col(esc(self.proc["process_check"]), C["oxblood"]))
            else:
                p = missing("Clara has not run a process audit yet.")
            r = (str(self.last_report["report_date"]) if self.last_report is not None
                 else missing("No closing report has been filed."))
            return [("Process check", p), ("Report filed", r)]

        return []


def phase_rail(desk: Desk) -> str:
    out = ""
    for num, label in PHASES:
        rows = (desk.runs_today[desk.runs_today["phase"] == num]
                if not desk.runs_today.empty else pd.DataFrame())
        if rows.empty:
            out += (f'<div class="mby-ph down">'
                    f'<span class="mby-dot" style="background:{C["rule_strong"]}"></span>'
                    f'<span class="n" style="color:{C["rule_strong"]}">PHASE {num}</span>'
                    f'<span class="l" style="color:{C["faint"]}">{label} '
                    f'&mdash; stood down</span></div>')
            continue
        if (rows["status"] == "failed").any():
            dot = C["oxblood"]
        elif (rows["status"] == "running").any():
            dot = C["amber"]
        else:
            dot = C["green"]
        done = rows["completed_at"].dropna()
        stamp = done.max().strftime("%H:%M") if not done.empty else ""
        out += (f'<div class="mby-ph">'
                f'<span class="mby-dot" style="background:{dot}"></span>'
                f'<span class="n">PHASE {num}</span>'
                f'<span class="l">{label}</span>'
                f'<span class="t">{stamp}</span></div>')
    return out


def tape(desk: Desk) -> str:
    """
    Real events only. An empty tape on a quiet day is the correct
    output — this desk is built to act rarely, and inventing filler
    would hide exactly the thing worth noticing.
    """
    items: list[str] = []
    if not desk.orders_today.empty:
        for _, o in desk.orders_today.head(2).iterrows():
            oid = (str(o["alpaca_order_id"])[:8] if pd.notna(o["alpaca_order_id"])
                   else "not submitted")
            px = fmt_money(o["fill_price"] if pd.notna(o["fill_price"])
                           else o["limit_price"])
            tone = (C["green"] if o["status"] == "filled"
                    else C["amber"] if pd.notna(o["alpaca_order_id"]) else C["oxblood"])
            items.append(f'ADA &middot; {oid} &middot; {esc(o["ticker"])} '
                         f'{fmt_money(o["shares"], 0)} @ {px} &middot; '
                         f'{col(esc(o["status"]), tone)}')
    if desk.risk is not None and desk.risk["review_date"] == et_today:
        items.append("NORA &middot; " +
                     col(str(desk.risk["portfolio_status"]).replace("_", " "), C["green"]))
    if pnl_row is not None:
        items.append(f'OTIS &middot; NAV {fmt_money(pnl_row["nav"])} '
                     f'&middot; {pnl_row["pnl_date"]}')
    if desk.disc is not None and int(desk.disc["open_n"] or 0):
        items.append(col(f'OTIS &middot; {int(desk.disc["open_n"])} discrepancy open',
                         C["oxblood"]))
    if desk.strat is not None and desk.strat["decision_date"] == et_today:
        items.append("SOLOMON &middot; " +
                     ("action needed" if desk.strat["action_needed"] else "no action"))

    if not items:
        return '<span class="ts">nothing on the tape today</span>'
    return ' <span class="sep">|</span> '.join(items[:5])


def page_floor(desk: Desk) -> None:
    desks_html = ""
    for key, desk_label in DESKS:
        cards = ""
        for name, mono, ext, role, group, run_keys in AGENTS:
            if group != key:
                continue
            lamp, state_word = desk.status(run_keys)
            down = state_word == "not called"
            met_html = ""
            for i, (k, v) in enumerate(desk.metrics(name)):
                cls = "mby-met first" if i == 0 else "mby-met"
                met_html += (f'<div class="{cls}"><span class="k">{k}</span>'
                             f'<span class="v">{v}</span></div>')
            cards += f"""
        <a class="mby-cardlink" target="_self" href="{href('agent', who=name)}">
          <div class="mby-card{' down' if down else ''}">
            <div class="mby-who">
              <div class="mby-av">
                <div class="disc">{mono}</div>
                <span class="lamp" style="background:{lamp}"></span>
              </div>
              <div style="flex:1;min-width:0">
                <div style="display:flex;align-items:baseline;gap:8px">
                  <span class="mby-name">{name}</span>
                  <span class="mby-ext">{ext}</span>
                </div>
                <div class="mby-role">{role}</div>
              </div>
            </div>
            {met_html}
            <div class="mby-open">OPEN DOSSIER &#9656;</div>
          </div>
        </a>"""
        desks_html += (f'<div class="mby-desk">'
                       f'<div class="mby-deskhd"><span>{desk_label}</span><i></i></div>'
                       f'{cards}</div>')

    done = (int((desk.runs_today["status"] == "completed").sum())
            if not desk.runs_today.empty else 0)
    total = len(desk.runs_today) if not desk.runs_today.empty else 0
    cycle_col = C["green"] if total and done == total else C["muted"]
    stamp = et_now.strftime("%A %d %B %Y").upper()

    st.markdown(f"""
<div class="mby-page">
  <div class="mby-head">
    <div style="display:flex;align-items:baseline;gap:18px">
      <span class="h">The Floor</span>
      <span class="mby-mono" style="font-size:12px;color:{C['muted']};letter-spacing:.06em">
        {stamp} &middot; NEW YORK</span>
    </div>
    <div class="mby-mono" style="display:flex;align-items:center;gap:10px;font-size:12px">
      <span style="color:{C['faint']};letter-spacing:.12em">CYCLE</span>
      <span style="color:{cycle_col}">{done} of {total} completed</span>
    </div>
  </div>
  <div class="mby-rail">{phase_rail(desk)}</div>
  <div class="mby-desks">{desks_html}</div>
  <div class="mby-tape">
    <span class="lbl">TAPE</span>
    <div class="items">{tape(desk)}</div>
  </div>
</div>
""", unsafe_allow_html=True)

    st.caption(
        "Every card is a link — click one to open that agent's dossier. "
        "A dotted em-dash is a metric with no table behind it yet; hover it for why."
    )


# =================================================================
# THE AGENT DOSSIER
# =================================================================
def page_agent(desk: Desk, name: str) -> None:
    if name not in BY_NAME:
        st.warning(f"No agent called {esc(name)} on this desk.")
        return
    _, mono, ext, role, group, run_keys = BY_NAME[name]
    lamp, state_word = desk.status(run_keys)

    # An explicit IN list rather than = ANY(%s): array adaptation depends
    # on which psycopg is installed, and this does not.
    holes = ",".join(["%s"] * len(run_keys))
    runs = load(f"""
        SELECT run_date, phase, agent_name, status, started_at, completed_at,
               error_message
        FROM agent_runs WHERE agent_name IN ({holes})
        ORDER BY run_date DESC, phase LIMIT 40""", tuple(run_keys))

    today_rows = runs[runs["run_date"] == et_today] if not runs.empty else pd.DataFrame()
    if today_rows.empty:
        last_txt = "not called today"
    else:
        done = today_rows["completed_at"].dropna()
        last_txt = (f'reported {done.max().strftime("%H:%M:%S")} &middot; '
                    f'ran {len(today_rows)} time'
                    f'{"s" if len(today_rows) != 1 else ""} today'
                    if not done.empty else f"{state_word} &middot; not yet complete")

    kv = ""
    for k, v in desk.metrics(name):
        kv += f'<div class="k">{k}</div><div class="v">{v}</div>'

    st.markdown(f"""
<div class="mby-page">
  <div class="mby-dossier">
    <div class="big">
      <div class="disc">{mono}</div>
      <span class="lamp" style="background:{lamp}"></span>
    </div>
    <div style="flex:1;min-width:0">
      <div style="display:flex;align-items:baseline;gap:10px">
        <span style="font-family:'Libre Baskerville',Georgia,serif;font-size:25px">{name}</span>
        <span class="mby-mono" style="font-size:12px;color:{C['faint']}">{ext}</span>
      </div>
      <div style="font-size:13px;color:{C['muted']};margin-top:2px">{role}</div>
      <div class="mby-mono" style="font-size:11px;color:{lamp};margin-top:6px">{last_txt}</div>
    </div>
  </div>
  <div class="mby-quote">&ldquo;{MANDATES.get(name, '')}&rdquo;</div>
  <div class="mby-band"><span class="mby-sec">TODAY</span></div>
  <div class="mby-kv">{kv}</div>
</div>
""", unsafe_allow_html=True)

    DETAIL[name](desk)

    st.markdown(f'<div class="mby-sec" style="margin:10px 0 4px 0">RUN HISTORY</div>',
                unsafe_allow_html=True)
    st.caption("Every invocation on record for this agent, newest first. "
               "Nora appears under two keys — nora_review (Phase 3, only when "
               "Solomon escalates) and nora_monitor (Phase 5, always).")
    if runs.empty:
        st.info(f"{name} has never run.")
    else:
        table(runs, height=260)


# ---- per-agent detail ------------------------------------------
def _d_atlas(desk: Desk) -> None:
    st.subheader("Regime history")
    df = load("SELECT brief_date, regime_signal, change_from_yesterday, confidence "
              "FROM macro_briefs ORDER BY brief_date DESC LIMIT 60")
    if df.empty:
        st.info("Atlas has not produced a brief yet.")
    else:
        table(df)
    if desk.macro is not None and desk.macro["narrative"]:
        st.subheader(f"Latest narrative — {desk.macro['brief_date']}")
        st.write(desk.macro["narrative"])


def _d_vera(desk: Desk) -> None:
    st.subheader("Open theses")
    st.caption("A thesis she opened is one she has to keep defending or close.")
    df = load("SELECT ticker, opened_date, original_conviction, sector, thesis_text "
              "FROM theses WHERE closed_date IS NULL ORDER BY opened_date DESC")
    if df.empty:
        st.info("No open theses.")
    else:
        table(df)

    st.subheader("Candidate history")
    df = load("SELECT candidate_date, ticker, conviction_score, sector, catalyst "
              "FROM new_candidates ORDER BY candidate_date DESC LIMIT 100")
    if df.empty:
        st.info("Nothing surfaced yet.")
    else:
        table(df)

    st.subheader("Position monitoring")
    df = load("SELECT log_date, ticker, status, trigger, conviction_score, reasoning "
              "FROM position_monitoring_log ORDER BY log_date DESC LIMIT 100")
    if df.empty:
        st.info("Nothing held to monitor.")
    else:
        table(df)


def _d_solomon(desk: Desk) -> None:
    st.subheader("Decisions")
    st.caption("Most days are 'no action'. That is the chair doing his job, "
               "not the desk failing to find ideas.")
    df = load("SELECT decision_date, action_needed, narrative "
              "FROM strategy_decisions ORDER BY decision_date DESC LIMIT 60")
    if df.empty:
        st.info("Solomon has not decided anything yet.")
    else:
        table(df)

    st.subheader("Proposals escalated")
    df = load("""
        SELECT sd.decision_date, p.ticker, p.action, p.linked_trigger, p.urgency,
               p.rationale
        FROM proposals p JOIN strategy_decisions sd ON p.strategy_decision_id = sd.id
        ORDER BY sd.decision_date DESC""")
    if df.empty:
        st.info("Nothing has been escalated.")
    else:
        table(df)


def _d_nora(desk: Desk) -> None:
    policy = one("SELECT effective_date, max_position_pct, max_sector_pct, "
                 "drawdown_breaker_pct, min_position_count, loss_review_pct, "
                 "profit_review_pct FROM risk_policy_versions "
                 "ORDER BY effective_date DESC, id DESC LIMIT 1")
    positions = load("SELECT ticker, sector, weight_pct FROM positions "
                     "ORDER BY weight_pct DESC NULLS LAST")

    st.markdown(f'<div class="mby-sec" style="margin:10px 0 4px 0">'
                f'LIMITS GOVERNED</div>', unsafe_allow_html=True)
    if policy is None:
        st.info("No risk policy version on record — Nora has nothing to enforce.")
    else:
        max_pos = float(policy["max_position_pct"])
        max_sec = float(policy["max_sector_pct"])
        dd = float(policy["drawdown_breaker_pct"])

        top_w = (float(positions["weight_pct"].max())
                 if not positions.empty and positions["weight_pct"].notna().any() else 0.0)
        if not positions.empty and positions["sector"].notna().any():
            sec_w = float(positions.groupby("sector")["weight_pct"].sum().max())
            sec_name = str(positions.groupby("sector")["weight_pct"].sum().idxmax())
        else:
            sec_w, sec_name = 0.0, "none held"
        invested = (float(positions["weight_pct"].sum())
                    if not positions.empty and positions["weight_pct"].notna().any() else 0.0)
        cash_w = max(0.0, 100.0 - invested)

        def bar(label, cur, cap, colour, note):
            pct = 0 if not cap else min(100.0, abs(cur) / abs(cap) * 100.0)
            return (f'<div><div class="mby-mono" style="display:flex;'
                    f'justify-content:space-between;font-size:11.5px;margin-bottom:5px">'
                    f'<span style="color:{C["muted"]}">{label}</span>'
                    f'<span style="color:{C["ink"]}">{cur:.2f}% '
                    f'<span style="color:{C["faint"]}">{note}</span></span></div>'
                    f'<div class="mby-bar"><div style="width:{pct:.1f}%;'
                    f'background:{colour}"></div></div></div>')

        bars = (bar("Largest single position", top_w, max_pos,
                    C["green"] if top_w <= max_pos else C["oxblood"],
                    f"/ {max_pos:.2f}%")
                + bar(f"Largest sector &mdash; {esc(sec_name)}", sec_w, max_sec,
                      C["green"] if sec_w <= max_sec else C["oxblood"],
                      f"/ {max_sec:.2f}%")
                + bar("Cash", cash_w, 100.0, C["green"], "of NAV")
                + bar("Drawdown breaker", dd, dd, C["amber"], "halt threshold"))
        st.markdown(f'<div class="mby-page"><div class="mby-limits">{bars}</div></div>',
                    unsafe_allow_html=True)
        st.caption(
            f"Policy effective {policy['effective_date']}. Review triggers: "
            f"loss {float(policy['loss_review_pct']):.1f}%, gain "
            f"{float(policy['profit_review_pct']):.1f}% — these mandate an "
            f"investigation by Vera, never an automatic exit. min_position_count "
            f"({int(policy['min_position_count'])}) is warn-only and never blocks a trade."
        )

    st.subheader("Breaches")
    df = load("""
        SELECT rr.review_date, rb.rule_violated, rb.ticker, rb.current_value,
               rb.limit_value, rb.source
        FROM risk_breaches rb JOIN risk_reviews rr ON rb.risk_review_id = rr.id
        ORDER BY rr.review_date DESC, rb.rule_violated""")
    if df.empty:
        st.success("No limit breaches on record.")
    else:
        table(df)

    st.subheader("Proposal reviews")
    st.caption("max_size_pct is HEADROOM — how much more of the book this name may "
               "take, net of what is already held. Marcus's target_size_pct is the "
               "resulting total weight. Confusing the two is what let a position "
               "reach 15.99% against an 8% cap.")
    df = load("""
        SELECT p.ticker, pr.decision, pr.max_size_pct, pr.reasoning
        FROM proposal_reviews pr JOIN proposals p ON pr.proposal_id = p.id
        ORDER BY pr.id DESC""")
    if df.empty:
        st.info("Nothing has reached her review yet.")
    else:
        table(df)

    st.subheader("Review history")
    df = load("SELECT review_date, portfolio_status, circuit_breaker_active "
              "FROM risk_reviews ORDER BY review_date DESC LIMIT 60")
    if df.empty:
        st.info("No reviews yet.")
    else:
        table(df)


def _d_marcus(desk: Desk) -> None:
    st.subheader("Allocations")
    st.caption("target_size_pct is the RESULTING total weight, not an increment.")
    df = load("SELECT allocation_date, ticker, action, target_size_pct, "
              "conviction_input, priority, rationale FROM allocations "
              "ORDER BY allocation_date DESC, priority LIMIT 100")
    if df.empty:
        st.info("Marcus has never sized a position.")
    else:
        table(df)


def _d_ada(desk: Desk) -> None:
    st.subheader("Orders today")
    if desk.orders_today.empty:
        st.info("No orders today. On most days that is correct — the desk runs "
                "daily but changes the portfolio only when Solomon escalates.")
    else:
        table(desk.orders_today)

    st.subheader("Execution quality")
    st.caption("Slippage in basis points against the limit price — Ada's success "
               "metric (Operating Manual §7.6). Measured here; not yet fed back "
               "into sizing.")
    df = load("""
        SELECT ticker, count(*) AS fills, round(avg(slippage_bps),2) AS mean_bps,
               round(max(slippage_bps),2) AS worst_bps
        FROM orders WHERE slippage_bps IS NOT NULL
        GROUP BY ticker ORDER BY mean_bps DESC NULLS LAST""")
    if df.empty:
        st.info("No fills with a recorded limit price yet.")
    else:
        table(df)

    st.subheader("Order outcomes, all time")
    df = load("SELECT status, count(*) AS orders, min(order_date) AS first_seen, "
              "max(order_date) AS last_seen FROM orders GROUP BY status "
              "ORDER BY count(*) DESC")
    if df.empty:
        st.info("No orders on record.")
    else:
        table(df)
        st.caption(
            "`halted_by_operator` is the kill switch; `halted_circuit_breaker` is "
            "Nora's drawdown freeze; `rejected_zero_shares` means the target was "
            "smaller than one share; `rejected_post_trade_limit` and "
            "`rejected_live_drift_limit` are the position cap refusing a trade "
            "that would breach it.")


def _d_otis(desk: Desk) -> None:
    st.subheader("Open discrepancies")
    st.caption(
        "Never auto-resolved — flagged for human review. A discrepancy describes the "
        "book AT THE MOMENT OTIS FOUND IT; it does not update itself when the "
        "underlying situation changes, so an item here can be factually stale until "
        "the next close re-evaluates it.")
    df = load("SELECT found_date, ticker, expected, actual, description "
              "FROM discrepancies WHERE NOT resolved ORDER BY found_date DESC")
    if df.empty:
        st.success("No unresolved discrepancies.")
    else:
        records(df, title_cols=["found_date", "ticker"], body_col="description")

    st.subheader("The book")
    df = load("SELECT ticker, sector, shares, avg_cost, market_value, "
              "unrealized_pnl, weight_pct, last_updated FROM positions "
              "ORDER BY weight_pct DESC NULLS LAST")
    if df.empty:
        st.info("No open positions.")
    else:
        table(df)

    st.subheader("Daily reconciliation")
    df = load("SELECT pnl_date, nav, total_pnl, realized_pnl, unrealized_pnl, "
              "cash_balance, reconciled FROM daily_pnl ORDER BY pnl_date DESC LIMIT 60")
    if df.empty:
        st.info("Otis has not closed a session.")
    else:
        table(df)


def _d_clara(desk: Desk) -> None:
    st.subheader("Process checks")
    st.caption("A violation is flagged regardless of profitability — process is "
               "checked independent of outcome.")
    df = load("SELECT check_date, process_check, violations FROM process_checks "
              "ORDER BY check_date DESC LIMIT 60")
    if df.empty:
        st.info("Clara has not run a process audit yet.")
    else:
        for _, row in df.iterrows():
            icon = "🟢" if row["process_check"] == "clean" else "🔴"
            with st.expander(f"{icon} {row['check_date']} — {row['process_check']}"):
                if row["violations"]:
                    st.json(row["violations"])
                else:
                    st.write("No violations.")

    st.subheader("Attribution")
    df = load("SELECT attribution_date, ticker, contribution_pct, thesis_status "
              "FROM attribution ORDER BY attribution_date DESC LIMIT 100")
    if df.empty:
        st.info("Nothing held or traded to attribute.")
    else:
        table(df)

    st.subheader("Reports filed")
    df = load("SELECT report_date, executive_summary FROM daily_reports "
              "ORDER BY report_date DESC LIMIT 30")
    if df.empty:
        st.info("No closing reports filed.")
    else:
        table(df)


DETAIL = {"Atlas": _d_atlas, "Vera": _d_vera, "Solomon": _d_solomon, "Nora": _d_nora,
          "Marcus": _d_marcus, "Ada": _d_ada, "Otis": _d_otis, "Clara": _d_clara}


# =================================================================
# THE CONFERENCE ROOM — Phase 2
#
# The one literal room worth drawing, because the morning meeting
# genuinely is a meeting: several agents contribute, one chairs, and
# the output is a single verdict the rest of the day hangs off.
# Empty seats are information — they say who stood down.
# =================================================================
# x, y within the 704x280 stage. The table occupies x 192-512, y 71-209,
# so every seat sits clear of it: three along each long side, one at
# each end.
SEATS = [
    ("Atlas",   218, 16),  ("Vera",   352, 6),   ("Solomon", 486, 16),
    ("Nora",    218, 216), ("Marcus", 352, 226), ("Clara",   486, 216),
    ("Otis",    112, 112), ("Ada",    592, 112),
]


def page_room(desk: Desk) -> None:
    meeting_runs = (desk.runs_today[desk.runs_today["phase"].isin([1, 2])]
                    if not desk.runs_today.empty else pd.DataFrame())
    present = set()
    if not meeting_runs.empty:
        for n, _, _, _, _, keys in AGENTS:
            if meeting_runs["agent_name"].isin(keys).any():
                present.add(n)

    seats_html = ""
    for name, x, y in SEATS:
        _, mono, _, _, _, _ = BY_NAME[name]
        off = "" if name in present else " off"
        seats_html += (f'<a class="mby-seat{off}" target="_self" '
                       f'href="{href("agent", who=name)}" '
                       f'style="left:{x}px;top:{y}px">'
                       f'<div class="d">{mono}</div>'
                       f'<div class="n">{name.upper()}</div></a>')

    if not meeting_runs.empty:
        starts = meeting_runs["started_at"].dropna()
        ends = meeting_runs["completed_at"].dropna()
        if not starts.empty and not ends.empty:
            span = (f'{starts.min().strftime("%H:%M")} &ndash; '
                    f'{ends.max().strftime("%H:%M")} ET &middot; '
                    f'{int((ends.max() - starts.min()).total_seconds() // 60)}m')
        else:
            span = "in progress"
    else:
        span = "not convened today"

    # ---- what was actually said -------------------------------
    says = ""

    def say(name: str, badge: str, badge_col: str, body: str) -> str:
        _, mono, _, _, _, _ = BY_NAME[name]
        return f"""
        <div class="mby-say">
          <div class="av">{mono}</div>
          <div style="flex:1">
            <div style="display:flex;align-items:baseline;gap:9px;margin-bottom:4px">
              <span style="font-family:'Libre Baskerville',Georgia,serif;font-size:14.5px">{name}</span>
              <span class="mby-mono" style="font-size:10px;color:{badge_col};
                    letter-spacing:.08em">{badge}</span>
            </div>
            <div class="body">{body}</div>
          </div>
        </div>"""

    if desk.macro is not None and desk.macro["brief_date"] == et_today:
        says += say("Atlas",
                    f'REGIME: {esc(desk.macro["regime_signal"]).upper()}', C["amber"],
                    esc(desk.macro["narrative"] or "No narrative recorded."))
    cands = load("SELECT ticker, conviction_score, catalyst FROM new_candidates "
                 "WHERE candidate_date = %s ORDER BY conviction_score DESC", (et_today,))
    if not cands.empty:
        lines = "; ".join(f'{esc(r["ticker"])} (conviction {r["conviction_score"]})'
                          for _, r in cands.head(4).iterrows())
        says += say("Vera", f"{len(cands)} SURFACED", C["faint"], lines)
    elif "Vera" in present:
        says += say("Vera", "0 SURFACED", C["faint"],
                    "Nothing cleared the conviction threshold today.")
    if desk.risk is not None and desk.risk["review_date"] == et_today:
        s = str(desk.risk["portfolio_status"])
        says += say("Nora", s.replace("_", " ").upper(),
                    C["green"] if s == "within_limits" else C["amber"],
                    "Book re-read against the four numeric limits.")
    if desk.strat is not None and desk.strat["decision_date"] == et_today:
        says += say("Solomon", "CHAIR", C["brass"],
                    esc(desk.strat["narrative"] or "No narrative recorded."))

    if not says:
        says = (f'<div style="padding:0 28px 20px 28px;color:{C["faint"]};'
                f'font-size:13px">The meeting has not convened today &mdash; '
                f'no Phase 1 or Phase 2 output on record for {et_today}.</div>')

    if desk.strat is not None and desk.strat["decision_date"] == et_today:
        verdict = ("Act &mdash; proposals escalated" if desk.strat["action_needed"]
                   else "No action &mdash; hold the book")
    else:
        verdict = "No verdict recorded"
    down = [n for n, _, _, _, _, _ in AGENTS if n not in present]

    st.markdown(f"""
<div class="mby-page">
  <div class="mby-head">
    <div style="display:flex;align-items:baseline;gap:14px">
      <span class="h">The Morning Meeting</span>
      <span class="mby-mono" style="font-size:10.5px;color:{C['faint']};
            letter-spacing:.14em">PHASE 1&ndash;2</span>
    </div>
    <span class="mby-mono" style="font-size:11.5px;color:{C['muted']}">{span}</span>
  </div>

  <div class="mby-room">
    <div class="mby-stage">
      <div class="mby-table"></div>
      <div class="mby-tablbl">
        <div class="mby-mono" style="font-size:9.5px;letter-spacing:.2em;
             color:{C['faint']}">CONFERENCE ROOM</div>
        <div class="mby-mono" style="font-size:12px;color:{C['brass']};margin-top:4px">
          {len(present)} of 8 present</div>
      </div>
      {seats_html}
    </div>
  </div>

  <div class="mby-band"><span class="mby-sec">MINUTES</span></div>
  <div style="padding-top:16px">{says}</div>

  <div class="mby-verdict">
    <div>
      <div class="mby-mono" style="font-size:10px;letter-spacing:.18em;
           color:{C['faint']};margin-bottom:5px">VERDICT</div>
      <div style="font-family:'Libre Baskerville',Georgia,serif;font-size:17px">{verdict}</div>
    </div>
    <div class="mby-mono" style="text-align:right;font-size:11px;color:{C['faint']};
         line-height:1.7">
      <div>stood down <span style="color:{C['muted']}">{', '.join(down) or 'none'}</span></div>
      <div>date <span style="color:{C['muted']}">{et_today}</span></div>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

    st.caption("Seats are links — click one to open that agent's dossier. "
               "A dashed seat means the agent was not called for this cycle, "
               "which on this desk is the ordinary outcome for Marcus and Ada.")


# =================================================================
# THE SEVEN — v2's views, one page each
# =================================================================
def page_report(desk: Desk) -> None:
    dates = load("SELECT report_date FROM daily_reports ORDER BY report_date DESC")
    if dates.empty:
        st.info("No closing report yet — Clara hasn't compiled one. "
                "Run orchestrator.py or agents.clara.")
        return
    selected = st.selectbox("Report date", dates["report_date"],
                            format_func=lambda d: d.strftime("%Y-%m-%d"))
    report = load("SELECT * FROM daily_reports WHERE report_date = %s", (selected,))
    st.markdown(report.iloc[0]["full_report_md"])


def page_briefing(desk: Desk) -> None:
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Macro Backdrop (Atlas)")
        macro = load("SELECT * FROM macro_briefs ORDER BY brief_date DESC LIMIT 1")
        if not macro.empty:
            row = macro.iloc[0]
            st.metric("Regime", row["regime_signal"], delta=row["change_from_yesterday"])
            st.caption(f"Confidence: {row['confidence']}")
            st.write(row["narrative"])
        else:
            st.info("Atlas hasn't run yet.")
    with col2:
        st.subheader("Strategy Decision (Solomon)")
        strat = load("SELECT * FROM strategy_decisions ORDER BY decision_date DESC LIMIT 1")
        if not strat.empty:
            row = strat.iloc[0]
            st.metric("Action needed today?", "Yes" if row["action_needed"] else "No")
            st.write(row["narrative"])
        else:
            st.info("Solomon hasn't run yet.")

    st.subheader("Latest Research Candidates (Vera)")
    candidates = load(
        "SELECT candidate_date, ticker, conviction_score, catalyst "
        "FROM new_candidates ORDER BY candidate_date DESC, conviction_score DESC LIMIT 10")
    if candidates.empty:
        st.info("No candidates surfaced yet.")
    else:
        table(candidates)

    col3, col4 = st.columns(2)
    with col3:
        st.subheader("Risk Status (Nora)")
        risk = load("SELECT * FROM risk_reviews ORDER BY review_date DESC LIMIT 1")
        if not risk.empty:
            row = risk.iloc[0]
            icon = {"within_limits": "🟢", "breach_warning": "🟡",
                    "breach_hard": "🔴"}.get(row["portfolio_status"], "⚪")
            st.metric("Portfolio status", f"{icon} {row['portfolio_status']}")
            st.metric("Circuit breaker",
                      "ACTIVE ⚠️" if row["circuit_breaker_active"] else "Inactive")
        else:
            st.info("Nora hasn't run yet.")
    with col4:
        st.subheader("Reconciliation (Otis)")
        pnl = load("SELECT * FROM daily_pnl ORDER BY pnl_date DESC LIMIT 1")
        if not pnl.empty:
            row = pnl.iloc[0]
            st.metric("Reconciled?",
                      "✅ Yes" if row["reconciled"] else "⚠️ Discrepancies found")
            st.metric("Today's total P&L", f"${row['total_pnl']:.2f}")
        else:
            st.info("Otis hasn't run yet.")


def page_activity(desk: Desk) -> None:
    st.subheader("Run ledger")
    st.caption(
        "Every agent invocation: when it started, whether it completed, and the "
        "traceback if it didn't. Nora appears twice a day under nora_review "
        "(Phase 3, only when Solomon escalates) and nora_monitor (Phase 5, always).")
    runs = load("""
        SELECT run_date, phase, agent_name, status, started_at, completed_at,
               error_message
        FROM agent_runs ORDER BY run_date DESC, phase, agent_name LIMIT 200""")
    if runs.empty:
        st.info("No runs recorded yet — the ledger fills from the next cycle.")
    else:
        failed = runs[runs["status"] == "failed"]
        if not failed.empty:
            st.error(f"{len(failed)} failed run(s) on record — see error_message below.")
        running = runs[runs["status"] == "running"]
        if not running.empty:
            st.warning(f"{len(running)} run(s) still marked 'running' — an interrupted "
                       "cycle leaves its last agent in this state.")
        table(runs, height=320)

    st.divider()
    st.subheader("Output timeline")
    st.caption("What each agent actually produced, by day — content rather than execution.")
    activity = load("""
        SELECT brief_date AS activity_date, 'Atlas' AS agent,
               'Macro brief' AS activity, narrative AS detail
        FROM macro_briefs
        UNION ALL
        SELECT candidate_date, 'Vera', 'New candidate: ' || ticker, thesis
        FROM new_candidates
        UNION ALL
        SELECT log_date, 'Vera', 'Position monitoring: ' || ticker, reasoning
        FROM position_monitoring_log
        UNION ALL
        SELECT decision_date, 'Solomon', 'Strategy decision', narrative
        FROM strategy_decisions
        UNION ALL
        SELECT review_date, 'Nora', 'Risk review', portfolio_status
        FROM risk_reviews
        UNION ALL
        SELECT pnl_date, 'Otis', 'Daily reconciliation',
               CASE WHEN reconciled THEN 'Clean' ELSE 'Discrepancies found' END
        FROM daily_pnl
        UNION ALL
        SELECT check_date, 'Clara', 'Process compliance check', process_check
        FROM process_checks
        ORDER BY activity_date DESC""")
    table(activity, height=500)


def page_research(desk: Desk) -> None:
    _d_vera(desk)


def page_risk(desk: Desk) -> None:
    st.subheader("Strategy Decisions (Solomon)")
    table(load("SELECT decision_date, action_needed, narrative "
               "FROM strategy_decisions ORDER BY decision_date DESC"))

    st.subheader("Proposals")
    proposals = load("""
        SELECT sd.decision_date, p.ticker, p.action, p.linked_trigger, p.urgency
        FROM proposals p JOIN strategy_decisions sd ON p.strategy_decision_id = sd.id
        ORDER BY sd.decision_date DESC""")
    if proposals.empty:
        st.info("No proposals have been escalated yet.")
    else:
        table(proposals)

    st.subheader("Risk Reviews (Nora)")
    table(load("SELECT review_date, portfolio_status, circuit_breaker_active "
               "FROM risk_reviews ORDER BY review_date DESC"))

    st.subheader("Trading Control (kill switch)")
    st.caption("Manual halt/resume history. Distinct from Nora's drawdown circuit "
               "breaker: that one is automatic and still permits de-risking, this one "
               "is operator-set and stops everything including exits.")
    ctrl = load("SELECT changed_at, trading_enabled, changed_by, reason "
                "FROM trading_control ORDER BY id DESC LIMIT 25")
    if ctrl.empty:
        st.info("No control rows on record — trading enabled by bootstrap default.")
    else:
        table(ctrl)

    st.divider()
    st.subheader("Risk Limit Breaches (Nora)")
    st.caption("Every limit in the active risk policy, checked daily against the book — "
               "not only on days a trade was proposed. `min_position_count` is warn-only: "
               "it is recorded and shown, but never blocks a trade.")
    breaches = load("""
        SELECT rr.review_date, rb.rule_violated, rb.ticker, rb.current_value,
               rb.limit_value, rb.source
        FROM risk_breaches rb JOIN risk_reviews rr ON rb.risk_review_id = rr.id
        ORDER BY rr.review_date DESC, rb.rule_violated""")
    if breaches.empty:
        st.success("No limit breaches on record.")
    else:
        table(breaches)

    st.subheader("Proposal Reviews (Nora's approve/reject decisions)")
    pr = load("""
        SELECT p.ticker, pr.decision, pr.max_size_pct, pr.reasoning
        FROM proposal_reviews pr JOIN proposals p ON pr.proposal_id = p.id
        ORDER BY pr.id DESC""")
    if pr.empty:
        st.info("No proposals have reached Nora's review yet.")
    else:
        table(pr)

    st.divider()
    st.subheader("Process Compliance (Clara)")
    st.caption("A violation is flagged regardless of profitability — process is "
               "checked independent of outcome.")
    process = load("SELECT check_date, process_check, violations FROM process_checks "
                   "ORDER BY check_date DESC")
    if process.empty:
        st.info("Clara hasn't run a process audit yet.")
    else:
        for _, row in process.iterrows():
            icon = "🟢" if row["process_check"] == "clean" else "🔴"
            with st.expander(f"{icon} {row['check_date']} — {row['process_check']}"):
                if row["violations"]:
                    st.json(row["violations"])
                else:
                    st.write("No violations.")

    st.subheader("Performance Attribution (Clara)")
    attribution = load("SELECT attribution_date, ticker, contribution_pct, thesis_status "
                       "FROM attribution ORDER BY attribution_date DESC")
    if attribution.empty:
        st.info("No attribution yet — nothing held/traded to attribute.")
    else:
        table(attribution)


def page_execution(desk: Desk) -> None:
    today_orders = load("""
        SELECT ticker, action, shares, limit_price, status, fill_price,
               slippage_bps, allocation_id, alpaca_order_id
        FROM orders WHERE order_date = CURRENT_DATE ORDER BY ticker""")

    if today_orders.empty:
        st.info("No orders today. On most days that is correct — the desk runs daily "
                "but changes the portfolio only when Solomon escalates.")
    else:
        reached = today_orders["alpaca_order_id"].notna()
        filled = today_orders["status"] == "filled"
        working = reached & ~filled & (today_orders["status"] != "expired_unfilled")
        c1, c2, c3 = st.columns(3)
        c1.metric("Filled", int(filled.sum()))
        c2.metric("Working at broker", int(working.sum()))
        c3.metric("Never submitted", int((~reached).sum()))
        table(today_orders)
        if not today_orders[~reached].empty:
            st.caption("Orders that never reached the broker are recorded rather than "
                       "dropped — the status is the reason. A halted day, a stale ledger "
                       "or a limit breach all leave a row saying so.")

    st.divider()
    st.subheader("Decision → order → ledger")
    st.caption("Where an intention stopped. A row with an allocation and no order means "
               "nothing was attempted; an order with no transaction means nothing filled. "
               "This is the chain Clara audits for process compliance.")
    chain = load("""
        SELECT a.allocation_date, a.id AS allocation, a.ticker, a.action,
               a.target_size_pct AS target_pct, o.status AS order_status, o.shares,
               o.fill_price, t.id AS txn, t.realized_pnl
        FROM allocations a
        LEFT JOIN orders o ON o.allocation_id = a.id
        LEFT JOIN transactions t ON t.alpaca_transaction_id = o.alpaca_order_id
        ORDER BY a.allocation_date DESC, a.ticker LIMIT 100""")
    if chain.empty:
        st.info("No allocations on record yet — Marcus has not sized anything.")
    else:
        table(chain)

    st.divider()
    st.subheader("Cancelled unfilled")
    st.caption("Every order is a DAY limit, so an unfilled one would lapse overnight on "
               "its own. Otis cancels them explicitly at the close instead, so the "
               "attempt leaves a record. Not errors — the market did not reach the price.")
    expired = load("""
        SELECT order_date, ticker, action, shares, limit_price, allocation_id
        FROM orders WHERE status = 'expired_unfilled'
        ORDER BY order_date DESC LIMIT 50""")
    if expired.empty:
        st.success("Nothing has expired unfilled.")
    else:
        table(expired)

    st.divider()
    st.subheader("Execution quality")
    st.caption("Slippage in basis points against the limit price — Ada's success metric "
               "(Operating Manual §7.6). Positive means the fill came in worse than the "
               "limit for a buy. Measured here; not yet fed back into sizing.")
    slippage = load("""
        SELECT ticker, count(*) AS fills, round(avg(slippage_bps),2) AS mean_bps,
               round(max(slippage_bps),2) AS worst_bps
        FROM orders WHERE slippage_bps IS NOT NULL
        GROUP BY ticker ORDER BY mean_bps DESC NULLS LAST""")
    if slippage.empty:
        st.info("No fills with a recorded limit price yet — nothing to measure.")
    else:
        table(slippage)

    st.divider()
    st.subheader("Order outcomes, all time")
    outcomes = load("SELECT status, count(*) AS orders, min(order_date) AS first_seen, "
                    "max(order_date) AS last_seen FROM orders GROUP BY status "
                    "ORDER BY count(*) DESC")
    if outcomes.empty:
        st.info("No orders on record yet.")
    else:
        table(outcomes)
        st.caption(
            "`halted_by_operator` is the kill switch; `halted_circuit_breaker` is "
            "Nora's drawdown freeze; `rejected_zero_shares` means the target was "
            "smaller than one share; `rejected_post_trade_limit` and "
            "`rejected_live_drift_limit` are the position cap refusing a trade "
            "that would breach it.")


def page_portfolio(desk: Desk) -> None:
    st.subheader("Open Discrepancies (Otis)")
    st.caption(
        "Never auto-resolved — flagged here for human review. Each one describes the "
        "book AT THE MOMENT OTIS FOUND IT and does not update itself afterwards, so an "
        "item can be factually stale until the next close re-evaluates it. Read the "
        "date on it.")
    unresolved = load("SELECT found_date, ticker, expected, actual, description "
                      "FROM discrepancies WHERE NOT resolved ORDER BY found_date DESC")
    if unresolved.empty:
        st.success("No unresolved discrepancies.")
    else:
        st.warning(f"{len(unresolved)} unresolved discrepancy(ies):")
        records(unresolved, title_cols=["found_date", "ticker"], body_col="description")

    st.subheader("P&L Over Time")
    pnl = load("SELECT pnl_date, nav, total_pnl, realized_pnl, unrealized_pnl, "
               "open_unrealized_pnl, cash_balance, "
               "realized_pnl + unrealized_pnl - total_pnl AS reconciliation_gap "
               "FROM daily_pnl ORDER BY pnl_date")
    if pnl.empty:
        st.info("No portfolio history yet — Otis hasn't run.")
    else:
        if pnl["nav"].notna().any():
            st.caption("Account equity (NAV) — the series the drawdown breaker measures.")
            st.line_chart(pnl.dropna(subset=["nav"]).set_index("pnl_date")["nav"])
        else:
            st.info("No NAV recorded yet — the breaker stays inactive until Otis has "
                    "closed two sessions.")
        st.caption(
            "realized_pnl + unrealized_pnl = total_pnl, all three daily flows. "
            "open_unrealized_pnl is the lifetime open gain — a stock, not a flow, "
            "which is why it sits apart. reconciliation_gap should be 0.00 on every "
            "row written from 2 Sept 2026 onward; earlier rows predate the fix and "
            "cannot be recomputed.")
        table(pnl)

    st.subheader("Current Positions")
    positions = load("SELECT ticker, sector, shares, avg_cost, market_value, "
                     "unrealized_pnl, weight_pct, last_updated FROM positions "
                     "ORDER BY weight_pct DESC NULLS LAST")
    if positions.empty:
        st.info("No open positions currently held.")
    else:
        table(positions)


ROUTES = {
    "report": page_report, "briefing": page_briefing, "activity": page_activity,
    "research": page_research, "risk": page_risk, "execution": page_execution,
    "portfolio": page_portfolio,
}


# =================================================================
# RENDER
# =================================================================
st.markdown(PAGE_CSS, unsafe_allow_html=True)
render_wall()

# The wall shows THAT the desk is halted. This says who, why and when —
# too much text for the strip, and exactly what you want in front of you
# during an incident. It stays on every page for the same reason.
if halted:
    st.error(
        f"**TRADING HALTED** by {control_row['changed_by']} — "
        f"\"{control_row['reason']}\"  \n"
        f"Set {control_row['changed_at']}. Ada will submit nothing, buys or sells, "
        "until resumed. Analysis and reconciliation continue as normal.  \n"
        "Resume with `python -m core.trading_control resume --reason \"...\"`.")

render_nav()

desk = Desk()

if VIEW == "floor":
    page_floor(desk)
elif VIEW == "agent":
    page_agent(desk, WHO)
elif VIEW == "room":
    page_room(desk)
elif VIEW in ROUTES:
    st.markdown(
        f'<div class="mby-page"><div class="mby-head">'
        f'<span class="h">{PAGE_TITLES[VIEW]}</span>'
        f'<span class="mby-mono" style="font-size:11px;color:{C["faint"]};'
        f'letter-spacing:.12em">{et_now.strftime("%d %b %Y").upper()} &middot; NEW YORK'
        f'</span></div></div>', unsafe_allow_html=True)
    ROUTES[VIEW](desk)
else:
    st.warning(f"No page called `{esc(VIEW)}`.")
    page_floor(desk)
