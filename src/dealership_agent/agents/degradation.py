"""Compute honest degradation signals from graph state (Step 7, Part C).

`compute_degradation` inspects what actually happened this turn - not
what the LLM said happened - and returns a plain boolean plus a list of
named reasons monitoring can alert on individually. Used in two places:
synthesis_node feeds the reasons into its own prompt so the model is
told, in plain terms, when it must be honest about an incomplete result
(Part C2); and make_verify_claims_node appends its own reasons (an
action-claim correction or replacement) on top, after synthesis has
already run.
"""

from __future__ import annotations

from dealership_agent.agents.state import GraphState, ToolLoopResult


def _result_degradation_reasons(prefix: str, result: ToolLoopResult | None) -> list[str]:
    if result is None:
        return []
    reasons: list[str] = []
    if result.get("hit_cap"):
        reasons.append(f"{prefix}_hit_iteration_cap")
    if result.get("llm_call_failed"):
        reasons.append(f"{prefix}_llm_call_failed")
    if result.get("hit_budget_guard"):
        reasons.append(f"{prefix}_budget_guard_fired")
    for call in result.get("tool_calls", []):
        if "error" in call:
            reasons.append(f"{prefix}_tool_error:{call.get('tool')}")
    return reasons


def compute_degradation(state: GraphState) -> tuple[bool, list[str]]:
    """Return (degraded, reasons) from the tool-loop results and
    escalation outcome already present in `state`. Does not consider
    action-claim verification - that happens after synthesis and is
    merged in separately by make_verify_claims_node."""
    reasons = [
        *_result_degradation_reasons("sales", state.get("sales_result")),
        *_result_degradation_reasons("account", state.get("account_result")),
    ]
    escalate_result = state.get("escalate_result")
    if escalate_result is not None and escalate_result.get("status") == "not_created":
        reasons.append("escalation_not_created")
    return bool(reasons), reasons
