# MBY-Trading — Operating Manual v1.0

*Modeled on the trading-desk discipline of Goldman Sachs, Morgan Stanley, and JPMorgan — reimagined as a team of specialized AI agents.*

---

## 1. Vision

> Build a disciplined, multi-asset investment operation where specialized AI agents each own one link in the research-to-execution chain — generating differentiated ideas, rigorously sizing and risking them, and executing with institutional-grade process — so that performance comes from process quality and synergy between agents, not from any single model's brilliance.

**Core principle:** process over prediction, synergy over solo genius.

---

## 2. Scope (v1)

| Dimension | Decision |
|---|---|
| Asset class | US large-cap equities only (expand to crypto/FX/futures later) |
| Trading style | Discretionary-style research, longer swing horizon (weeks–months) |
| Capital | Paper trading / simulation first, real capital later as a gate |
| Cadence | All agents run **daily** — daily vigilance, not daily turnover |
| Broker / execution / live price feed | **Alpaca** (paper trading mirrors live API exactly) |
| Research / fundamentals data | **Financial Modeling Prep (FMP)** |

**Guiding rule:** *"Runs daily" and "changes the portfolio daily" are not the same thing.* The portfolio is modified only when strictly necessary — every agent's design encodes this restraint explicitly, not as an assumption.

---

## 3. What Real Trading Desks Taught This Design

A real desk's daily rhythm — pre-market triage → morning meeting → market-open execution → midday research → close/reconciliation — and weekly rhythm (Monday setup, Tue–Thu peak activity, Friday risk/performance review) is not primarily about being busy. Most of the day is information processing; trading itself is a small, disciplined slice of a larger cycle. An idea never goes straight from insight to trade — it always passes through:

**thesis → scrutiny → sizing → risk check → execution → reconciliation → attribution**

That closed loop *is* the trading desk. Our 8-agent system is a direct translation of this loop, with **separation of duties** as the non-negotiable architectural principle: the agent that generates an idea is never the one that approves risk; the one that approves risk is never the one that executes.

---

## 4. Architecture Principle: Code vs. Judgment

A rule that governs every agent's design: **the closer an agent sits to money actually moving, the more its logic should be deterministic code rather than LLM judgment.** LLM reasoning is used only where genuine judgment is required; everywhere a rule is nameable in numbers, it's code.

```
Atlas  ───────────  pure LLM judgment (regime reading has no formula)
Vera   ──────────   judgment-heavy hybrid (data fetch = code, thesis = LLM)
Solomon ─────────   judgment-heavy hybrid, but escalation criteria are checkable
Nora   ───────────  code-heavy hybrid (hard limits = code, cannot be LLM-overridden)
Marcus ───────────  code-heavy hybrid (formula gives starting size, LLM adjusts)
Ada    ───────────  nearly pure code (order mechanics), thin LLM layer for edge cases
Otis   ───────────  nearly pure code (reconciliation is fact-matching)
Clara  ───────────  code for audit/attribution math, LLM for narrative synthesis
```

Two deliberate **non-automation seams** exist in this system, by design:
1. **Nora's hard risk limits cannot be overridden by LLM reasoning** — code enforces the wall; the LLM layer only explains and catches soft/qualitative risk on top of it.
2. **Clara cannot auto-modify any other agent's rubric or parameters** — all calibration recommendations (e.g. "Vera's conviction scores are miscalibrated") go to human review. This prevents a closed self-modifying feedback loop where a losing streak could push the system to badly "correct" itself with no oversight.

**Otis is the single source of truth** for current portfolio state — every other agent (Vera, Nora, Marcus) reads portfolio state from Otis's reconciled ledger, never from a separate raw broker query. This avoids agents working off inconsistent snapshots of the same portfolio.

---

## 5. The Team

