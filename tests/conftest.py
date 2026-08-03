"""Shared test fixtures across unit/integration/security tests."""

import pytest

from dealership_agent.llm.base import LLMProvider, Message

# Distinctive substrings from the start of each node's system prompt
# (agents/prompts.py) - used only to route a scripted response to the
# right per-agent queue in tests; production code never inspects prompt
# content like this.
_AGENT_PROMPT_MARKERS = {
    "router": "routing classifier",
    "sales": "sales assistant",
    "account": "account assistant",
    "synthesis": "helpful, honest assistant",
    "verifier": "strict fact-checker",
}


class FakeLLMProvider(LLMProvider):
    """Returns pre-scripted responses per agent, in order, with NO live
    LLM call. Multi-scope turns run sales_agent and account_agent
    concurrently (see agents/graph.py's fan-out), so responses are keyed
    per agent rather than pulled from one shared queue - a single shared
    queue can't be scripted deterministically when two agents' calls may
    interleave in either order.

    Records every call's messages (per agent) so tests can inspect
    exactly what was sent to the "LLM", including tool-loop observations.
    """

    def __init__(self, responses: dict[str, list[str]]) -> None:
        self._queues: dict[str, list[str]] = {key: list(value) for key, value in responses.items()}
        self.calls: list[list[Message]] = []
        self.calls_by_agent: dict[str, list[list[Message]]] = {key: [] for key in responses}

    def complete(self, messages: list[Message], *, model: str) -> str:
        self.calls.append(messages)
        agent = self._classify(messages)
        self.calls_by_agent.setdefault(agent, []).append(messages)

        queue = self._queues.get(agent)
        if not queue:
            raise AssertionError(
                f"FakeLLMProvider: no more scripted responses for agent {agent!r} "
                f"(known agents: {sorted(self._queues)})"
            )
        return queue.pop(0)

    @staticmethod
    def _classify(messages: list[Message]) -> str:
        system = messages[0].content if messages and messages[0].role == "system" else ""
        for agent, marker in _AGENT_PROMPT_MARKERS.items():
            if marker in system:
                return agent
        return "unknown"


@pytest.fixture
def fake_llm_provider() -> type[FakeLLMProvider]:
    return FakeLLMProvider
