"""
AI DBRE Composite Tool — Slow Query Diagnosis

Pipeline: get_slow_queries → explain_query (per candidate) → classified verdict

Encodes the DBA workflow that would otherwise be re-inferred by the LLM:
1. Pull the worst queries from pg_stat_statements
2. Run EXPLAIN on each to understand WHY it's slow
3. Classify the root cause (missing index, N+1 pattern, stale stats, etc.)
4. Recommend specific fixes

The LLM's job shrinks from "figure out the whole slow-query procedure"
to "recognize this is a performance question and call this one tool."
"""

import json
import re
from langchain_core.tools import tool

from tools.slow_queries import get_slow_queries
from tools.explain_query import explain_query


# ─── Pattern classification thresholds ───────────────────────────

N_PLUS_ONE_CALL_THRESHOLD = 100       # queries called this many times suggest N+1
SEQ_SCAN_ROW_THRESHOLD = 5000         # seq scan above this many rows = likely missing index
ESTIMATE_RATIO_THRESHOLD = 10.0       # actual/estimated ratio above this = stale stats
LOW_CACHE_HIT_THRESHOLD = 90.0        # cache hit % below this = working set exceeds memory


# ─── EXPLAIN plan parsing helpers ────────────────────────────────

def _parse_plan(explain_output: str) -> dict | None:
    """Parse EXPLAIN JSON output into a usable plan dict."""
    try:
        data = json.loads(explain_output)
        if isinstance(data, dict) and "error" in data:
            return None
        # EXPLAIN FORMAT JSON wraps in a list
        if isinstance(data, list) and len(data) > 0:
            return data[0]
        if isinstance(data, dict) and "Plan" in data:
            return data
        return None
    except (json.JSONDecodeError, TypeError):
        return None


def _walk_plan_nodes(node: dict):
    """Yield every node in the plan tree (depth-first)."""
    yield node
    for child in node.get("Plans", []):
        yield from _walk_plan_nodes(child)


def _analyze_plan(plan: dict) -> dict:
    """Extract actionable signals from an EXPLAIN plan."""
    root = plan.get("Plan", {})
    if not root:
        return {"error": "empty plan"}

    signals = {
        "seq_scans": [],
        "estimate_errors": [],
        "sorts_on_disk": [],
        "total_cost": root.get("Total Cost", 0),
        "execution_time_ms": plan.get("Execution Time", 0),
        "planning_time_ms": plan.get("Planning Time", 0),
    }

    for node in _walk_plan_nodes(root):
        node_type = node.get("Node Type", "")
        actual_rows = node.get("Actual Rows", 0)
        plan_rows = node.get("Plan Rows", 0)
        relation = node.get("Relation Name", "")

        # Use actual rows when available (ANALYZE mode), fall back to
        # planner estimates (plan-only mode for parameterized queries)
        effective_rows = actual_rows if actual_rows > 0 else plan_rows

        # Detect sequential scans
        # Key insight: a Seq Scan WITH a Filter always reads the entire
        # table — Plan Rows only counts rows that pass the filter, not
        # rows scanned. So filter presence itself is the signal.
        if node_type == "Seq Scan":
            has_filter = bool(node.get("Filter"))

            if has_filter:
                # Seq Scan + Filter = full table scan with row-by-row
                # filtering — strong index candidate regardless of output rows
                scan_info = {
                    "table": relation,
                    "rows": effective_rows,
                    "estimated": actual_rows == 0,
                    "filter": node["Filter"],
                    "index_candidate": True,
                }
                rows_removed = node.get("Rows Removed by Filter", 0)
                if rows_removed > 0:
                    selectivity = actual_rows / max(actual_rows + rows_removed, 1)
                    scan_info["selectivity"] = round(selectivity, 4)
                signals["seq_scans"].append(scan_info)

            elif effective_rows > SEQ_SCAN_ROW_THRESHOLD:
                # Filterless seq scan on large table — may be intentional
                # (aggregation, full dump) but worth flagging
                signals["seq_scans"].append({
                    "table": relation,
                    "rows": effective_rows,
                    "estimated": actual_rows == 0,
                    "index_candidate": False,
                })

        # Detect estimate-vs-actual divergence (stale stats signal)
        if plan_rows > 0 and actual_rows > 0:
            ratio = actual_rows / plan_rows
            if ratio > ESTIMATE_RATIO_THRESHOLD or ratio < (1 / ESTIMATE_RATIO_THRESHOLD):
                signals["estimate_errors"].append({
                    "node_type": node_type,
                    "table": relation,
                    "estimated": plan_rows,
                    "actual": actual_rows,
                    "ratio": round(ratio, 1),
                })

        # Detect sorts spilling to disk
        if node_type == "Sort" and node.get("Sort Method", "").startswith("external"):
            signals["sorts_on_disk"].append({
                "sort_key": node.get("Sort Key", []),
                "sort_space_kb": node.get("Sort Space Used", 0),
            })

    return signals


