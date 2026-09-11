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
