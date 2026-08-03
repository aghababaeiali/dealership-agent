"""AWS Bedrock LLM provider - prod, per CLAUDE.md.

Stubbed intentionally: CLAUDE.md's Anti-Over-Engineering Rules and this
task both say not to implement Bedrock yet. Groq covers local dev; wire
this up when there's an actual prod deployment to target.
"""

from dealership_agent.llm.base import LLMProvider, Message


class BedrockProvider(LLMProvider):
    def __init__(self, region: str) -> None:
        self._region = region

    def complete(self, messages: list[Message], *, model: str) -> str:
        raise NotImplementedError(
            "BedrockProvider is not implemented yet. Groq is used for local "
            "dev (LLM_PROVIDER=groq); Bedrock is prod-only and pending a "
            "real deployment target - see CLAUDE.md."
        )
