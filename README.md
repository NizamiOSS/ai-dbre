
# AI DBRE — AI Database Reliability Engineer

An autonomous AI agent that detects, analyzes, and remediates performance issues in PostgreSQL databases — with human-in-the-loop approval for all write operations.

Built with **LangGraph** + **Claude Sonnet** + **pg_stat_statements** + **pgstattuple**.

> 🎯 Phase 3: Human-in-the-loop remediation workflows

## Demo

[![asciicast](https://asciinema.org/a/yZg00cNeEJrhPJvu.svg)](https://asciinema.org/a/yZg00cNeEJrhPJvu)

---

## What It Does

You ask a question about your database. The agent investigates autonomously:

```
🔍 You: What are the slowest queries and why?

🤖 AI DBRE:
   🔧 Calling: get_slow_queries...
   🔧 Calling: explain_query...
   🔧 Calling: get_table_stats...

   I found 3 problematic query patterns:

   1. **Sequential scan on audit_log** (500K rows, no index on changed_at)
      - Total time: 4,230ms across 9 calls
      - Root cause: Full table scan — no index on the filtered column
      - Fix: CREATE INDEX idx_audit_log_changed_at ON audit_log(changed_at);

   2. **Filter on orders.status without index**
      ...
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        User (CLI)                                │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                   LangGraph Agent Loop                           │
│                                                                  │
│   ┌─────────┐    ┌──────────────┐    ┌──────────────────────┐   │
│   │  Agent   │───▸│  Diagnostic  │───▸│       Agent          │   │
│   │ (Claude) │    │  Tools (SQL) │    │    (reasons over     │   │
│   │          │    │  [run freely]│    │     evidence)        │   │
│   └────┬─────┘    └──────────────┘    └──────────────────────┘   │
│        │                                                         │
│        │ proposes fix                                            │
│        ▼                                                         │
│   ┌──────────────────────────────────────────────────────┐      │
│   │  ⏸  INTERRUPT — Human Approval Required              │      │
│   │                                                       │      │
│   │  "The agent wants to execute:"                        │      │
│   │   SQL: CREATE INDEX CONCURRENTLY idx_... ON ...        │      │
│   │   Reason: Sequential scans on 333K-row table          │      │
│   │                                                       │      │
│   │   Approve? (y)es / (n)o                               │      │
│   └───────────────────────┬──────────────────────────────┘      │
│                           │                                      │
│               ┌───────────┴───────────┐                         │
│               ▼                       ▼                         │
│          ✅ Approved             ❌ Rejected                    │
│               │                       │                         │
│               ▼                       ▼                         │
│   ┌──────────────────┐   ┌──────────────────────┐              │
│   │  Remediation     │   │  Agent acknowledges   │              │
│   │  Tool executes   │   │  and suggests          │              │
│   │  (dbre_remediation│  │  alternatives          │              │
│   │   DB user)       │   │                        │              │
│   └──────────────────┘   └──────────────────────┘              │
│               │                                                  │
│               ▼                                                  │
│   ┌──────────────────────────────────────────────────────┐      │
│   │  📋 Audit Trail (remediation_audit.jsonl)             │      │
│   │  Every action logged: SQL, reason, status, timestamp  │      │
│   └──────────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────────┘
                           │
           ┌───────────────┴───────────────┐
           ▼                               ▼
┌──────────────────┐           ┌──────────────────────┐
│   PostgreSQL 16  │           │   Tools (11 total)    │
│                  │           │                       │
│  pg_stat_stmts   │           │  Diagnostic (10):     │
│  pg_stat_activity│           │  • get_slow_queries   │
│  pg_stat_tables  │           │  • get_active_queries │
│  pg_locks        │           │  • explain_query      │
│  pgstattuple     │           │  • get_table_stats    │
│                  │           │  • get_index_usage    │
│  Users:          │           │  • get_lock_info      │
│  • dbre_agent    │           │  • get_table_bloat    │
│    (read-only)   │           │  • get_index_bloat    │
│  • dbre_remediation│         │  • get_vacuum_status  │
│    (limited write)│          │  • get_autovac_activity│
│                  │           │                       │
│                  │           │  Remediation (1):      │
│                  │           │  • execute_remediation │
│                  │           │    ⚠️ requires approval │
└──────────────────┘           └──────────────────────┘
```

## Quick Start

### Prerequisites

- Docker & Docker Compose
- Python 3.11+
- Anthropic API key ([get one here](https://console.anthropic.com/))

### 1. Clone and configure

```bash
git clone https://github.com/YOUR_USERNAME/ai-dbre.git
cd ai-dbre

cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY
```

### 2. Start PostgreSQL

```bash
docker compose up -d

# Wait for it to be healthy (seeds ~860K rows, takes ~30 seconds)
docker compose logs -f postgres
```

### 3. Install Python dependencies

```bash
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 4. Verify everything works

```bash
python scripts/test_connection.py
```

You should see all green checkmarks.

### 5. Generate slow query workload

```bash
python scripts/generate_slow_workload.py
```

This runs intentionally bad queries so the agent has something to find.

### 6. Talk to the agent

```bash
python main.py
```

Try these questions:
- "What are the slowest queries in the database?"
- "Are there any unused indexes?"
- "Analyze this query: SELECT * FROM audit_log WHERE table_name = 'orders'"
- "Which tables need vacuuming?"
- "Are there any lock contention issues?"
- "Check for table and index bloat"
- "Is autovacuum keeping up with the workload?"
- "Give me a health check of the database"
- "Find slow queries on audit_log and fix them" *(triggers remediation approval)*
- "Create missing indexes for the worst queries" *(triggers remediation approval)*
- "Run ANALYZE on tables with stale statistics" *(triggers remediation approval)*

### 7. Run proactive health scanner (Phase 2)

```bash
# One-shot health check
python scheduler/runner.py --once

# Continuous scanning every 5 minutes
python scheduler/runner.py --interval 300

# Scan + feed critical alerts to the agent for deep analysis
python scheduler/runner.py --interval 300 --agent
```

## Project Structure

```
ai-dbre/
├── docker-compose.yml              # PostgreSQL 16 test environment
├── pg_config/
│   ├── postgresql.conf             # PG config (pg_stat_statements, logging)
│   └── init.sql                    # Schema, seed data, agent + remediation users
├── agent/
│   ├── config.py                   # Settings from .env
│   ├── db.py                       # Read-only + remediation DB connections
│   ├── state.py                    # LangGraph state schema
│   ├── prompts.py                  # System prompt (DBA + remediation expertise)
│   ├── graph.py                    # LangGraph graph (two-tier tools + interrupt)
│   └── health_check.py             # Threshold-based health check engine
├── tools/
│   ├── slow_queries.py             # pg_stat_statements + pg_stat_activity
│   ├── explain_query.py            # EXPLAIN ANALYZE (SELECT only)
│   ├── table_stats.py              # Table health + index usage
│   ├── lock_info.py                # Lock contention detection (pg_locks)
│   ├── bloat_detection.py          # Table + index bloat (pgstattuple)
│   ├── vacuum_monitor.py           # Autovacuum health + vacuum status
│   └── remediation.py              # Whitelisted write ops (requires approval)
├── scheduler/
│   └── runner.py                   # Proactive health check scheduler
├── scripts/
│   ├── generate_slow_workload.py   # Create bad queries for testing
│   └── test_connection.py          # Smoke test DB + tools
├── main.py                         # Interactive CLI with approval flow
├── remediation_audit.jsonl         # Auto-generated audit trail
├── requirements.txt
├── .env.example
├── TROUBLESHOOTING.md              # Setup issues and fixes
└── README.md
```

## Safety

Two-tier security model with separate database users:

**Diagnostic tools** connect as `dbre_agent` (read-only). Cannot modify anything.

**Remediation tool** connects as `dbre_remediation` (limited write). Can only:
- CREATE INDEX / CREATE INDEX CONCURRENTLY
- ANALYZE
- VACUUM / VACUUM FREEZE / VACUUM FULL
- REINDEX CONCURRENTLY

Cannot DROP, DELETE, UPDATE, INSERT, ALTER, or TRUNCATE — these are blocked at both
the SQL whitelist level (in `tools/remediation.py`) and the PostgreSQL permission level.

**Human-in-the-loop**: The LangGraph graph uses `interrupt_before` on the remediation
node. Every write operation pauses execution, displays the exact SQL to the operator,
and waits for explicit `y/n` approval before running.

**Audit trail**: Every proposed and executed action is logged to `remediation_audit.jsonl`
with timestamp, SQL, reason, and status (SUCCESS/REJECTED/BLOCKED/ERROR).

## Roadmap

- [x] Phase 1 — Slow query detection, EXPLAIN analysis, lock contention diagnosis
- [x] Phase 2 — Bloat detection, vacuum monitoring, proactive scanning
- [x] Phase 3 — Human-in-the-loop remediation workflows
- [ ] Phase 4 — Streamlit dashboard + open-source release

## Tech Stack

| Component | Choice | Why |
|-----------|--------|-----|
| Agent framework | LangGraph 1.2.x | Stateful graphs, ReAct loops, HITL support |
| LLM | Claude Sonnet 4.6 | Best cost/quality for tool-calling agents |
| Database | PostgreSQL 16 | Industry standard, rich pg_stat ecosystem |
| Language | Python 3.11+ | LangGraph native, psycopg2/asyncpg ecosystem |

## License

MIT
