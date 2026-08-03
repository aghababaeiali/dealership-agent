"""A bounded tool-calling loop shared by every sub-agent.

The LLM sees its sub-agent's tool descriptions, decides whether to call
one, sees the result (or error), and decides whether to call another or
answer. Capped at MAX_ITERATIONS - hitting the cap is returned as an
explicit `hit_cap=True` result, never silently truncated or looped
forever. A tool call that raises is caught and fed back to the model as
an observation, so a broken tool degrades the conversation rather than
crashing the node.
"""

from __future__ import annotations

import json

import structlog

from dealership_agent.agents.state import ToolLoopResult
from dealership_agent.agents.tool_binding import SubAgent
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


async def run_tool_loop(
    llm: LLMProvider,
    model: str,
    sub_agent: SubAgent,
    system_prompt: str,
    user_query: str,
    *,
    max_iterations: int = MAX_ITERATIONS,
) -> ToolLoopResult:
    """Run `sub_agent`'s bounded tool-calling loop for one query.

    Returns a ToolLoopResult with either a final_answer or hit_cap=True -
    callers must handle hit_cap explicitly (e.g. escalate), never treat a
    capped-out loop as if it produced an answer.
    """
    instructions = LOOP_INSTRUCTIONS.format(tool_descriptions=_tool_descriptions(sub_agent))
    messages = [
        Message(role="system", content=f"{system_prompt}\n\n{instructions}"),
        Message(role="user", content=user_query),
    ]
    tool_calls: list[dict[str, object]] = []

    for iteration in range(max_iterations):
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
            # the same as hitting the iteration cap: an explicit terminal
            # state the caller must escalate, never a raised exception that
            # crashes the whole turn on a transient rate limit or timeout.
            return ToolLoopResult(final_answer=None, tool_calls=tool_calls, hit_cap=True)

        try:
            decision = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("tool_loop_unparseable_response", agent=sub_agent.name, raw=raw[:200])
            return ToolLoopResult(final_answer=raw, tool_calls=tool_calls, hit_cap=False)

        action = decision.get("action")

        if action == "final":
            return ToolLoopResult(
                final_answer=decision.get("answer", ""), tool_calls=tool_calls, hit_cap=False
            )

        if action != "call_tool":
            logger.warning("tool_loop_unknown_action", agent=sub_agent.name, decision=decision)
            return ToolLoopResult(final_answer=raw, tool_calls=tool_calls, hit_cap=False)

        tool_name = decision.get("tool")
        arguments = decision.get("arguments") or {}
        messages.append(Message(role="assistant", content=raw))

        try:
            result = await sub_agent.call_tool(tool_name, arguments)
            tool_calls.append({"tool": tool_name, "arguments": arguments, "result": result})
            observation = json.dumps({"tool": tool_name, "result": result}, default=str)
        except Exception as exc:  # noqa: BLE001 -- tool failures must degrade, never crash the node
            tool_calls.append({"tool": tool_name, "arguments": arguments, "error": str(exc)})
            observation = json.dumps({"tool": tool_name, "error": str(exc)})
            logger.warning(
                "tool_loop_tool_error", agent=sub_agent.name, tool=tool_name, error=str(exc)
            )

        messages.append(Message(role="user", content=f"Tool observation: {observation}"))
        logger.info(
            "tool_loop_iteration",
            agent=sub_agent.name,
            iteration=iteration + 1,
            tool=tool_name,
        )

    logger.warning("tool_loop_hit_cap", agent=sub_agent.name, max_iterations=max_iterations)
    return ToolLoopResult(final_answer=None, tool_calls=tool_calls, hit_cap=True)