| # | Name | Role | Emulates | Decision rights (summary) |
|---|---|---|---|---|
| 1 | **Atlas** (m) | Macro/Market Intelligence | Macro research desk | None — pure information producer |
| 2 | **Vera** (f) | Equity Research | Equity research analyst | None — proposes theses/flags, never sizes or trades |
| 3 | **Solomon** (m) | CIO / Strategy | Head of desk / CIO | Gatekeeper of *attention* — escalates or doesn't; cannot size or approve risk |
| 4 | **Nora** (f) | Risk Manager | Market Risk desk | Hard veto on limit breaches; sets max size ceiling |
| 5 | **Marcus** (m) | Portfolio Manager | PM function | Sets exact size within Nora's ceiling; prioritizes across proposals |
| 6 | **Ada** (f) | Execution | Trading desk execution | Order type/timing only; never what/how much |
| 7 | **Otis** (m) | Operations/Reconciliation | Middle/back office | System of record; flags discrepancies, never resolves by assumption |
| 8 | **Clara** (f) | Performance/Compliance | Performance attribution & compliance | Attribution + process audit; recommends, never auto-changes other agents |

**Run order (daily):** Atlas → Vera → Solomon → (Nora → Marcus → Ada, only if triggered) → Otis → Clara

---

## 6. Daily Operating Cycle

| Phase | When | Agents | Output |
|---|---|---|---|
| 1. Pre-market scan | Before open | Atlas + Vera (parallel) | Macro backdrop brief; position status tags + new candidates |
| 2. Morning-meeting synthesis | Open | Solomon | Daily verdict: action needed (rare) or not (common) |
| 3. Risk & sizing | Only if triggered | Nora → Marcus | Approved/rejected proposals with sizing |
| 4. Execution | Only if approved | Ada | Paper orders placed, fills confirmed |
| 5. Close & reconciliation | End of day | Otis → Clara | Reconciled ledger, P&L, attribution, process audit |

**Weekly roll-up:** Clara aggregates the week's daily logs into a portfolio-level risk/performance review and calibration report (conviction-vs-outcome correlation, escalation precision/recall) — surfaced to the human for review, not auto-applied.

---

## 7. Agent Charters

Each agent is defined by the same nine parameters: **Role & mandate, Inputs, Tools, Decision rights, Output contract, Memory, Guardrails, Model/judgment level, Success metric.**

---

### 7.1 Atlas — Macro/Market Intelligence Agent

**Role & mandate**
Answers one question daily: "What kind of market are we in, and has anything changed since yesterday?" No stock-specific commentary — that discipline is what keeps him useful and prevents scope creep into Vera's job.

**Inputs**
Overnight/pre-market macro news · index futures/overnight moves · today's economic calendar · yesterday's regime signal.

**Tools**
News/web search or news API (FMP news endpoint) · economic calendar source. No trading tools, no portfolio visibility (architecturally incapable of touching positions).

**Decision rights**
None. Pure information producer — cannot use stock-picking language.

**Output contract**
```json
{
  "date": "...",
  "regime_signal": "risk-on | risk-off | neutral | transitioning",
  "key_events_today": [...],
  "notable_overnight_moves": [...],
  "change_from_yesterday": "none | minor | material",
  "confidence": "low | medium | high",
  "narrative": "2-3 sentence summary"
}
```

**Memory**
Only yesterday's `regime_signal` — leanest memory footprint on the desk.

**Guardrails**
Must produce output every trading day (silence stalls the whole chain) · no ticker-specific language · `material` change requires naming the specific driving event.

**Model / judgment level**
Almost entirely LLM judgment; data-fetching is the only deterministic piece.

**Success metric**
Did `material` flags precede real regime shifts without over-flagging on quiet days?

---

### 7.2 Vera — Equity Research Agent

**Role & mandate**
Two jobs: (1) daily monitoring of every held position for thesis drift, (2) screening a fixed large-cap universe for new candidates. Kept separate because defending a prior judgment and forming a new one are different cognitive postures.

**Inputs**
Current holdings + each one's *original thesis* · Atlas's daily brief (context, not directive) · fundamentals/financials/ratios/news/earnings calendar (FMP) · a fixed, pre-filtered investment universe (market-cap/liquidity screened by code before any LLM reasoning).

