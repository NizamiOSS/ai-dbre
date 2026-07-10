"""
AI DBRE — Proactive Health Check Engine

Defines thresholds for database health metrics and runs automated
scans that trigger the agent when problems are detected.

This is the brain of proactive monitoring — instead of waiting for
a human to ask "what's wrong?", the system periodically checks
metrics against thresholds and generates alerts.
"""

import json
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum

from agent.db import execute_query


class Severity(str, Enum):
    OK = "OK"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass
class HealthAlert:
    """A single health check finding."""
    check_name: str
    severity: Severity
    table_name: str | None
    message: str
    metric_value: float | str
    threshold: float | str
    recommendation: str

    def to_dict(self) -> dict:
        return {
            "check_name": self.check_name,
            "severity": self.severity.value,
            "table_name": self.table_name,
            "message": self.message,
            "metric_value": str(self.metric_value),
            "threshold": str(self.threshold),
            "recommendation": self.recommendation,
        }


@dataclass
class HealthCheckConfig:
    """Configurable thresholds for health checks."""
    # Cache hit ratio
    cache_hit_warning_pct: float = 95.0
    cache_hit_critical_pct: float = 90.0

    # Dead tuple ratio
    dead_tuple_warning_pct: float = 10.0
    dead_tuple_critical_pct: float = 20.0

    # Slow queries
    slow_query_warning_ms: float = 1000.0
    slow_query_critical_ms: float = 5000.0

    # Table sequential scan ratio (for tables > 10K rows)
    seq_scan_warning_pct: float = 70.0
    seq_scan_critical_pct: float = 90.0

    # Hours since last vacuum
    vacuum_age_warning_hours: float = 24.0
    vacuum_age_critical_hours: float = 72.0

    # Transaction ID age (wraparound)
    xid_age_warning: int = 500_000_000
    xid_age_critical: int = 1_000_000_000

    # Long-running queries
    long_query_warning_seconds: int = 30
    long_query_critical_seconds: int = 120


def run_health_check(config: HealthCheckConfig | None = None) -> dict:
    """
    Run all health checks and return a structured report.

    Returns a dict with:
    - timestamp: when the check ran
    - overall_status: worst severity across all checks
    - alerts: list of all findings (WARNING and CRITICAL)
    - summary: counts by severity
    - checks_passed: number of checks that returned OK
    """
    if config is None:
        config = HealthCheckConfig()

    alerts: list[HealthAlert] = []

    # ── Check 1: Cache hit ratio ──
    alerts.extend(_check_cache_hit_ratio(config))

    # ── Check 2: Dead tuples / bloat ──
    alerts.extend(_check_dead_tuples(config))

    # ── Check 3: Slow queries ──
    alerts.extend(_check_slow_queries(config))

    # ── Check 4: Sequential scan ratio ──
    alerts.extend(_check_seq_scan_ratio(config))

    # ── Check 5: Vacuum age ──
    alerts.extend(_check_vacuum_age(config))

    # ── Check 6: Transaction ID age ──
    alerts.extend(_check_xid_age(config))

    # ── Check 7: Long-running queries ──
    alerts.extend(_check_long_queries(config))

    # ── Check 8: Unused indexes ──
    alerts.extend(_check_unused_indexes())

    # Determine overall status
    severities = [a.severity for a in alerts]
    if Severity.CRITICAL in severities:
        overall = Severity.CRITICAL
    elif Severity.WARNING in severities:
        overall = Severity.WARNING
    else:
        overall = Severity.OK

    critical_count = sum(1 for a in alerts if a.severity == Severity.CRITICAL)
    warning_count = sum(1 for a in alerts if a.severity == Severity.WARNING)

    return {
        "timestamp": datetime.now().isoformat(),
        "overall_status": overall.value,
        "summary": {
            "critical": critical_count,
            "warning": warning_count,
            "total_alerts": len(alerts),
        },
        "alerts": [a.to_dict() for a in alerts],
    }


# =============================================================================
# Individual Health Checks
# =============================================================================


