"""Supervisor graph:

    intake -> router -> fan-out to {sales_agent, account_agent} -> merge
              -> (escalate | synthesis)
    router -> clarify -> END
    router -> escalate -> synthesis -> verify_claims -> END
    escalate -> synthesis -> verify_claims -> END
    synthesis -> verify_claims -> END

CLAUDE.md: Sales Agent (search_listings, search_policy_docs) and Account
Agent (get_order_status, list_my_orders, escalate_to_human) are bound to
disjoint tool sets - see tool_binding.py, which enforces this at
construction. This module only wires graph control flow; it never binds
a tool itself.

A single turn can route to BOTH sub-agents (e.g. "find me a cheap SUV and
tell me if my order shipped"): the router may return
`routes: ["sales", "account"]`, LangGraph runs both nodes, and they always
converge on the `merge` node before either `escalate` (if either hit its
tool-call cap) or `synthesis`, which combines whatever result(s) are
present into one reply. Each sub-agent still only ever sees its own tools.

`verify_claims` (Step 7, Part A) is a second, independent pass after
synthesis: it checks the drafted reply for action claims (a human
handoff, a cancellation, a booking, a refund) that aren't backed by a
real tool result, and corrects or replaces the reply if it finds one.
`clarify`'s response is a deterministic, hardcoded question - never
LLM-generated free text - so it skips verification entirely.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from dealership_agent.agents.nodes import (
    any_result_needs_escalation,
    make_account_agent_node,
    make_clarify_node,
    make_escalate_node,
    make_intake_node,
    make_merge_node,
    make_router_node,
    make_sales_agent_node,
    make_synthesis_node,
    make_verify_claims_node,
)
from dealership_agent.agents.state import GraphState
from dealership_agent.agents.tool_binding import SubAgent
from dealership_agent.llm.base import LLMProvider


def _route_after_intake(state: GraphState) -> str:
    return "clarify" if state.get("routes") == ["clarify"] else "router"


def _routes_after_router(state: GraphState) -> list[str]:
    routes = state.get("routes") or []
    if "clarify" in routes:
        return ["clarify"]
    if "escalate" in routes:
        return ["escalate"]
    destinations = []
    if "sales" in routes:
        destinations.append("sales_agent")
    if "account" in routes:
        destinations.append("account_agent")
    return destinations or ["clarify"]


def _route_after_merge(state: GraphState) -> str:
    return "escalate" if any_result_needs_escalation(state) else "synthesis"


def build_supervisor_graph(
    llm: LLMProvider, sales_agent: SubAgent, account_agent: SubAgent
) -> CompiledStateGraph[GraphState]:
    """Wire the supervisor graph given already-bound sub-agents.

    Sub-agents must be built from a live MCP ClientSession before calling
    this (see agents/runner.py::run_turn) - this function does no I/O
    itself, only graph construction.
    """
    # LangGraph's add_node overloads don't structurally match a plain
    # `Callable[[GraphState], Awaitable[dict[str, Any]]]` alias under
    # strict mypy, even though this is the standard, working runtime
    # pattern for async node functions - see langgraph's own examples.
    graph = StateGraph(GraphState)
    graph.add_node("intake", make_intake_node())  # type: ignore[call-overload]
    graph.add_node("router", make_router_node(llm))  # type: ignore[call-overload]
    graph.add_node("sales_agent", make_sales_agent_node(llm, sales_agent))  # type: ignore[call-overload]
    graph.add_node("account_agent", make_account_agent_node(llm, account_agent))  # type: ignore[call-overload]
    graph.add_node("merge", make_merge_node())  # type: ignore[call-overload]
    graph.add_node("escalate", make_escalate_node(account_agent))  # type: ignore[call-overload]
    graph.add_node("clarify", make_clarify_node())  # type: ignore[call-overload]
    graph.add_node("synthesis", make_synthesis_node(llm))  # type: ignore[call-overload]
    graph.add_node("verify_claims", make_verify_claims_node(llm))  # type: ignore[call-overload]

    graph.add_edge(START, "intake")
    graph.add_conditional_edges(
        "intake", _route_after_intake, {"clarify": "clarify", "router": "router"}
    )
    graph.add_conditional_edges(
        "router",
        _routes_after_router,
        {
            "sales_agent": "sales_agent",
            "account_agent": "account_agent",
            "clarify": "clarify",
            "escalate": "escalate",
        },
    )
    # Both sub-agents converge unconditionally on `merge`, so `merge`'s
    # cap-hit check always sees the full picture regardless of which
    # branch(es) ran - see nodes.py::make_merge_node.
    graph.add_edge("sales_agent", "merge")
    graph.add_edge("account_agent", "merge")
    graph.add_conditional_edges(
        "merge", _route_after_merge, {"escalate": "escalate", "synthesis": "synthesis"}
    )
    graph.add_edge("escalate", "synthesis")
    graph.add_edge("clarify", END)
    graph.add_edge("synthesis", "verify_claims")
    graph.add_edge("verify_claims", END)

    return graph.compile()