def _extract_filter_columns(filter_str: str) -> list[str]:
    """Try to extract column names from a plan filter expression."""
    matches = re.findall(r'\((\w+)\s*[=<>!]+', filter_str)
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for col in matches:
        if col not in seen:
            seen.add(col)
            unique.append(col)
    return unique

# ─── Query classification ───────────────────────────────────────

def _classify_query(query_info: dict, plan_signals: dict | None) -> dict:
    """Classify a slow query's root cause and build a compact verdict."""
    issues = []
    suggested_fixes = []
    pattern = "unclassified"

    calls = query_info.get("calls", 0)
    mean_time = query_info.get("mean_time_ms", 0)
    total_time = query_info.get("total_time_ms", 0)
    cache_hit = query_info.get("cache_hit_pct")

    # ── N+1 pattern detection (from call count, not plan) ────────
    if calls >= N_PLUS_ONE_CALL_THRESHOLD and mean_time < 50:
        pattern = "n_plus_one"
        issues.append(f"called {calls:,} times ({mean_time:.1f}ms avg) — likely N+1 pattern")
        suggested_fixes.append("batch queries or use eager loading to reduce call count")

    # ── Plan-based classification ────────────────────────────────
    if plan_signals and "error" not in plan_signals:
        # Sequential scan on large table
        for scan in plan_signals.get("seq_scans", []):
            table = scan.get("table", "unknown")
            rows = scan.get("rows", 0)
            est_note = " (estimated)" if scan.get("estimated") else ""
            issue = f"sequential scan on {table} ({rows:,} rows{est_note})"

            if scan.get("index_candidate"):
                filter_str = scan.get("filter", "")
                columns = _extract_filter_columns(filter_str)
                if columns:
                    issue += f", filtering on: {', '.join(columns)}"
                    suggested_fixes.append(
                        f"CREATE INDEX on {table} ({', '.join(columns)})"
                    )
                else:
                    suggested_fixes.append(f"add index on {table} for filtered columns")
                if pattern == "unclassified":
                    pattern = "missing_index"
            else:
                issue += " (full table read, may be intentional for wide aggregation)"

            issues.append(issue)

        # Stale statistics
        for est_err in plan_signals.get("estimate_errors", []):
            table = est_err.get("table", "unknown")
            ratio = est_err.get("ratio", 0)
            direction = "underestimate" if ratio > 1 else "overestimate"
            issues.append(
                f"planner {direction} on {table}: estimated {est_err['estimated']:,} "
                f"rows, got {est_err['actual']:,} ({ratio}x off)"
            )
            suggested_fixes.append(f"run ANALYZE on {table} to update statistics")
            if pattern == "unclassified":
                pattern = "stale_statistics"

        # Sorts on disk
        for sort in plan_signals.get("sorts_on_disk", []):
            issues.append(
                f"sort spilling to disk ({sort['sort_space_kb']}KB)"
            )
            suggested_fixes.append("increase work_mem or add index to avoid sort")
            if pattern == "unclassified":
                pattern = "memory_pressure"

    # ── Cache hit ratio ──────────────────────────────────────────
    if cache_hit is not None and cache_hit < LOW_CACHE_HIT_THRESHOLD:
        issues.append(f"low cache hit ratio ({cache_hit}%)")
        if pattern == "unclassified":
            pattern = "memory_pressure"

    # ── Fallback ─────────────────────────────────────────────────
    if not issues:
        issues.append(f"high total time ({total_time:,.0f}ms across {calls:,} calls)")
        if pattern == "unclassified":
            pattern = "expensive_query"

    if not suggested_fixes:
        suggested_fixes.append("review query logic and access patterns")

    return {
        "pattern": pattern,
        "issues": issues,
        "suggested_fixes": suggested_fixes,
    }


# ─── Composite tool ─────────────────────────────────────────────

