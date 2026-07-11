"""
AI DBRE — Streamlit Dashboard

Run: streamlit run ui/app.py

Main page: Health Overview with real-time metrics.
"""

import sys
import os
import json
from pathlib import Path

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st
from agent.health_check import run_health_check, HealthCheckConfig
from agent.db import execute_query

st.set_page_config(
    page_title="AI DBRE — Dashboard",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# =============================================================================
# Sidebar
# =============================================================================

st.sidebar.title("🛡️ AI DBRE")
st.sidebar.caption("AI Database Reliability Engineer")
st.sidebar.divider()

st.sidebar.markdown("""
**Pages** *(use sidebar navigation)*
- 🏥 Health Overview *(this page)*
- 🔍 Query Explorer
- 📋 Remediation Log
- 🤖 Agent Chat
""")

st.sidebar.divider()
if st.sidebar.button("🔄 Refresh Data"):
    st.cache_data.clear()
    st.rerun()

# =============================================================================
# Health Check
# =============================================================================

st.title("🏥 Health Overview")
st.caption("Real-time database health metrics")


@st.cache_data(ttl=30)
def get_health_report():
    return run_health_check()


@st.cache_data(ttl=30)
def get_database_summary():
    query = """
    SELECT
        count(*) AS table_count,
        sum(n_live_tup) AS total_live_rows,
        sum(n_dead_tup) AS total_dead_rows,
        pg_size_pretty(sum(pg_total_relation_size(relid))) AS total_size
    FROM pg_stat_user_tables
    WHERE schemaname = 'public'
    """
    return execute_query(query)[0]


@st.cache_data(ttl=30)
def get_table_health():
    query = """
    SELECT
        s.relname                                              AS table_name,
        s.n_live_tup                                           AS live_rows,
        s.n_dead_tup                                           AS dead_rows,
        CASE
            WHEN s.n_live_tup + s.n_dead_tup > 0
            THEN round(100.0 * s.n_dead_tup / (s.n_live_tup + s.n_dead_tup), 2)
            ELSE 0
        END                                                    AS dead_pct,
        s.seq_scan,
        COALESCE(s.idx_scan, 0)                                AS idx_scan,
        CASE
            WHEN s.seq_scan + COALESCE(s.idx_scan, 0) > 0
            THEN round(100.0 * COALESCE(s.idx_scan, 0) / (s.seq_scan + COALESCE(s.idx_scan, 0)), 2)
            ELSE 0
        END                                                    AS index_usage_pct,
        CASE
            WHEN heap_blks_hit + heap_blks_read > 0
            THEN round(100.0 * heap_blks_hit / (heap_blks_hit + heap_blks_read), 2)
            ELSE 100
        END                                                    AS cache_hit_pct,
        GREATEST(s.last_vacuum, s.last_autovacuum)             AS last_vacuum,
        pg_size_pretty(pg_total_relation_size(s.relid))        AS total_size,
        age(c.relfrozenxid)                                    AS xid_age
    FROM pg_stat_user_tables s
    JOIN pg_statio_user_tables io ON s.relid = io.relid
    JOIN pg_class c ON c.oid = s.relid
    WHERE s.schemaname = 'public'
    ORDER BY s.n_dead_tup DESC
    """
    return execute_query(query)


# --- Top-level metrics ---
try:
    report = get_health_report()
    db_summary = get_database_summary()

    # Status banner
    status = report["overall_status"]
    status_colors = {"OK": "green", "WARNING": "orange", "CRITICAL": "red"}
    status_icons = {"OK": "✅", "WARNING": "⚠️", "CRITICAL": "🔴"}

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        color = status_colors.get(status, "gray")
        st.markdown(
            f"""<div style="padding: 1rem; border-radius: 0.5rem; border: 2px solid {color}; text-align: center;">
            <h2 style="margin:0;">{status_icons.get(status, '❓')} {status}</h2>
            <p style="margin:0; opacity:0.7;">Overall Health</p>
            </div>""",
            unsafe_allow_html=True,
        )

    with col2:
        st.metric("Tables", db_summary["table_count"])

    with col3:
        total_rows = db_summary["total_live_rows"] or 0
        st.metric("Total Rows", f"{total_rows:,}")

    with col4:
        st.metric("Database Size", db_summary["total_size"])

    # --- Alerts ---
    st.divider()

    alerts = report.get("alerts", [])
    critical_alerts = [a for a in alerts if a["severity"] == "CRITICAL"]
    warning_alerts = [a for a in alerts if a["severity"] == "WARNING"]

    if critical_alerts:
        st.subheader(f"🔴 Critical Issues ({len(critical_alerts)})")
        for alert in critical_alerts:
            table_label = f"**[{alert['table_name']}]** " if alert["table_name"] else ""
            st.error(f"{table_label}{alert['message']}\n\n→ {alert['recommendation']}")

    if warning_alerts:
        st.subheader(f"⚠️ Warnings ({len(warning_alerts)})")
        for alert in warning_alerts:
            table_label = f"**[{alert['table_name']}]** " if alert["table_name"] else ""
            st.warning(f"{table_label}{alert['message']}\n\n→ {alert['recommendation']}")

    if not alerts:
        st.success("All health checks passed. Database is healthy.")

    # --- Per-Table Health Grid ---
    st.divider()
    st.subheader("📊 Table Health")

    table_data = get_table_health()
    if table_data:
        for row in table_data:
            with st.expander(f"**{row['table_name']}** — {row['total_size']} — {row['live_rows']:,} rows"):
                m1, m2, m3, m4 = st.columns(4)

                dead_pct = float(row["dead_pct"])
                cache_pct = float(row["cache_hit_pct"])
                idx_pct = float(row["index_usage_pct"])

                m1.metric("Dead Tuple %", f"{dead_pct}%",
                          delta=None if dead_pct < 10 else f"{dead_pct}% bloat",
                          delta_color="inverse" if dead_pct >= 10 else "off")
                m2.metric("Cache Hit %", f"{cache_pct}%")
                m3.metric("Index Usage %", f"{idx_pct}%")
                m4.metric("XID Age", f"{row['xid_age']:,}")

                st.caption(
                    f"Seq scans: {row['seq_scan']:,} | "
                    f"Idx scans: {row['idx_scan']:,} | "
                    f"Last vacuum: {row['last_vacuum'] or 'never'}"
                )

except Exception as e:
    st.error(f"Could not connect to database: {e}")
    st.info("Make sure PostgreSQL is running: `docker compose up -d`")
