"""
AI DBRE Composite Tool — Bloat Diagnosis

Combines table bloat, index bloat, and vacuum status into a single
diagnostic call with cross-referenced urgency assessment.

This encodes DBA judgment that would otherwise be re-inferred by the
LLM on every bloat-related question:
- Bloat alone doesn't tell you if vacuum is keeping up
- Vacuum lag alone doesn't tell you how bad the bloat actually is
- Index bloat and table bloat on the same table compound each other

Pipeline: get_table_bloat + get_index_bloat + get_vacuum_status → unified verdict
"""

import json
from langchain_core.tools import tool

# Import the existing tool objects — we call .invoke() to reuse
# their SQL and formatting logic without duplicating it.
from tools.bloat_detection import get_table_bloat, get_index_bloat
from tools.vacuum_monitor import get_vacuum_status


# ─── Urgency thresholds (encode DBA judgment once) ───────────────

URGENCY_RULES = {
    # (bloat_pct_min, autovacuum_ok, vacuum_recent) → urgency
    # Order matters: first match wins
    "critical": {
        "description": "High bloat with vacuum not running or disabled",
        "conditions": lambda b, v: (
            b["dead_pct"] >= 20
            and (not v["autovacuum_enabled"] or v["hours_since_vacuum"] is None
                 or v["hours_since_vacuum"] > 24)
        ),
    },
    "high": {
        "description": "Significant bloat with vacuum falling behind",
        "conditions": lambda b, v: (
            b["dead_pct"] >= 15
            or (b["dead_pct"] >= 10 and v.get("exceeds_av_threshold", False))
        ),
    },
    "moderate": {
        "description": "Notable bloat but vacuum may catch up",
        "conditions": lambda b, v: (
            b["dead_pct"] >= 5
        ),
    },
    "low": {
        "description": "Within normal operating range",
        "conditions": lambda b, v: True,  # fallback
    },
}


# ─── Helper functions ────────────────────────────────────────────

def _parse_tool_output(json_str: str) -> list[dict] | dict:
    """Parse JSON string from a tool, handling both list and dict returns."""
    data = json.loads(json_str)
    if isinstance(data, dict) and "status" in data:
        return []  # "healthy" / "no_candidates" response
    if isinstance(data, dict) and "error" in data:
        return []
    if isinstance(data, list):
        return data
    return [data]


def _safe_float(val, default=0.0) -> float:
    """Convert a value to float safely."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _safe_hours(val) -> float | None:
    """Parse hours_since_vacuum, which might be a string or None."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _build_vacuum_lookup(vacuum_rows: list[dict]) -> dict[str, dict]:
    """Index vacuum status rows by table name for O(1) lookup."""
    lookup = {}
    for row in vacuum_rows:
        table = row.get("table_name", "")
        lookup[table] = {
            "autovacuum_enabled": not row.get("autovacuum_disabled", False),
            "exceeds_av_threshold": row.get("exceeds_av_threshold", False),
            "hours_since_vacuum": _safe_hours(row.get("hours_since_vacuum")),
            "last_any_vacuum": row.get("last_any_vacuum"),
            "xid_risk_level": row.get("xid_risk_level", "OK"),
            "dead_tuples": _safe_float(row.get("dead_tuples", 0)),
            "threshold_pct": _safe_float(row.get("threshold_pct", 0)),
        }
    return lookup


def _build_index_bloat_lookup(index_rows: list[dict]) -> dict[str, list[dict]]:
    """Group index bloat entries by table name."""
    lookup: dict[str, list[dict]] = {}
    for row in index_rows:
        table = row.get("table_name", "")
        if table not in lookup:
            lookup[table] = []
        lookup[table].append({
            "index_name": row.get("index_name", ""),
            "index_size": row.get("index_size", ""),
            "bloat_pct": _safe_float(row.get("bloat_pct", 0)),
            "times_used": int(row.get("times_used", 0)),
            "recommendation": row.get("recommendation", ""),
        })
    return lookup


def _assess_urgency(bloat_info: dict, vacuum_info: dict) -> str:
    """Apply urgency rules — first matching rule wins."""
    for level, rule in URGENCY_RULES.items():
        try:
            if rule["conditions"](bloat_info, vacuum_info):
                return level
        except (KeyError, TypeError):
            continue
    return "low"


# ─── Composite tool ──────────────────────────────────────────────

