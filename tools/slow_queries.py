"""
AI DBRE Tool — Slow Query Detection via pg_stat_statements

Identifies the most expensive queries by total execution time,
mean execution time, or call frequency.
"""

import json
from langchain_core.tools import tool
from agent.db import execute_query
from agent.config import settings


@tool
def get_slow_queries(
    order_by: str = "total_time",
    min_calls: int = 1,
    limit: int = 10,
) -> str:
    """
    Get the slowest queries from pg_stat_statements.

    Args:
        order_by: How to rank queries. Options:
            - "total_time": Queries consuming the most total database time (default, best for overall impact)
            - "mean_time": Queries with highest average execution time (best for finding individually slow queries)
            - "calls": Most frequently called queries (best for finding N+1 patterns)
        min_calls: Minimum number of executions to include (filters noise from one-off queries)
        limit: Number of results to return (max 20)

    Returns:
        JSON array of slow queries with metrics: query text, call count,
        total/mean/max execution times, rows returned, cache hit ratio,
        and percentage of total database time.
    """
    limit = min(limit, 20)

    order_column = {
        "total_time": "total_exec_time",
        "mean_time": "mean_exec_time",
        "calls": "calls",
    }.get(order_by, "total_exec_time")

    query = f"""
    SELECT
        queryid,
        substring(query, 1, 200)                                     AS query_preview,
        calls,
        round(total_exec_time::numeric, 2)                           AS total_time_ms,
        round(mean_exec_time::numeric, 2)                            AS mean_time_ms,
        round(max_exec_time::numeric, 2)                             AS max_time_ms,
        round(min_exec_time::numeric, 2)                             AS min_time_ms,
        round(stddev_exec_time::numeric, 2)                          AS stddev_time_ms,
        rows,
        round(
            100.0 * shared_blks_hit / NULLIF(shared_blks_hit + shared_blks_read, 0), 2
        )                                                            AS cache_hit_pct,
        shared_blks_read                                             AS disk_reads,
        temp_blks_written                                            AS temp_disk_writes,
        round(
            (100.0 * total_exec_time / NULLIF(sum(total_exec_time) OVER (), 0))::numeric, 2
        )                                                            AS pct_of_total_time
    FROM pg_stat_statements
    WHERE
        calls >= %(min_calls)s
        AND query NOT LIKE '%%pg_stat%%'           -- Exclude our own monitoring queries
        AND query NOT LIKE '%%pg_catalog%%'
        AND dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
    ORDER BY {order_column} DESC
    LIMIT %(limit)s
    """

    results = execute_query(query, {"min_calls": min_calls, "limit": limit})

    # Convert Decimal types to float for JSON serialization
    for row in results:
        for key, val in row.items():
            if hasattr(val, "as_integer_ratio"):  # Decimal or float
                row[key] = float(val)
            elif isinstance(val, int):
                pass  # Already fine
            elif val is None:
                row[key] = None
            else:
                row[key] = str(val)

    return json.dumps(results, indent=2, default=str)


@tool
def get_active_long_queries(min_duration_seconds: int = 5) -> str:
    """
    Get currently running queries that have been executing longer than the threshold.

    Args:
        min_duration_seconds: Minimum execution duration in seconds to include (default: 5)

    Returns:
        JSON array of long-running queries with: PID, user, database,
        query text, duration, state, wait event type, and client address.
    """
    query = """
    SELECT
        pid,
        usename                                          AS username,
        datname                                          AS database,
        substring(query, 1, 300)                         AS query_preview,
        state,
        wait_event_type,
        wait_event,
        round(EXTRACT(EPOCH FROM (now() - query_start))::numeric, 2) AS duration_seconds,
        round(EXTRACT(EPOCH FROM (now() - xact_start))::numeric, 2)  AS transaction_seconds,
        client_addr,
        application_name,
        backend_type
    FROM pg_stat_activity
    WHERE
        state != 'idle'
        AND pid != pg_backend_pid()
        AND query NOT LIKE '%%pg_stat%%'
        AND EXTRACT(EPOCH FROM (now() - query_start)) > %(min_duration)s
    ORDER BY query_start ASC
    """

    results = execute_query(query, {"min_duration": min_duration_seconds})

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
            "message": f"No queries running longer than {min_duration_seconds} seconds.",
            "active_count": 0,
        })

    return json.dumps(results, indent=2, default=str)
