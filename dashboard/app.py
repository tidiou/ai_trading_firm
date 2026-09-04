"""
MBY-Trading — CEO Dashboard

Streamlit app reading directly from the shared Postgres database —
the SAME single source of truth every agent reads and writes to
(via core.db.engine). Pure read-only view; never writes anything.

Run with (from the project root):
    streamlit run dashboard/app.py

v2 — now that Otis and Clara exist, this pulls in reconciled
portfolio state, discrepancies, process compliance, and the actual
Daily Closing Report, none of which existed when this was first built.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

from core.db import engine

st.set_page_config(page_title="MBY-Trading", layout="wide")
st.title("MBY-Trading — Daily Operations")

# Halt state, above the tabs. A kill switch nobody can see the state of
# is a kill switch you find out about by wondering why nothing traded.
_control = pd.read_sql(
    "SELECT trading_enabled, changed_by, reason, changed_at "
    "FROM trading_control ORDER BY id DESC LIMIT 1",
    engine,
)
if not _control.empty and not bool(_control.iloc[0]["trading_enabled"]):
    _row = _control.iloc[0]
    st.error(
        f"**TRADING HALTED** by {_row['changed_by']} — \"{_row['reason']}\"  \n"
        f"Set {_row['changed_at']}. Ada will submit nothing, buys or sells, until resumed. "
        "Analysis and reconciliation continue as normal."
    )


def load(query: str, params: tuple | None = None) -> pd.DataFrame:
    return pd.read_sql(query, engine, params=params)


tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs(
    ["Daily Closing Report", "Today's Briefing", "Agent Activity Log",
     "Research (Vera)", "Risk, Strategy & Compliance", "Execution (Ada)", "Portfolio"]
)

# =================================================================
# TAB 1 — Daily Closing Report (Clara): the actual CEO-facing
# document, pulled straight from daily_reports. This is the feature
# requested early in this whole project and only became buildable
# once every other agent existed.
# =================================================================
with tab1:
    st.header("Daily Closing Report")
    available_dates = load("SELECT report_date FROM daily_reports ORDER BY report_date DESC")
    if available_dates.empty:
        st.info("No closing report yet — Clara hasn't compiled one. Run orchestrator.py or agents.clara.")
    else:
        selected_date = st.selectbox(
            "Report date", available_dates["report_date"], format_func=lambda d: d.strftime("%Y-%m-%d")
        )
        report = load("SELECT * FROM daily_reports WHERE report_date = %s", (selected_date,))
        st.markdown(report.iloc[0]["full_report_md"])

# =================================================================
# TAB 2 — Today's Briefing: quick-glance snapshot across all agents.
# =================================================================
with tab2:
    st.header("Today's Briefing")
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
        "FROM new_candidates ORDER BY candidate_date DESC, conviction_score DESC LIMIT 10"
    )
    if candidates.empty:
        st.info("No candidates surfaced yet.")
    else:
        st.dataframe(candidates, use_container_width=True)

    col3, col4 = st.columns(2)
    with col3:
        st.subheader("Risk Status (Nora)")
        risk = load("SELECT * FROM risk_reviews ORDER BY review_date DESC LIMIT 1")
        if not risk.empty:
            row = risk.iloc[0]
            status_icon = {"within_limits": "🟢", "breach_warning": "🟡", "breach_hard": "🔴"}.get(
                row["portfolio_status"], "⚪"
            )
            st.metric("Portfolio status", f"{status_icon} {row['portfolio_status']}")
            st.metric("Circuit breaker", "ACTIVE ⚠️" if row["circuit_breaker_active"] else "Inactive")
        else:
            st.info("Nora hasn't run yet.")

    with col4:
        st.subheader("Reconciliation (Otis)")
        pnl = load("SELECT * FROM daily_pnl ORDER BY pnl_date DESC LIMIT 1")
        if not pnl.empty:
            row = pnl.iloc[0]
            st.metric("Reconciled?", "✅ Yes" if row["reconciled"] else "⚠️ Discrepancies found")
            st.metric("Today's total P&L", f"${row['total_pnl']:.2f}")
        else:
            st.info("Otis hasn't run yet.")

# =================================================================
# TAB 3 — Agent Activity Log: unified timeline across ALL agents,
# now including Otis and Clara.
# =================================================================
with tab3:
    st.header("Agent Activity Log")

    st.subheader("Run ledger")
    st.caption(
        "Every agent invocation: when it started, whether it completed, and the "
        "traceback if it didn't. Nora appears twice a day under nora_review "
        "(Phase 3, only when Solomon escalates) and nora_monitor (Phase 5, always)."
    )
    runs = load("""
        SELECT run_date, phase, agent_name, status,
               started_at, completed_at, error_message
        FROM agent_runs
        ORDER BY run_date DESC, phase, agent_name
        LIMIT 200
    """)
    if runs.empty:
        st.info("No runs recorded yet — the ledger fills from the next cycle.")
    else:
        failed = runs[runs["status"] == "failed"]
        if not failed.empty:
            st.error(f"{len(failed)} failed run(s) on record — see error_message below.")
        running = runs[runs["status"] == "running"]
        if not running.empty:
            st.warning(
                f"{len(running)} run(s) still marked 'running' — an interrupted cycle "
                "leaves its last agent in this state."
            )
        st.dataframe(runs, use_container_width=True, height=320)

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
        ORDER BY activity_date DESC
    """)
    st.dataframe(activity, use_container_width=True, height=500)