def _check_cache_hit_ratio(config: HealthCheckConfig) -> list[HealthAlert]:
    """Check cache hit ratio per table."""
    alerts = []
    query = """
    SELECT
        relname AS table_name,
        CASE
            WHEN heap_blks_hit + heap_blks_read > 0
            THEN round(100.0 * heap_blks_hit / (heap_blks_hit + heap_blks_read), 2)
            ELSE 100.0
        END AS cache_hit_pct,
        heap_blks_hit + heap_blks_read AS total_blocks
    FROM pg_statio_user_tables
    WHERE heap_blks_hit + heap_blks_read > 100  -- Only tables with meaningful I/O
    ORDER BY cache_hit_pct ASC
    """
    for row in execute_query(query):
        pct = float(row["cache_hit_pct"])
        table = row["table_name"]

        if pct < config.cache_hit_critical_pct:
            alerts.append(HealthAlert(
                check_name="cache_hit_ratio",
                severity=Severity.CRITICAL,
                table_name=table,
                message=f"Cache hit ratio is {pct}% — most reads are going to disk",
                metric_value=pct,
                threshold=config.cache_hit_critical_pct,
                recommendation=f"Investigate access patterns on '{table}'. Consider adding indexes to reduce scans or increasing shared_buffers.",
            ))
        elif pct < config.cache_hit_warning_pct:
            alerts.append(HealthAlert(
                check_name="cache_hit_ratio",
                severity=Severity.WARNING,
                table_name=table,
                message=f"Cache hit ratio is {pct}% — below healthy threshold",
                metric_value=pct,
                threshold=config.cache_hit_warning_pct,
                recommendation=f"Monitor '{table}' access patterns. May need index optimization.",
            ))
    return alerts


def _check_dead_tuples(config: HealthCheckConfig) -> list[HealthAlert]:
    """Check dead tuple ratio per table."""
    alerts = []
    query = """
    SELECT
        relname AS table_name,
        n_live_tup AS live_tuples,
        n_dead_tup AS dead_tuples,
        CASE
            WHEN n_live_tup + n_dead_tup > 0
            THEN round(100.0 * n_dead_tup / (n_live_tup + n_dead_tup), 2)
            ELSE 0
        END AS dead_pct
    FROM pg_stat_user_tables
    WHERE n_dead_tup > 100  -- Only tables with meaningful dead tuples
    ORDER BY dead_pct DESC
    """
    for row in execute_query(query):
        pct = float(row["dead_pct"])
        table = row["table_name"]
        dead = row["dead_tuples"]

        if pct >= config.dead_tuple_critical_pct:
            alerts.append(HealthAlert(
                check_name="dead_tuples",
                severity=Severity.CRITICAL,
                table_name=table,
                message=f"{dead:,} dead tuples ({pct}%) — severe bloat",
                metric_value=pct,
                threshold=config.dead_tuple_critical_pct,
                recommendation=f"Run VACUUM (VERBOSE) on '{table}'. If ratio stays high, consider VACUUM FULL during maintenance window.",
            ))
        elif pct >= config.dead_tuple_warning_pct:
            alerts.append(HealthAlert(
                check_name="dead_tuples",
                severity=Severity.WARNING,
                table_name=table,
                message=f"{dead:,} dead tuples ({pct}%) — autovacuum may be falling behind",
                metric_value=pct,
                threshold=config.dead_tuple_warning_pct,
                recommendation=f"Check autovacuum settings for '{table}'. Consider lowering autovacuum_vacuum_scale_factor.",
            ))
    return alerts


def _check_slow_queries(config: HealthCheckConfig) -> list[HealthAlert]:
    """Check for queries exceeding time thresholds."""
    alerts = []
    query = """
    SELECT
        substring(query, 1, 100) AS query_preview,
        calls,
        round(mean_exec_time::numeric, 2) AS mean_time_ms,
        round(max_exec_time::numeric, 2) AS max_time_ms,
        round(total_exec_time::numeric, 2) AS total_time_ms
    FROM pg_stat_statements
    WHERE
        calls >= 3
        AND query NOT LIKE '%%pg_stat%%'
        AND query NOT LIKE '%%pg_catalog%%'
        AND dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
    ORDER BY mean_exec_time DESC
    LIMIT 20
    """
    for row in execute_query(query):
        mean_ms = float(row["mean_time_ms"])
        preview = row["query_preview"]

        if mean_ms >= config.slow_query_critical_ms:
            alerts.append(HealthAlert(
                check_name="slow_queries",
                severity=Severity.CRITICAL,
                table_name=None,
                message=f"Query averaging {mean_ms}ms: {preview}",
                metric_value=mean_ms,
                threshold=config.slow_query_critical_ms,
                recommendation="Run EXPLAIN ANALYZE on this query to identify the bottleneck.",
            ))
        elif mean_ms >= config.slow_query_warning_ms:
            alerts.append(HealthAlert(
                check_name="slow_queries",
                severity=Severity.WARNING,
                table_name=None,
                message=f"Query averaging {mean_ms}ms: {preview}",
                metric_value=mean_ms,
                threshold=config.slow_query_warning_ms,
                recommendation="Investigate query execution plan — possible missing index or stale statistics.",
            ))
    return alerts


