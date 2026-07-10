"""
AI DBRE — Interactive CLI (Phase 3)

Run: python main.py

Chat with the AI DBRE agent. When the agent proposes a remediation
action (CREATE INDEX, VACUUM, ANALYZE), you'll be prompted to approve
or reject it before execution.
"""

import uuid
from langchain_core.messages import HumanMessage, ToolMessage

from agent.graph import app, REMEDIATION_TOOL_NAMES


BANNER = """
╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║     █████╗ ██╗    ██████╗ ██████╗ ██████╗ ███████╗           ║
║    ██╔══██╗██║    ██╔══██╗██╔══██╗██╔══██╗██╔════╝           ║
║    ███████║██║    ██║  ██║██████╔╝██████╔╝█████╗             ║
║    ██╔══██║██║    ██║  ██║██╔══██╗██╔══██╗██╔══╝             ║
║    ██║  ██║██║    ██████╔╝██████╔╝██║  ██║███████╗           ║
║    ╚═╝  ╚═╝╚═╝    ╚═════╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝           ║
║                                                              ║
║    AI Database Reliability Engineer — Phase 3                ║
║    Human-in-the-Loop Remediation                             ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝

Type your question about the database. The agent will investigate
and can propose fixes — you approve before anything runs.

Commands:
  /quit  — Exit
  /new   — Start a new conversation (fresh context)
  /help  — Show example questions

────────────────────────────────────────────────────────────────
"""

HELP_TEXT = """
Example questions you can ask:

  Diagnostic (read-only):
  • What are the slowest queries in the database?
  • Check for table and index bloat
  • Is autovacuum keeping up?
  • Are there any lock contention issues?

  Remediation (will ask for approval):
  • Find slow queries on audit_log and fix them
  • Create missing indexes for the worst-performing queries
  • The audit_log table needs vacuuming, please do it
  • Run ANALYZE on all tables with stale statistics

────────────────────────────────────────────────────────────────
"""


def format_approval_request(tool_calls: list) -> str:
    """Format the remediation tool calls for human review."""
    lines = []
    lines.append("\n" + "═" * 60)
    lines.append("  ⚠️  REMEDIATION APPROVAL REQUIRED")
    lines.append("═" * 60)
    lines.append("")
    lines.append("  The agent wants to execute the following:")
    lines.append("")

    for i, tc in enumerate(tool_calls, 1):
        args = tc.get("args", {})
        sql = args.get("sql_statement", "N/A")
        reason = args.get("reason", "No reason provided")
        lines.append(f"  Action {i}:")
        lines.append(f"    SQL:    {sql}")
        lines.append(f"    Reason: {reason}")
        lines.append("")

    lines.append("═" * 60)
    return "\n".join(lines)


def handle_approval(config: dict, tool_calls: list) -> bool:
    """
    Show the proposed remediation to the user and get approval.
    Returns True if approved, False if rejected.
    """
    print(format_approval_request(tool_calls))

    while True:
        try:
            choice = input("  Approve? (y)es / (n)o / (s)kip: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return False

        if choice in ("y", "yes"):
            return True
        elif choice in ("n", "no", "s", "skip"):
            return False
        else:
            print("  Please enter y, n, or s.")


def reject_remediation(config: dict, tool_calls: list):
    """
    When the user rejects a remediation, inject a ToolMessage saying
    the action was rejected, so the agent knows and can respond.
    """
    reject_messages = []
    for tc in tool_calls:
        reject_messages.append(
            ToolMessage(
                content='{"status": "REJECTED", "reason": "Human operator rejected this remediation action."}',
                tool_call_id=tc["id"],
            )
        )

    # Resume the graph with rejection messages
    app.update_state(config, {"messages": reject_messages})


def stream_agent(input_data: dict | None, config: dict):
    """
    Stream the agent, handling interrupts for remediation approval.
    """
    try:
        events = app.stream(
            input_data,
            config=config,
            stream_mode="values",
        )

        for event in events:
            last_msg = event["messages"][-1]

            if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
                for tc in last_msg.tool_calls:
                    if tc["name"] in REMEDIATION_TOOL_NAMES:
                        print(f"\n   🛡️  Proposing: {tc['name']}...", flush=True)
                    else:
                        print(f"\n   🔧 Calling: {tc['name']}...", flush=True)
            elif hasattr(last_msg, "content") and isinstance(last_msg.content, str):
                if last_msg.type == "ai" and not getattr(last_msg, "tool_calls", None):
                    print(f"\n{last_msg.content}")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        print("   Make sure the database is running and .env is configured.")
        return

    # Check if the graph was interrupted (waiting for approval)
    state = app.get_state(config)

    if state.next and "remediation_tools" in state.next:
        # Graph paused before remediation — get approval
        last_msg = state.values["messages"][-1]
        remediation_calls = [
            tc for tc in last_msg.tool_calls
            if tc["name"] in REMEDIATION_TOOL_NAMES
        ]

        if remediation_calls:
            approved = handle_approval(config, remediation_calls)

            if approved:
                print("\n  ✅ Approved — executing...\n")
                # Resume the graph — it will execute the remediation tool
                stream_agent(None, config)
            else:
                print("\n  ❌ Rejected — skipping remediation.\n")
                reject_remediation(config, remediation_calls)
                # Resume with rejection so agent can acknowledge
                stream_agent(None, config)


def run_interactive():
    print(BANNER)

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    while True:
        try:
            user_input = input("\n🔍 You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n\nGoodbye!")
            break

        if not user_input:
            continue

        if user_input.lower() == "/quit":
            print("\nGoodbye!")
            break

        if user_input.lower() == "/new":
            thread_id = str(uuid.uuid4())
            config = {"configurable": {"thread_id": thread_id}}
            print("\n✨ New conversation started.\n")
            continue

        if user_input.lower() == "/help":
            print(HELP_TEXT)
            continue

        print("\n🤖 AI DBRE: ", end="", flush=True)
        stream_agent(
            {"messages": [HumanMessage(content=user_input)]},
            config,
        )


if __name__ == "__main__":
    run_interactive()
