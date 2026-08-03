"""Supervisor graph node implementations.

Every node is a plain async function of (state) -> partial state update,
built by a factory that closes over its dependencies (LLM provider, bound
sub-agents) - no globals, so tests can build a graph with a fake provider
and a real or stubbed MCP session with no live LLM call required.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from dealership_agent.agents.degradation import compute_degradation
from dealership_agent.agents.pricing import derive_price_filters
from dealership_agent.agents.prompts import (
    ACCOUNT_AGENT_SYSTEM_PROMPT,
    ROUTER_SYSTEM_PROMPT,
    SALES_AGENT_SYSTEM_PROMPT,
    SYNTHESIS_SYSTEM_PROMPT,
)
from dealership_agent.agents.state import GraphState, ToolLoopResult
from dealership_agent.agents.tool_binding import SubAgent
from dealership_agent.agents.tool_loop import run_tool_loop
from dealership_agent.config import get_settings
from dealership_agent.llm.base import LLMProvider, Message
from dealership_agent.tools.identity import bind_identity

logger = structlog.get_logger(__name__)

Node = Callable[[GraphState], Awaitable[dict[str, Any]]]

DEFAULT_CLARIFY_QUESTION = (
    "Could you tell me a bit more about what you're looking for - a "
    "vehicle, a dealership policy, or help with an existing order?"
)
VALID_ROUTES = {"sales", "account", "clarify", "escalate"}


def make_intake_node() -> Node:
    async def intake_node(state: GraphState) -> dict[str, Any]:
        messages = state.get("messages") or []
        last_content = messages[-1].content.strip() if messages else ""
        if not last_content:
            return {
                "routes": ["clarify"],
                "clarify_question": "I didn't catch that - what can I help you with?",
            }
        return {}

    return intake_node


def make_router_node(llm: LLMProvider) -> Node:
    async def router_node(state: GraphState) -> dict[str, Any]:
        # Already decided by intake (e.g. empty message) - don't re-route.
        if state.get("routes") == ["clarify"]:
            return {}

        settings = get_settings()
        messages = state["messages"]

        llm_messages = [Message(role="system", content=ROUTER_SYSTEM_PROMPT), *messages]
        raw = llm.complete(llm_messages, model=settings.llm_model_classifier)

        try:
            decision = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("router_decision_unparseable", raw=raw[:200])
            decision = {}

        routes = decision.get("routes")
        clarify_question = decision.get("clarify_question")
        if (
            not isinstance(routes, list)
            or not routes
            or not set(routes).issubset(VALID_ROUTES)
            or ("clarify" in routes and len(routes) > 1)
            or ("escalate" in routes and len(routes) > 1)
        ):
            routes = ["clarify"]
            clarify_question = clarify_question or DEFAULT_CLARIFY_QUESTION

        return {
            "routes": routes,
            "order_ref": decision.get("order_ref"),
            "price_filters": derive_price_filters(messages[-1].content),
            "clarify_question": clarify_question,
            "escalate_summary": decision.get("escalate_summary"),
            "escalate_reason": decision.get("escalate_reason"),
        }

    return router_node


def make_sales_agent_node(llm: LLMProvider, sales_agent: SubAgent) -> Node:
    async def sales_agent_node(state: GraphState) -> dict[str, Any]:
        settings = get_settings()
        original_query = state["messages"][-1].content
        price_filters = state.get("price_filters")

        query = f"Customer asked: {original_query}"
        if price_filters:
            # Grounded in docs/DATA_PRICE_AUDIT.md's real distribution
            # (agents/pricing.py) - the LLM must use these exact numbers
            # for vague price language, never invent its own.
            query += (
                f"\nThe customer used vague price language. Use exactly this "
                f"price filter if you call search_listings: {json.dumps(price_filters)}"
            )

        # Per-node model routing (CLAUDE.md): tool selection is a
        # constrained decision, so the loop uses the cheap classifier
        # model, not the stronger synthesis model.
        result = await run_tool_loop(
            llm=llm,
            model=settings.llm_model_classifier,
            sub_agent=sales_agent,
            system_prompt=SALES_AGENT_SYSTEM_PROMPT,
            user_query=query,
        )
        return {"sales_result": result}

    return sales_agent_node


def make_account_agent_node(llm: LLMProvider, account_agent: SubAgent) -> Node:
    async def account_agent_node(state: GraphState) -> dict[str, Any]:
        settings = get_settings()
        order_ref = state.get("order_ref")
        original_query = state["messages"][-1].content
        query = (
            f"Customer asked: {original_query}\n"
            f"Extracted order reference (may be empty): {order_ref or ''}"
        )

        # Identity is bound to the contextvar only inside the MCP server
        # subprocess (see docs/adr/0004-mcp-identity-propagation.md) - this
        # `bind_identity` call is for any in-process code path that also
        # checks `tools.identity.get_current_identity()` (e.g. logging);
        # the tool calls themselves are scoped by the subprocess's own
        # environment, established once at spawn time by agents/runner.py.
        with bind_identity(state["identity"]):
            result = await run_tool_loop(
                llm=llm,
                model=settings.llm_model_classifier,
                sub_agent=account_agent,
                system_prompt=ACCOUNT_AGENT_SYSTEM_PROMPT,
                user_query=query,
            )
        return {"account_result": result}

    return account_agent_node


def make_merge_node() -> Node:
    """Trivial fan-in point: sales_agent and account_agent both route here
    unconditionally, so the cap-hit check downstream always sees whichever
    result(s) actually ran, regardless of which branch(es) fired."""

    async def merge_node(state: GraphState) -> dict[str, Any]:  # noqa: ARG001
        return {}

    return merge_node


def any_result_needs_escalation(state: GraphState) -> bool:
    """True if a sub-agent's loop ended without any answer at all - the
    iteration cap, an LLM call failure, or the token budget guard firing
    (see agents/state.py's ToolLoopResult) - as opposed to a tool error
    the loop was able to recover from and still produce an answer for.
    Only the former needs escalation; the latter is handled by honest
    degradation in synthesis (Part C)."""
    for key in ("sales_result", "account_result"):
        result: ToolLoopResult | None = state.get(key)  # type: ignore[assignment]
        if result is not None and (
            result.get("hit_cap") or result.get("llm_call_failed") or result.get("hit_budget_guard")
        ):
            return True
    return False


def _escalation_cause(state: GraphState) -> tuple[str, str] | None:
    """Describe *which* of the three no-answer causes (see
    any_result_needs_escalation) fired, per sub-agent, so the escalation
    record and the customer-facing framing are specific rather than a
    generic "something went wrong" - or None if escalation wasn't
    triggered by a loop failure at all (e.g. the customer just asked for
    a human)."""
    causes: list[str] = []
    result_labels = (
        ("sales_result", "the sales lookup"),
        ("account_result", "the account lookup"),
    )
    for key, label in result_labels:
        result: ToolLoopResult | None = state.get(key)  # type: ignore[assignment]
        if result is None:
            continue
        if result.get("hit_cap"):
            causes.append(f"{label} reached its tool-call limit without finding an answer")
        if result.get("llm_call_failed"):
            causes.append(f"{label} hit a technical issue reaching the language model")
        if result.get("hit_budget_guard"):
            causes.append(f"{label}'s conversation grew too large to continue safely")
    if not causes:
        return None
    summary = (
        "The assistant could not fully resolve the customer's request: "
        + "; ".join(causes)
        + ". Escalating rather than failing silently."
    )
    return summary, "tool_loop_could_not_complete"


def make_escalate_node(account_agent: SubAgent) -> Node:
    async def escalate_node(state: GraphState) -> dict[str, Any]:
        cause = _escalation_cause(state)
        if cause is not None:
            summary, reason = cause
        else:
            summary = state.get("escalate_summary") or "Customer requested human assistance."
            reason = state.get("escalate_reason") or "customer_requested"

        identity = state["identity"]
        if identity.customer_id is None:
            # escalate_to_human is Account Agent's tool - customer-scoped,
            # per CLAUDE.md's security boundary - and genuinely cannot
            # persist a record for an unauthenticated session (the
            # escalations table requires a customer_id). An anonymous
            # sales conversation can still reach this node (e.g. an
            # iteration-cap hit), so this must degrade to a plain
            # "no ticket created" result rather than let the tool call
            # raise PermissionError and crash the turn.
            logger.warning("escalate_skipped_no_authenticated_customer", reason=reason)
            return {
                "escalate_result": {
                    "status": "not_created",
                    "reason": "no_authenticated_customer",
                }
            }

        with bind_identity(identity):
            result = await account_agent.call_tool(
                "escalate_to_human", {"summary": summary, "reason": reason}
            )
        return {"escalate_result": result}

    return escalate_node


def make_clarify_node() -> Node:
    async def clarify_node(state: GraphState) -> dict[str, Any]:
        question = state.get("clarify_question") or DEFAULT_CLARIFY_QUESTION
        return {"final_response": question}

    return clarify_node


def _build_synthesis_messages(
    state: GraphState, *, degradation_note: str | None = None
) -> list[Message]:
    payload = {
        "sales": state.get("sales_result"),
        "account": state.get("account_result"),
        "escalation": state.get("escalate_result"),
    }
    messages = [
        Message(role="system", content=SYNTHESIS_SYSTEM_PROMPT),
        *state["messages"],
        Message(role="user", content=f"Agent results: {json.dumps(payload, default=str)}"),
    ]
    if degradation_note:
        messages.append(Message(role="user", content=degradation_note))
    return messages


def _degradation_note(reasons: list[str]) -> str:
    return (
        "Internal note, do not quote verbatim: this turn did not fully complete "
        f"({', '.join(reasons)}). If this means part of the customer's request "
        "could not be answered, you MUST say so plainly in your reply and offer "
        "to try again or escalate - never present partial or missing results as "
        "if they were complete."
    )


def make_synthesis_node(llm: LLMProvider) -> Node:
    async def synthesis_node(state: GraphState) -> dict[str, Any]:
        settings = get_settings()
        # Part C: computed from what actually happened this turn, not
        # from anything the model says - fed into the prompt so synthesis
        # is *told*, in plain terms, when it must be honest about an
        # incomplete result rather than left to notice on its own.
        degraded, reasons = compute_degradation(state)
        note = _degradation_note(reasons) if degraded else None
        llm_messages = _build_synthesis_messages(state, degradation_note=note)
        response = llm.complete(llm_messages, model=settings.llm_model_synthesis)
        return {"final_response": response, "degraded": degraded, "degradation_reasons": reasons}

    return synthesis_node
