# AI DBRE — Evaluation Harness

Automated evaluation suite that scores the AI DBRE agent's diagnostic accuracy
against known database problems.

## How It Works

```
┌─────────────────┐     ┌──────────────┐     ┌──────────────┐     ┌────────────┐
│  Scenario YAML  │────▶│  Runner      │────▶│  Scoring     │────▶│  Report    │
│                 │     │              │     │              │     │            │
│ • setup SQL     │     │ • apply SQL  │     │ • tool-call  │     │ • pass/fail│
│ • prompt        │     │ • run agent  │     │   verify     │     │ • scores   │
│ • expected      │     │ • capture    │     │ • LLM judge  │     │ • details  │
│ • teardown SQL  │     │   output     │     │   (optional) │     │            │
└─────────────────┘     └──────────────┘     └──────────────┘     └────────────┘
```

Each scenario:
1. **Induces** a known database problem via SQL
2. **Runs** the agent with a standardized prompt
3. **Scores** the agent's diagnosis against the expected root cause
4. **Tears down** the problem so scenarios don't interfere

## Quick Start

```bash
# Run all scenarios (tool-call scoring only, no LLM costs)
pytest tests/eval/ -v

# Run with LLM-as-judge scoring (uses one API call per scenario)
pytest tests/eval/ -v --llm-judge

# Run a single scenario
pytest tests/eval/ -v -k missing_index

# Generate a summary report
python -m tests.eval.report
```

## Adding a New Scenario

Create a YAML file in `tests/eval/scenarios/`:

```yaml
name: my_new_scenario
description: "What this tests"
category: index  # index | bloat | vacuum | lock | config | query

setup:
  - "CREATE TABLE test_eval_mine (id serial, val text);"
  - "INSERT INTO test_eval_mine SELECT g, repeat('x', 100) FROM generate_series(1, 50000) g;"

prompt: "Check the database for performance issues"

expected:
  root_cause: "sequential scan on test_eval_mine due to missing index"
  affected_objects: ["test_eval_mine"]
  tools_must_call: ["get_slow_queries", "explain_query"]
  tools_should_call: ["get_index_usage"]
  fix_category: "create_index"

teardown:
  - "DROP TABLE IF EXISTS test_eval_mine CASCADE;"
```

## Scoring

Each scenario gets a score from 0.0 to 1.0 based on weighted criteria:

| Criterion           | Weight | How It's Checked                                    |
|----------------------|--------|-----------------------------------------------------|
| Required tools called| 0.30   | Did the agent call every tool in `tools_must_call`?  |
| Suggested tools called| 0.10  | Did it also call `tools_should_call`?                |
| Root cause identified| 0.35   | Keyword/LLM match against `root_cause`               |
| Affected objects found| 0.15  | Are `affected_objects` mentioned in the output?       |
| Fix category correct | 0.10   | Does the recommendation match `fix_category`?         |

**Pass threshold: 0.70** (configurable via `--pass-threshold`)
