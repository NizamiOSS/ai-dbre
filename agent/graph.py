"""
AI DBRE — LangGraph Agent Graph (Phase 3)

Implements a ReAct-style agent with human-in-the-loop remediation.

Two-tier tool architecture:
- Diagnostic tools: run freely in the ReAct loop (read-only)
- Remediation tool: requires human approval before execution

The graph uses LangGraph's interrupt_before mechanism:
when the agent wants to call execute_remediation, the graph
pauses, shows the proposed action to the human, and waits
for approval before continuing.

Flow:
    User question
        ↓
    [agent] ←──────────────┐
        ↓                   │
    should_continue         │
        ↓       ↓     ↓    │
       END   diag   remed  │
              tools   ↓     │
              │    ⏸ PAUSE  │
              │    (human   │
              │    approval)│
              │       ↓     │
              └───────┴─────┘
"""

from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.memory import MemorySaver

from agent.state import DBREState
from agent.prompts import SYSTEM_PROMPT
from agent.config import settings

# Import diagnostic tools (read-only)
from tools.diagnose_bloat import diagnose_bloat
from tools.diagnose_slow_queries import diagnose_slow_queries
from tools.slow_queries import get_slow_queries, get_active_long_queries
from tools.explain_query import explain_query
from tools.table_stats import get_table_stats, get_index_usage
from tools.lock_info import get_lock_info
from tools.bloat_detection import get_table_bloat, get_index_bloat
from tools.vacuum_monitor import get_vacuum_status, get_autovacuum_activity

# Import remediation tool (write — requires approval)
from tools.remediation import execute_remediation


# =============================================================================
# Tool Registry — Two Tiers
# =============================================================================

DIAGNOSTIC_TOOLS = [
    diagnose_bloat,
    diagnose_slow_queries,
    get_slow_queries,
    get_active_long_queries,
    explain_query,
    get_table_stats,
    get_index_usage,
    get_lock_info,
    get_table_bloat,
    get_index_bloat,
    get_vacuum_status,
    get_autovacuum_activity,
]

REMEDIATION_TOOLS = [
    execute_remediation,
]

ALL_TOOLS = DIAGNOSTIC_TOOLS + REMEDIATION_TOOLS

REMEDIATION_TOOL_NAMES = {t.name for t in REMEDIATION_TOOLS}


# =============================================================================
# LLM Setup
# =============================================================================


def create_llm():
    """
    Create the LLM based on the configured provider.

    Supports:
    - anthropic: Claude models via Anthropic API
    - openai: GPT models via OpenAI API
    - ollama: Local models via Ollama (llama3, mistral, etc.)

    Configure via .env:
        LLM_PROVIDER=anthropic
        LLM_MODEL=claude-sonnet-4-6
    """
    provider = settings.llm_provider.lower()

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        llm = ChatAnthropic(
            model=settings.llm_model,
            api_key=settings.anthropic_api_key,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
        )

    elif provider == "openai":
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(
            model=settings.llm_model,
            api_key=settings.openai_api_key,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
        )

    elif provider == "ollama":
        from langchain_ollama import ChatOllama
        llm = ChatOllama(
            model=settings.llm_model,
            base_url=settings.ollama_base_url,
            temperature=settings.llm_temperature,
        )

    else:
        raise ValueError(
            f"Unknown LLM_PROVIDER: '{provider}'. "
            f"Supported: anthropic, openai, ollama"
        )

    return llm.bind_tools(ALL_TOOLS)


# =============================================================================
# Graph Nodes
# =============================================================================


def agent_node(state: DBREState) -> dict:
    """
    The reasoning node. Sends conversation history to the LLM,
    which decides to call a diagnostic tool, propose remediation,
    or produce a final response.
    """
    llm = create_llm()

    from langchain_core.messages import SystemMessage
    messages = [SystemMessage(content=SYSTEM_PROMPT)] + state["messages"]

    response = llm.invoke(messages)
    return {"messages": [response]}


def should_continue(state: DBREState) -> str:
    """
    Three-way routing:
    - No tool calls → END (done)
    - Diagnostic tool calls → "diagnostic_tools" (run freely)
    - Remediation tool calls → "remediation_tools" (will be interrupted)
    """
    last_message = state["messages"][-1]

    if not hasattr(last_message, "tool_calls") or not last_message.tool_calls:
        return END

    # Check if ANY of the tool calls is a remediation tool
    for tc in last_message.tool_calls:
        if tc["name"] in REMEDIATION_TOOL_NAMES:
            return "remediation_tools"

    return "diagnostic_tools"


# =============================================================================
# Build the Graph
# =============================================================================


def build_graph():
    """
    Construct the AI DBRE agent graph with human-in-the-loop remediation.
    """
    diagnostic_node = ToolNode(DIAGNOSTIC_TOOLS)
    remediation_node = ToolNode(REMEDIATION_TOOLS)

    graph = StateGraph(DBREState)

    # Add nodes
    graph.add_node("agent", agent_node)
    graph.add_node("diagnostic_tools", diagnostic_node)
    graph.add_node("remediation_tools", remediation_node)

    # Entry point
    graph.set_entry_point("agent")

    # Three-way conditional edge from agent
    graph.add_conditional_edges(
        "agent",
        should_continue,
        ["diagnostic_tools", "remediation_tools", END],
    )

    # Both tool nodes loop back to agent
    graph.add_edge("diagnostic_tools", "agent")
    graph.add_edge("remediation_tools", "agent")

    # Compile with interrupt BEFORE remediation execution
    checkpointer = MemorySaver()
    return graph.compile(
        checkpointer=checkpointer,
        interrupt_before=["remediation_tools"],
    )


# Singleton graph instance
app = build_graph()