**Tools**
FMP API (statements, ratios, news, earnings calendar) · price/volume data for context only, not signal generation. No trading tools.

**Decision rights**
None over the portfolio. Highest-stakes output is a classification: `thesis intact / at_risk / broken`. Never uses "buy"/"sell" language.

**Output contract**
```json
// Position monitoring (per held name)
{
  "ticker": "...", "original_thesis_date": "...",
  "status": "intact | at_risk | broken",
  "trigger": "none | earnings | news | fundamental_drift | macro_conflict",
  "reasoning": "...", "conviction_score": "1-5"
}
// New candidates (zero or more per day)
{
  "ticker": "...", "thesis": "...", "catalyst": "...",
  "conviction_score": "1-5", "key_risks": [...], "valuation_snapshot": {...}
}
```

**Memory**
Persistent: each held position's original thesis and conviction score, kept until the position closes — the anchor that makes "drift" measurable.

**Guardrails**
Every claim traces to a specific data point · conviction scored on a fixed written rubric · no "buy/sell" vocabulary · `thesis_broken` must cite the specific thing that broke, not vague sentiment.

**Model / judgment level**
Hybrid: code fetches/filters/screens; LLM interprets what the data means for the thesis.

**Success metric**
Screening quality (did high-conviction ideas outperform low-conviction ones?) and monitoring quality (did `thesis_broken` flags predate bad news or lag it?).

---

### 7.3 Solomon — CIO/Strategy Agent

**Role & mandate**
Synthesizes Atlas + Vera into a daily verdict: does anything clear the bar for action? Most days: no. His value is being **hard to convince**, not a source of ideas — this is the restraint principle encoded as his primary function.

**Inputs**
Atlas's backdrop · Vera's monitoring tags and new candidates · current portfolio composition · his own recent escalation log.

**Tools**
None external — pure reasoning over Atlas's and Vera's already-synthesized outputs.

**Decision rights**
Can escalate proposals to Nora/Marcus and rank multiple candidates. Cannot size, approve against risk limits, or execute — gatekeeper of *attention*, not capital.

**Output contract**
```json
{
  "date": "...", "action_needed": "true | false",
  "proposals": [
    {"ticker": "...", "action": "exit | trim | add | new_position",
     "linked_trigger": "vera:thesis_broken | vera:new_candidate | atlas:regime_change",
     "rationale": "...", "urgency": "same_day | this_week"}
  ],
  "watching": ["tickers noted but not escalated, with why"],
  "narrative": "..."
}
```

**Memory**
Rolling log of recent escalation decisions — a self-monitoring signal (escalating every day suggests the bar is set too low).

**Guardrails**
`exit`/`trim` requires `at_risk` *with a new deteriorating trigger* or `broken` — a static "at_risk" alone is not sufficient · `new_position` requires conviction ≥ threshold *and* a named catalyst · macro alone (Atlas), without a company-specific trigger from Vera, should not drive action on a specific stock · must always produce a narrative, even on no-action days.

**Model / judgment level**
Mostly LLM reasoning, but guardrails are checkable conditions that can be validated in code as a second layer.

**Success metric**
Precision and recall of escalations — false alarms wasting Nora/Marcus's attention vs. missed calls that should have been escalated.

---

### 7.4 Nora — Risk Manager Agent

**Role & mandate**
Gatekeeper of capital-at-risk. Checks every proposal *and* the existing portfolio daily against a fixed, written risk policy. Decides what's allowed and the maximum size allowed — never the exact size.

**Inputs**
Solomon's proposals · full current portfolio state (from Otis) · the firm's written risk policy (static config) · volatility/price history.

**Tools**
Portfolio state query (Otis) · price/volatility data (Alpaca/FMP) · deterministic risk-calculation functions (position/sector/drawdown/correlation checks) — **code, not prompts.**

**Decision rights**
Hard veto on limit breaches (non-negotiable, code-enforced) · sets max allowable size ceiling for approved proposals · can raise a **portfolio-wide circuit breaker** freezing new positions on drawdown breach · monitors existing positions daily even absent new proposals (price moves alone can breach limits). Cannot originate ideas, set exact size, or execute.

