"""
AI DBRE Tool — Lock Contention Detection

Joins pg_locks with pg_stat_activity to identify:
- Blocked queries waiting for locks
- Blocking sessions holding locks
- Lock chains (A blocks B blocks C)
"""

import json
from langchain_core.tools import tool
from agent.db import execute_query


@tool
def get_lock_info(include_granted: bool = False) -> str:
    """
    Detect lock contention in the database.

    Finds blocked queries and identifies which sessions are blocking them.
    This is critical for diagnosing "the application is hanging" scenarios.

    Args:
        include_granted: If True, also show locks that are currently held
            (not just waiting). Default False — focuses on actual contention.

    Returns:
        JSON with two sections:
        - blocked_queries: Queries currently waiting for a lock, with details
          about who is blocking them and for how long.
        - blocking_sessions: Sessions that are holding locks and blocking others,
          with their current query and how many sessions they block.
    """
    # Find blocked queries and their blockers
    blocked_query = """
    SELECT
        blocked.pid                                              AS blocked_pid,
        blocked.usename                                          AS blocked_user,
        substring(blocked.query, 1, 200)                         AS blocked_query,
        blocked.state                                            AS blocked_state,
        round(EXTRACT(EPOCH FROM (now() - blocked.query_start))::numeric, 2)
                                                                 AS waiting_seconds,
        blocked.wait_event_type,
        blocked.wait_event,
        blocking.pid                                             AS blocking_pid,
        blocking.usename                                         AS blocking_user,
        substring(blocking.query, 1, 200)                        AS blocking_query,
        blocking.state                                           AS blocking_state,
        round(EXTRACT(EPOCH FROM (now() - blocking.query_start))::numeric, 2)
                                                                 AS blocking_duration_seconds,
        bl.mode                                                  AS blocked_lock_mode,
        bl.locktype                                              AS lock_type,
        COALESCE(bl.relation::regclass::text, '')                AS locked_table
    FROM pg_locks bl
    JOIN pg_stat_activity blocked
        ON bl.pid = blocked.pid
    JOIN pg_locks kl
        ON  kl.locktype = bl.locktype
        AND kl.database IS NOT DISTINCT FROM bl.database
        AND kl.relation IS NOT DISTINCT FROM bl.relation
        AND kl.page IS NOT DISTINCT FROM bl.page
        AND kl.tuple IS NOT DISTINCT FROM bl.tuple
        AND kl.virtualxid IS NOT DISTINCT FROM bl.virtualxid
        AND kl.transactionid IS NOT DISTINCT FROM bl.transactionid
        AND kl.classid IS NOT DISTINCT FROM bl.classid
        AND kl.objid IS NOT DISTINCT FROM bl.objid
        AND kl.objsubid IS NOT DISTINCT FROM bl.objsubid
        AND kl.pid != bl.pid
    JOIN pg_stat_activity blocking
        ON kl.pid = blocking.pid
    WHERE NOT bl.granted
      AND blocked.pid != pg_backend_pid()
    ORDER BY blocked.query_start ASC
    """

    # Summary of blocking sessions
    blocking_summary_query = """
    SELECT
        kl.pid                                                   AS blocking_pid,
        a.usename                                                AS blocking_user,
        substring(a.query, 1, 200)                               AS blocking_query,
        a.state                                                  AS session_state,
        round(EXTRACT(EPOCH FROM (now() - a.query_start))::numeric, 2)
                                                                 AS query_duration_seconds,
        round(EXTRACT(EPOCH FROM (now() - a.xact_start))::numeric, 2)
                                                                 AS transaction_duration_seconds,
        count(DISTINCT bl.pid)                                   AS num_blocked_sessions,
        a.application_name,
        a.client_addr
    FROM pg_locks bl
    JOIN pg_locks kl
        ON  kl.locktype = bl.locktype
        AND kl.database IS NOT DISTINCT FROM bl.database
        AND kl.relation IS NOT DISTINCT FROM bl.relation
        AND kl.page IS NOT DISTINCT FROM bl.page
        AND kl.tuple IS NOT DISTINCT FROM bl.tuple
        AND kl.virtualxid IS NOT DISTINCT FROM bl.virtualxid
        AND kl.transactionid IS NOT DISTINCT FROM bl.transactionid
        AND kl.classid IS NOT DISTINCT FROM bl.classid
        AND kl.objid IS NOT DISTINCT FROM bl.objid
        AND kl.objsubid IS NOT DISTINCT FROM bl.objsubid
        AND kl.pid != bl.pid
    JOIN pg_stat_activity a ON kl.pid = a.pid
    WHERE NOT bl.granted
      AND kl.granted
    GROUP BY kl.pid, a.usename, a.query, a.state,
             a.query_start, a.xact_start, a.application_name, a.client_addr
    ORDER BY count(DISTINCT bl.pid) DESC
    """

    blocked = execute_query(blocked_query)
    blockers = execute_query(blocking_summary_query)

    # Serialize
    for rows in [blocked, blockers]:
        for row in rows:
            for key, val in row.items():
                if hasattr(val, "as_integer_ratio"):
                    row[key] = float(val)
                elif val is None:
                    row[key] = None
                else:
                    row[key] = str(val)

    if not blocked and not blockers:
        return json.dumps({
            "message": "No lock contention detected. No sessions are currently blocked.",
            "blocked_count": 0,
            "blocking_count": 0,
        })

    return json.dumps({
        "blocked_queries": blocked,
        "blocking_sessions": blockers,
        "summary": f"{len(blocked)} blocked queries, {len(blockers)} blocking sessions",
    }, indent=2, default=str)
