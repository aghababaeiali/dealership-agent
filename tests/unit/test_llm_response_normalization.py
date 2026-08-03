"""Step 10, Part A2: response normalization must behave identically
regardless of which provider produced the raw text - Claude on Bedrock
tends to wrap JSON in markdown fences (sometimes with prose around it);
Groq/Llama usually doesn't. Same fixtures run against the shared
normalizer directly, and against both GroqProvider.complete() and
BedrockProvider.complete() with their underlying SDK clients faked out,
so a provider forgetting to call the normalizer would be caught here too.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from dealership_agent.llm.base import Message, normalize_llm_response
from dealership_agent.llm.bedrock_provider import BedrockProvider
from dealership_agent.llm.groq_provider import GroqProvider

BARE_JSON = '{"routes": ["sales"]}'
FENCED_JSON = '```\n{"routes": ["sales"]}\n```'
FENCED_JSON_WITH_LANG_TAG = '```json\n{"routes": ["sales"]}\n```'
JSON_WITH_PROSE_PREAMBLE = 'Sure, here is the routing decision:\n{"routes": ["sales"]}'
JSON_WITH_PROSE_ON_BOTH_SIDES = (
    'Here you go:\n{"routes": ["sales"]}\nLet me know if you need anything else.'
)
MALFORMED_TRUNCATED_JSON = '{"routes": ["sales"'
MALFORMED_NOT_JSON_AT_ALL = "I'm not sure how to route this one."
# A real live-observed Bedrock/Claude quirk (Step 10, Part A3): Claude
# falls back to its own tool-call convention - wrapping the single
# expected object as a one-element array inside a <function_calls> tag -
# even though no native toolConfig was offered.
FUNCTION_CALLS_TAG_WRAPPED_ARRAY = '\n<function_calls>\n[{"routes": ["sales"]}]\n</function_calls>'
# Also live-observed: Claude drops the "action" discriminator entirely,
# replying with its own bare tool-call shape.
MISSING_ACTION_CALL_TOOL_SHAPE = (
    '{"tool": "search_listings", "arguments": {"query": "SUV", "price_max": 30000}}'
)
MISSING_ACTION_FINAL_SHAPE = '{"answer": "Here are a few SUVs in stock."}'

EXPECTED = '{"routes": ["sales"]}'
EXPECTED_CALL_TOOL_WITH_INFERRED_ACTION = (
    '{"action": "call_tool", "tool": "search_listings", '
    '"arguments": {"query": "SUV", "price_max": 30000}}'
)
EXPECTED_FINAL_WITH_INFERRED_ACTION = (
    '{"action": "final", "answer": "Here are a few SUVs in stock."}'
)


class TestNormalizeLlmResponse:
    @pytest.mark.parametrize(
        "raw",
        [BARE_JSON, FENCED_JSON, FENCED_JSON_WITH_LANG_TAG, JSON_WITH_PROSE_PREAMBLE],
    )
    def test_extracts_the_bare_json_object(self, raw: str) -> None:
        assert normalize_llm_response(raw) == EXPECTED

    def test_strips_prose_on_both_sides_of_the_json(self) -> None:
        assert normalize_llm_response(JSON_WITH_PROSE_ON_BOTH_SIDES) == EXPECTED

    def test_unwraps_a_function_calls_tag_wrapped_single_element_array(self) -> None:
        assert normalize_llm_response(FUNCTION_CALLS_TAG_WRAPPED_ARRAY) == EXPECTED

    def test_infers_call_tool_action_when_omitted(self) -> None:
        assert (
            normalize_llm_response(MISSING_ACTION_CALL_TOOL_SHAPE)
            == EXPECTED_CALL_TOOL_WITH_INFERRED_ACTION
        )

    def test_infers_final_action_when_omitted(self) -> None:
        assert (
            normalize_llm_response(MISSING_ACTION_FINAL_SHAPE)
            == EXPECTED_FINAL_WITH_INFERRED_ACTION
        )

    def test_does_not_inject_action_into_unrelated_json_shapes(self) -> None:
        # A router decision has neither (tool + arguments) nor answer -
        # the inference must never fire here.
        router_shape = '{"routes": ["sales"], "order_ref": null}'
        assert normalize_llm_response(router_shape) == router_shape

    def test_truncated_json_is_returned_unchanged_not_raised(self) -> None:
        # No balanced span exists - normalization is a no-op here, and
        # the caller's own json.loads() is what fails, exactly as before
        # this change. This function must never raise.
        result = normalize_llm_response(MALFORMED_TRUNCATED_JSON)
        assert result == MALFORMED_TRUNCATED_JSON
        with pytest.raises(Exception):  # noqa: B017 -- asserting json.loads still fails cleanly
            import json

            json.loads(result)

    def test_non_json_plain_text_passes_through_unchanged(self) -> None:
        assert normalize_llm_response(MALFORMED_NOT_JSON_AT_ALL) == MALFORMED_NOT_JSON_AT_ALL

    def test_plain_customer_facing_prose_is_never_mangled(self) -> None:
        # A normal synthesis answer - no fences, no braces. Must be a
        # complete no-op; this is the case normalization must never break.
        prose = "We have a 2020 Honda CR-V in stock for $18,500 with 62,000 miles."
        assert normalize_llm_response(prose) == prose


class _FakeGroqClient:
    """Fakes just enough of groq.Groq's surface for GroqProvider.complete()."""

    def __init__(self, raw_content: str) -> None:
        self._raw_content = raw_content
        message = SimpleNamespace(content=self._raw_content)
        choice = SimpleNamespace(message=message)
        response = SimpleNamespace(choices=[choice])
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: response))


