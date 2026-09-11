# AI DBRE — AI Database Reliability Engineer

An autonomous AI agent that detects, analyzes, and remediates performance issues in PostgreSQL databases — with human-in-the-loop approval for all write operations.

Built with **LangGraph** + **Claude** + **pg_stat_statements** + **pgstattuple**.

> 🎯 Phase 4: Streamlit dashboard, GitHub Actions CI, open-source release


---

## What It Does

You ask a question about your database. The agent investigates autonomously:

```
🔍 You: What are the slowest queries and why?

🤖 AI DBRE:
   🔧 Calling: diagnose_slow_queries...

   3 queries analyzed: 2 missing_index, 1 n_plus_one

   1. eval_orders_missing_idx — missing_index (critical)
      Problem: sequential scan on eval_orders_missing_idx,
               filtering on: customer_id
      Fix: CREATE INDEX on eval_orders_missing_idx (customer_id)

   2. eval_n_plus_one — n_plus_one
      Problem: called 1,000 times (0.5ms avg) — likely N+1 pattern
      Fix: batch queries or use eager loading to reduce call count

   3. ...
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
│   ┌──────────────────────────────────────────────────────────┐  │
│   │  ⏸  INTERRUPT — Human Approval Required                  │  │
│   │                                                           │  │
│   │  "The agent wants to execute:"                            │  │
│   │   SQL: CREATE INDEX CONCURRENTLY idx_... ON ...            │  │
│   │   Reason: Sequential scans on 333K-row table              │  │
│   │                                                           │  │
│   │   Approve? (y)es / (n)o                                   │  │
│   └───────────────────────┬──────────────────────────────────┘  │
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
│   ┌──────────────────────────────────────────────────────────┐  │
│   │  📋 Audit Trail (remediation_audit.jsonl)                 │  │
│   │  Every action logged: SQL, reason, status, timestamp      │  │
│   └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                           │
           ┌───────────────┴───────────────┐
           ▼                               ▼
┌──────────────────┐           ┌───────────────────────────┐
│   PostgreSQL 16  │           │   Tools (13 total)         │
│                  │           │                            │
│  pg_stat_stmts   │           │  Composite (2):            │
│  pg_stat_activity│           │  • diagnose_bloat          │
│  pg_stat_tables  │           │  • diagnose_slow_queries   │
│  pg_locks        │           │                            │
│  pgstattuple     │           │  Diagnostic (10):          │
│                  │           │  • get_slow_queries        │
│  Users:          │           │  • get_active_queries      │
│  • dbre_agent    │           │  • explain_query           │
│    (read-only)   │           │  • get_table_stats         │
│  • dbre_remediation│         │  • get_index_usage         │
│    (limited write)│          │  • get_lock_info           │
│                  │           │  • get_table_bloat         │
│                  │           │  • get_index_bloat         │
│                  │           │  • get_vacuum_status       │
│                  │           │  • get_autovac_activity    │
│                  │           │                            │
│                  │           │  Remediation (1):           │
│                  │           │  • execute_remediation      │
│                  │           │    ⚠️ requires approval      │
└──────────────────┘           └───────────────────────────┘
```

## Composite Diagnostic Tools

The agent includes two **composite tools** that encode DBA judgment in deterministic
Python instead of re-deriving it via LLM reasoning on every request. These reduce
token usage by ~60% and cut diagnostic latency by ~40% compared to the agent
chaining primitive tools manually.

