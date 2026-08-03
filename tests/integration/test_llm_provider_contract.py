"""Cross-provider contract test (Step 9, Part C3): the same tool-call
fixture, run against both real providers, must normalize to identical
internal structures - proving GroqProvider and BedrockProvider are
actually interchangeable behind the shared LLMProvider interface (see
docs/adr/0007-bedrock-provider-and-model-routing.md), not just
type-compatible.

Requires live credentials for BOTH providers (a real GROQ_API_KEY, and
real AWS credentials with Bedrock access) - marked
`requires_live_credentials` and excluded from default CI (see
pyproject.toml's marker registration and .github/workflows/ci.yml, which
never passes `-m requires_live_credentials`).
"""

from __future__ import annotations

import json

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError

from dealership_agent.config import get_settings
from dealership_agent.llm.base import Message
from dealership_agent.llm.bedrock_provider import BedrockProvider
from dealership_agent.llm.groq_provider import GroqProvider

settings = get_settings()

# The exact tool-call fixture both providers see - an unambiguous request
# that should elicit the same call_tool shape regardless of which model
# answers it.
_FIXTURE_SYSTEM_PROMPT = (
    "You are a tool-calling assistant. Respond with EXACTLY ONE line of "
    'JSON, no other text: {"action": "call_tool", "tool": "search_listings", '
    '"arguments": {"query": "<a short search query matching the request>"}}'
)
_FIXTURE_USER_MESSAGE = "Find me a cheap reliable family SUV."


def _aws_bedrock_credentials_available() -> bool:
    try:
        boto3.client("sts", region_name=settings.aws_region).get_caller_identity()
    except (BotoCoreError, ClientError):
        return False
    return True


def _groq_credentials_available() -> bool:
    return bool(settings.groq_api_key)


pytestmark = pytest.mark.requires_live_credentials


@pytest.mark.skipif(not _groq_credentials_available(), reason="GROQ_API_KEY is not configured")
@pytest.mark.skipif(
    not _aws_bedrock_credentials_available(),
    reason="No AWS credentials with Bedrock access available",
)
def test_groq_and_bedrock_normalize_identically() -> None:
    messages = [
        Message(role="system", content=_FIXTURE_SYSTEM_PROMPT),
        Message(role="user", content=_FIXTURE_USER_MESSAGE),
    ]

    groq = GroqProvider(
        api_key=settings.groq_api_key,
        max_retries=settings.llm_retry_max_retries,
        base_delay_seconds=settings.llm_retry_base_delay_seconds,
        max_delay_seconds=settings.llm_retry_max_delay_seconds,
    )
    bedrock = BedrockProvider(region=settings.aws_region)

    groq_raw = groq.complete(messages, model=settings.llm_model_classifier)
    bedrock_raw = bedrock.complete(messages, model=settings.bedrock_model_classifier)

    groq_parsed = json.loads(groq_raw)
    bedrock_parsed = json.loads(bedrock_raw)

    for parsed in (groq_parsed, bedrock_parsed):
        assert parsed["action"] == "call_tool"
        assert parsed["tool"] == "search_listings"
        assert isinstance(parsed["arguments"], dict)
        assert isinstance(parsed["arguments"].get("query"), str)
        assert parsed["arguments"]["query"]

    # Both normalize to the same key set - the actual query text is
    # model-generated free text and is not asserted equal between
    # providers, only that its *shape* (a non-empty string under
    # "query") matches.
    assert set(groq_parsed.keys()) == set(bedrock_parsed.keys())
    assert set(groq_parsed["arguments"].keys()) == set(bedrock_parsed["arguments"].keys())
