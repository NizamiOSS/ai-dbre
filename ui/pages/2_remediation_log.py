"""
AI DBRE — Remediation Log Page

Displays the audit trail from remediation_audit.jsonl.
Shows every proposed, approved, rejected, and errored action.
"""

import sys
import os
import json
from pathlib import Path
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import streamlit as st

st.set_page_config(page_title="AI DBRE — Remediation Log", page_icon="📋", layout="wide")

st.title("📋 Remediation Log")
st.caption("Audit trail of all remediation actions")

# =============================================================================
# Load Audit Log
# =============================================================================

AUDIT_LOG_PATH = Path("remediation_audit.jsonl")

# Also check in project root if running from ui/
if not AUDIT_LOG_PATH.exists():
    AUDIT_LOG_PATH = Path(__file__).parent.parent.parent / "remediation_audit.jsonl"


def load_audit_log():
    entries = []
    if not AUDIT_LOG_PATH.exists():
        return entries

    with open(AUDIT_LOG_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    # Reverse chronological order
    entries.reverse()
    return entries


entries = load_audit_log()

if not entries:
    st.info(
        "No remediation actions recorded yet.\n\n"
        "Ask the agent to fix something: *\"Find slow queries on audit_log and fix them\"*"
    )
    st.stop()

# =============================================================================
# Summary Metrics
# =============================================================================

total = len(entries)
success = sum(1 for e in entries if e.get("status") == "SUCCESS")
errors = sum(1 for e in entries if e.get("status") == "ERROR")
blocked = sum(1 for e in entries if e.get("status") == "BLOCKED")
rejected = sum(1 for e in entries if e.get("status") == "REJECTED")

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Total Actions", total)
col2.metric("Successful", success)
col3.metric("Errors", errors)
col4.metric("Blocked", blocked)
col5.metric("Rejected", rejected)

st.divider()

# =============================================================================
# Filter
# =============================================================================

status_filter = st.multiselect(
    "Filter by status",
    ["SUCCESS", "ERROR", "BLOCKED", "REJECTED"],
    default=["SUCCESS", "ERROR", "BLOCKED", "REJECTED"],
)

filtered = [e for e in entries if e.get("status") in status_filter]

# =============================================================================
# Timeline
# =============================================================================

st.subheader(f"Actions ({len(filtered)})")

for entry in filtered:
    status = entry.get("status", "UNKNOWN")
    sql = entry.get("sql", "N/A")
    reason = entry.get("reason", "No reason provided")
    timestamp = entry.get("timestamp", "")
    operation = entry.get("operation", "")
    risk = entry.get("risk_level", "")
    exec_time = entry.get("execution_time_seconds", "")
    error_msg = entry.get("error", "")

    # Status styling
    status_config = {
        "SUCCESS": ("✅", "green"),
        "ERROR": ("❌", "red"),
        "BLOCKED": ("🚫", "orange"),
        "REJECTED": ("⛔", "gray"),
    }
    icon, color = status_config.get(status, ("❓", "gray"))

    # Format timestamp
    try:
        dt = datetime.fromisoformat(timestamp)
        time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        time_str = timestamp

    with st.expander(f"{icon} **{status}** — `{operation or sql[:50]}` — {time_str}"):
        st.markdown(f"**Status:** {status}")
        st.markdown(f"**Timestamp:** {time_str}")

        if operation:
            st.markdown(f"**Operation:** {operation}")
        if risk:
            st.markdown(f"**Risk Level:** {risk}")

        st.markdown("**SQL:**")
        st.code(sql, language="sql")

        st.markdown("**Reason:**")
        st.markdown(reason)

        if exec_time:
            st.markdown(f"**Execution Time:** {exec_time}s")

        if error_msg:
            st.error(f"**Error:** {error_msg}")
