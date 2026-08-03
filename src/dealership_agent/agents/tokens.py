"""Token estimation shared by the tool-loop's token-budget guard and its
before/after compaction measurements (Step 7, Part B).

Neither provider CLAUDE.md specifies (Groq/Llama, AWS Bedrock) shares one
canonical tokenizer, and pinning to either one exactly would make the
other's estimate wrong. Rather than add a tokenizer dependency that's
only exactly right for one provider, this uses the standard ~4
characters-per-token approximation for English text that both OpenAI and
Anthropic publish as their own rule of thumb. It's consistently
approximate in the same direction for both providers, which is what a
*relative* before/after measurement and a budget-guard headroom check
need - it does not need to be exact, it needs to not let the loop walk
off a real provider's limit before noticing.
"""

from __future__ import annotations

from collections.abc import Iterable

from dealership_agent.llm.base import Message

CHARS_PER_TOKEN_ESTIMATE = 4


def estimate_tokens(text: str) -> int:
    """Rough token count for `text`. See module docstring for the caveat."""
    return max(1, len(text) // CHARS_PER_TOKEN_ESTIMATE)


def estimate_messages_tokens(messages: Iterable[Message]) -> int:
    return sum(estimate_tokens(message.content) for message in messages)
