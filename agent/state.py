"""
AI DBRE — Agent State Schema
Defines the data that flows through the LangGraph graph.
"""

from typing import Annotated
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages


class DBREState(TypedDict):
    """
    State schema for the AI DBRE agent.

    - messages: The conversation history (LLM + tool calls + results).
                Uses add_messages reducer to append, not overwrite.
    """
    messages: Annotated[list, add_messages]
