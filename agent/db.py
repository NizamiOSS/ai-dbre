"""
AI DBRE — Database Connection Helper
Provides a safe, read-only connection pool for agent tools.
"""

import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from typing import Any

from agent.config import settings


@contextmanager
def get_connection():
    """Get a read-only database connection."""
    conn = psycopg2.connect(settings.pg_dsn)
    conn.set_session(readonly=True, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def get_remediation_connection():
    """
    Get a write-capable database connection for approved remediation actions.
    Uses a separate user (dbre_remediation) with limited privileges.
    ONLY used by the remediation tool after human approval.
    """
    conn = psycopg2.connect(settings.pg_remediation_dsn)
    conn.set_session(autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


def execute_query(query: str, params: tuple | None = None) -> list[dict[str, Any]]:
    """
    Execute a read-only query and return results as list of dicts.
    This is the primary interface for all agent tools.
    """
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]


def execute_query_single(query: str, params: tuple | None = None) -> dict[str, Any] | None:
    """Execute a query and return a single row or None."""
    results = execute_query(query, params)
    return results[0] if results else None