@tool
def diagnose_bloat(table_name: str | None = None) -> str:
    """
    Comprehensive bloat diagnosis combining table bloat, index bloat,
    and vacuum status into a single cross-referenced assessment.

    Use this for broad "is there bloat?" or "check database health"
    questions. For targeted inspection of one specific metric, use
    the individual tools (get_table_bloat, get_index_bloat,
    get_vacuum_status) instead.

    Args:
        table_name: Specific table to diagnose, or None to scan all tables.

    Returns:
        JSON with per-table diagnosis including: bloat metrics, vacuum
        health, index bloat, cross-referenced urgency level (critical /
        high / moderate / low), and recommended actions.
    """
    # ── Step 1: Gather data from all three primitives ────────────
    table_bloat_raw = get_table_bloat.invoke(
        {"table_name": table_name, "min_dead_pct": 0.0}
        if table_name else
        {"min_dead_pct": 2.0}
    )
    index_bloat_raw = get_index_bloat.invoke({"min_size_mb": 0.5})
    vacuum_status_raw = get_vacuum_status.invoke(
        {"table_name": table_name} if table_name else {}
    )

    # ── Step 2: Parse results ────────────────────────────────────
    table_bloat_rows = _parse_tool_output(table_bloat_raw)
    index_bloat_rows = _parse_tool_output(index_bloat_raw)
    vacuum_rows = _parse_tool_output(vacuum_status_raw)

    vacuum_lookup = _build_vacuum_lookup(vacuum_rows)
    index_lookup = _build_index_bloat_lookup(index_bloat_rows)

    # ── Step 3: Cross-reference and assess ───────────────────────
    diagnoses = []
    default_vacuum = {
        "autovacuum_enabled": True,
        "exceeds_av_threshold": False,
        "hours_since_vacuum": None,
        "last_any_vacuum": None,
        "xid_risk_level": "OK",
        "dead_tuples": 0,
        "threshold_pct": 0,
    }

    # Start from table bloat (primary signal)
    seen_tables = set()
    for row in table_bloat_rows:
        tbl = row.get("table_name", "")
        seen_tables.add(tbl)

        bloat_info = {
            "dead_pct": _safe_float(row.get("dead_pct", 0)),
            "dead_tuples": _safe_float(row.get("dead_tuples", 0)),
            "live_tuples": _safe_float(row.get("live_tuples", 0)),
        }

        vacuum_info = vacuum_lookup.get(tbl, default_vacuum)
        urgency = _assess_urgency(bloat_info, vacuum_info)
        idx_bloat = index_lookup.get(tbl, [])

        diagnoses.append({
            "table_name": tbl,
            "urgency": urgency,
            "problem": _describe_problem(bloat_info, vacuum_info, row),
            "index_note": _describe_index_bloat(idx_bloat),
            "recommended_actions": _recommend_actions(
                bloat_info, vacuum_info, idx_bloat
            ),
        })

    # Also surface tables with vacuum issues but no table bloat yet
    for tbl, vinfo in vacuum_lookup.items():
        if tbl in seen_tables:
            continue
        if vinfo["xid_risk_level"] != "OK" or not vinfo["autovacuum_enabled"]:
            bloat_info = {"dead_pct": 0, "dead_tuples": vinfo["dead_tuples"],
                          "live_tuples": 0}
            idx_bloat = index_lookup.get(tbl, [])

            diagnoses.append({
                "table_name": tbl,
                "urgency": "high" if vinfo["xid_risk_level"] != "OK" else "moderate",
                "problem": _describe_vacuum_only_problem(vinfo),
                "index_note": _describe_index_bloat(idx_bloat),
                "recommended_actions": _recommend_actions(
                    bloat_info, vinfo, idx_bloat
                ),
            })

    # ── Step 4: Sort by urgency and return ───────────────────────
    urgency_order = {"critical": 0, "high": 1, "moderate": 2, "low": 3}
    diagnoses.sort(key=lambda d: (
        urgency_order.get(d.get("urgency", "low"), 4),
    ))

    if not diagnoses:
        return json.dumps({
            "status": "healthy",
            "message": "No bloat or vacuum issues detected.",
            "tables_checked": len(vacuum_rows),
        })

    summary = _build_summary(diagnoses)

    return json.dumps({
        "summary": summary,
        "diagnoses": diagnoses,
    }, indent=2, default=str)


