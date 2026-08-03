"""Unit tests for the loop's token estimator (agents/tokens.py)."""

from dealership_agent.agents.tokens import estimate_messages_tokens, estimate_tokens
from dealership_agent.llm.base import Message


class TestEstimateTokens:
    def test_roughly_four_characters_per_token(self) -> None:
        assert estimate_tokens("a" * 400) == 100

    def test_never_returns_zero_for_nonempty_text(self) -> None:
        assert estimate_tokens("hi") == 1

    def test_empty_string_is_at_least_one(self) -> None:
        assert estimate_tokens("") == 1


class TestEstimateMessagesTokens:
    def test_sums_across_all_messages(self) -> None:
        messages = [
            Message(role="system", content="a" * 400),
            Message(role="user", content="b" * 400),
        ]
        assert estimate_messages_tokens(messages) == 200

    def test_empty_message_list_is_zero(self) -> None:
        assert estimate_messages_tokens([]) == 0
