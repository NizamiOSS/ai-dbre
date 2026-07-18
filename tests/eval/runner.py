"""
Runner — executes eval scenarios against the AI DBRE agent.

Responsibilities:
  1. Execute setup SQL to induce the problem
  2. Run the agent with the scenario's prompt
  3. Capture tool calls and final text output
  4. Execute teardown SQL to clean up

The runner captures a structured AgentTrace containing:
  - Every tool call the agent made (name + arguments)
  - The full text of the agent's final response
  - Timing information
  - Any errors encountered
"""

import json
import time
import traceback
from dataclasses import dataclass, field
from typing import Optional

import psycopg2

from tests.eval.scenarios import Scenario


@dataclass
class ToolCall:
    """A single tool invocation by the agent."""

    name: str
    arguments: dict = field(default_factory=dict)
    result_preview: str = ""  # First 500 chars of tool output


@dataclass
class AgentTrace:
    """Complete record of an agent evaluation run."""

    scenario_name: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    final_response: str = ""
    duration_seconds: float = 0.0
    error: Optional[str] = None
    raw_messages: list[dict] = field(default_factory=list)

    @property
    def tool_names_called(self) -> set[str]:
        """Set of unique tool names the agent invoked."""
        return {tc.name for tc in self.tool_calls}


def get_db_connection():
    """
    Get a database connection using the agent's read-only credentials.
    Used by conftest.py for the shared connection — but setup/teardown
    needs the admin connection below instead.
    """
    from agent.config import settings

    return psycopg2.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        database=settings.pg_database,
        user=settings.pg_user,
        password=settings.pg_password,
    )


def get_setup_connection():
    """
    Get a database connection with WRITE privileges for setup/teardown SQL.
    Uses dbre_admin (the owner role), not dbre_agent (read-only).
    Credentials read from .env via agent.config.settings.
    """
    from agent.config import settings

    return psycopg2.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        database=settings.pg_database,
        user=settings.eval_db_user,
        password=settings.eval_db_password,
    )


def execute_sql_steps(
    conn, steps: list[str], label: str = "SQL"
) -> list[str]:
    """
    Execute a list of SQL statements. Returns list of any errors.

    Each statement runs in its own transaction (autocommit) to avoid
    one failure blocking subsequent statements.
    """
    errors = []
    conn.autocommit = True
    cursor = conn.cursor()

    for i, sql in enumerate(steps):
        sql = sql.strip()
        if not sql:
            continue
        try:
            cursor.execute(sql)
        except Exception as e:
            error_msg = f"{label} step {i+1} failed: {e}\n  SQL: {sql[:200]}"
            errors.append(error_msg)
            print(f"  WARNING: {error_msg}")

    cursor.close()
    return errors


def run_agent_and_capture(prompt: str) -> AgentTrace:
    """
    Run the AI DBRE agent with the given prompt and capture its trace.

    This function imports the agent graph and streams its execution,
    collecting every tool call and the final response.

    Returns an AgentTrace with the full execution record.
    """
    # Import the agent components
    # These imports are deferred so the eval framework can be loaded
    # independently of the agent code (useful for testing the eval
    # framework itself)
    import uuid
    from agent.graph import build_graph
    from langchain_core.messages import HumanMessage, AIMessage, ToolMessage

    trace = AgentTrace(scenario_name="")
    start_time = time.time()

    try:
        graph = build_graph()

        # Each eval run gets a unique thread_id so runs don't share state
        config = {"configurable": {"thread_id": f"eval-{uuid.uuid4().hex[:8]}"}}

        # Stream the agent execution to capture intermediate steps
        initial_state = {
            "messages": [HumanMessage(content=prompt)]
        }

        all_messages = []

        for event in graph.stream(initial_state, config, stream_mode="values"):
            messages = event.get("messages", [])
            all_messages = messages  # Keep the latest full state

        # Extract tool calls and final response from the message history
        for msg in all_messages:
            # Store raw message for debugging
            trace.raw_messages.append({
                "type": type(msg).__name__,
                "content": str(msg.content)[:1000] if hasattr(msg, "content") else "",
            })

            # Capture AI messages with tool calls
            if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls"):
                for tc in msg.tool_calls:
                    tool_call = ToolCall(
                        name=tc.get("name", "unknown"),
                        arguments=tc.get("args", {}),
                    )
                    trace.tool_calls.append(tool_call)

            # Capture tool results (preview only)
            if isinstance(msg, ToolMessage):
                # Match this result to the most recent tool call
                # without a result preview
                content_preview = str(msg.content)[:500]
                for tc in reversed(trace.tool_calls):
                    if not tc.result_preview:
                        tc.result_preview = content_preview
                        break

        # The final AI message is the agent's diagnosis
        for msg in reversed(all_messages):
            if isinstance(msg, AIMessage) and msg.content:
                # Skip messages that are only tool calls with no text
                if isinstance(msg.content, str) and msg.content.strip():
                    trace.final_response = msg.content
                    break
                elif isinstance(msg.content, list):
                    # Handle structured content blocks
                    text_parts = [
                        block.get("text", "")
                        for block in msg.content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    if text_parts:
                        trace.final_response = "\n".join(text_parts)
                        break

    except Exception as e:
        trace.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"

    trace.duration_seconds = time.time() - start_time
    return trace


def run_scenario(scenario: Scenario, conn=None) -> AgentTrace:
    """
    Execute a complete eval scenario: setup → agent → teardown.

    Args:
        scenario: The scenario to run.
        conn: Optional database connection. If None, creates one using
              defaults from environment variables.

    Returns:
        AgentTrace with the complete execution record.
    """
    setup_conn = get_setup_connection()

    trace = AgentTrace(scenario_name=scenario.name)

    try:
        # 1. Setup: induce the problem
        print(f"\n{'='*60}")
        print(f"SCENARIO: {scenario.name}")
        print(f"{'='*60}")
        print(f"  Setting up: {scenario.description[:80]}...")

        setup_errors = execute_sql_steps(setup_conn, scenario.setup, label="Setup")
        if setup_errors:
            print(f"  Setup had {len(setup_errors)} error(s), continuing...")

        # Small delay to let pg_stat_statements register the queries
        time.sleep(1)

        # 2. Run the agent
        print(f"  Running agent with prompt: {scenario.prompt[:60]}...")
        trace = run_agent_and_capture(scenario.prompt)
        trace.scenario_name = scenario.name

        print(f"  Agent finished in {trace.duration_seconds:.1f}s")
        print(f"  Tools called: {trace.tool_names_called}")

        if trace.error:
            print(f"  ERROR: {trace.error[:200]}")

    finally:
        # 3. Teardown: always clean up
        print(f"  Tearing down...")
        teardown_errors = execute_sql_steps(
            setup_conn, scenario.teardown, label="Teardown"
        )
        setup_conn.close()

    return trace


def run_all_scenarios(
    scenarios: list[Scenario],
) -> dict[str, AgentTrace]:
    """
    Run all scenarios sequentially, sharing a single DB connection.

    Returns a dict mapping scenario name → AgentTrace.
    """
    results = {}
    for scenario in scenarios:
        trace = run_scenario(scenario)
        results[scenario.name] = trace

    return results
