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


def load(query: str) -> pd.DataFrame:
    return pd.read_sql(query, engine)


tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(
    ["Daily Closing Report", "Today's Briefing", "Agent Activity Log",
     "Research (Vera)", "Risk, Strategy & Compliance", "Portfolio"]
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
        report = load(f"SELECT * FROM daily_reports WHERE report_date = '{selected_date}'")
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
    st.caption(
        "Chronological record of what each agent produced, by day. "
        "A unified agent_runs audit trail (start/end time, status, cost) "
        "is a planned upgrade — see the note at the top of this file."
    )
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
# TAB 6 — Portfolio (Otis): real reconciled state, no more placeholder.
# =================================================================
with tab6:
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
    pnl = load("SELECT pnl_date, total_pnl, realized_pnl, unrealized_pnl, cash_balance FROM daily_pnl ORDER BY pnl_date")
    if pnl.empty:
        st.info("No portfolio history yet — Otis hasn't run.")
    else:
        st.line_chart(pnl.set_index("pnl_date")["total_pnl"])
        st.dataframe(pnl, use_container_width=True)

    st.subheader("Current Positions")
    positions = load(
        "SELECT ticker, shares, avg_cost, market_value, unrealized_pnl, weight_pct, last_updated FROM positions"
    )
    if positions.empty:
        st.info("No open positions currently held.")
    else:
        st.dataframe(positions, use_container_width=True)