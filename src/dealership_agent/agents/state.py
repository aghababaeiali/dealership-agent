"""Supervisor graph state.

CLAUDE.md's Core Security Invariant: the Session/identity object lives in
LangGraph state, NEVER in any prompt, message, system instruction, or tool
argument. `identity` is a field of this state - it is never read into a
`Message.content` string anywhere in agents/nodes.py, and it is bound to
the tools.identity contextvar only server-side, inside the MCP server
subprocess, at spawn time (see agents/mcp_session.py and
docs/adr/0004-mcp-identity-propagation.md) - not per node, not per call.
"""

from __future__ import annotations

from typing import Any, TypedDict

from dealership_agent.agents.pricing import PriceFilters
from dealership_agent.llm.base import Message
from dealership_agent.tools.identity import RequestIdentity


class ToolLoopResult(TypedDict):
    """The outcome of one sub-agent's bounded tool-calling loop.

    `hit_cap`, `llm_call_failed`, and `hit_budget_guard` are three distinct
    ways a loop can end without a real answer - kept separate (rather than
    one generic "failed" bool) so synthesis and monitoring can say
    something honest and specific about which one happened, per Step 7's
    Part C. A loop can only hit one of these per run (each is a terminal
    return), but callers should not assume that - check all three.
    """

    final_answer: str | None
    tool_calls: list[dict[str, Any]]
    hit_cap: bool
    llm_call_failed: bool
    hit_budget_guard: bool


class GraphState(TypedDict, total=False):
    messages: list[Message]
    identity: RequestIdentity

    # Router output. `routes` is a subset of {"sales", "account"} for a
    # normal (possibly multi-scope) turn, or exactly ["clarify"] /
    # ["escalate"] for those exclusive outcomes.
    routes: list[str]
    order_ref: str | None
    # Deterministically derived from vague price language ("cheap" ->
    # price_max), never left for the LLM to invent a number - see
    # agents/pricing.py. Passed to the sales tool loop as grounding
    # context so it can construct a correct search_listings call.
    price_filters: PriceFilters | None
    clarify_question: str | None
    escalate_summary: str | None
    escalate_reason: str | None

    sales_result: ToolLoopResult | None
    account_result: ToolLoopResult | None
    escalate_result: dict[str, Any] | None

    final_response: str | None
