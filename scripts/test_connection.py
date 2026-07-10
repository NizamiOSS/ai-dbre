"""
AI DBRE — Connection & Tool Smoke Test

Run: python scripts/test_connection.py

Verifies that:
1. PostgreSQL is reachable
2. pg_stat_statements extension is active
3. Test data was seeded
4. Each tool returns valid data
"""

import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.db import execute_query, execute_query_single
from agent.config import settings


def test_connection():
    print("1️⃣  Testing database connection...")
    try:
        result = execute_query_single("SELECT current_database(), current_user, version()")
        print(f"   ✅ Connected to: {result['current_database']}")
        print(f"   ✅ User: {result['current_user']}")
        print(f"   ✅ Version: {result['version'][:60]}...")
    except Exception as e:
        print(f"   ❌ Connection failed: {e}")
        print(f"   Config: {settings.pg_host}:{settings.pg_port}/{settings.pg_database}")
        sys.exit(1)


def test_pg_stat_statements():
    print("\n2️⃣  Testing pg_stat_statements...")
    try:
        result = execute_query_single("SELECT count(*) AS stmt_count FROM pg_stat_statements")
        count = result["stmt_count"]
        if count > 0:
            print(f"   ✅ pg_stat_statements active — {count} statements tracked")
        else:
            print(f"   ⚠️  pg_stat_statements has 0 entries (run some queries first)")
    except Exception as e:
        print(f"   ❌ pg_stat_statements not available: {e}")
        print("   Hint: Check shared_preload_libraries in postgresql.conf")


def test_seed_data():
    print("\n3️⃣  Testing seed data...")
    tables = ["customers", "products", "orders", "order_items", "audit_log"]
    for table in tables:
        result = execute_query_single(f"SELECT count(*) AS cnt FROM {table}")
        count = result["cnt"]
        status = "✅" if count > 0 else "❌"
        print(f"   {status} {table}: {count:,} rows")


def test_tools():
    print("\n4️⃣  Testing agent tools...")

    # Test get_slow_queries
    from tools.slow_queries import get_slow_queries
    result = get_slow_queries.invoke({"order_by": "total_time", "min_calls": 1, "limit": 3})
    data = json.loads(result)
    if isinstance(data, list):
        print(f"   ✅ get_slow_queries: returned {len(data)} queries")
    else:
        print(f"   ⚠️  get_slow_queries: unexpected format")

    # Test get_table_stats
    from tools.table_stats import get_table_stats
    result = get_table_stats.invoke({"table_name": "orders"})
    data = json.loads(result)
    if isinstance(data, list) and len(data) > 0:
        print(f"   ✅ get_table_stats: orders table — {data[0].get('live_rows', '?')} rows")
    else:
        print(f"   ⚠️  get_table_stats: {data}")

    # Test explain_query
    from tools.explain_query import explain_query
    result = explain_query.invoke({"query_text": "SELECT count(*) FROM orders", "analyze": True})
    data = json.loads(result)
    if isinstance(data, list) and len(data) > 0:
        print(f"   ✅ explain_query: plan returned successfully")
    else:
        print(f"   ⚠️  explain_query: {str(data)[:80]}")

    # Test get_index_usage
    from tools.table_stats import get_index_usage
    result = get_index_usage.invoke({})
    data = json.loads(result)
    unused = data.get("unused_indexes", [])
    low = data.get("tables_with_low_index_usage", [])
    print(f"   ✅ get_index_usage: {len(unused)} unused indexes, {len(low)} low-index tables")


if __name__ == "__main__":
    print("=" * 60)
    print("  AI DBRE — Smoke Test")
    print("=" * 60)
    print()

    test_connection()
    test_pg_stat_statements()
    test_seed_data()
    test_tools()

    print()
    print("=" * 60)
    print("  All checks complete! Run the workload generator next:")
    print("  python scripts/generate_slow_workload.py")
    print("=" * 60)
