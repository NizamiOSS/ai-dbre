"""
AI DBRE Tool — Remediation Execution

Executes approved database remediation actions:
- CREATE INDEX CONCURRENTLY
- ANALYZE (refresh statistics)
- VACUUM / VACUUM VERBOSE
- VACUUM FREEZE
- REINDEX CONCURRENTLY

Safety model:
- Strict whitelist of allowed SQL patterns
- Every action is logged to an audit trail
- Uses a separate DB user (dbre_remediation) with limited write privileges
- This tool is behind a human-in-the-loop gate — the graph interrupts
  BEFORE this tool runs, requiring explicit human approval

NEVER allowed:
- DROP (anything)
- DELETE / UPDATE / INSERT
- ALTER TABLE (structural changes)
- TRUNCATE
- Any raw SQL not matching the whitelist
"""

import re
import json
from datetime import datetime
from pathlib import Path
from langchain_core.tools import tool
from agent.db import get_remediation_connection


# =============================================================================
# Safety Whitelist
# =============================================================================

ALLOWED_PATTERNS = [
    {
        "name": "CREATE INDEX CONCURRENTLY",
        "pattern": re.compile(
            r"^\s*CREATE\s+INDEX\s+CONCURRENTLY\s+",
            re.IGNORECASE,
        ),
        "risk": "LOW",
        "description": "Creates an index without blocking reads or writes",
    },
    {
        "name": "CREATE INDEX",
        "pattern": re.compile(
            r"^\s*CREATE\s+INDEX\s+(?!CONCURRENTLY)",
            re.IGNORECASE,
        ),
        "risk": "MEDIUM",
        "description": "Creates an index — blocks writes during build. Prefer CONCURRENTLY.",
    },
    {
        "name": "ANALYZE",
        "pattern": re.compile(
            r"^\s*ANALYZE(\s+\w+)?\s*;?\s*$",
            re.IGNORECASE,
        ),
        "risk": "LOW",
        "description": "Updates table statistics for the query planner",
    },
    {
        "name": "VACUUM",
        "pattern": re.compile(
            r"^\s*VACUUM\s*(\(\s*VERBOSE\s*\))?\s+\w+\s*;?\s*$",
            re.IGNORECASE,
        ),
        "risk": "LOW",
        "description": "Reclaims dead tuple space without blocking reads",
    },
    {
        "name": "VACUUM VERBOSE",
        "pattern": re.compile(
            r"^\s*VACUUM\s+VERBOSE\s+\w+\s*;?\s*$",
            re.IGNORECASE,
        ),
        "risk": "LOW",
        "description": "Reclaims dead tuple space with detailed output",
    },
    {
        "name": "VACUUM FREEZE",
        "pattern": re.compile(
            r"^\s*VACUUM\s+FREEZE\s+\w+\s*;?\s*$",
            re.IGNORECASE,
        ),
        "risk": "LOW",
        "description": "Freezes transaction IDs to prevent wraparound",
    },
    {
        "name": "VACUUM FULL",
        "pattern": re.compile(
            r"^\s*VACUUM\s+FULL\s+\w+\s*;?\s*$",
            re.IGNORECASE,
        ),
        "risk": "HIGH",
        "description": "Rewrites entire table — takes ACCESS EXCLUSIVE lock, blocks ALL access",
    },
    {
        "name": "REINDEX CONCURRENTLY",
        "pattern": re.compile(
            r"^\s*REINDEX\s+.*CONCURRENTLY\s+",
            re.IGNORECASE | re.DOTALL,
        ),
        "risk": "LOW",
        "description": "Rebuilds index without blocking reads or writes (PG 12+)",
    },
]

BLOCKED_PATTERNS = re.compile(
    r"\b(DROP|DELETE|UPDATE|INSERT|ALTER|TRUNCATE|GRANT|REVOKE|COPY)\b",
    re.IGNORECASE,
)

# Audit log file path
AUDIT_LOG_PATH = Path("remediation_audit.jsonl")


# =============================================================================
# Audit Trail
# =============================================================================


def log_action(action: dict):
    """Append an action record to the audit log (JSONL format)."""
    action["timestamp"] = datetime.now().isoformat()
    with open(AUDIT_LOG_PATH, "a") as f:
        f.write(json.dumps(action) + "\n")


# =============================================================================
# The Tool
# =============================================================================


@tool
def execute_remediation(sql_statement: str, reason: str) -> str:
    """
    Execute an approved remediation SQL statement against the database.

    THIS TOOL REQUIRES HUMAN APPROVAL before execution. The agent graph
    will pause and ask the user to confirm before this tool runs.

    Only whitelisted operations are allowed:
    - CREATE INDEX / CREATE INDEX CONCURRENTLY
    - ANALYZE
    - VACUUM / VACUUM VERBOSE / VACUUM FREEZE / VACUUM FULL
    - REINDEX CONCURRENTLY

    Args:
        sql_statement: The exact SQL to execute. Must match the whitelist.
        reason: Why this remediation is needed — the diagnosis that led here.

    Returns:
        JSON with execution result: success/failure, execution time,
        and any output from the command.
    """
    sql_clean = sql_statement.strip().rstrip(";").strip()

    # Safety check 1: blocked patterns
    if BLOCKED_PATTERNS.search(sql_clean):
        result = {
            "status": "BLOCKED",
            "sql": sql_clean,
            "reason": reason,
            "error": "Statement contains a blocked operation (DROP/DELETE/UPDATE/INSERT/ALTER/TRUNCATE). "
                     "Only index creation, VACUUM, ANALYZE, and REINDEX are allowed.",
        }
        log_action(result)
        return json.dumps(result, indent=2)

    # Safety check 2: must match whitelist
    matched_pattern = None
    for pattern_def in ALLOWED_PATTERNS:
        if pattern_def["pattern"].match(sql_clean):
            matched_pattern = pattern_def
            break

    if not matched_pattern:
        result = {
            "status": "BLOCKED",
            "sql": sql_clean,
            "reason": reason,
            "error": "Statement does not match any allowed pattern. "
                     "Allowed: CREATE INDEX, ANALYZE, VACUUM, REINDEX CONCURRENTLY.",
        }
        log_action(result)
        return json.dumps(result, indent=2)

    # Execute the approved statement
    start_time = datetime.now()
    try:
        with get_remediation_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql_clean)

                # Try to capture any output (VACUUM VERBOSE produces NOTICE messages)
                notices = list(conn.notices) if hasattr(conn, "notices") else []

        elapsed = (datetime.now() - start_time).total_seconds()

        result = {
            "status": "SUCCESS",
            "sql": sql_clean,
            "operation": matched_pattern["name"],
            "risk_level": matched_pattern["risk"],
            "reason": reason,
            "execution_time_seconds": round(elapsed, 3),
            "notices": notices[-5:] if notices else [],  # Last 5 notices max
        }
        log_action(result)
        return json.dumps(result, indent=2)

    except Exception as e:
        elapsed = (datetime.now() - start_time).total_seconds()
        result = {
            "status": "ERROR",
            "sql": sql_clean,
            "operation": matched_pattern["name"],
            "reason": reason,
            "error": str(e),
            "execution_time_seconds": round(elapsed, 3),
        }
        log_action(result)
        return json.dumps(result, indent=2)