# =================================================================
# TAB 4 — Research (Vera)
# =================================================================
with tab4:
    st.header("Research — Vera")

    st.subheader("Open Theses")
    theses = load(
        "SELECT ticker, opened_date, original_conviction, thesis_text "
        "FROM theses WHERE closed_date IS NULL"
    )
    if theses.empty:
        st.info("No open positions yet — nothing bought so far.")
    else:
        st.dataframe(theses, use_container_width=True)

    st.subheader("Candidate History")
    all_candidates = load(
        "SELECT candidate_date, ticker, conviction_score, catalyst, thesis "
        "FROM new_candidates ORDER BY candidate_date DESC"
    )
    st.dataframe(all_candidates, use_container_width=True)

    st.subheader("Position Monitoring History")
    monitoring = load(
        "SELECT log_date, ticker, status, trigger, reasoning "
        "FROM position_monitoring_log ORDER BY log_date DESC"
    )
    if monitoring.empty:
        st.info("Nothing to monitor yet — portfolio is empty.")
    else:
        st.dataframe(monitoring, use_container_width=True)

# =================================================================
# TAB 5 — Risk, Strategy & Compliance (Solomon + Nora + Clara)
# =================================================================
with tab5:
    st.header("Risk, Strategy & Compliance")

    st.subheader("Strategy Decisions (Solomon)")
    decisions = load(
        "SELECT decision_date, action_needed, narrative FROM strategy_decisions ORDER BY decision_date DESC"
    )
    st.dataframe(decisions, use_container_width=True)

    st.subheader("Proposals")
    proposals = load("""
        SELECT sd.decision_date, p.ticker, p.action, p.linked_trigger, p.urgency
        FROM proposals p JOIN strategy_decisions sd ON p.strategy_decision_id = sd.id
        ORDER BY sd.decision_date DESC
    """)
    if proposals.empty:
        st.info("No proposals have been escalated yet.")
    else:
        st.dataframe(proposals, use_container_width=True)

    st.subheader("Risk Reviews (Nora)")
    risk_hist = load(
        "SELECT review_date, portfolio_status, circuit_breaker_active "
        "FROM risk_reviews ORDER BY review_date DESC"
    )
    st.dataframe(risk_hist, use_container_width=True)

    st.subheader("Trading Control (kill switch)")
    st.caption(
        "Manual halt/resume history. Distinct from Nora's drawdown circuit breaker: "
        "that one is automatic and still permits de-risking, this one is operator-set "
        "and stops everything including exits."
    )
    control_hist = load(
        "SELECT changed_at, trading_enabled, changed_by, reason "
        "FROM trading_control ORDER BY id DESC LIMIT 25"
    )
    if control_hist.empty:
        st.info("No control rows on record — trading enabled by bootstrap default.")
    else:
        st.dataframe(control_hist, use_container_width=True)

    st.divider()
    st.subheader("Risk Limit Breaches (Nora)")
    st.caption(
        "Every limit in the active risk policy, checked daily against the book — "
        "not only on days a trade was proposed. `min_position_count` is warn-only: "
        "it is recorded and shown, but never blocks a trade."
    )
    breaches = load("""
        SELECT rr.review_date, rb.rule_violated, rb.ticker,
               rb.current_value, rb.limit_value, rb.source
        FROM risk_breaches rb JOIN risk_reviews rr ON rb.risk_review_id = rr.id
        ORDER BY rr.review_date DESC, rb.rule_violated
    """)
    if breaches.empty:
        st.success("No limit breaches on record.")
    else:
        st.dataframe(breaches, use_container_width=True)

    st.subheader("Proposal Reviews (Nora's approve/reject decisions)")
    prop_reviews = load("""
        SELECT p.ticker, pr.decision, pr.max_size_pct, pr.reasoning
        FROM proposal_reviews pr JOIN proposals p ON pr.proposal_id = p.id
        ORDER BY pr.id DESC
    """)
    if prop_reviews.empty:
        st.info("No proposals have reached Nora's review yet.")
    else:
        st.dataframe(prop_reviews, use_container_width=True)

    st.divider()
    st.subheader("Process Compliance (Clara)")
    st.caption("A violation is flagged regardless of profitability — process is checked independent of outcome.")
    process = load("SELECT check_date, process_check, violations FROM process_checks ORDER BY check_date DESC")
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
    attribution = load(
        "SELECT attribution_date, ticker, contribution_pct, thesis_status "
        "FROM attribution ORDER BY attribution_date DESC"
    )
    if attribution.empty:
        st.info("No attribution yet — nothing held/traded to attribute.")
    else:
        st.dataframe(attribution, use_container_width=True)

