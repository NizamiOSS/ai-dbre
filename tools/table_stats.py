"""
AI DBRE Tool — Table Statistics

Pulls table-level health metrics from pg_stat_user_tables and related views:
scan patterns, dead tuple ratios, vacuum status, and table sizes.
"""

import json
from langchain_core.tools import tool
from agent.db import execute_query


@tool
def get_table_stats(table_name: str | None = None) -> str:
    """
    Get health statistics for database tables.

    Args:
        table_name: Specific table to inspect, or None for all tables ranked by issues.

    Returns:
        JSON with per-table metrics: row counts, sequential vs index scan ratio,
        dead tuple count and percentage, last vacuum/analyze timestamps,
        table size on disk, and whether the table appears unhealthy.
    """
    where_clause = ""
    params = {}
    if table_name:
        where_clause = "AND s.relname = %(table_name)s"
        params["table_name"] = table_name

    query = f"""
    SELECT
        s.schemaname,
        s.relname                                                    AS table_name,
        s.n_live_tup                                                 AS live_rows,
        s.n_dead_tup                                                 AS dead_rows,
        CASE
            WHEN s.n_live_tup + s.n_dead_tup > 0
            THEN round(100.0 * s.n_dead_tup / (s.n_live_tup + s.n_dead_tup), 2)
            ELSE 0
        END                                                          AS dead_row_pct,
        s.seq_scan,
        s.idx_scan,
        CASE
            WHEN s.seq_scan + COALESCE(s.idx_scan, 0) > 0
            THEN round(100.0 * COALESCE(s.idx_scan, 0) / (s.seq_scan + COALESCE(s.idx_scan, 0)), 2)
            ELSE 0
        END                                                          AS index_usage_pct,
        s.seq_tup_read                                               AS rows_from_seq_scans,
        s.idx_tup_fetch                                              AS rows_from_idx_scans,
        s.n_tup_ins                                                  AS total_inserts,
        s.n_tup_upd                                                  AS total_updates,
        s.n_tup_del                                                  AS total_deletes,
        s.last_vacuum,
        s.last_autovacuum,
        s.last_analyze,
        s.last_autoanalyze,
        s.vacuum_count,
        s.autovacuum_count,
        pg_size_pretty(pg_total_relation_size(s.relid))              AS total_size,
        pg_size_pretty(pg_relation_size(s.relid))                    AS table_size,
        pg_size_pretty(pg_indexes_size(s.relid))                     AS indexes_size
    FROM pg_stat_user_tables s
    WHERE s.schemaname = 'public'
    {where_clause}
    ORDER BY s.n_dead_tup DESC, s.seq_scan DESC
    """

    results = execute_query(query, params if params else None)

    for row in results:
        for key, val in row.items():
            if hasattr(val, "as_integer_ratio"):
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
def get_index_usage() -> str:
    """
    Analyze index usage across all tables. Finds:
    - Unused indexes (wasting disk space and slowing writes)
    - Tables with low index usage (potential missing indexes)
    - Overall index health

    Returns:
        JSON with two sections:
        - unused_indexes: Indexes that have never been scanned
        - low_index_tables: Tables where most access is via sequential scan
    """
    unused_query = """
    SELECT
        s.schemaname,
        s.relname                                           AS table_name,
        s.indexrelname                                      AS index_name,
        s.idx_scan                                          AS times_used,
        pg_size_pretty(pg_relation_size(s.indexrelid))      AS index_size,
        i.indisunique                                       AS is_unique,
        i.indisprimary                                      AS is_primary
    FROM pg_stat_user_indexes s
    JOIN pg_index i ON s.indexrelid = i.indexrelid
    WHERE
        s.idx_scan = 0
        AND NOT i.indisprimary     -- Don't flag primary keys
        AND NOT i.indisunique      -- Don't flag unique constraints
    ORDER BY pg_relation_size(s.indexrelid) DESC
    """

    low_index_query = """
    SELECT
        relname                                             AS table_name,
        seq_scan,
        idx_scan,
        n_live_tup                                          AS live_rows,
        CASE
            WHEN seq_scan + COALESCE(idx_scan, 0) > 0
            THEN round(100.0 * seq_scan / (seq_scan + COALESCE(idx_scan, 0)), 2)
            ELSE 0
        END                                                 AS seq_scan_pct,
        pg_size_pretty(pg_total_relation_size(relid))       AS table_size
    FROM pg_stat_user_tables
    WHERE
        seq_scan > COALESCE(idx_scan, 0)    -- More seq scans than index scans
        AND n_live_tup > 1000               -- Only meaningful tables
    ORDER BY seq_scan DESC
    """

    unused = execute_query(unused_query)
    low_index = execute_query(low_index_query)

    # Serialize
    for rows in [unused, low_index]:
        for row in rows:
            for key, val in row.items():
                if isinstance(val, bool):
                    pass
                elif hasattr(val, "as_integer_ratio"):
                    row[key] = float(val)
                elif val is None:
                    row[key] = None
                else:
                    row[key] = str(val)

    return json.dumps({
        "unused_indexes": unused,
        "tables_with_low_index_usage": low_index,
    }, indent=2, default=str)
