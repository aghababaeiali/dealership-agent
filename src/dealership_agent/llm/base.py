"""LLM provider interface.

CLAUDE.md: provider interface with two implementations - Groq (local dev)
and AWS Bedrock (prod). Per-node model routing: a cheap model for
classification/guardrails, a stronger model for final synthesis. Callers
pass the model name explicitly per call (from Settings.llm_model_classifier
/ llm_model_synthesis) - this interface never hardcodes a model.
"""

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class LLMProvider(ABC):
    @abstractmethod
    def complete(self, messages: list[Message], *, model: str) -> str:
        """Return the assistant's completion text for `messages`."""