# =================================================================
# TAB 6 — Execution (Ada)
#
# The layer where money actually moves, and the one the dashboard had
# no view of at all: orders, fills, slippage and the audit chain were
# only ever readable through psql.
#
# It sits between Risk and Portfolio because that is the cycle order —
# what was decided, what was attempted, what resulted.
# =================================================================
with tab6:
    st.header("Execution — Ada")

    # ---------------------------------------------------------
    # Today first. A desk's most common outcome is NOT trading, so the
    # headline is deliberately a count of outcomes rather than a P&L
    # figure — "did anything reach the broker today, and if not why"
    # is the question this tab exists to answer.
    # ---------------------------------------------------------
    today_orders = load("""
        SELECT ticker, action, shares, limit_price, status,
               fill_price, slippage_bps, allocation_id, alpaca_order_id
        FROM orders WHERE order_date = CURRENT_DATE
        ORDER BY ticker
    """)

    if today_orders.empty:
        st.info(
            "No orders today. On most days that is correct — the desk runs daily "
            "but changes the portfolio only when Solomon escalates."
        )
    else:
        # Three outcome groups, because the raw status vocabulary is
        # wide (fifteen values) and the distinction that matters when
        # you glance at this is much simpler.
        reached_broker = today_orders["alpaca_order_id"].notna()
        filled = today_orders["status"] == "filled"
        working = reached_broker & ~filled & (today_orders["status"] != "expired_unfilled")

        c1, c2, c3 = st.columns(3)
        c1.metric("Filled", int(filled.sum()))
        c2.metric("Working at broker", int(working.sum()))
        c3.metric("Never submitted", int((~reached_broker).sum()))

        st.dataframe(today_orders, use_container_width=True)

        not_submitted = today_orders[~reached_broker]
        if not not_submitted.empty:
            st.caption(
                "Orders that never reached the broker are recorded rather than "
                "dropped — the status is the reason. A halted day, a stale ledger "
                "or a limit breach all leave a row saying so."
            )

    st.divider()

    # ---------------------------------------------------------
    # The audit chain. This is the join Clara's compliance check walks,
    # made visible — and it only became possible once Order.allocation_id
    # was actually populated (E9). Before that it had to be reconstructed
    # from ticker and date, which cannot distinguish two attempts on the
    # same name in one day.
    # ---------------------------------------------------------
    st.subheader("Decision → order → ledger")
    st.caption(
        "Where an intention stopped. A row with an allocation and no order means "
        "nothing was attempted; an order with no transaction means nothing filled. "
        "This is the chain Clara audits for process compliance."
    )
    chain = load("""
        SELECT a.allocation_date, a.id AS allocation, a.ticker, a.action,
               a.target_size_pct AS target_pct,
               o.status AS order_status, o.shares, o.fill_price,
               t.id AS txn, t.realized_pnl
        FROM allocations a
        LEFT JOIN orders o ON o.allocation_id = a.id
        LEFT JOIN transactions t ON t.alpaca_transaction_id = o.alpaca_order_id
        ORDER BY a.allocation_date DESC, a.ticker
        LIMIT 100
    """)
    if chain.empty:
        st.info("No allocations on record yet — Marcus has not sized anything.")
    else:
        st.dataframe(chain, use_container_width=True)

    st.divider()

    # ---------------------------------------------------------
    # D3's output. An order that lapses silently is indistinguishable
    # from one that was never placed; these rows are the difference.
    # ---------------------------------------------------------
    st.subheader("Cancelled unfilled")
    st.caption(
        "Every order is a DAY limit, so an unfilled one would lapse overnight on "
        "its own. Otis cancels them explicitly at the close instead, so the "
        "attempt leaves a record. Not errors — the market did not reach the price."
    )
    expired = load("""
        SELECT order_date, ticker, action, shares, limit_price, allocation_id
        FROM orders WHERE status = 'expired_unfilled'
        ORDER BY order_date DESC LIMIT 50
    """)
    if expired.empty:
        st.success("Nothing has expired unfilled.")
    else:
        st.dataframe(expired, use_container_width=True)

    st.divider()

    # ---------------------------------------------------------
    # D9, partially: slippage has been computed carefully since the
    # beginning and read by nothing. Surfacing it closes the
    # measurement loop; feeding it back into sizing is a separate job
    # and belongs to Marcus, not here.
    # ---------------------------------------------------------
    st.subheader("Execution quality")
    st.caption(
        "Slippage in basis points against the limit price — Ada's success metric "
        "(Operating Manual §7.6). Positive means the fill came in worse than the "
        "limit for a buy. Measured here; not yet fed back into sizing."
    )
    slippage = load("""
        SELECT ticker,
               count(*)                     AS fills,
               round(avg(slippage_bps), 2)  AS mean_bps,
               round(max(slippage_bps), 2)  AS worst_bps
        FROM orders
        WHERE slippage_bps IS NOT NULL
        GROUP BY ticker ORDER BY mean_bps DESC NULLS LAST
    """)
    if slippage.empty:
        st.info("No fills with a recorded limit price yet — nothing to measure.")
    else:
        st.dataframe(slippage, use_container_width=True)

    st.divider()

    # ---------------------------------------------------------
    # "Why doesn't this thing ever trade?" is a fair question about a
    # desk built to act rarely, and the status vocabulary answers it
    # precisely. Worth having in one place rather than inferring it.
    # ---------------------------------------------------------
    st.subheader("Order outcomes, all time")
    outcomes = load("""
        SELECT status, count(*) AS orders,
               min(order_date) AS first_seen, max(order_date) AS last_seen
        FROM orders GROUP BY status ORDER BY count(*) DESC
    """)
    if outcomes.empty:
        st.info("No orders on record yet.")
    else:
        st.dataframe(outcomes, use_container_width=True)
        st.caption(
            "`halted_by_operator` is the kill switch; `halted_circuit_breaker` is "
            "Nora's drawdown freeze; `rejected_zero_shares` means the target was "
            "smaller than one share; `rejected_post_trade_limit` and "
            "`rejected_live_drift_limit` are the position cap refusing a trade "
            "that would breach it."
        )


