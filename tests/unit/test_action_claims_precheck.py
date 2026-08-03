"""Unit tests for the cheap keyword pre-check (Step 8, Part C1) - the
gate that decides whether a draft could possibly contain an action claim
before spending any LLM call on it. No live LLM calls; this only tests
`mentions_action_vocabulary` directly."""

from dealership_agent.agents.action_claims import mentions_action_vocabulary


class TestMentionsActionVocabulary:
    def test_true_for_handoff_language(self) -> None:
        assert mentions_action_vocabulary("I've escalated this to a human agent.") is True

    def test_true_for_cancellation_language(self) -> None:
        assert mentions_action_vocabulary("Your order has been cancelled.") is True

    def test_true_for_refund_language(self) -> None:
        assert mentions_action_vocabulary("A refund has been issued.") is True

    def test_true_for_booking_language(self) -> None:
        assert mentions_action_vocabulary("I've booked your test drive for tomorrow.") is True

    def test_true_for_offer_language_too(self) -> None:
        # The pre-check only decides whether to look closer, not whether
        # something is a violation - offers mention the same vocabulary
        # and must still reach stage 1 for the real judgment.
        assert mentions_action_vocabulary("Would you like me to escalate this?") is True

    def test_false_for_pure_vehicle_description(self) -> None:
        assert (
            mentions_action_vocabulary(
                "We have a 2006 Chevrolet Equinox LT at $3,477.50 with 108,024 miles."
            )
            is False
        )

    def test_false_for_pure_clarifying_question(self) -> None:
        assert (
            mentions_action_vocabulary(
                "Could you tell me a bit more about what you're looking for?"
            )
            is False
        )

    def test_false_for_order_status_report(self) -> None:
        assert (
            mentions_action_vocabulary("Your order is confirmed and was created on August 3rd.")
            is False
        )