**Output contract**
```json
{
  "date": "...", "portfolio_status": "within_limits | breach_warning | breach_hard",
  "existing_breaches": [{"ticker": "...", "rule_violated": "...", "current_value": "...", "limit": "..."}],
  "proposal_reviews": [
    {"ticker": "...", "action": "...", "decision": "approved | rejected",
     "max_size_pct": "...", "rules_checked": [...], "reasoning": "..."}
  ],
  "circuit_breaker_active": "true | false"
}
```

**Memory**
The risk policy itself (static, versioned) · a trend log of how close positions run to their limits (to catch "trending toward breach" early).

**Starting risk policy (strawman — tune as needed)**
- Max single position: 8% of portfolio
- Max sector weight: 25%
- Portfolio drawdown circuit breaker: −10% from peak (freezes new positions only)
- Minimum position count once fully deployed: 12–15 names

**Guardrails**
Hard limits are code, evaluated before any LLM reasoning runs · LLM layer can only *tighten* (flag soft risk) never *loosen* (override a hard breach) · every rejection cites the specific rule and numbers.

**Model / judgment level**
The cleanest hybrid case: hard limits = pure deterministic code; explaining reasoning and catching non-obvious correlation risk = thin LLM layer.

**Success metric**
Hard-limit breaches slipping through should be ~zero (by construction) · precision on soft/qualitative calls (real risk caught vs. false alarms).

---

### 7.5 Marcus — Portfolio Manager Agent

**Role & mandate**
Where Nora sets the ceiling, Marcus decides what a proposal actually deserves. Owns **portfolio construction** — turning individually-approved ideas into a coherent, well-proportioned book, especially when multiple proposals compete for limited cash the same day.

**Inputs**
Nora's approved proposals + ceilings · Vera's conviction scores and Solomon's rationale/urgency · current portfolio state and cash (from Otis) · his own sizing rubric.

**Tools**
Portfolio state query (Otis) · a conviction-tiered sizing formula (deterministic starting point).

**Decision rights**
Exact position size within Nora's ceiling (never above it) · prioritization across same-day competing proposals · trim sizing for at-risk positions. Cannot exceed Nora's ceiling, approve a rejected proposal, or execute.

**Output contract**
```json
{
  "date": "...",
  "allocations": [
    {"ticker": "...", "action": "buy | trim | exit", "target_size_pct": "...",
     "conviction_input": "...", "rationale": "...", "priority": 1}
  ],
  "cash_reserve_pct": "...", "narrative": "..."
}
```

**Memory**
A stable conviction-to-size rubric (consistency over time) · live portfolio weights sourced fresh from Otis.

**Guardrails**
Sizing follows a fixed conviction-to-size tier (e.g. conviction 5 → up to ceiling, 4 → ~70%, 3 → starter/none) · minimum cash reserve (5–10%) never fully deployed · can never *increase* size on a Vera `at_risk` position, even with idle cash · competing proposals ranked explicitly by conviction.

**Model / judgment level**
Deterministic formula gives a starting size; LLM adjusts for correlation with existing holdings and cash-constrained prioritization across competing ideas.

**Success metric**
Did size correlate with conviction actually working out over time? (Tests whether Vera's and Marcus's rubrics are well-calibrated together.)

---

### 7.6 Ada — Execution Agent

**Role & mandate**
Narrowest mandate on the desk by design: not *what* or *how much*, only **how to execute well**. The closer to money moving, the less discretion — Ada sits right before capital actually moves.

**Inputs**
Marcus's finalized allocations · live price/quote data (Alpaca) · current account state (Alpaca).

**Tools**
Alpaca API: place/check/cancel/replace orders, query account/positions.

**Decision rights**
Order type (market/limit) and limit price · timing (e.g. avoiding volatile open/close minutes, splitting orders) · partial-fill handling. Cannot change what/how much to trade (beyond share rounding); cannot simply refuse an approved order — but can pause and escalate on a staleness trigger.