def _check_seq_scan_ratio(config: HealthCheckConfig) -> list[HealthAlert]:
    """Check for large tables relying heavily on sequential scans."""
    alerts = []
    query = """
    SELECT
        relname AS table_name,
        seq_scan,
        COALESCE(idx_scan, 0) AS idx_scan,
        n_live_tup AS live_rows,
        CASE
            WHEN seq_scan + COALESCE(idx_scan, 0) > 0
            THEN round(100.0 * seq_scan / (seq_scan + COALESCE(idx_scan, 0)), 2)
            ELSE 0
        END AS seq_scan_pct
    FROM pg_stat_user_tables
    WHERE
        n_live_tup > 10000
        AND seq_scan + COALESCE(idx_scan, 0) > 10
    ORDER BY seq_scan_pct DESC
    """
    for row in execute_query(query):
        pct = float(row["seq_scan_pct"])
        table = row["table_name"]

        if pct >= config.seq_scan_critical_pct:
            alerts.append(HealthAlert(
                check_name="sequential_scans",
                severity=Severity.CRITICAL,
                table_name=table,
                message=f"{pct}% of scans are sequential on {row['live_rows']:,}-row table",
                metric_value=pct,
                threshold=config.seq_scan_critical_pct,
                recommendation=f"Table '{table}' is almost entirely seq-scanned. Identify common WHERE/JOIN columns and add indexes.",
            ))
        elif pct >= config.seq_scan_warning_pct:
            alerts.append(HealthAlert(
                check_name="sequential_scans",
                severity=Severity.WARNING,
                table_name=table,
                message=f"{pct}% of scans are sequential on {row['live_rows']:,}-row table",
                metric_value=pct,
                threshold=config.seq_scan_warning_pct,
                recommendation=f"Review query patterns on '{table}' — indexes may improve performance.",
            ))
    return alerts


def _check_vacuum_age(config: HealthCheckConfig) -> list[HealthAlert]:
    """Check for tables that haven't been vacuumed recently."""
    alerts = []
    query = """
    SELECT
        relname AS table_name,
        n_dead_tup AS dead_tuples,
        last_vacuum,
        last_autovacuum,
        GREATEST(last_vacuum, last_autovacuum) AS last_any_vacuum,
        round(EXTRACT(EPOCH FROM (
            now() - GREATEST(last_vacuum, last_autovacuum)
        )) / 3600.0, 1) AS hours_since_vacuum
    FROM pg_stat_user_tables
    WHERE
        schemaname = 'public'
        AND n_live_tup > 1000
    ORDER BY hours_since_vacuum DESC NULLS FIRST
    """
    for row in execute_query(query):
        hours = row["hours_since_vacuum"]
        table = row["table_name"]

        if hours is None:
            alerts.append(HealthAlert(
                check_name="vacuum_age",
                severity=Severity.WARNING,
                table_name=table,
                message="Table has NEVER been vacuumed",
                metric_value="never",
                threshold=f"{config.vacuum_age_warning_hours}h",
                recommendation=f"Run VACUUM ANALYZE on '{table}'. Check if autovacuum is enabled.",
            ))
        elif float(hours) >= config.vacuum_age_critical_hours:
            alerts.append(HealthAlert(
                check_name="vacuum_age",
                severity=Severity.CRITICAL,
                table_name=table,
                message=f"Last vacuum was {hours} hours ago",
                metric_value=float(hours),
                threshold=config.vacuum_age_critical_hours,
                recommendation=f"Run VACUUM on '{table}' immediately. Check autovacuum_naptime and worker count.",
            ))
        elif float(hours) >= config.vacuum_age_warning_hours:
            alerts.append(HealthAlert(
                check_name="vacuum_age",
                severity=Severity.WARNING,
                table_name=table,
                message=f"Last vacuum was {hours} hours ago",
                metric_value=float(hours),
                threshold=config.vacuum_age_warning_hours,
                recommendation=f"Monitor '{table}' — vacuum is aging. May need autovacuum tuning.",
            ))
    return alerts