@tool
def diagnose_slow_queries(
    order_by: str = "total_time",
    min_calls: int = 1,
    top_n: int = 5,
) -> str:
    """
    Diagnose the slowest queries by pulling top offenders from
    pg_stat_statements and running EXPLAIN on each to identify
    root causes (missing indexes, N+1 patterns, stale stats, etc.).

    Use this for broad "what's slow?" or "find performance issues"
    questions. For examining a specific known query, use explain_query
    directly instead.

    Args:
        order_by: How to rank queries — "total_time" (default, overall impact),
            "mean_time" (individually slowest), or "calls" (most frequent, N+1 detection).
        min_calls: Minimum call count to include (default 1).
        top_n: Number of top queries to diagnose (default 5, max 10).

    Returns:
        JSON with a summary line and per-query diagnoses including:
        classified pattern, problem description, and suggested fixes.
    """
    top_n = min(top_n, 10)

    # ── Step 1: Get slow queries ─────────────────────────────────
    slow_raw = get_slow_queries.invoke({
        "order_by": order_by,
        "min_calls": min_calls,
        "limit": top_n,
    })

    candidates = _parse_candidates(slow_raw)

    # Filter out noise: EXPLAIN queries from our own tool calls,
    # SET statements, and other non-application queries
    candidates = [
        c for c in candidates
        if not c.get("query_preview", "").strip().upper().startswith(("EXPLAIN", "SET "))
    ]

    if not candidates:
        return json.dumps({
            "status": "healthy",
            "message": "No slow queries found matching criteria.",
        })

    # ── Step 2: EXPLAIN each candidate ───────────────────────────
    diagnoses = []
    for q in candidates:
        query_text = q.get("query_preview", "")

        # Try to run EXPLAIN — may fail for non-SELECT or truncated queries
        plan_signals = None
        explain_note = None
        if _is_explainable(query_text):
            # Deparameterize $1, $2, ... so EXPLAIN can parse the query
            explain_text, was_parameterized = _deparameterize(query_text)
            # Use plan-only mode for parameterized queries (no real values
            # to execute with), full ANALYZE for literal queries
            use_analyze = not was_parameterized

            explain_raw = explain_query.invoke({
                "query_text": explain_text,
                "analyze": use_analyze,
            })
            plan = _parse_plan(explain_raw)
            if plan:
                plan_signals = _analyze_plan(plan)
                if was_parameterized:
                    explain_note = "plan-only estimate (parameterized query)"
            else:
                explain_note = "EXPLAIN failed or returned no plan"
        else:
            explain_note = _explain_skip_reason(query_text)

        # ── Step 3: Classify and build verdict ───────────────────
        classification = _classify_query(q, plan_signals)

        diagnosis = {
            "query": query_text,
            "pattern": classification["pattern"],
            "calls": q.get("calls", 0),
            "total_time_ms": q.get("total_time_ms", 0),
            "mean_time_ms": q.get("mean_time_ms", 0),
            "pct_of_total": q.get("pct_of_total_time", 0),
            "problem": "; ".join(classification["issues"]),
            "suggested_fixes": classification["suggested_fixes"],
        }

        if explain_note:
            diagnosis["explain_note"] = explain_note

        diagnoses.append(diagnosis)

    # ── Step 4: Sort by impact and return ────────────────────────
    diagnoses.sort(key=lambda d: d.get("total_time_ms", 0), reverse=True)

    summary = _build_summary(diagnoses)

    return json.dumps({
        "summary": summary,
        "diagnoses": diagnoses,
    }, indent=2, default=str)


# ─── Output helpers ──────────────────────────────────────────────

def _parse_candidates(json_str: str) -> list[dict]:
    """Parse get_slow_queries output."""
    try:
        data = json.loads(json_str)
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, TypeError):
        return []


def _is_explainable(query_text: str) -> bool:
    """Check if a query can be passed to EXPLAIN (must be complete SELECT)."""
    stripped = query_text.strip().rstrip(";").strip()
    if not stripped.upper().startswith("SELECT"):
        return False
    # Truncated queries from pg_stat_statements end abruptly
    if stripped.endswith("...") or stripped.endswith("…"):
        return False
    # If query_preview was truncated at 200 chars, it's likely incomplete
    if len(stripped) >= 198:
        return False
    return True


def _deparameterize(query_text: str) -> tuple[str, bool]:
    """
    Replace pg_stat_statements $N parameters with safe literal values
    so EXPLAIN can parse the query.

    Returns (processed_query, was_parameterized).

    Uses 1 as the default substitute — the planner relies on table
    statistics for row estimates, not literal values, so the plan
    shape (Seq Scan vs Index Scan) is accurate regardless.
    """
    if "$" not in query_text:
        return query_text, False

    # Replace $1, $2, etc. with literal 1
    deparameterized = re.sub(r'\$\d+', '1', query_text)
    return deparameterized, True


def _explain_skip_reason(query_text: str) -> str:
    """Explain why EXPLAIN was skipped for this query."""
    stripped = query_text.strip()
    if not stripped.upper().startswith("SELECT"):
        return "not a SELECT query — EXPLAIN skipped for safety"
    if len(stripped) >= 198:
        return "query truncated in pg_stat_statements — EXPLAIN skipped"
    return "query not suitable for EXPLAIN"


def _build_summary(diagnoses: list[dict]) -> str:
    """Build a one-line summary for quick triage."""
    total = len(diagnoses)
    patterns = {}
    for d in diagnoses:
        p = d.get("pattern", "unclassified")
        patterns[p] = patterns.get(p, 0) + 1

    if not patterns:
        return f"{total} queries analyzed, no issues classified"

    pattern_parts = [f"{count} {name}" for name, count in patterns.items()]
    query_word = "query" if total == 1 else "queries"
    return f"{total} {query_word} analyzed: {', '.join(pattern_parts)}"