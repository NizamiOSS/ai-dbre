"""
AI DBRE — Slow Workload Generator

Runs intentionally bad queries against the test database to create
realistic slow query patterns for the agent to detect and diagnose.

Run: python scripts/generate_slow_workload.py

This simulates common real-world problems:
1. Sequential scans on large unindexed tables
2. Missing index on filter columns
3. Function-in-WHERE preventing index usage
4. N+1 query pattern (many small queries)
5. Cartesian join (accidental cross join)
6. Large sort without index support
"""

import time
import random
import psycopg2
import sys
import os

# Add parent dir to path so we can import agent.config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import settings


def get_admin_connection():
    """Connect as admin user (can run more complex queries)."""
    return psycopg2.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        dbname=settings.pg_database,
        user="dbre_admin",
        password="dbre_secret",
    )


SLOW_QUERIES = [
    {
        "name": "Sequential scan on large unindexed table (audit_log)",
        "query": """
            SELECT count(*), table_name, action
            FROM audit_log
            WHERE changed_at > NOW() - interval '30 days'
            GROUP BY table_name, action
            ORDER BY count(*) DESC
        """,
    },
    {
        "name": "Missing index — filter on orders.status",
        "query": """
            SELECT o.id, o.total_amount, c.full_name
            FROM orders o
            JOIN customers c ON o.customer_id = c.id
            WHERE o.status = 'pending'
              AND o.order_date > NOW() - interval '90 days'
            ORDER BY o.total_amount DESC
            LIMIT 50
        """,
    },
    {
        "name": "Function in WHERE — prevents index usage",
        "query": """
            SELECT id, order_date, total_amount
            FROM orders
            WHERE EXTRACT(YEAR FROM order_date) = 2025
              AND EXTRACT(MONTH FROM order_date) = 6
        """,
    },
    {
        "name": "Missing index — filter on customers.country",
        "query": """
            SELECT c.country, count(*) AS order_count, sum(o.total_amount) AS revenue
            FROM customers c
            JOIN orders o ON o.customer_id = c.id
            WHERE c.country = 'AZ'
            GROUP BY c.country
        """,
    },
    {
        "name": "Large sort without index — orders by date range",
        "query": """
            SELECT o.id, o.order_date, o.total_amount, o.status,
                   c.full_name, c.email
            FROM orders o
            JOIN customers c ON c.id = o.customer_id
            WHERE o.shipping_country IN ('US', 'UK', 'DE')
            ORDER BY o.order_date DESC
            LIMIT 1000
        """,
    },
    {
        "name": "Subquery with sequential scan",
        "query": """
            SELECT *
            FROM audit_log
            WHERE record_id IN (
                SELECT id FROM orders WHERE status = 'cancelled'
            )
            AND table_name = 'orders'
            ORDER BY changed_at DESC
            LIMIT 100
        """,
    },
    {
        "name": "Aggregation on unindexed JSONB column",
        "query": """
            SELECT
                new_values->>'status' AS new_status,
                count(*) AS change_count
            FROM audit_log
            WHERE action = 'UPDATE'
              AND table_name = 'orders'
            GROUP BY new_values->>'status'
            ORDER BY change_count DESC
        """,
    },
]

# N+1 pattern: many small queries in a loop
N_PLUS_ONE_QUERY = """
    SELECT oi.quantity, oi.unit_price, p.name, p.category
    FROM order_items oi
    JOIN products p ON p.id = oi.product_id
    WHERE oi.order_id = %s
"""


def run_workload(rounds: int = 3, pause: float = 0.5):
    """Generate slow query workload."""
    conn = get_admin_connection()
    conn.set_session(autocommit=True)
    cur = conn.cursor()

    print("🔥 Starting slow workload generation...")
    print(f"   Rounds: {rounds} | Pause between queries: {pause}s\n")

    for round_num in range(1, rounds + 1):
        print(f"── Round {round_num}/{rounds} ──")

        # Run each intentionally slow query
        for sq in SLOW_QUERIES:
            print(f"   ▸ {sq['name']}", end="", flush=True)
            start = time.time()
            try:
                cur.execute(sq["query"])
                cur.fetchall()
                elapsed = time.time() - start
                print(f" — {elapsed:.2f}s")
            except Exception as e:
                print(f" — ERROR: {e}")
            time.sleep(pause)

        # Simulate N+1 pattern (50 individual queries)
        print(f"   ▸ N+1 pattern (50 individual order lookups)", end="", flush=True)
        start = time.time()
        for _ in range(50):
            order_id = random.randint(1, 100000)
            try:
                cur.execute(N_PLUS_ONE_QUERY, (order_id,))
                cur.fetchall()
            except Exception:
                pass
        elapsed = time.time() - start
        print(f" — {elapsed:.2f}s total")

        print()
        time.sleep(1)

    cur.close()
    conn.close()
    print("✅ Workload generation complete.")
    print("   Now run the agent and ask: 'What are the slowest queries?'")


if __name__ == "__main__":
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    run_workload(rounds=rounds)
