"""Groq LLM provider - used for local dev per CLAUDE.md."""

from groq import Groq

from dealership_agent.llm.base import LLMProvider, Message


class GroqProvider(LLMProvider):
    def __init__(self, api_key: str) -> None:
        self._client = Groq(api_key=api_key)

    def complete(self, messages: list[Message], *, model: str) -> str:
        response = self._client.chat.completions.create(
            model=model,
            messages=[{"role": m.role, "content": m.content} for m in messages],  # type: ignore[misc]
        )
        content = response.choices[0].message.content
        return content or ""
