# Contributing to AI DBRE

Thanks for your interest in contributing! This project is an AI-powered PostgreSQL
diagnostic and remediation agent. Here's how to get involved.

---

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/YOUR_USERNAME/ai-dbre.git`
3. Follow the [Quick Start](README.md#quick-start) to set up the environment
4. Create a branch: `git checkout -b feature/your-feature-name`

## Development Setup

```bash
# Start PostgreSQL
docker compose up -d

# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
# Add your ANTHROPIC_API_KEY to .env

# Verify everything works
python scripts/test_connection.py
```

---

## Adding a New Diagnostic Tool

This is the most common contribution. Each tool is a Python function that queries
PostgreSQL and returns JSON. Here's the pattern:

### 1. Create the tool file

```python
# tools/your_tool.py

import json
from langchain_core.tools import tool
from agent.db import execute_query


@tool
def your_new_tool(param: str = "default") -> str:
    """
    Clear description of what this tool does.

    Args:
        param: Explain what this parameter controls.

    Returns:
        JSON with the diagnostic data.
    """
    query = """
    SELECT ... FROM pg_stat_... WHERE ...
    """
    results = execute_query(query)

    # Serialize for JSON (handle Decimal, None, etc.)
    for row in results:
        for key, val in row.items():
            if hasattr(val, "as_integer_ratio"):
                row[key] = float(val)
            elif val is None:
                row[key] = None
            else:
                row[key] = str(val)

    return json.dumps(results, indent=2, default=str)
```

### 2. Register it in the graph

In `agent/graph.py`, add the import and include it in `DIAGNOSTIC_TOOLS`:

```python
from tools.your_tool import your_new_tool

DIAGNOSTIC_TOOLS = [
    # ... existing tools ...
    your_new_tool,
]
```

### 3. Update the system prompt

Add relevant diagnostic knowledge to `agent/prompts.py` so the agent knows
when and how to use the new tool.

### 4. Test it

```bash
python main.py
# Ask a question that should trigger your new tool
```

---

## Adding a New Health Check

Health checks live in `agent/health_check.py`. Each check is a function that
queries the database and returns a list of `HealthAlert` objects:

```python
def _check_your_metric(config: HealthCheckConfig) -> list[HealthAlert]:
    alerts = []
    results = execute_query("SELECT ...")

    for row in results:
        value = float(row["metric"])
        if value >= config.your_critical_threshold:
            alerts.append(HealthAlert(
                check_name="your_check",
                severity=Severity.CRITICAL,
                table_name=row["table_name"],
                message=f"Description of what's wrong ({value})",
                metric_value=value,
                threshold=config.your_critical_threshold,
                recommendation="What the user should do",
            ))
    return alerts
```

Then add it to the `run_health_check()` function's check list.

---

## Extending the Remediation Whitelist

The remediation tool only executes SQL that matches patterns in `ALLOWED_PATTERNS`
in `tools/remediation.py`. To add a new allowed operation:

```python
{
    "name": "YOUR OPERATION",
    "pattern": re.compile(r"^\s*YOUR SQL PATTERN\s+", re.IGNORECASE),
    "risk": "LOW",  # LOW, MEDIUM, or HIGH
    "description": "What this operation does and its impact",
},
```

Be conservative — every pattern you add is something the agent can execute
with human approval. Prefer `CONCURRENTLY` variants where available.

---

## Code Style

- Python 3.11+ type hints
- Docstrings on all tools (the LLM reads these to decide when to use them)
- SQL queries should filter out the agent's own monitoring queries
- Serialize all Decimal/datetime types before returning JSON
- Use `execute_query()` from `agent/db.py` for all database access

## Pull Request Process

1. Make sure `python scripts/test_connection.py` passes
2. Make sure `python scheduler/runner.py --once` runs without errors
3. Test your changes with the interactive CLI (`python main.py`)
4. Update `README.md` if you added new tools or capabilities
5. Add any new issues to `TROUBLESHOOTING.md`
6. Submit a PR with a clear description of what you added and why

---

## Questions?

Open an issue or start a discussion. All skill levels welcome.