def _check_xid_age(config: HealthCheckConfig) -> list[HealthAlert]:
    """Check transaction ID age for wraparound risk."""
    alerts = []
    query = """
    SELECT
        c.relname AS table_name,
        age(c.relfrozenxid) AS xid_age,
        pg_size_pretty(pg_total_relation_size(c.oid)) AS table_size
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE
        c.relkind = 'r'
        AND n.nspname = 'public'
    ORDER BY age(c.relfrozenxid) DESC
    """
    for row in execute_query(query):
        xid = int(row["xid_age"])
        table = row["table_name"]

        if xid >= config.xid_age_critical:
            alerts.append(HealthAlert(
                check_name="xid_wraparound",
                severity=Severity.CRITICAL,
                table_name=table,
                message=f"Transaction ID age is {xid:,} — approaching wraparound limit",
                metric_value=xid,
                threshold=config.xid_age_critical,
                recommendation=f"URGENT: Run VACUUM FREEZE on '{table}' immediately to prevent wraparound shutdown.",
            ))
        elif xid >= config.xid_age_warning:
            alerts.append(HealthAlert(
                check_name="xid_wraparound",
                severity=Severity.WARNING,
                table_name=table,
                message=f"Transaction ID age is {xid:,}",
                metric_value=xid,
                threshold=config.xid_age_warning,
                recommendation=f"Schedule VACUUM FREEZE on '{table}' during next maintenance window.",
            ))
    return alerts


def _check_long_queries(config: HealthCheckConfig) -> list[HealthAlert]:
    """Check for currently running long queries."""
    alerts = []
    query = """
    SELECT
        pid,
        usename AS username,
        substring(query, 1, 100) AS query_preview,
        state,
        round(EXTRACT(EPOCH FROM (now() - query_start))::numeric, 2) AS duration_seconds
    FROM pg_stat_activity
    WHERE
        state != 'idle'
        AND pid != pg_backend_pid()
        AND query NOT LIKE '%%pg_stat%%'
        AND EXTRACT(EPOCH FROM (now() - query_start)) > %(threshold)s
    ORDER BY query_start ASC
    """
    for row in execute_query(query, {"threshold": config.long_query_warning_seconds}):
        seconds = float(row["duration_seconds"])

        severity = Severity.CRITICAL if seconds >= config.long_query_critical_seconds else Severity.WARNING
        alerts.append(HealthAlert(
            check_name="long_running_queries",
            severity=severity,
            table_name=None,
            message=f"PID {row['pid']} running for {seconds}s: {row['query_preview']}",
            metric_value=seconds,
            threshold=config.long_query_critical_seconds if severity == Severity.CRITICAL else config.long_query_warning_seconds,
            recommendation="Investigate query — may need EXPLAIN ANALYZE or cancellation.",
        ))
    return alerts


def _check_unused_indexes() -> list[HealthAlert]:
    """Check for unused indexes wasting disk space."""
    alerts = []
    query = """
    SELECT
        s.relname AS table_name,
        s.indexrelname AS index_name,
        pg_size_pretty(pg_relation_size(s.indexrelid)) AS index_size,
        pg_relation_size(s.indexrelid) AS index_size_bytes
    FROM pg_stat_user_indexes s
    JOIN pg_index i ON s.indexrelid = i.indexrelid
    WHERE
        s.idx_scan = 0
        AND NOT i.indisprimary
        AND NOT i.indisunique
        AND pg_relation_size(s.indexrelid) > 65536  -- > 64KB
    ORDER BY pg_relation_size(s.indexrelid) DESC
    """
    for row in execute_query(query):
        alerts.append(HealthAlert(
            check_name="unused_indexes",
            severity=Severity.WARNING,
            table_name=row["table_name"],
            message=f"Index '{row['index_name']}' ({row['index_size']}) has never been used",
            metric_value=row["index_name"],
            threshold="0 scans",
            recommendation=f"Consider dropping '{row['index_name']}' — it slows down writes without benefiting reads.",
        ))
    return alerts
