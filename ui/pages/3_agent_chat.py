"""
AI DBRE — Agent Chat Page

Web-based chat interface for the AI DBRE agent.
Supports full diagnostic conversation and remediation approval.
"""

import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import streamlit as st
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from agent.graph import app, REMEDIATION_TOOL_NAMES

st.set_page_config(page_title="AI DBRE — Agent Chat", page_icon="🤖", layout="wide")

st.title("🤖 Agent Chat")
st.caption("Ask questions about your database — the agent will investigate and can propose fixes")

# =============================================================================
# Session State
# =============================================================================

if "chat_messages" not in st.session_state:
    st.session_state.chat_messages = []

if "thread_id" not in st.session_state:
    st.session_state.thread_id = str(uuid.uuid4())

if "pending_approval" not in st.session_state:
    st.session_state.pending_approval = None

if "tool_calls_log" not in st.session_state:
    st.session_state.tool_calls_log = []


def get_config():
    return {"configurable": {"thread_id": st.session_state.thread_id}}


def new_conversation():
    st.session_state.chat_messages = []
    st.session_state.thread_id = str(uuid.uuid4())
    st.session_state.pending_approval = None
    st.session_state.tool_calls_log = []


# =============================================================================
# Sidebar Controls
# =============================================================================

with st.sidebar:
    st.subheader("Chat Controls")
    if st.button("🆕 New Conversation"):
        new_conversation()
        st.rerun()

    st.divider()
    st.markdown("**Example questions:**")
    examples = [
        "What are the slowest queries?",
        "Check for table bloat",
        "Is autovacuum keeping up?",
        "Find slow queries and fix them",
        "Run ANALYZE on all tables",
    ]
    for example in examples:
        if st.button(example, key=f"ex_{example}"):
            st.session_state.pending_input = example
            st.rerun()


# =============================================================================
# Chat Display
# =============================================================================

for msg in st.session_state.chat_messages:
    if msg["role"] == "user":
        with st.chat_message("user"):
            st.markdown(msg["content"])
    elif msg["role"] == "assistant":
        with st.chat_message("assistant"):
            st.markdown(msg["content"])
    elif msg["role"] == "tools":
        with st.chat_message("assistant"):
            for tool_name in msg["tools"]:
                if tool_name in REMEDIATION_TOOL_NAMES:
                    st.markdown(f"🛡️ Proposing: `{tool_name}`")
                else:
                    st.markdown(f"🔧 Calling: `{tool_name}`")


# =============================================================================
# Run Agent
# =============================================================================

def run_agent(user_input: str):
    """Stream agent response, handling interrupts for remediation."""
    config = get_config()

    # Collect tool calls and final response
    tool_names = []
    final_response = ""

    try:
        events = app.stream(
            {"messages": [HumanMessage(content=user_input)]},
            config=config,
            stream_mode="values",
        )

        for event in events:
            last_msg = event["messages"][-1]

            if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
                for tc in last_msg.tool_calls:
                    tool_names.append(tc["name"])
            elif hasattr(last_msg, "content") and isinstance(last_msg.content, str):
                if last_msg.type == "ai" and not getattr(last_msg, "tool_calls", None):
                    final_response = last_msg.content

    except Exception as e:
        final_response = f"❌ Error: {e}\n\nMake sure the database is running and .env is configured."

    # Log tool calls
    if tool_names:
        st.session_state.chat_messages.append({"role": "tools", "tools": tool_names})

    # Check for interrupt (remediation approval needed)
    state = app.get_state(config)
    if state.next and "remediation_tools" in state.next:
        last_msg = state.values["messages"][-1]
        remediation_calls = [
            tc for tc in last_msg.tool_calls
            if tc["name"] in REMEDIATION_TOOL_NAMES
        ]
        st.session_state.pending_approval = remediation_calls

        # Show what the agent said before the proposal
        if final_response:
            st.session_state.chat_messages.append({"role": "assistant", "content": final_response})
        return

    # Normal response
    if final_response:
        st.session_state.chat_messages.append({"role": "assistant", "content": final_response})