class _FakeBedrockClient:
    """Fakes just enough of a boto3 bedrock-runtime client for
    BedrockProvider.complete()."""

    def __init__(self, raw_content: str) -> None:
        self._raw_content = raw_content

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        return {"output": {"message": {"content": [{"text": self._raw_content}]}}}


class TestBothProvidersNormalizeIdentically:
    @pytest.mark.parametrize(
        "raw",
        [
            BARE_JSON,
            FENCED_JSON,
            FENCED_JSON_WITH_LANG_TAG,
            JSON_WITH_PROSE_PREAMBLE,
            JSON_WITH_PROSE_ON_BOTH_SIDES,
            FUNCTION_CALLS_TAG_WRAPPED_ARRAY,
        ],
    )
    def test_groq_provider_normalizes(self, raw: str) -> None:
        provider = GroqProvider(api_key="fake-key")
        provider._client = _FakeGroqClient(raw)  # type: ignore[assignment]
        result = provider.complete([Message(role="user", content="find me an SUV")], model="m")
        assert result == EXPECTED

    @pytest.mark.parametrize(
        "raw",
        [
            BARE_JSON,
            FENCED_JSON,
            FENCED_JSON_WITH_LANG_TAG,
            JSON_WITH_PROSE_PREAMBLE,
            JSON_WITH_PROSE_ON_BOTH_SIDES,
            FUNCTION_CALLS_TAG_WRAPPED_ARRAY,
        ],
    )
    def test_bedrock_provider_normalizes(self, raw: str) -> None:
        provider = BedrockProvider(region="eu-west-1")
        provider._client = _FakeBedrockClient(raw)  # type: ignore[assignment]
        result = provider.complete([Message(role="user", content="find me an SUV")], model="m")
        assert result == EXPECTED

    def test_malformed_json_fails_cleanly_through_both_providers(self) -> None:
        for provider, fake_client in (
            (GroqProvider(api_key="fake-key"), _FakeGroqClient(MALFORMED_TRUNCATED_JSON)),
            (BedrockProvider(region="eu-west-1"), _FakeBedrockClient(MALFORMED_TRUNCATED_JSON)),
        ):
            provider._client = fake_client  # type: ignore[assignment]
            result = provider.complete([Message(role="user", content="x")], model="m")
            assert result == MALFORMED_TRUNCATED_JSON
