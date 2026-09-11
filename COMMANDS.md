# Command reference

Everything the desk answers to. Two standing assumptions for every line
below: the **project root** is the working directory, and the
**virtualenv is active**.

> `ALPACA_PAPER` defaults to paper when unset, but it is an exact string
> match — `1`, `yes` or a typo all evaluate to *live*. The only safe
> values are `true` and leaving it out.

Jump to: [Running the desk](#running-the-desk) · [Kill switch](#the-kill-switch) ·
[Performance](#performance-vs-the-index) · [Database](#database) ·
[Scheduler](#the-scheduler) · [Dashboard](#dashboard) · [Tests](#tests) ·
[Single agents](#single-agents)

---

## Running the desk

`orchestrator.py`. The day is three scheduled runs. The named groups run
**immediately**, ignoring the clock and the window; `auto` is the only one
that consults it.

| command | what it does |
|---|---|
| `python -m orchestrator status` | **What has and hasn't run today**, and what is overdue. Exits 1 if anything is overdue — run this first when something looks wrong. |
| `python -m orchestrator auto` | **Run whatever is due right now**, then exit. This is what the scheduler calls. Does nothing outside the windows, on a non-trading day, or if the group already completed — safe every 15 minutes. |
| `python -m orchestrator premarket` | **Phases 1–2** — Atlas, Vera, Solomon. The morning meeting. |
| `python -m orchestrator execution` | **Phases 3–4** — Nora, Marcus, Ada. Refuses if no completed Solomon run exists today. |
| `python -m orchestrator close` | **Phase 5** — Otis, Nora's monitor, Clara. Writes the NAV row and syncs the benchmark. The run that builds your history. |
| `python -m orchestrator all` | **All three groups back to back**, ignoring the clock. For a catch-up or a demo. Never schedule this — whichever hour you pick, two of the three run at the wrong time. |

### The windows

| group | ET | Berlin (Sept) |
|---|---|---|
| `premarket` | 07:00 – 09:25 | 13:00 – 15:25 |
| `execution` | 09:45 – 15:30 | 15:45 – 21:30 |
| `close` | 16:15 – 22:00 | 22:15 – 04:00 |

Windows are stated in Eastern because the desk is. Berlin is six hours
ahead in September and five for the week in late October, when the US and
EU clocks change on different dates — which is exactly why `auto` reads
the Eastern clock itself instead of trusting a local schedule.

---

## The kill switch

`core.trading_control`. Stops **all** order submission including exits —
deliberately unlike Nora's drawdown breaker, which freezes new risk but
still lets you de-risk. Analysis, reconciliation and reporting carry on
regardless.

| command | what it does |
|---|---|
| `python -m core.trading_control status` | **Is the desk allowed to trade?** Check before any run you care about. |
| `python -m core.trading_control halt --reason "..."` | ⚠️ **Stop every submission**, buys and sells alike. Ada records each blocked order as `halted_by_operator` with its sizing basis, so a halted day still shows what the desk would have done. Reason is mandatory and recorded. |
| `python -m core.trading_control resume --reason "..."` | **Re-enable submission.** Reason mandatory in this direction too. |
| `python -m core.trading_control history --limit 20` | **Every halt and resume ever**, with who and why. The table is append-only, enforced by a database trigger — this history cannot be rewritten. |

---

## Performance vs the index

`core.benchmark`. Absolute P&L is not a performance measure. These answer
the only question that is: did the desk beat the alternative of doing
nothing?

| command | what it does |
|---|---|
| `python -m core.benchmark report` | **Desk vs SPY since inception**, split into selection and cash drag. The default — bare `python -m core.benchmark` does the same. Risk statistics stay withheld below 20 matched sessions. |
| `python -m core.benchmark backfill` | **Pull SPY from the first session with a NAV** to today. Run once after migration 009; safe to re-run. |
| `python -m core.benchmark sync` | **Bring the series up to today only.** Otis does this at each close; this is the manual catch-up. A bar for a session that has not closed yet is dropped rather than stored as a half-formed close. |

The decomposition is exact by construction:
`selection − cash_drag = active return`, where
`selection = r_desk − w·r_bench` and `cash_drag = (1 − w)·r_bench`.
A book sitting in cash shows almost all of its shortfall as drag, not as
bad picking — which is the distinction the number exists to make.

---

## Database

Postgres in Docker, port **5433**. Your code, `db/schema.sql` and the
running database are three separate things, and only the first two are in
git. These reconcile them.

| command | what it does |
|---|---|
| `python -m core.db` | **Connectivity check.** Prints `Database connection OK.` or the failure. |
| `python -m core.check_schema` | **Drift report** — tables and columns in `models.py` that the live database lacks. Names only: it cannot see triggers, types or constraints, so a database missing migration 006's append-only triggers still reports clean. |
| `python -m core.check_schema --emit-sql` | **Print the repair SQL** without running it. |
| `python -m core.check_schema --fix` | ⚠️ **Apply the repair SQL.** Asks first; `--yes` skips the prompt. |
| `docker exec -i mby-trading-db psql -U mby -d mby_trading < db/migrations/009_benchmark_history.sql` | **Apply one migration.** All are safe to re-run. Applied so far: 002 → 009. There is no record table, so *which have run* is answered by inspection. |
| `docker exec -i mby-trading-db psql -U mby -d mby_trading -c "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY tgname;"` | **Are the append-only triggers there?** Expect **four**. The one check `check_schema` cannot do for you — run it on any database you did not build by hand. |
| `docker start mby-trading-db` | **Bring the database up** after a reboot. `docker compose up -d` from the project root also works. |

---

## The scheduler

launchd, macOS. Ticks every 15 minutes and calls `orchestrator auto`,
which decides whether anything is due.

| command | what it does |
|---|---|
| `launchctl print gui/$(id -u)/com.mby.trading.desk` | **Is it loaded, and what did it last do?** State, run interval, last exit code, resolved paths. |
| `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mby.trading.desk.plist` | **Load the agent.** The modern command — gives real errors. |
| `launchctl bootout gui/$(id -u)/com.mby.trading.desk` | **Unload it.** Run this before re-loading after any edit to the plist. |
| `plutil -lint ~/Library/LaunchAgents/com.mby.trading.desk.plist` | **Validate the plist XML** before blaming anything else. |
| `tail -f logs/desk-$(date +%Y-%m).log` | **Watch the scheduler work.** One block per tick, with the exit code. Only the wrapper writes here — running `python -m orchestrator …` by hand prints to your terminal instead. |
| `cat /tmp/mby-desk.launchd.err` | **Failures before the wrapper started** — a bad path, a missing shell. Empty is what you want. |
| `sudo pmset repeat wakeorpoweron MTWRF 22:10:00` | **Wake the Mac for the close run.** Check with `pmset -g sched`. macOS will not fire a scheduled job while asleep, and the close window is 22:15–04:00 Berlin — the run most likely to be slept through. |

> **The trap.** `launchctl load` is the legacy command and fails with
> `Load failed: 5: Input/output error`, which names nothing. Use
> `bootstrap` / `bootout`. And ignore the hint about running as root —
> this is a per-user LaunchAgent, and bootstrapping it into the system
> domain loses your home directory, your venv and Docker.

The project path contains a space and needs no escaping inside
`ProgramArguments` — each array element is already one argument.

A closed laptop is a desk that does not trade. For operation that survives
that, run the same wrapper on an always-on host.

---

## Dashboard

| command | what it does |
|---|---|
| `streamlit run dashboard/app.py` | **Open the Floor** at localhost:8501. |
| `python dashboard/smoke.py populated floor` | **Render one page with no database** and assert it holds together. First argument `empty` or `populated`; second is any view — `floor`, `wall`, `portfolio`, `risk`, `research`, `orders`, `pnl`, `activity`, `room`, or `agent Vera`. |
| `streamlit run dashboard/app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true` | **The hosted form** — what Railway's start command needs. |

---

## Tests

pytest. No database, no network — which is why it runs in about two
seconds and is worth running.

| command | what it does |
|---|---|
| `python -m pytest -q` | **The whole suite.** |
| `python -m pytest -q -k benchmark` | **One area by name.** Also `-k "ada or solomon"`, `-k orchestrator`. |
| `python -m pytest tests/test_benchmark.py -q` | **One file.** Drop `-q` to see each test name. |

---

## Single agents

Each agent has a `__main__` for running it alone. These are development
aids — the real entry point is always the orchestrator.

| command | what it does |
|---|---|
| `python -m agents.otis` | **Close the books now.** Also unblocks Ada when the ledger is too stale to size against. Same pattern for `atlas`, `vera`, `solomon`, `nora`, `marcus`, `clara`. |
| `ADA_SMOKE_TEST_CONFIRM=yes python -m agents.ada` | ⚠️ **Places a real order.** Paper by default, but it reads `ALPACA_PAPER` from `.env` at import. It bypasses the database entirely — this is the path that once created an untracked position with no allocation behind it. The pipeline entry point is `ada.run()` via the orchestrator, never this. |

---

Companion to the Risk & Control Review and the Maintainability Review.
