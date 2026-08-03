"""Shared test fixtures across unit/integration/security tests."""

import pytest

from dealership_agent.llm.base import LLMProvider, Message


class FakeLLMProvider(LLMProvider):
    """Returns pre-scripted responses in order; records every call's
    messages so tests can inspect exactly what was sent to the "LLM"
    without any live LLM call."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[Message]] = []

    def complete(self, messages: list[Message], *, model: str) -> str:
        self.calls.append(messages)
        if not self._responses:
            raise AssertionError("FakeLLMProvider: no more scripted responses")
        return self._responses.pop(0)


@pytest.fixture
def fake_llm_provider() -> type[FakeLLMProvider]:
    return FakeLLMProvider
