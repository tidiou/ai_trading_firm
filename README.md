# MBY-Trading

A multi-agent, US large-cap equities research-to-execution pipeline,
modeled on institutional trading-desk discipline. Paper trading (Alpaca)
first; real capital later as a gate.

See `docs/ai_trading_firm_operating_manual.md` for the full design:
vision, scope, architecture principles, daily operating cycle, and
each agent's charter (mandate, inputs, tools, decision rights,
output contract, memory, guardrails, model/judgment level, success metric).

## Project layout

- `agents/` — one module per agent (Atlas, Vera, Solomon, Nora, Marcus, Ada, Otis, Clara)
- `core/` — db session, market calendar, cycle timetable, logging, API clients
- `orchestrator.py` — the daily cycle runner
- `config/` — risk policy defaults and other tunables
- `db/schema.sql` — full Postgres schema; `db/migrations/` applied in order
- `dashboard/` — the Streamlit read-only view (`streamlit run dashboard/app.py`)
- `scripts/` — the scheduler wrapper and its launchd agent
- `tests/` — the test suite
- `COMMANDS.md` — every command the desk answers to, grouped by what you are trying to do

## Setup

1. `pip install -r requirements.txt`
2. `cp .env.example .env` and fill in your keys
3. Run `db/schema.sql` against your Postgres instance, then every file in
   `db/migrations/` in numeric order
4. `python -m orchestrator all` to run a whole day manually

## Daily cycle (Operating Manual §6)

Atlas -> Vera -> Solomon -> [Nora -> Marcus -> Ada, only if triggered]
      -> Otis -> Nora monitor -> Clara

Once per NYSE trading day, in **three scheduled runs** rather than one.
Ada submits DAY limit orders, so the hour matters: an order placed after
the close is queued for the next session's open, and used to be cancelled
by Otis's own end-of-day sweep minutes later.

Each run has a **window**, not a fixed time: `auto` fires whatever is due
inside it and skips the rest, so a missed tick is picked up by the next one.

| run | window (ET) | phases | agents |
|-----|-------------|--------|--------|
| `premarket` | 07:00–09:25 | 1-2 | Atlas, Vera, Solomon |
| `execution` | 09:45–15:30 | 3-4 | Nora, Marcus, Ada |
| `close`     | 16:15–22:00 | 5   | Otis, Nora, Clara |

```
python -m orchestrator auto        # run whatever is due now — what the scheduler calls
python -m orchestrator status      # what has and has not run today
python -m orchestrator premarket   # force one group, ignoring the clock
python -m orchestrator all         # the whole day back to back (catch-up / demo)
```

## Unattended operation

`auto` reads the Eastern clock itself and does nothing almost every time
it is called, so the scheduler can be dumb: it ticks every fifteen
minutes and `auto` decides. That removes DST from the equation — cron
and launchd fire on local time, and the US, Europe and West Africa change
clocks on three different schedules — and a tick missed because the
machine was asleep is picked up by the next one.

```
cp scripts/com.mby.trading.desk.plist ~/Library/LaunchAgents/
# edit the two paths marked EDIT ME first
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mby.trading.desk.plist
launchctl print gui/$(id -u)/com.mby.trading.desk        # confirm it loaded
```

Use `bootstrap`, not the legacy `launchctl load` — `load` fails with
`Load failed: 5: Input/output error`, which names nothing. Run
`launchctl bootout gui/$(id -u)/com.mby.trading.desk` before re-loading
after any edit to the plist. And ignore the error's hint about root: this
is a per-user LaunchAgent, and bootstrapping it into the system domain
loses your home directory, your venv and Docker.

Logs land in `logs/desk-YYYY-MM.log`. The Floor shows a strip of the
three runs and turns it red when one is overdue — the only failure mode
that otherwise leaves no trace, since a machine that never woke up looks
exactly like a quiet day.

A closed laptop is a desk that does not trade. For operation that
survives that, run the same wrapper on an always-on host.

## Abbreviations

Only the ones that actually appear in this codebase, alphabetically.
Where a term has a general meaning and a narrower one here, both are given.

| | |
|---|---|
| **API** | Application Programming Interface — here, Alpaca (trading, market data) and FMP (fundamentals) |
| **bps** | Basis points. 1 bp = 0.01%, so 100 bps = 1%. Used for execution slippage (`orders.slippage_bps`) because the numbers are too small to read in percent |
| **CLI** | Command-Line Interface — the `python -m …` entry points. See `COMMANDS.md` |
| **DAY** | An order's time-in-force: it expires at the close of the session it belongs to. Ada submits DAY limits, which is why the hour of submission matters |
| **DST** | Daylight Saving Time. The US and EU change clocks on different dates, so for one week a year Berlin is ET + 5h instead of + 6h |
| **ET** | Eastern Time (`America/New_York`), covering both EST in winter and EDT in summer. The desk's authoritative clock — every window is stated in ET |
| **ETF** | Exchange-Traded Fund — a fund that trades like a single stock |
| **FMP** | Financial Modeling Prep — the fundamentals data provider Vera uses for valuation and sector |
| **IEX** | Investors Exchange — one exchange's own quotes. Alpaca's free data plan serves IEX only; `ALPACA_DATA_FEED=iex` |
| **IR** | Information Ratio — active return divided by tracking error. The standard measure of skill against a benchmark, and the one that needs years of data to mean anything |
| **JSONB** | Postgres' binary JSON column type. Used wherever an agent's output is structured but not worth its own table |
| **LLM** | Large Language Model — Claude, in every agent that needs judgment rather than arithmetic |
| **NAV** | Net Asset Value — cash plus the market value of all positions. Written each close to `daily_pnl.nav`; this is the equity curve the drawdown breaker runs on, deliberately not `total_pnl` |
| **NYSE** | New York Stock Exchange. Its calendar decides what counts as a trading day |
| **ORM** | Object-Relational Mapper — SQLAlchemy, mapping `core/models.py` classes onto tables |
| **P&L** | Profit and Loss. *Realized* = crystallised by a sale; *unrealized* = today's mark-to-market change on what is still held |
| **pp** | Percentage points — the gap between two percentages. A desk at +2% against an index at +3% is 1pp behind, not 1% behind. Kept distinct from `%` throughout |
| **SIP** | Securities Information Processor — the consolidated tape across all US exchanges. A paid Alpaca tier; requesting it on the free plan returns HTTP 403 |
| **SPY** | Ticker of the SPDR S&P 500 ETF Trust — the benchmark the desk is measured against. Fetched dividend-adjusted so it is a total-return series, matching how NAV already counts dividends received |
