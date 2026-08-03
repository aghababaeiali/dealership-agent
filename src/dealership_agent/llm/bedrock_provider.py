"""AWS Bedrock LLM provider - prod, per CLAUDE.md.

Implemented against the Bedrock Converse API (bedrock-runtime's unified,
model-agnostic chat interface). This provider's `complete()` deliberately
mirrors GroqProvider's: dealership-agent's own tool-calling loop
(agents/tool_loop.py) is a structured-JSON protocol carried *inside* the
completion text (see tool_loop.py's system prompt), not a provider's
native function-calling - so no Converse `toolConfig` is passed here.
Both providers speak the exact same plain-text LLMProvider.complete
contract, which is what makes the cross-provider contract test
(tests/integration/test_llm_provider_contract.py) meaningful: the same
scripted tool-call text, normalized identically, regardless of which
provider produced it.

Credentials: the standard boto3 credential chain (env vars, shared
config/credentials file, or an ECS task role in prod) - never hardcoded,
never read from Settings.
"""

from __future__ import annotations

import boto3
from mypy_boto3_bedrock_runtime.type_defs import MessageTypeDef, SystemContentBlockTypeDef

from dealership_agent.llm.base import LLMProvider, Message, normalize_llm_response


class BedrockProvider(LLMProvider):
    def __init__(self, region: str) -> None:
        self._client = boto3.client("bedrock-runtime", region_name=region)

    def complete(self, messages: list[Message], *, model: str) -> str:
        system: list[SystemContentBlockTypeDef] = [
            {"text": m.content} for m in messages if m.role == "system"
        ]
        converse_messages: list[MessageTypeDef] = [
            {"role": m.role, "content": [{"text": m.content}]}
            for m in messages
            if m.role != "system"
        ]
        response = self._client.converse(
            modelId=model,
            messages=converse_messages,
            system=system,
        )
        content = response["output"]["message"]["content"]
        raw = "".join(block["text"] for block in content if "text" in block)
        return normalize_llm_response(raw)
