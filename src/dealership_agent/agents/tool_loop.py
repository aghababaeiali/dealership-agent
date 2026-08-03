"""A bounded tool-calling loop shared by every sub-agent.

The LLM sees its sub-agent's tool descriptions, decides whether to call
one, sees the result (or error), and decides whether to call another or
answer. Three distinct ways this can end without a real answer, tracked
separately (Step 7, Part C - synthesis needs to know which one to be
honest about it):

  - `hit_cap`: MAX_ITERATIONS reached without a "final" answer.
  - `llm_call_failed`: the LLM provider call itself raised (rate limit,
    timeout, connection error) - a broken tool degrades the conversation
    rather than crashing the node.
  - `hit_budget_guard`: the loop's own estimated cumulative token usage
    would exceed its budget on the next call, so it stops itself
    *before* making a call likely to fail with a 413/429, rather than
    finding out the hard way (Step 6's live smoke test hit exactly this).

Tool observations fed back into the loop's own message history are
compacted (agents/compaction.py) - never the raw tool-result JSON. The
raw result is still kept in the returned `tool_calls`, for synthesis,
tests, and logging.
"""

from __future__ import annotations

import json

import structlog

from dealership_agent.agents.compaction import compact_error, compact_tool_result
from dealership_agent.agents.state import ToolLoopResult
from dealership_agent.agents.tokens import estimate_messages_tokens, estimate_tokens
from dealership_agent.agents.tool_binding import SubAgent
from dealership_agent.config import get_settings
from dealership_agent.llm.base import LLMProvider, Message

logger = structlog.get_logger(__name__)

MAX_ITERATIONS = 5

LOOP_INSTRUCTIONS = """\
You have the following tools available:

{tool_descriptions}

For each turn, reply with ONLY a single JSON object (no other text), in \
one of these two shapes:

  {{"action": "call_tool", "tool": "<tool name>", "arguments": {{...}}}}
  {{"action": "final", "answer": "<your answer to the customer>"}}

Call a tool when you need information you don't already have. If a tool \
call returns zero results, try again with broader or different \
arguments (e.g. drop a filter) before giving up - do not immediately \
answer "no results" after a single narrow search. If a tool call fails, \
explain the limitation to the customer plainly rather than repeating the \
same failing call. Only reply with "final" once you can give the \
customer a real answer grounded in a tool result, or once you've made a \
genuine effort and should be honest that you couldn't find a match.
"""


def _tool_descriptions(sub_agent: SubAgent) -> str:
    lines = []
    for name, spec in sub_agent.tool_specs.items():
        lines.append(f"- {name}: {spec.description}\n  schema: {json.dumps(spec.input_schema)}")
    return "\n".join(lines)


def _loop_result(
    *,
    final_answer: str | None = None,
    tool_calls: list[dict[str, object]] | None = None,
    hit_cap: bool = False,
    llm_call_failed: bool = False,
    hit_budget_guard: bool = False,
) -> ToolLoopResult:
    return {
        "final_answer": final_answer,
        "tool_calls": tool_calls if tool_calls is not None else [],
        "hit_cap": hit_cap,
        "llm_call_failed": llm_call_failed,
        "hit_budget_guard": hit_budget_guard,
    }


async def run_tool_loop(
    llm: LLMProvider,
    model: str,
    sub_agent: SubAgent,
    system_prompt: str,
    user_query: str,
    *,
    max_iterations: int = MAX_ITERATIONS,
    max_observation_chars: int | None = None,
    token_budget: int | None = None,
) -> ToolLoopResult:
    """Run `sub_agent`'s bounded tool-calling loop for one query.

    Returns a ToolLoopResult with either a final_answer, or exactly one
    of hit_cap / llm_call_failed / hit_budget_guard set - callers must
    handle all three explicitly (e.g. escalate), never treat a
    non-final-answer result as if it produced one.
    """
    settings = get_settings()
    max_observation_chars = (
        max_observation_chars
        if max_observation_chars is not None
        else settings.loop_observation_max_chars
    )
    token_budget = token_budget if token_budget is not None else settings.loop_token_budget

    instructions = LOOP_INSTRUCTIONS.format(tool_descriptions=_tool_descriptions(sub_agent))
    messages = [
        Message(role="system", content=f"{system_prompt}\n\n{instructions}"),
        Message(role="user", content=user_query),
    ]
    tool_calls: list[dict[str, object]] = []

    for iteration in range(max_iterations):
        estimated_prompt_tokens = estimate_messages_tokens(messages)
        logger.info(
            "tool_loop_token_estimate",
            agent=sub_agent.name,
            iteration=iteration + 1,
            estimated_prompt_tokens=estimated_prompt_tokens,
            token_budget=token_budget,
        )
        if estimated_prompt_tokens >= token_budget:
            logger.warning(
                "tool_loop_budget_guard_fired",
                agent=sub_agent.name,
                iteration=iteration + 1,
                estimated_prompt_tokens=estimated_prompt_tokens,
                token_budget=token_budget,
            )
            return _loop_result(tool_calls=tool_calls, hit_budget_guard=True)

        try:
            raw = llm.complete(messages, model=model)
        except Exception as exc:  # noqa: BLE001 -- a provider hiccup must degrade, never crash the turn
            logger.warning(
                "tool_loop_llm_call_failed",
                agent=sub_agent.name,
                iteration=iteration + 1,
                error=str(exc),
            )
            # No answer is possible without a working LLM call - treat this
            # as an explicit terminal state the caller must escalate, never
            # a raised exception that crashes the whole turn.
            return _loop_result(tool_calls=tool_calls, llm_call_failed=True)

        try:
            decision = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("tool_loop_unparseable_response", agent=sub_agent.name, raw=raw[:200])
            return _loop_result(final_answer=raw, tool_calls=tool_calls)

        action = decision.get("action")

        if action == "final":
            return _loop_result(final_answer=decision.get("answer", ""), tool_calls=tool_calls)

        if action != "call_tool":
            logger.warning("tool_loop_unknown_action", agent=sub_agent.name, decision=decision)
            return _loop_result(final_answer=raw, tool_calls=tool_calls)

        tool_name = decision.get("tool")
        arguments = decision.get("arguments") or {}
        messages.append(Message(role="assistant", content=raw))

        try:
            result = await sub_agent.call_tool(tool_name, arguments)
            tool_calls.append({"tool": tool_name, "arguments": arguments, "result": result})
            compacted = compact_tool_result(result, max_chars=max_observation_chars)
            observation = json.dumps({"tool": tool_name, "result": compacted})
        except Exception as exc:  # noqa: BLE001 -- tool failures must degrade, never crash the node
            error_text = compact_error(str(exc), max_chars=max_observation_chars)
            tool_calls.append({"tool": tool_name, "arguments": arguments, "error": error_text})
            observation = json.dumps({"tool": tool_name, "error": error_text})
            logger.warning(
                "tool_loop_tool_error", agent=sub_agent.name, tool=tool_name, error=error_text
            )

        messages.append(Message(role="user", content=f"Tool observation: {observation}"))
        logger.info(
            "tool_loop_iteration",
            agent=sub_agent.name,
            iteration=iteration + 1,
            tool=tool_name,
            observation_tokens_estimate=estimate_tokens(observation),
        )

    logger.warning("tool_loop_hit_cap", agent=sub_agent.name, max_iterations=max_iterations)
    return _loop_result(tool_calls=tool_calls, hit_cap=True)
