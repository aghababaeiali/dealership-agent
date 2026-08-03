"""Supervisor graph node implementations.

Every node is a plain async function of (state) -> partial state update,
built by a factory that closes over its dependencies (LLM provider, bound
sub-agents) - no globals, so tests can build a graph with a fake provider
and in-memory sub-agents with no live LLM or network calls.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from dealership_agent.agents.pricing import derive_price_filters
from dealership_agent.agents.prompts import ROUTER_SYSTEM_PROMPT, SYNTHESIS_SYSTEM_PROMPT
from dealership_agent.agents.state import GraphState
from dealership_agent.agents.tool_binding import SubAgent
from dealership_agent.config import get_settings
from dealership_agent.llm.base import LLMProvider, Message
from dealership_agent.tools.identity import bind_identity

logger = structlog.get_logger(__name__)

Node = Callable[[GraphState], Awaitable[dict[str, Any]]]

DEFAULT_CLARIFY_QUESTION = (
    "Could you tell me a bit more about what you're looking for - a "
    "vehicle, a dealership policy, or help with an existing order?"
)


def make_intake_node() -> Node:
    async def intake_node(state: GraphState) -> dict[str, Any]:
        messages = state.get("messages") or []
        last_content = messages[-1].content.strip() if messages else ""
        if not last_content:
            return {
                "route": "clarify",
                "clarify_question": "I didn't catch that - what can I help you with?",
            }
        return {}

    return intake_node


def make_router_node(llm: LLMProvider) -> Node:
    async def router_node(state: GraphState) -> dict[str, Any]:
        # Already decided by intake (e.g. empty message) - don't re-route.
        if state.get("route") == "clarify":
            return {}

        settings = get_settings()
        messages = state["messages"]
        price_filters = derive_price_filters(messages[-1].content)

        llm_messages = [Message(role="system", content=ROUTER_SYSTEM_PROMPT), *messages]
        raw = llm.complete(llm_messages, model=settings.llm_model_classifier)

        try:
            decision = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("router_decision_unparseable", raw=raw[:200])
            decision = {}

        route = decision.get("route")
        clarify_question = decision.get("clarify_question")
        if route not in {"sales", "account", "clarify", "escalate"}:
            route = "clarify"
            clarify_question = clarify_question or DEFAULT_CLARIFY_QUESTION

        return {
            "route": route,
            "sales_intent": decision.get("sales_intent"),
            "order_ref": decision.get("order_ref"),
            "clarify_question": clarify_question,
            "escalate_summary": decision.get("escalate_summary"),
            "escalate_reason": decision.get("escalate_reason"),
            "price_filters": price_filters,
        }

    return router_node


def make_sales_agent_node(sales_agent: SubAgent) -> Node:
    async def sales_agent_node(state: GraphState) -> dict[str, Any]:
        query = state["messages"][-1].content
        filters = state.get("price_filters") or {}
        intent = state.get("sales_intent") or "listings"

        if intent == "policy":
            result = await sales_agent.call_tool("search_policy_docs", {"query": query, "limit": 5})
        else:
            result = await sales_agent.call_tool(
                "search_listings", {"query": query, "limit": 5, **filters}
            )
        return {"tool_result": result}

    return sales_agent_node


def make_account_agent_node(account_agent: SubAgent) -> Node:
    async def account_agent_node(state: GraphState) -> dict[str, Any]:
        order_ref = state.get("order_ref")
        if not order_ref:
            return {
                "route": "clarify",
                "clarify_question": "What's your order reference number?",
            }

        # Identity is bound to the contextvar right at the MCP server
        # boundary, for exactly the duration of this tool call - see
        # state.py's docstring and CLAUDE.md's Core Security Invariant.
        with bind_identity(state["identity"]):
            result = await account_agent.call_tool("get_order_status", {"order_ref": order_ref})
        return {"tool_result": result}

    return account_agent_node


def make_escalate_node(account_agent: SubAgent) -> Node:
    async def escalate_node(state: GraphState) -> dict[str, Any]:
        summary = state.get("escalate_summary") or "Customer requested human assistance."
        reason = state.get("escalate_reason") or "customer_requested"

        with bind_identity(state["identity"]):
            result = await account_agent.call_tool(
                "escalate_to_human", {"summary": summary, "reason": reason}
            )
        return {"tool_result": result}

    return escalate_node


def make_clarify_node() -> Node:
    async def clarify_node(state: GraphState) -> dict[str, Any]:
        question = state.get("clarify_question") or DEFAULT_CLARIFY_QUESTION
        return {"final_response": question}

    return clarify_node


def make_synthesis_node(llm: LLMProvider) -> Node:
    async def synthesis_node(state: GraphState) -> dict[str, Any]:
        settings = get_settings()
        tool_result_json = json.dumps(state.get("tool_result"), default=str)
        llm_messages = [
            Message(role="system", content=SYNTHESIS_SYSTEM_PROMPT),
            *state["messages"],
            Message(role="user", content=f"Tool result: {tool_result_json}"),
        ]
        response = llm.complete(llm_messages, model=settings.llm_model_synthesis)
        return {"final_response": response}

    return synthesis_node
