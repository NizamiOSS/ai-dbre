"""
AI DBRE Tool — Bloat Detection

Identifies wasted space in tables and indexes caused by:
- Dead tuples not yet reclaimed by VACUUM
- B-tree index bloat from page splits and deletions
- Tables that have grown far beyond their live data size

Uses pgstattuple extension for accurate physical-level analysis
and pg_stat_user_tables for lightweight dead tuple estimates.
"""

import json
from langchain_core.tools import tool
from agent.db import execute_query


@tool
def get_table_bloat(table_name: str | None = None, min_dead_pct: float = 5.0) -> str:
    """
    Detect table bloat — wasted space from dead tuples.

    Combines the fast estimate from pg_stat_user_tables (dead tuple count)
    with physical size analysis to identify tables that need VACUUM.

    Args:
        table_name: Specific table to inspect, or None for all bloated tables.
        min_dead_pct: Minimum dead tuple percentage to report (default 5%).
            Tables below this threshold are considered healthy.

    Returns:
        JSON array of bloated tables with: live/dead tuple counts and ratio,
        table size on disk, last vacuum timestamps, and whether the table
        is a candidate for VACUUM FULL (severe bloat) vs regular VACUUM.
    """
    where_clause = ""
    params = {"min_dead_pct": min_dead_pct}

    if table_name:
        where_clause = "AND s.relname = %(table_name)s"
        params["table_name"] = table_name

    query = f"""
    WITH table_bloat AS (
        SELECT
            s.schemaname,
            s.relname                                            AS table_name,
            s.n_live_tup                                         AS live_tuples,
            s.n_dead_tup                                         AS dead_tuples,
            CASE
                WHEN s.n_live_tup + s.n_dead_tup > 0
                THEN round(100.0 * s.n_dead_tup / (s.n_live_tup + s.n_dead_tup), 2)
                ELSE 0
            END                                                  AS dead_pct,
            pg_size_pretty(pg_total_relation_size(s.relid))      AS total_size,
            pg_size_pretty(pg_relation_size(s.relid))            AS table_size,
            pg_size_pretty(pg_indexes_size(s.relid))             AS indexes_size,
            pg_total_relation_size(s.relid)                      AS total_size_bytes,
            pg_relation_size(s.relid)                            AS table_size_bytes,
            s.n_tup_ins                                          AS total_inserts,
            s.n_tup_upd                                          AS total_updates,
            s.n_tup_del                                          AS total_deletes,
            s.n_tup_hot_upd                                      AS hot_updates,
            CASE
                WHEN s.n_tup_upd > 0
                THEN round(100.0 * s.n_tup_hot_upd / s.n_tup_upd, 2)
                ELSE 0
            END                                                  AS hot_update_pct,
            s.last_vacuum,
            s.last_autovacuum,
            s.last_analyze,
            s.last_autoanalyze,
            s.vacuum_count + s.autovacuum_count                  AS total_vacuum_count,
            CASE
                WHEN s.n_dead_tup > 50000 AND
                     (100.0 * s.n_dead_tup / NULLIF(s.n_live_tup + s.n_dead_tup, 0)) > 20
                THEN 'VACUUM FULL recommended'
                WHEN s.n_dead_tup > 10000 AND
                     (100.0 * s.n_dead_tup / NULLIF(s.n_live_tup + s.n_dead_tup, 0)) > 10
                THEN 'VACUUM recommended'
                WHEN s.n_dead_tup > 1000
                THEN 'Monitor — approaching threshold'
                ELSE 'Healthy'
            END                                                  AS recommendation
        FROM pg_stat_user_tables s
        WHERE s.schemaname = 'public'
        {where_clause}
    )
    SELECT *
    FROM table_bloat
    WHERE dead_pct >= %(min_dead_pct)s
       OR %(table_name)s IS NOT NULL
    ORDER BY dead_pct DESC, dead_tuples DESC
    """

    # Fix: if no table_name, pass NULL explicitly
    if "table_name" not in params:
        params["table_name"] = None

    results = execute_query(query, params)

    for row in results:
        for key, val in row.items():
            if hasattr(val, "as_integer_ratio"):
                row[key] = float(val)
            elif val is None:
                row[key] = None
            else:
                row[key] = str(val)

    if not results:
        return json.dumps({
            "message": f"No tables found with dead tuple ratio above {min_dead_pct}%.",
            "status": "healthy",
        })

    return json.dumps(results, indent=2, default=str)


@tool
def get_index_bloat(min_size_mb: float = 1.0) -> str:
    """
    Detect index bloat using pgstattuple for physical-level analysis.

    Identifies indexes where a significant portion of pages are dead or
    reusable, meaning the index has grown larger than necessary.

    Args:
        min_size_mb: Minimum index size in MB to analyze (default 1MB).
            Skips tiny indexes where bloat doesn't matter.

    Returns:
        JSON array of bloated indexes with: index size, estimated live vs dead
        space, bloat ratio, and whether REINDEX is recommended.
    """
    # First get candidate indexes above size threshold
    candidates_query = """
    SELECT
        schemaname,
        relname                                            AS table_name,
        indexrelname                                       AS index_name,
        pg_relation_size(indexrelid)                       AS index_size_bytes,
        pg_size_pretty(pg_relation_size(indexrelid))       AS index_size,
        idx_scan                                           AS times_used
    FROM pg_stat_user_indexes
    WHERE
        schemaname = 'public'
        AND pg_relation_size(indexrelid) >= %(min_size_bytes)s
    ORDER BY pg_relation_size(indexrelid) DESC
    """

    params = {"min_size_bytes": int(min_size_mb * 1024 * 1024)}
    candidates = execute_query(candidates_query, params)

    results = []
    for idx in candidates:
        index_name = idx["index_name"]
        try:
            # Use pgstattuple for accurate bloat measurement
            bloat_query = """
            SELECT
                avg_leaf_density,
                leaf_pages,
                empty_pages,
                deleted_pages,
                CASE
                    WHEN leaf_pages > 0
                    THEN round((100.0 - avg_leaf_density)::numeric, 2)
                    ELSE 0
                END AS bloat_pct
            FROM pgstatindex(%(index_name)s)
            """
            bloat_data = execute_query(bloat_query, {"index_name": index_name})

            if bloat_data:
                bd = bloat_data[0]
                bloat_pct = float(bd.get("bloat_pct", 0))

                recommendation = "Healthy"
                if bloat_pct > 50:
                    recommendation = "REINDEX recommended"
                elif bloat_pct > 30:
                    recommendation = "Monitor — moderate bloat"

                results.append({
                    "table_name": str(idx["table_name"]),
                    "index_name": str(index_name),
                    "index_size": str(idx["index_size"]),
                    "index_size_bytes": int(idx["index_size_bytes"]),
                    "times_used": int(idx["times_used"]),
                    "avg_leaf_density": float(bd.get("avg_leaf_density", 0)),
                    "bloat_pct": bloat_pct,
                    "leaf_pages": int(bd.get("leaf_pages", 0)),
                    "empty_pages": int(bd.get("empty_pages", 0)),
                    "deleted_pages": int(bd.get("deleted_pages", 0)),
                    "recommendation": recommendation,
                })
        except Exception as e:
            results.append({
                "table_name": str(idx["table_name"]),
                "index_name": str(index_name),
                "index_size": str(idx["index_size"]),
                "error": f"Could not analyze: {str(e)}",
            })

    if not results:
        return json.dumps({
            "message": f"No indexes found above {min_size_mb}MB.",
            "status": "no_candidates",
        })

    return json.dumps(results, indent=2, default=str)