**`diagnose_bloat`** — cross-references table bloat, index bloat, and vacuum status
in a single call. Returns a pre-assessed urgency level (critical/high/moderate/low)
and recommended actions. Internally calls `get_table_bloat`, `get_index_bloat`, and
`get_vacuum_status`, applies threshold-based rules (e.g., "high dead tuple ratio +
autovacuum disabled = critical"), and returns a compact verdict instead of raw data.

**`diagnose_slow_queries`** — pipelines slow query detection through EXPLAIN plan
analysis. Pulls top offenders from `pg_stat_statements`, deparameterizes `$1`/`$2`
placeholders, runs EXPLAIN to inspect plan nodes, and classifies each query's root
cause: `missing_index`, `n_plus_one`, `stale_statistics`, or `memory_pressure`.

Both composites and their underlying primitives remain available — the agent picks
whichever fits the question. Broad health checks use the composite; targeted
single-metric queries use the primitive directly.

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

### 8. Launch the dashboard (Phase 4)

```bash
streamlit run ui/app.py
```

Opens a web UI at `http://localhost:8501` with four pages:
- **Health Overview** — real-time metrics, alerts, per-table health
- **Query Explorer** — browse slow queries, run EXPLAIN ANALYZE with one click
- **Remediation Log** — audit trail of all proposed/executed actions
- **Agent Chat** — web-based agent chat with approval flow for remediation

## MCP Server

AI DBRE exposes its diagnostic tools and live database state via the
[Model Context Protocol](https://modelcontextprotocol.io), so any MCP-compatible
client — Claude Desktop, Cursor, or a custom agent — can use them directly. The
MCP server delegates to the same tool functions the LangGraph agent uses, so
there's a single source of truth for all diagnostic logic.

**Tools (12)** — the full diagnostic toolset: two composite diagnostic tools
(`diagnose_bloat`, `diagnose_slow_queries`), plus ten primitives covering slow
queries, active long queries, EXPLAIN analysis, table stats, index usage, lock
info, table bloat, index bloat, vacuum status, and autovacuum activity.

**Resources** — live database state that a client can read as context before
asking a question:

| Resource URI | Returns |
|--------------|---------|
| `dbre://health/summary` | All active alerts from the 8 automated health checks |
| `dbre://stats/tables` | Table-level statistics for all user tables |
| `dbre://stats/indexes` | Index usage statistics |
| `dbre://stats/vacuum` | Vacuum and autovacuum status |
| `dbre://stats/bloat` | Table bloat levels |
| `dbre://tables/{table}/stats` | Stats for a specific table |
| `dbre://tables/{table}/indexes` | Index usage for a specific table |
| `dbre://tables/{table}/bloat` | Bloat analysis for a specific table |
| `dbre://tables/{table}/vacuum` | Vacuum status for a specific table |

### Testing with MCP Inspector

```bash
mcp dev mcp_server/server.py
```

Opens a browser UI. Set **Command** to `python`, **Arguments** to
`mcp_server/server.py`, and connect. The Tools and Resources tabs let you call
each one manually.

### Connecting to Claude Desktop (STDIO)

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`
(use absolute paths — Claude Desktop doesn't know your venv or working directory):

```json
{
  "mcpServers": {
    "ai-dbre": {
      "command": "/absolute/path/to/ai-dbre/.venv/bin/python",
      "args": ["/absolute/path/to/ai-dbre/mcp_server/server.py"]
    }
  }
}
```

Credentials are read from `.env` automatically. Restart Claude Desktop, then ask
about your database — Claude will call the tools and read the health resources as
needed.

### Remote Access (StreamableHTTP)

For team access or integration with remote clients, run the server in HTTP mode:

```bash
# Default: localhost:8000
python mcp_server/server.py --http

# Custom host/port (e.g. accessible from the network)
python mcp_server/server.py --http --host 0.0.0.0 --port 9000
```

The server listens at `http://<host>:<port>/mcp`. Same tools and resources,
accessible over the network instead of local stdio.

## Evaluation Suite

The eval harness validates diagnostic accuracy against known problems. Each
scenario is defined declaratively in YAML — induce a problem, run the agent,
score the diagnosis, clean up. Eight scenarios ship today: missing indexes,
table bloat, vacuum starvation, unused indexes, stale statistics, index bloat,
high-frequency N+1 queries, and duplicate indexes.

```bash
# Run all scenarios (deterministic scoring)
pytest tests/eval/ -v

# With stdout visible (see which tools were called per scenario)
pytest tests/eval/ -v -s

# Include LLM-as-judge semantic scoring (one extra API call per scenario)
pytest tests/eval/ -v --llm-judge

# Run a single scenario
pytest tests/eval/ -v -k missing_index
```

Scoring combines three layers: tool-call verification (did the agent call the
right diagnostic tools?), keyword matching against the expected root cause and
affected objects, and optional LLM-as-judge semantic scoring. Each scenario gets
a weighted composite score; the pass threshold is 70%.

Scenarios use `tools_must_call_any` for flexible tool verification — the agent
can use either a composite tool or its underlying primitives and the eval accepts
both paths to the same correct diagnosis.

Adding a new scenario is just a new YAML file in `tests/eval/scenarios/` — no
Python changes needed. The eval workflow also runs in CI on every PR that touches
the agent, tools, or eval code.

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
│   ├── diagnose_bloat.py           # Composite: table + index bloat + vacuum status
│   ├── diagnose_slow_queries.py    # Composite: slow queries + EXPLAIN + classification
│   ├── slow_queries.py             # pg_stat_statements + pg_stat_activity
│   ├── explain_query.py            # EXPLAIN ANALYZE (SELECT only)
│   ├── table_stats.py              # Table health + index usage
│   ├── lock_info.py                # Lock contention detection (pg_locks)
│   ├── bloat_detection.py          # Table + index bloat (pgstattuple)
│   ├── vacuum_monitor.py           # Autovacuum health + vacuum status
│   └── remediation.py              # Whitelisted write ops (requires approval)
├── mcp_server/
│   ├── __init__.py
│   └── server.py                   # MCP server — 12 tools + health/stats resources
├── ui/
│   ├── app.py                      # Streamlit dashboard — Health Overview
│   └── pages/
│       ├── 1_query_explorer.py     # Browse + EXPLAIN slow queries
│       ├── 2_remediation_log.py    # Audit trail timeline
│       └── 3_agent_chat.py         # Web-based agent chat with approval
├── scheduler/
│   └── runner.py                   # Proactive health check scheduler
├── scripts/
│   ├── generate_slow_workload.py   # Create bad queries for testing
│   └── test_connection.py          # Smoke test DB + tools
├── tests/
│   └── eval/                       # Evaluation harness
│       ├── scenarios/              # YAML scenario definitions
│       ├── scenarios.py            # Scenario loader
│       ├── runner.py               # Setup → run agent → capture trace → teardown
│       ├── scoring.py              # Tool-call + keyword + LLM-as-judge scoring
│       ├── report.py               # Terminal + JSON reports
│       ├── conftest.py             # Pytest fixtures + CLI flags
│       └── test_eval.py            # Parametrized eval tests
├── .github/
│   └── workflows/
│       ├── ci.yml                  # GitHub Actions CI pipeline
│       └── eval.yml                # Eval suite workflow
├── main.py                         # Interactive CLI with approval flow
├── remediation_audit.jsonl         # Auto-generated audit trail
├── requirements.txt
├── .env.example
├── LICENSE                         # MIT License
├── CONTRIBUTING.md                 # How to add tools and contribute
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

The MCP server exposes **only the read-only diagnostic tools and resources** —
remediation is not exposed over MCP, so external clients cannot trigger writes.


## LLM Configuration

The agent supports multiple LLM providers. Configure in `.env`:

**Anthropic (default):**
```env
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-4-6
ANTHROPIC_API_KEY=sk-ant-xxxxx
```

**OpenAI:**
```env
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o
OPENAI_API_KEY=sk-xxxxx
```
```bash
pip install langchain-openai
```

**Ollama (local, free):**
```env
LLM_PROVIDER=ollama
LLM_MODEL=llama3.1
OLLAMA_BASE_URL=http://localhost:11434
```
```bash
pip install langchain-ollama
ollama pull llama3.1
```

Note: tool-calling quality varies by model. Claude Sonnet and GPT-4o handle complex
multi-tool diagnostic chains well. Smaller local models may struggle with reasoning
over EXPLAIN plans or chaining 5+ tool calls.

## Tech Stack

| Component | Choice | Why |
|-----------|--------|-----|
| Agent framework | LangGraph 1.2.x | Stateful graphs, ReAct loops, HITL support |
| LLM (default) | Claude Sonnet 4.6 | Best cost/quality for tool-calling agents |
| LLM (alternatives) | GPT-4o, Llama 3.1, Mistral | Configurable via .env |
| Database | PostgreSQL 16 | Industry standard, rich pg_stat ecosystem |
| Dashboard | Streamlit | Python-native, fast to build |
| Language | Python 3.11+ | LangGraph native, psycopg2/asyncpg ecosystem |

## License

MIT