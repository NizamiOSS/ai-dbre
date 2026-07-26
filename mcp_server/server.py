"""
AI DBRE — MCP Server

Exposes the 10 diagnostic tools as an MCP server, so any MCP-compatible
client (Claude Desktop, Cursor, ChatGPT, custom agents) can query your
PostgreSQL database.

Architecture:
    MCP Client  ──stdio──▶  This server  ──.invoke()──▶  Existing LangChain tools
                                                              │
                                                         execute_query()
                                                              │
                                                         PostgreSQL

The server delegates to the same tool functions the LangGraph agent uses.
No SQL duplication — one source of truth.

Usage:
    # Test with MCP Inspector
    mcp dev mcp_server/server.py

    # Run directly (stdio transport)
    python mcp_server/server.py
"""

import sys
import os
import json

# Ensure the project root is on the Python path so we can import
# agent.db, agent.config, and tools.*
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env before anything else imports settings
from dotenv import load_dotenv
load_dotenv()

from typing import Optional
from mcp.server.fastmcp import FastMCP

# Import existing LangChain tools — we call them via .invoke()
from tools.slow_queries import get_slow_queries as _get_slow_queries
from tools.slow_queries import get_active_long_queries as _get_active_long_queries
from tools.explain_query import explain_query as _explain_query
from tools.table_stats import get_table_stats as _get_table_stats
from tools.table_stats import get_index_usage as _get_index_usage
from tools.lock_info import get_lock_info as _get_lock_info
from tools.bloat_detection import get_table_bloat as _get_table_bloat
from tools.bloat_detection import get_index_bloat as _get_index_bloat
from tools.vacuum_monitor import get_vacuum_status as _get_vacuum_status
from tools.vacuum_monitor import get_autovacuum_activity as _get_autovacuum_activity


# =============================================================================
# MCP Server
# =============================================================================

mcp = FastMCP("AI DBRE")


# =============================================================================
# Diagnostic Tools — Queries & Performance
# =============================================================================

@mcp.tool()
def get_slow_queries(
    order_by: str = "total_time",
    min_calls: int = 1,
    limit: int = 10,
) -> str:
    """
    Get the slowest queries from pg_stat_statements.

    Args:
        order_by: How to rank queries.
            "total_time" — most total database time consumed (default)
            "mean_time" — highest average execution time per call
            "calls" — most frequently executed queries
        min_calls: Minimum execution count to include (filters one-off queries).
        limit: Number of results (max 20).

    Returns:
        JSON array with query text, call count, total/mean/max execution times,
        rows returned, cache hit ratio, and percentage of total database time.
    """
    return _get_slow_queries.invoke({
        "order_by": order_by,
        "min_calls": min_calls,
        "limit": limit,
    })


@mcp.tool()
def get_active_long_queries(min_duration_seconds: float = 5.0) -> str:
    """
    Find currently running queries that exceed a duration threshold.

    Args:
        min_duration_seconds: Minimum runtime in seconds to include (default: 5).

    Returns:
        JSON array with PID, user, database, query text, duration,
        state, wait event, and client address.
    """
    return _get_active_long_queries.invoke({
        "min_duration_seconds": min_duration_seconds,
    })


@mcp.tool()
def explain_query(query_text: str, analyze: bool = True) -> str:
    """
    Run EXPLAIN on a SQL query to reveal how PostgreSQL executes it.

    Only SELECT queries are allowed — INSERT/UPDATE/DELETE/DDL are rejected
    for safety. This tool is read-only.

    Args:
        query_text: The SELECT query to analyze.
        analyze: If True (default), actually executes the query to get
            real timing and row counts. False for estimate-only.

    Returns:
        JSON execution plan with node types, actual vs estimated rows,
        timing per node, buffer usage, and cost estimates.
    """
    return _explain_query.invoke({
        "query_text": query_text,
        "analyze": analyze,
    })


# =============================================================================
# Diagnostic Tools — Table & Index Stats
# =============================================================================

@mcp.tool()
def get_table_stats(table_name: Optional[str] = None) -> str:
    """
    Get table-level statistics from pg_stat_user_tables.

    Args:
        table_name: Specific table to check, or None for all tables.

    Returns:
        JSON with live/dead tuple counts, sequential vs index scan counts,
        last vacuum/analyze timestamps, and table sizes.
    """
    params = {"table_name": table_name} if table_name else {}
    return _get_table_stats.invoke(params)


@mcp.tool()
def get_index_usage(table_name: Optional[str] = None) -> str:
    """
    Analyze index usage efficiency across tables.

    Args:
        table_name: Specific table to check, or None for all tables.

    Returns:
        JSON with index names, scan counts, tuple reads/fetches,
        index sizes, and unused index identification.
    """
    params = {"table_name": table_name} if table_name else {}
    return _get_index_usage.invoke(params)


# =============================================================================
# Diagnostic Tools — Locks
# =============================================================================

@mcp.tool()
def get_lock_info() -> str:
    """
    Check for lock contention — blocked queries and blocking sessions.

    Returns:
        JSON with blocked queries, blocking sessions, lock types,
        wait durations, and blocking chain details.
    """
    return _get_lock_info.invoke({})


# =============================================================================
# Diagnostic Tools — Bloat
# =============================================================================

@mcp.tool()
def get_table_bloat(
    table_name: Optional[str] = None,
    min_dead_pct: float = 5.0,
) -> str:
    """
    Check tables for dead tuple bloat.

    Args:
        table_name: Specific table to check, or None for all tables.
        min_dead_pct: Minimum dead tuple percentage to report (default: 5%).

    Returns:
        JSON with live/dead tuple counts, dead tuple percentage,
        table sizes, HOT update ratio, vacuum history, and
        recommendations (VACUUM, VACUUM FULL, or healthy).
    """
    params = {}
    if table_name is not None:
        params["table_name"] = table_name
    if min_dead_pct != 5.0:
        params["min_dead_pct"] = min_dead_pct
    return _get_table_bloat.invoke(params)