**Output contract**
```json
{
  "date": "...",
  "orders": [
    {"ticker": "...", "action": "...", "target_size_pct": "...",
     "order_type": "market | limit", "limit_price": "...", "shares": "...",
     "status": "filled | partial | pending | rejected",
     "fill_price": "...", "slippage_bps": "..."}
  ],
  "narrative": "..."
}
```

**Memory**
Open/pending orders through the day (avoids double-submission) · rolling slippage history (informs order-type strategy over time).

**Guardrails**
**Staleness check**: if price moved materially since Marcus's decision, pause and escalate rather than execute blindly · defaults to limit orders for price control · never exceeds approved size (rounding only) · idempotent — one execution per instruction · halts/rejections/illiquidity are logged and flagged loudly, never silently dropped.

**Model / judgment level**
The most code-heavy agent on the desk; thin LLM layer only for staleness materiality judgment and order-type strategy in ambiguous cases.

**Success metric**
Slippage vs. decision-time price · fill rate · correct staleness-catch rate.

---

### 7.7 Otis — Operations/Reconciliation Agent

**Role & mandate**
The **single source of truth** for "what do we currently hold." Every agent needing portfolio state reads from Otis's reconciled ledger — not from independent raw broker queries — to avoid inconsistent snapshots across the system.

**Inputs**
Ada's order outputs (submitted/filled/price) · Alpaca account state (ground truth) · Marcus's *intended* allocations (to compare intention vs. outcome) · yesterday's reconciled ledger.

**Tools**
Alpaca API (read-only) · write access to an internal ledger database (Postgres).

**Decision rights**
Authoritative record of positions, cost basis, cash, P&L · flags discrepancies between intended and actual · computes daily realized/unrealized P&L. Cannot trade or adjudicate a discrepancy by assumption — records and flags only.

**Output contract**
```json
{
  "date": "...", "reconciled": "true | false",
  "positions": [{"ticker": "...", "shares": "...", "avg_cost": "...",
                 "market_value": "...", "unrealized_pnl": "...", "weight_pct": "..."}],
  "cash_balance": "...", "realized_pnl_today": "...",
  "discrepancies": [{"ticker": "...", "expected": "...", "actual": "...", "description": "..."}],
  "narrative": "..."
}
```