# =================================================================
# TAB 7 — Portfolio (Otis): real reconciled state, no more placeholder.
# =================================================================
with tab7:
    st.header("Portfolio")

    st.subheader("Open Discrepancies (Otis)")
    st.caption("Never auto-resolved — flagged here for human review.")
    discrepancies = load(
        "SELECT found_date, ticker, expected, actual, description, resolved "
        "FROM discrepancies ORDER BY found_date DESC"
    )
    unresolved = discrepancies[~discrepancies["resolved"]] if not discrepancies.empty else discrepancies
    if unresolved.empty:
        st.success("No unresolved discrepancies.")
    else:
        st.warning(f"{len(unresolved)} unresolved discrepancy(ies):")
        st.dataframe(unresolved, use_container_width=True)

    st.subheader("P&L Over Time")
    pnl = load(
        "SELECT pnl_date, nav, total_pnl, realized_pnl, unrealized_pnl, "
        "open_unrealized_pnl, cash_balance, "
        "realized_pnl + unrealized_pnl - total_pnl AS reconciliation_gap "
        "FROM daily_pnl ORDER BY pnl_date"
    )
    if pnl.empty:
        st.info("No portfolio history yet — Otis hasn't run.")
    else:
        # NAV, not total_pnl — this is the series the drawdown circuit
        # breaker actually runs on, so it is the one worth watching.
        if pnl["nav"].notna().any():
            st.caption("Account equity (NAV) — the series the drawdown breaker measures.")
            st.line_chart(pnl.dropna(subset=["nav"]).set_index("pnl_date")["nav"])
        else:
            st.info("No NAV recorded yet — the breaker stays inactive until Otis has closed two sessions.")
        st.caption(
            "realized_pnl + unrealized_pnl = total_pnl, all three daily flows. "
            "open_unrealized_pnl is the lifetime open gain — a stock, not a flow, "
            "which is why it sits apart. reconciliation_gap should be 0.00 on every "
            "row written from 2 Sept 2026 onward; earlier rows predate the fix and "
            "cannot be recomputed."
        )
        st.dataframe(pnl, use_container_width=True)

    st.subheader("Current Positions")
    positions = load(
        "SELECT ticker, sector, shares, avg_cost, market_value, unrealized_pnl, "
        "weight_pct, last_updated FROM positions ORDER BY weight_pct DESC NULLS LAST"
    )
    if positions.empty:
        st.info("No open positions currently held.")
    else:
        st.dataframe(positions, use_container_width=True)