@mcp.tool()
def get_index_bloat(table_name: Optional[str] = None) -> str:
    """
    Estimate index bloat using pgstattuple.

    Args:
        table_name: Specific table's indexes to check, or None for all.

    Returns:
        JSON with index names, sizes, estimated bloat percentage,
        and reindex recommendations.
    """
    params = {"table_name": table_name} if table_name else {}
    return _get_index_bloat.invoke(params)


# =============================================================================
# Diagnostic Tools — Vacuum & Autovacuum
# =============================================================================

@mcp.tool()
def get_vacuum_status(table_name: Optional[str] = None) -> str:
    """
    Check vacuum and autovacuum status for tables.

    Args:
        table_name: Specific table to check, or None for all tables.

    Returns:
        JSON with last vacuum/autovacuum timestamps, dead tuple counts,
        autovacuum settings, and whether vacuum is overdue.
    """
    params = {"table_name": table_name} if table_name else {}
    return _get_vacuum_status.invoke(params)


@mcp.tool()
def get_autovacuum_activity() -> str:
    """
    Check currently running autovacuum workers and their progress.

    Returns:
        JSON with active autovacuum worker count, which tables they're
        processing, progress percentage, and runtime.
    """
    return _get_autovacuum_activity.invoke({})



# =============================================================================
# MCP Resources — Live Database State
# =============================================================================
#
# Resources are read-only data the client can pull as context BEFORE asking
# questions. Unlike tools (which the LLM decides to call), resources are
# loaded by the client to give the LLM background knowledge upfront.
#

@mcp.resource(
    "dbre://health/summary",
    description=(
        "Live database health summary. Returns all active alerts from "
        "8 automated checks: cache hit ratio, sequential scans, dead tuples, "
        "index bloat, unused indexes, XID wraparound, long queries, vacuum status."
    ),
    mime_type="application/json",
)
def health_summary() -> str:
    """Run all health checks and return alerts as JSON."""
    from agent.health_check import run_health_check

    result = run_health_check()
    return json.dumps(result, indent=2, default=str)


@mcp.resource(
    "dbre://stats/tables",
    description=(
        "Current table-level statistics for all user tables: "
        "live/dead tuple counts, sequential vs index scan ratios, "
        "last vacuum/analyze timestamps, and table sizes."
    ),
    mime_type="application/json",
)
def table_stats_resource() -> str:
    """Fetch table statistics via the existing diagnostic tool."""
    return _get_table_stats.invoke({})


@mcp.resource(
    "dbre://stats/indexes",
    description=(
        "Index usage statistics for all user indexes: "
        "scan counts, tuple reads, index sizes, and unused index identification."
    ),
    mime_type="application/json",
)
def index_stats_resource() -> str:
    """Fetch index usage stats via the existing diagnostic tool."""
    return _get_index_usage.invoke({})


@mcp.resource(
    "dbre://stats/vacuum",
    description=(
        "Vacuum and autovacuum status for all tables: "
        "last vacuum timestamps, dead tuple counts, autovacuum settings, "
        "and whether vacuum is overdue."
    ),
    mime_type="application/json",
)
def vacuum_status_resource() -> str:
    """Fetch vacuum status via the existing diagnostic tool."""
    return _get_vacuum_status.invoke({})


@mcp.resource(
    "dbre://stats/bloat",
    description=(
        "Table bloat levels across the database: "
        "dead tuple percentages, table sizes, HOT update ratios, "
        "and vacuum recommendations."
    ),
    mime_type="application/json",
)
def bloat_resource() -> str:
    """Fetch bloat stats via the existing diagnostic tool."""
    return _get_table_bloat.invoke({})


# =============================================================================
# MCP Resource Templates — Per-Table Details
# =============================================================================

@mcp.resource(
    "dbre://tables/{table_name}/stats",
    description="Detailed statistics for a specific table.",
    mime_type="application/json",
)
def table_detail_stats(table_name: str) -> str:
    """Fetch stats for a single table."""
    return _get_table_stats.invoke({"table_name": table_name})


@mcp.resource(
    "dbre://tables/{table_name}/indexes",
    description="Index usage details for a specific table.",
    mime_type="application/json",
)
def table_detail_indexes(table_name: str) -> str:
    """Fetch index usage for a single table."""
    return _get_index_usage.invoke({"table_name": table_name})


@mcp.resource(
    "dbre://tables/{table_name}/bloat",
    description="Bloat analysis for a specific table.",
    mime_type="application/json",
)
def table_detail_bloat(table_name: str) -> str:
    """Fetch bloat stats for a single table."""
    return _get_table_bloat.invoke({"table_name": table_name})


@mcp.resource(
    "dbre://tables/{table_name}/vacuum",
    description="Vacuum status for a specific table.",
    mime_type="application/json",
)
def table_detail_vacuum(table_name: str) -> str:
    """Fetch vacuum status for a single table."""
    return _get_vacuum_status.invoke({"table_name": table_name})


# =============================================================================
# Entry point
# =============================================================================
#
# Two transport modes:
#   python mcp_server/server.py              → stdio (Claude Desktop, local)
#   python mcp_server/server.py --http       → StreamableHTTP (remote, team access)
#

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="AI DBRE MCP Server")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Run with StreamableHTTP transport instead of stdio",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="HTTP host to bind to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="HTTP port to bind to (default: 8000)",
    )
    args = parser.parse_args()

    if args.http:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        print(f"AI DBRE MCP Server — StreamableHTTP on http://{args.host}:{args.port}/mcp")
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