**Memory**
His memory *is* the ledger — the complete, permanent transaction history (different in kind from every other agent's lighter memory).

**Guardrails**
Never silently resolves a discrepancy — flags for human/Solomon review · ledger is append-only (corrections added as new entries, never rewritten) · Alpaca is ground truth for what happened; Otis compares that against what was supposed to happen.

**Model / judgment level**
Mostly deterministic (dataset matching); thin LLM layer for narrative and triage of likely cause (delayed settlement vs. genuine error).

**Success metric**
Reconciliation accuracy (ideally zero unresolved discrepancies) and speed of catching mismatches.

---

### 7.8 Clara — Performance/Compliance Agent

**Role & mandate**
Looks at the *whole chain*, not one link. Two jobs: (1) performance attribution — why results happened, tied to original theses; (2) process compliance — did every trade follow the full approval chain (Vera → Solomon → Nora → Marcus → Ada → Otis)? A profitable trade that skipped the chain is still a failure — process is checked independent of outcome.

**Inputs**
Otis's ledger/P&L · Vera's original theses and conviction scores · Solomon's escalation decisions/triggers · Nora's approvals/rejections · Marcus's sizing · Ada's execution/slippage — the full daily audit trail.

**Tools**
Read access to the other agents' logged outputs. No external market APIs needed.

**Decision rights**
Authoritative performance attribution · flags process violations (hard finding, as serious as a risk breach, regardless of profitability) · produces calibration recommendations. **Cannot modify any other agent's rubric or parameters** — all recommendations go to human review, preventing a closed self-modifying feedback loop.

**Output contract**
```json
// Daily
{
  "date": "...", "daily_pnl": {"realized": "...", "unrealized": "...", "total": "..."},
  "attribution": [{"ticker": "...", "contribution_pct": "...", "linked_thesis": "...", "thesis_status": "..."}],
  "process_check": "clean | violation_found", "violations": [...], "narrative": "..."
}
// Weekly roll-up
{
  "week_of": "...", "portfolio_return": "...", "win_rate": "...",
  "vera_calibration": {"conviction_vs_outcome_correlation": "..."},
  "solomon_calibration": {"escalation_precision": "...", "escalation_recall": "..."},
  "risk_events": [...], "recommendations": ["surfaced to human, never auto-applied"]
}
```

**Memory**
Deepest analytical history besides Otis's ledger — the full decision log across time, needed for rolling calibration stats.

**Guardrails**
Never auto-modifies another agent · violations flagged regardless of profitability · attribution always ties to a specific `linked_thesis`/trigger, never "the market moved."

**Model / judgment level**
Audit/attribution math is close to pure code; LLM layer handles narrative synthesis and framing calibration findings for human decision-making.

**Success metric**
Near-100% reliability catching process violations · whether acting on her calibration recommendations improves subsequent performance (measurable only with real run history).

**Expanded responsibility — Daily Closing Report**
Clara also compiles the end-of-day **Daily Closing Report**, aggregating every agent's already-produced structured output and `narrative` field into one readable document:

```
Daily Report — [date]

BACKDROP (Atlas): [narrative + regime_signal]
RESEARCH (Vera): [X positions monitored, Y flagged at_risk/broken, Z new candidates]
STRATEGY (Solomon): [action_needed: y/n, proposals made, narrative]
RISK (Nora): [portfolio_status, any breaches, circuit_breaker state]
SIZING (Marcus): [allocations made, if any]
EXECUTION (Ada): [orders placed, fill quality/slippage]
OPERATIONS (Otis): [reconciled: y/n, P&L, any discrepancies]
PERFORMANCE (Clara): [daily P&L, attribution, process_check result]

EXECUTIVE SUMMARY: [2-4 sentence synthesis of the day]
```

This report requires almost no new judgment — every agent's output contract already includes a `narrative` field for exactly this purpose, so compilation is largely deterministic formatting. The one place a light LLM pass earns its keep is the executive summary, tying the day together in plain language. This is a natural extension of Clara's existing role (she already runs last and already touches every other agent's data), not a new agent.

---

## 8. Live Dashboard (design open — not yet finalized)

Distinct from the Daily Closing Report: not a point-in-time summary, but a **live view of what the system is doing right now**. This is a UI/build decision, not a new agent — it's a window onto data every agent already produces plus Otis's live ledger. No agent's mandate changes because of it.

**Suggested minimum data the dashboard should surface**, regardless of final design:
- Which phase of the daily cycle is currently active (1–5, per Section 6)
- Each agent's status (idle / running / last completed) with a one-line summary of its latest output
- **Live portfolio snapshot, sourced from Otis** — positions, weights, cash, unrealized P&L — reinforcing his role as single source of truth rather than a separate live query
- Active alerts surfaced prominently: Nora's circuit-breaker state, Clara's process violations, Solomon's watchlist

**A few directions worth considering when the design is ready (not decided yet):**
- *Simple read-only web page* — lightweight, just renders the shared data store; fastest to build, least interactive
- *Operational dashboard with drill-down* — click into any agent to see its full recent output history, not just the latest snapshot; more useful once agents have real run history to review
- *Prototype-first artifact* — a mocked-up interactive view built before the agents are actually wired up for real, useful purely to visualize "what does the team look like in action" and sanity-check the information architecture before investing in the real build

This section will be filled in once the dashboard design is finalized.

---

## 9. Open Items for Next Phase

- Finalize numeric risk policy values with Nora (position/sector/drawdown limits above are a starting strawman)
- Define the conviction-to-size tier formula precisely for Marcus
- Decide build/test sequence — which agent to stand up and validate first
- Define the shared data store/schema each agent reads/writes to (the "audit trail" Clara depends on)
