"""
AI DBRE Tool — EXPLAIN ANALYZE

Runs EXPLAIN (ANALYZE, BUFFERS) on a query to reveal the execution plan,
actual vs estimated rows, buffer usage, and timing per node.

Safety: Only SELECT queries are allowed. The tool rejects any DDL/DML.
"""

import json
import re
from langchain_core.tools import tool
from agent.db import execute_query


FORBIDDEN_PATTERNS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|COPY|VACUUM|REINDEX)\b",
    re.IGNORECASE,
)


@tool
def explain_query(query_text: str, analyze: bool = True) -> str:
    """
    Run EXPLAIN on a SQL query to reveal how PostgreSQL executes it.

    Args:
        query_text: The SELECT query to analyze. Must be a SELECT statement —
            INSERT/UPDATE/DELETE/DDL are rejected for safety.
        analyze: If True (default), actually executes the query to get real timing
            and row counts. Set to False for a plan-only estimate without execution.

    Returns:
        JSON execution plan showing: node types (Seq Scan, Index Scan, etc.),
        estimated vs actual rows, execution time per node, buffer hits/reads,
        and total planning + execution time.
    """
    # Safety: reject non-SELECT queries
    stripped = query_text.strip().rstrip(";").strip()
    if FORBIDDEN_PATTERNS.search(stripped):
        return json.dumps({
            "error": "Only SELECT queries are allowed for EXPLAIN analysis. "
                     "This tool is read-only for safety."
        })

    if not stripped.upper().startswith("SELECT"):
        return json.dumps({
            "error": "Query must start with SELECT. Got: " + stripped[:50]
        })

    # Build EXPLAIN command
    explain_options = ["BUFFERS", "FORMAT JSON"]
    if analyze:
        explain_options.insert(0, "ANALYZE")

    explain_sql = f"EXPLAIN ({', '.join(explain_options)}) {stripped}"

    try:
        results = execute_query(explain_sql)
        # EXPLAIN FORMAT JSON returns a single row with a single column
        if results:
            plan = results[0].get("QUERY PLAN", results[0])
            return json.dumps(plan, indent=2, default=str)
        return json.dumps({"error": "No execution plan returned."})
    except Exception as e:
        return json.dumps({
            "error": f"EXPLAIN failed: {str(e)}",
            "hint": "Check that the query is syntactically valid and references existing tables."
        })
