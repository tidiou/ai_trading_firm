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
- `core/` — db session, market calendar, logging, and API clients (Alpaca/FMP/Claude)
- `orchestrator/` — the daily cycle runner
- `config/` — risk policy defaults and other tunables
- `db/schema.sql` — full Postgres schema
- `tests/` — one placeholder test module per agent + orchestrator

## Setup

1. `pip install -r requirements.txt`
2. `cp .env.example .env` and fill in your keys
3. Run `db/schema.sql` against your Postgres instance
4. `python orchestrator.py` to run one daily cycle manually

## Daily cycle (Operating Manual §6)

Atlas -> Vera -> Solomon -> [Nora -> Marcus -> Ada, only if triggered] -> Otis -> Clara

Runs once per NYSE trading day, scheduled against `America/New_York`
(not a fixed CET time, to avoid DST drift).
