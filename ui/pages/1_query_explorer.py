"""
AI DBRE — Query Explorer Page

Browse pg_stat_statements data, click a query to run EXPLAIN ANALYZE,
and see performance metrics.
"""

import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import streamlit as st
from agent.db import execute_query

st.set_page_config(page_title="AI DBRE — Query Explorer", page_icon="🔍", layout="wide")

st.title("🔍 Query Explorer")
st.caption("Analyze slow queries from pg_stat_statements")

# =============================================================================
# Controls
# =============================================================================

col1, col2, col3 = st.columns(3)

with col1:
    order_by = st.selectbox("Sort by", [
        ("Total Time", "total_exec_time"),
        ("Mean Time", "mean_exec_time"),
        ("Call Count", "calls"),
        ("Max Time", "max_exec_time"),
    ], format_func=lambda x: x[0])

with col2:
    min_calls = st.number_input("Min calls", min_value=1, value=1, step=1)

with col3:
    limit = st.slider("Results", min_value=5, max_value=50, value=15)


# =============================================================================
# Slow Queries Table
# =============================================================================

@st.cache_data(ttl=15)
def get_queries(order_col, min_calls_val, limit_val):
    query = f"""
    SELECT
        queryid,
        substring(query, 1, 300)                                     AS query_text,
        calls,
        round(total_exec_time::numeric, 2)                           AS total_time_ms,
        round(mean_exec_time::numeric, 2)                            AS mean_time_ms,
        round(max_exec_time::numeric, 2)                             AS max_time_ms,
        round(min_exec_time::numeric, 2)                             AS min_time_ms,
        rows,
        round(
            100.0 * shared_blks_hit / NULLIF(shared_blks_hit + shared_blks_read, 0), 2
        )                                                            AS cache_hit_pct,
        shared_blks_read                                             AS disk_reads,
        temp_blks_written                                            AS temp_writes,
        round(
            (100.0 * total_exec_time / NULLIF(sum(total_exec_time) OVER (), 0))::numeric, 2
        )                                                            AS pct_of_total
    FROM pg_stat_statements
    WHERE
        calls >= {min_calls_val}
        AND query NOT LIKE '%%pg_stat%%'
        AND query NOT LIKE '%%pg_catalog%%'
        AND dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
    ORDER BY {order_col} DESC
    LIMIT {limit_val}
    """
    return execute_query(query)


try:
    queries = get_queries(order_by[1], min_calls, limit)

    if not queries:
        st.info("No queries found. Run the workload generator first: `python scripts/generate_slow_workload.py`")
    else:
        st.subheader(f"Top {len(queries)} queries by {order_by[0]}")

        for i, q in enumerate(queries):
            cache_pct = q["cache_hit_pct"] or 0
            cache_icon = "✅" if float(cache_pct) >= 95 else ("⚠️" if float(cache_pct) >= 80 else "🔴")

            with st.expander(
                f"**#{i+1}** — {q['mean_time_ms']}ms avg | "
                f"{q['calls']} calls | "
                f"{q['pct_of_total']}% of DB time | "
                f"{cache_icon} {cache_pct}% cache"
            ):
                st.code(q["query_text"], language="sql")

                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric("Total Time", f"{q['total_time_ms']}ms")
                m2.metric("Mean Time", f"{q['mean_time_ms']}ms")
                m3.metric("Max Time", f"{q['max_time_ms']}ms")
                m4.metric("Calls", f"{q['calls']:,}")
                m5.metric("Rows/Call", f"{int(q['rows']) // max(int(q['calls']), 1):,}")

                # EXPLAIN ANALYZE button
                query_text = q["query_text"].strip().rstrip(";")
                # Only offer EXPLAIN for SELECT queries
                if query_text.upper().startswith("SELECT"):
                    if st.button(f"Run EXPLAIN ANALYZE", key=f"explain_{i}"):
                        with st.spinner("Running EXPLAIN ANALYZE..."):
                            try:
                                explain_sql = f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {query_text}"
                                result = execute_query(explain_sql)
                                if result:
                                    plan = result[0].get("QUERY PLAN", result[0])
                                    st.json(plan)
                            except Exception as e:
                                st.error(f"EXPLAIN failed: {e}")
                else:
                    st.caption("EXPLAIN only available for SELECT queries")

except Exception as e:
    st.error(f"Could not load queries: {e}")
    st.info("Make sure PostgreSQL is running and the `.env` file is configured.")
