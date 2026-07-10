"""
AI DBRE Tool — Vacuum Monitoring

Monitors autovacuum health and identifies tables at risk:
- Tables approaching autovacuum thresholds
- Autovacuum worker activity and lag
- Transaction ID wraparound risk (the silent killer)
- Tables where vacuum is falling behind write rate
"""

import json
from langchain_core.tools import tool
from agent.db import execute_query


@tool
def get_vacuum_status(table_name: str | None = None) -> str:
    """
    Comprehensive vacuum health check for all tables or a specific table.

    Analyzes autovacuum timing, dead tuple accumulation rate, and whether
    vacuum is keeping pace with writes. Also checks for transaction ID
    wraparound risk — the most dangerous vacuum-related failure.

    Args:
        table_name: Specific table to inspect, or None for all tables
            ranked by vacuum urgency.

    Returns:
        JSON with per-table vacuum metrics: last vacuum times, dead tuple
        counts, autovacuum thresholds, whether the table is overdue for
        vacuum, and transaction ID age for wraparound risk assessment.
    """
    where_clause = ""
    params = {}
    if table_name:
        where_clause = "AND s.relname = %(table_name)s"
        params["table_name"] = table_name

    query = f"""
    WITH vacuum_info AS (
        SELECT
            s.schemaname,
            s.relname                                                AS table_name,
            s.n_live_tup                                             AS live_tuples,
            s.n_dead_tup                                             AS dead_tuples,
            CASE
                WHEN s.n_live_tup + s.n_dead_tup > 0
                THEN round(100.0 * s.n_dead_tup / (s.n_live_tup + s.n_dead_tup), 2)
                ELSE 0
            END                                                      AS dead_pct,

            -- Autovacuum threshold calculation
            -- Default: 50 + (0.2 * n_live_tup)
            (50 + 0.2 * s.n_live_tup)::bigint                       AS autovacuum_threshold,
            s.n_dead_tup >= (50 + 0.2 * s.n_live_tup)               AS exceeds_av_threshold,
            CASE
                WHEN (50 + 0.2 * s.n_live_tup) > 0
                THEN round(100.0 * s.n_dead_tup / (50 + 0.2 * s.n_live_tup), 2)
                ELSE 0
            END                                                      AS threshold_pct,

            -- Vacuum timestamps
            s.last_vacuum,
            s.last_autovacuum,
            s.last_analyze,
            s.last_autoanalyze,
            GREATEST(s.last_vacuum, s.last_autovacuum)               AS last_any_vacuum,
            round(EXTRACT(EPOCH FROM (
                now() - GREATEST(s.last_vacuum, s.last_autovacuum)
            )) / 3600.0, 1)                                         AS hours_since_vacuum,

            -- Vacuum counts
            s.vacuum_count,
            s.autovacuum_count,

            -- Write rate indicators
            s.n_tup_ins                                              AS total_inserts,
            s.n_tup_upd                                              AS total_updates,
            s.n_tup_del                                              AS total_deletes,
            s.n_tup_ins + s.n_tup_upd + s.n_tup_del                 AS total_writes,

            -- Transaction ID age (wraparound risk)
            age(c.relfrozenxid)                                      AS xid_age,
            -- Warning at 500M, critical at 1B, emergency at 1.5B (out of 2B max)
            CASE
                WHEN age(c.relfrozenxid) > 1500000000 THEN 'EMERGENCY'
                WHEN age(c.relfrozenxid) > 1000000000 THEN 'CRITICAL'
                WHEN age(c.relfrozenxid) > 500000000  THEN 'WARNING'
                ELSE 'OK'
            END                                                      AS xid_risk_level,

            -- Table size
            pg_size_pretty(pg_total_relation_size(s.relid))          AS total_size

        FROM pg_stat_user_tables s
        JOIN pg_class c ON c.oid = s.relid
        WHERE s.schemaname = 'public'
        {where_clause}
    )
    SELECT *
    FROM vacuum_info
    ORDER BY
        -- Prioritize: wraparound risk first, then threshold exceedance, then dead tuples
        CASE xid_risk_level
            WHEN 'EMERGENCY' THEN 1
            WHEN 'CRITICAL' THEN 2
            WHEN 'WARNING' THEN 3
            ELSE 4
        END,
        exceeds_av_threshold DESC,
        dead_pct DESC
    """

    results = execute_query(query, params if params else None)

    for row in results:
        for key, val in row.items():
            if isinstance(val, bool):
                pass
            elif hasattr(val, "as_integer_ratio"):
                row[key] = float(val)
            elif val is None:
                row[key] = None
            else:
                row[key] = str(val)

    if not results:
        msg = f"Table '{table_name}' not found." if table_name else "No user tables found."
        return json.dumps({"error": msg})

    return json.dumps(results, indent=2, default=str)


@tool
def get_autovacuum_activity() -> str:
    """
    Check currently running autovacuum workers and recent autovacuum activity.

    Shows what autovacuum is doing RIGHT NOW, plus a summary of recent
    autovacuum behavior to identify whether it's keeping up with the workload.

    Returns:
        JSON with two sections:
        - active_workers: Currently running autovacuum processes
        - recent_summary: Per-table summary of autovacuum frequency and timing
    """
    # Active autovacuum workers right now
    active_query = """
    SELECT
        pid,
        datname                                              AS database,
        substring(query, 1, 200)                             AS query,
        state,
        wait_event_type,
        wait_event,
        round(EXTRACT(EPOCH FROM (now() - query_start))::numeric, 2)
                                                             AS duration_seconds,
        round(EXTRACT(EPOCH FROM (now() - xact_start))::numeric, 2)
                                                             AS transaction_seconds
    FROM pg_stat_activity
    WHERE
        query LIKE 'autovacuum:%'
        OR backend_type = 'autovacuum worker'
    ORDER BY query_start ASC
    """

    # Recent autovacuum summary per table
    summary_query = """
    SELECT
        s.relname                                            AS table_name,
        s.autovacuum_count,
        s.autoanalyze_count,
        s.last_autovacuum,
        s.last_autoanalyze,
        round(EXTRACT(EPOCH FROM (
            now() - s.last_autovacuum
        )) / 3600.0, 1)                                     AS hours_since_autovacuum,
        s.n_dead_tup                                         AS current_dead_tuples,
        s.n_live_tup                                         AS live_tuples,
        CASE
            WHEN s.n_dead_tup >= (50 + 0.2 * s.n_live_tup)
            THEN 'OVERDUE'
            WHEN s.n_dead_tup >= 0.5 * (50 + 0.2 * s.n_live_tup)
            THEN 'APPROACHING'
            ELSE 'OK'
        END                                                  AS vacuum_urgency
    FROM pg_stat_user_tables s
    WHERE s.schemaname = 'public'
    ORDER BY
        CASE
            WHEN s.n_dead_tup >= (50 + 0.2 * s.n_live_tup) THEN 1
            WHEN s.n_dead_tup >= 0.5 * (50 + 0.2 * s.n_live_tup) THEN 2
            ELSE 3
        END,
        s.n_dead_tup DESC
    """

    active = execute_query(active_query)
    summary = execute_query(summary_query)

    for rows in [active, summary]:
        for row in rows:
            for key, val in row.items():
                if hasattr(val, "as_integer_ratio"):
                    row[key] = float(val)
                elif val is None:
                    row[key] = None
                else:
                    row[key] = str(val)

    return json.dumps({
        "active_autovacuum_workers": active,
        "worker_count": len(active),
        "table_summary": summary,
    }, indent=2, default=str)
