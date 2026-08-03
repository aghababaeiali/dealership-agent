"""Supervisor graph: intake -> router -> (sales_agent | account_agent |
clarify | escalate) -> synthesis.

CLAUDE.md: Sales Agent (search_listings, search_policy_docs) and Account
Agent (get_order_status, escalate_to_human) are bound to disjoint tool
sets - see tool_binding.py, which enforces this at construction. This
module only wires graph control flow; it never binds a tool itself.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from dealership_agent.agents.nodes import (
    make_account_agent_node,
    make_clarify_node,
    make_escalate_node,
    make_intake_node,
    make_router_node,
    make_sales_agent_node,
    make_synthesis_node,
)
from dealership_agent.agents.state import GraphState
from dealership_agent.agents.tool_binding import (
    build_account_agent,
    build_sales_agent,
)
from dealership_agent.llm.base import LLMProvider


def _route_after_intake(state: GraphState) -> str:
    return "clarify" if state.get("route") == "clarify" else "router"


def _route_after_router(state: GraphState) -> str:
    route = state.get("route")
    if route in {"sales", "account", "clarify", "escalate"}:
        return route
    return "clarify"


def _route_after_account_agent(state: GraphState) -> str:
    # account_agent_node redirects to clarify itself if order_ref is missing.
    return "clarify" if state.get("route") == "clarify" else "synthesis"


async def build_supervisor_graph(llm: LLMProvider) -> CompiledStateGraph[GraphState]:
    sales_agent = await build_sales_agent()
    account_agent = await build_account_agent()

    # LangGraph's add_node overloads don't structurally match a plain
    # `Callable[[GraphState], Awaitable[dict[str, Any]]]` alias under
    # strict mypy, even though this is the standard, working runtime
    # pattern for async node functions - see langgraph's own examples.
    graph = StateGraph(GraphState)
    graph.add_node("intake", make_intake_node())  # type: ignore[call-overload]
    graph.add_node("router", make_router_node(llm))  # type: ignore[call-overload]
    graph.add_node("sales_agent", make_sales_agent_node(sales_agent))  # type: ignore[call-overload]
    graph.add_node("account_agent", make_account_agent_node(account_agent))  # type: ignore[call-overload]
    graph.add_node("escalate", make_escalate_node(account_agent))  # type: ignore[call-overload]
    graph.add_node("clarify", make_clarify_node())  # type: ignore[call-overload]
    graph.add_node("synthesis", make_synthesis_node(llm))  # type: ignore[call-overload]

    graph.add_edge(START, "intake")
    graph.add_conditional_edges(
        "intake", _route_after_intake, {"clarify": "clarify", "router": "router"}
    )
    graph.add_conditional_edges(
        "router",
        _route_after_router,
        {
            "sales": "sales_agent",
            "account": "account_agent",
            "clarify": "clarify",
            "escalate": "escalate",
        },
    )
    graph.add_edge("sales_agent", "synthesis")
    graph.add_conditional_edges(
        "account_agent",
        _route_after_account_agent,
        {"clarify": "clarify", "synthesis": "synthesis"},
    )
    graph.add_edge("escalate", "synthesis")
    graph.add_edge("clarify", END)
    graph.add_edge("synthesis", END)

    return graph.compile()