def _recommend_actions(
    bloat_info: dict, vacuum_info: dict, index_bloat: list[dict]
) -> list[str]:
    """Generate ordered action recommendations based on cross-referenced data."""
    actions = []

    # Vacuum-related
    if not vacuum_info.get("autovacuum_enabled", True):
        actions.append("Enable autovacuum on this table (ALTER TABLE ... SET (autovacuum_enabled = true))")

    if bloat_info.get("dead_pct", 0) >= 20 and bloat_info.get("dead_tuples", 0) > 50000:
        actions.append("Run VACUUM FULL to reclaim space (requires ACCESS EXCLUSIVE lock)")
    elif bloat_info.get("dead_pct", 0) >= 5:
        actions.append("Run VACUUM to reclaim dead tuples")

    if vacuum_info.get("xid_risk_level", "OK") in ("WARNING", "CRITICAL", "EMERGENCY"):
        actions.append(
            f"URGENT: Transaction ID age at {vacuum_info['xid_risk_level']} level — "
            "run VACUUM FREEZE to prevent wraparound"
        )

    # Index-related
    bloated_indexes = [idx for idx in index_bloat if idx.get("bloat_pct", 0) > 30]
    if bloated_indexes:
        idx_names = ", ".join(idx["index_name"] for idx in bloated_indexes)
        actions.append(f"REINDEX bloated indexes: {idx_names}")

    if not actions:
        actions.append("No action needed — within normal parameters")

    return actions


def _describe_problem(
    bloat_info: dict, vacuum_info: dict, raw_row: dict
) -> str:
    """Compose a one-line human-readable problem description."""
    parts = []

    dead_pct = bloat_info["dead_pct"]
    dead = int(bloat_info["dead_tuples"])
    live = int(bloat_info["live_tuples"])
    parts.append(f"{dead_pct}% dead tuples ({dead:,} dead / {live:,} live)")

    size = raw_row.get("total_size", "")
    if size:
        parts.append(f"table size {size}")

    if not vacuum_info.get("autovacuum_enabled", True):
        parts.append("autovacuum disabled")
    elif vacuum_info.get("hours_since_vacuum") is None:
        parts.append("never vacuumed")
    elif vacuum_info["hours_since_vacuum"] > 24:
        hours = vacuum_info["hours_since_vacuum"]
        parts.append(f"last vacuum {hours:.0f}h ago")

    if vacuum_info.get("xid_risk_level", "OK") != "OK":
        parts.append(f"XID age {vacuum_info['xid_risk_level']}")

    return ", ".join(parts)


def _describe_vacuum_only_problem(vacuum_info: dict) -> str:
    """Describe a table with vacuum issues but no significant bloat yet."""
    parts = ["no significant bloat yet"]

    if not vacuum_info.get("autovacuum_enabled", True):
        parts.append("but autovacuum is disabled")
    if vacuum_info.get("xid_risk_level", "OK") != "OK":
        parts.append(f"XID age at {vacuum_info['xid_risk_level']} level")

    dead = int(vacuum_info.get("dead_tuples", 0))
    if dead > 0:
        parts.append(f"{dead:,} dead tuples accumulating")

    return ", ".join(parts)


def _describe_index_bloat(indexes: list[dict]) -> str:
    """Summarize index bloat as a short note."""
    if not indexes:
        return "no indexes analyzed"

    bloated = [idx for idx in indexes if idx.get("bloat_pct", 0) > 30]
    if not bloated:
        healthy_summary = ", ".join(
            f"{idx['index_name']} ({idx['bloat_pct']:.0f}%)"
            for idx in indexes[:3]
        )
        return f"indexes healthy: {healthy_summary}"

    bloated_summary = ", ".join(
        f"{idx['index_name']} ({idx['bloat_pct']:.0f}% bloat, {idx['index_size']})"
        for idx in bloated
    )
    return f"bloated indexes: {bloated_summary}"


def _build_summary(diagnoses: list[dict]) -> str:
    """Build a one-line summary for quick triage."""
    by_urgency = {"critical": 0, "high": 0, "moderate": 0, "low": 0}
    for d in diagnoses:
        level = d.get("urgency", "low")
        by_urgency[level] = by_urgency.get(level, 0) + 1

    total = len(diagnoses)
    needs_attention = by_urgency["critical"] + by_urgency["high"]

    if needs_attention == 0:
        return f"{total} tables analyzed, all within normal range"

    parts = []
    if by_urgency["critical"]:
        parts.append(f"{by_urgency['critical']} critical")
    if by_urgency["high"]:
        parts.append(f"{by_urgency['high']} high")

    tbl_word = "table" if total == 1 else "tables"
    return f"{total} {tbl_word} analyzed, {needs_attention} need attention ({', '.join(parts)})"