def resume_agent_approved():
    """Resume graph after approval — execute the remediation."""
    config = get_config()
    tool_names = []
    final_response = ""

    try:
        events = app.stream(None, config=config, stream_mode="values")

        for event in events:
            last_msg = event["messages"][-1]
            if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
                for tc in last_msg.tool_calls:
                    tool_names.append(tc["name"])
            elif hasattr(last_msg, "content") and isinstance(last_msg.content, str):
                if last_msg.type == "ai" and not getattr(last_msg, "tool_calls", None):
                    final_response = last_msg.content

    except Exception as e:
        final_response = f"❌ Error during execution: {e}"

    if tool_names:
        st.session_state.chat_messages.append({"role": "tools", "tools": tool_names})

    # Check for another interrupt (agent might propose additional fixes)
    state = app.get_state(config)
    if state.next and "remediation_tools" in state.next:
        last_msg = state.values["messages"][-1]
        remediation_calls = [
            tc for tc in last_msg.tool_calls
            if tc["name"] in REMEDIATION_TOOL_NAMES
        ]
        st.session_state.pending_approval = remediation_calls
        if final_response:
            st.session_state.chat_messages.append({"role": "assistant", "content": final_response})
        return

    if final_response:
        st.session_state.chat_messages.append({"role": "assistant", "content": final_response})

    st.session_state.pending_approval = None


def resume_agent_rejected():
    """Resume graph after rejection — inject rejection messages."""
    config = get_config()

    reject_messages = []
    for tc in st.session_state.pending_approval:
        reject_messages.append(
            ToolMessage(
                content='{"status": "REJECTED", "reason": "Human operator rejected this remediation action."}',
                tool_call_id=tc["id"],
            )
        )

    app.update_state(config, {"messages": reject_messages})
    st.session_state.pending_approval = None

    # Resume so agent can acknowledge
    final_response = ""
    try:
        events = app.stream(None, config=config, stream_mode="values")
        for event in events:
            last_msg = event["messages"][-1]
            if hasattr(last_msg, "content") and isinstance(last_msg.content, str):
                if last_msg.type == "ai" and not getattr(last_msg, "tool_calls", None):
                    final_response = last_msg.content
    except Exception:
        pass

    if final_response:
        st.session_state.chat_messages.append({"role": "assistant", "content": final_response})


# =============================================================================
# Approval Banner
# =============================================================================

if st.session_state.pending_approval:
    st.divider()
    st.warning("⚠️ **Remediation Approval Required**")

    for tc in st.session_state.pending_approval:
        args = tc.get("args", {})
        st.code(args.get("sql_statement", "N/A"), language="sql")
        st.caption(f"**Reason:** {args.get('reason', 'No reason provided')}")

    col1, col2, _ = st.columns([1, 1, 4])
    with col1:
        if st.button("✅ Approve", type="primary"):
            st.session_state.chat_messages.append(
                {"role": "assistant", "content": "✅ Approved — executing..."}
            )
            resume_agent_approved()
            st.rerun()
    with col2:
        if st.button("❌ Reject"):
            st.session_state.chat_messages.append(
                {"role": "assistant", "content": "❌ Rejected — skipping."}
            )
            resume_agent_rejected()
            st.rerun()


# =============================================================================
# Input
# =============================================================================

# Handle example button input
pending = st.session_state.pop("pending_input", None)

if not st.session_state.pending_approval:
    user_input = st.chat_input("Ask about your database...")

    input_to_process = pending or user_input

    if input_to_process:
        st.session_state.chat_messages.append({"role": "user", "content": input_to_process})

        with st.chat_message("user"):
            st.markdown(input_to_process)

        with st.chat_message("assistant"):
            with st.spinner("Investigating..."):
                run_agent(input_to_process)

        st.rerun()
