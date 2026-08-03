"""Supervisor graph state.

CLAUDE.md's Core Security Invariant: the Session/identity object lives in
LangGraph state, NEVER in any prompt, message, system instruction, or tool
argument. `identity` is a field of this state - it is never read into a
`Message.content` string anywhere in agents/nodes.py, and it is bound to
the tools.identity contextvar only at the moment a tool call crosses the
MCP server boundary (see nodes.py's account_agent/escalate nodes).
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from dealership_agent.agents.pricing import PriceFilters
from dealership_agent.llm.base import Message
from dealership_agent.tools.identity import RequestIdentity

Route = Literal["sales", "account", "clarify", "escalate"]
SalesIntent = Literal["listings", "policy"]


class GraphState(TypedDict, total=False):
    messages: list[Message]
    identity: RequestIdentity

    route: Route | None
    sales_intent: SalesIntent | None
    price_filters: PriceFilters | None
    order_ref: str | None
    clarify_question: str | None
    escalate_summary: str | None
    escalate_reason: str | None

    tool_result: Any
    final_response: str | None
