"""Step 7, Part A: the account agent must never tell a customer an action
was taken (a human handoff, a cancellation, a booking, a refund) unless a
real tool result backs that claim up. Step 6's live smoke test showed the
account agent claiming "this conversation will now be handed off to a
human agent" after 3 failed order-ref guesses, with no escalate_to_human
call ever made - this file is the regression test for exactly that, plus
the surrounding cases the verification layer (agents/action_claims.py,
agents/nodes.py::make_verify_claims_node) needs to get right:

  - a false handoff claim with no real escalate_result -> invalid,
    corrected or replaced, and the violation is logged
  - the same claim WITH a real, successful escalate_result -> valid,
    passed through unchanged
  - a normal informational answer -> valid, no false positive
  - conversation 3 reproduced exactly: bare "where is my order?" with a
    bound identity - the account agent must call list_my_orders, ask a
    clarifying question, or genuinely escalate; it must never fabricate
    a handoff claim that reaches the customer.

Runs against the real MCP stdio transport and real Postgres, no live LLM
calls - FakeLLMProvider scripts the verifier's responses too now (see
tests/conftest.py's "verifier" queue).
"""

import json
import uuid
from collections.abc import Iterator
from typing import TypedDict

import pytest
import structlog.testing
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from dealership_agent.agents.action_claims import SAFE_FALLBACK_TEXT
from dealership_agent.agents.runner import run_turn
from dealership_agent.config import get_settings
from dealership_agent.llm.base import Message
from dealership_agent.tools.identity import RequestIdentity

settings = get_settings()

FALSE_HANDOFF_CLAIM = (
    "This conversation will now be handed off to a human agent who will follow up shortly."
)


class OrderFixture(TypedDict):
    customer_id: int
    order_ref: str
    vehicle_id: int


@pytest.fixture(scope="module")
def owner_engine() -> Iterator[Engine]:
    engine = create_engine(settings.database_migration_url)
    yield engine
    engine.dispose()


@pytest.fixture
def customer_with_order(owner_engine: Engine) -> Iterator[OrderFixture]:
    suffix = uuid.uuid4().hex[:8]
    with owner_engine.begin() as conn:
        vehicle_id = conn.execute(
            text(
                "INSERT INTO vehicles (external_ref, year, make, model, mileage, is_available) "
                "VALUES (:ref, 2024, 'Test', 'Model', 10000, true) RETURNING id"
            ),
            {"ref": f"claim-veh-{suffix}"},
        ).scalar_one()
        customer_id = conn.execute(
            text(
                "INSERT INTO customers (external_ref, email, full_name) "
                "VALUES (:ref, :email, 'Action Claim Test Customer') RETURNING id"
            ),
            {"ref": f"claim-cust-{suffix}", "email": f"claim-{suffix}@example.com"},
        ).scalar_one()
        order_ref = conn.execute(
            text(
                "INSERT INTO orders (order_ref, customer_id, vehicle_id, status, total_amount) "
                "VALUES (:ref, :cust, :veh, 'confirmed', 21999.00) RETURNING order_ref"
            ),
            {"ref": f"claim-order-{suffix}", "cust": customer_id, "veh": vehicle_id},
        ).scalar_one()

    yield {"customer_id": customer_id, "order_ref": order_ref, "vehicle_id": vehicle_id}

    with owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM escalations WHERE customer_id = :c"), {"c": customer_id})
        conn.execute(text("DELETE FROM orders WHERE customer_id = :c"), {"c": customer_id})
        conn.execute(text("DELETE FROM customers WHERE id = :c"), {"c": customer_id})
        conn.execute(text("DELETE FROM vehicles WHERE id = :v"), {"v": vehicle_id})


class TestFalseHandoffClaimWithNoEscalation:
    async def test_false_claim_is_corrected_and_logged(self, fake_llm_provider: type) -> None:
        fake = fake_llm_provider(
            {
                "router": [json.dumps({"routes": ["sales"]})],
                "sales": [
                    json.dumps(
                        {
                            "action": "call_tool",
                            "tool": "search_listings",
                            "arguments": {"query": "reliable sedan"},
                        }
                    ),
                    json.dumps({"action": "final", "answer": "Found some reliable sedans."}),
                ],
                # Synthesis fabricates a handoff claim, exactly like Step
                # 6's live failure - no escalate route was ever taken, so
                # there is no escalate_result to back this up.
                "synthesis": [
                    FALSE_HANDOFF_CLAIM,
                    "Here are some reliable sedans I found for you.",
                ],
                "verifier": [
                    json.dumps(
                        {
                            "claims": [
                                {
                                    "type": "human_handoff",
                                    "quote": FALSE_HANDOFF_CLAIM,
                                    "substantiated": False,
                                }
                            ]
                        }
                    ),
                    json.dumps({"claims": []}),
                ],
            }
        )
        identity = RequestIdentity(session_id="sess-false-handoff", customer_id=None)

        with structlog.testing.capture_logs() as logs:
            result = await run_turn(
                fake, identity, [Message(role="user", content="Do you have any reliable sedans?")]
            )

        assert result["final_response"] == "Here are some reliable sedans I found for you."
        assert FALSE_HANDOFF_CLAIM not in (result["final_response"] or "")
        assert result["degraded"] is True
        assert "action_claim_corrected" in result["degradation_reasons"]

        violation_logs = [log for log in logs if log.get("event") == "action_claim_violation"]
        assert len(violation_logs) == 1
        assert violation_logs[0]["log_level"] == "warning"
        assert violation_logs[0]["draft"] == FALSE_HANDOFF_CLAIM

    async def test_false_claim_is_replaced_with_fallback_if_correction_also_fails(
        self, fake_llm_provider: type
    ) -> None:
        """If even the regenerated draft still makes an unsubstantiated
        claim, the customer must get the deterministic safe fallback -
        never a second fabricated claim."""
        fake = fake_llm_provider(
            {
                "router": [json.dumps({"routes": ["sales"]})],
                "sales": [
                    json.dumps(
                        {
                            "action": "call_tool",
                            "tool": "search_listings",
                            "arguments": {"query": "reliable sedan"},
                        }
                    ),
                    json.dumps({"action": "final", "answer": "Found some reliable sedans."}),
                ],
                "synthesis": [FALSE_HANDOFF_CLAIM, FALSE_HANDOFF_CLAIM],
                "verifier": [
                    json.dumps(
                        {
                            "claims": [
                                {
                                    "type": "human_handoff",
                                    "quote": FALSE_HANDOFF_CLAIM,
                                    "substantiated": False,
                                }
                            ]
                        }
                    ),
                    json.dumps(
                        {
                            "claims": [
                                {
                                    "type": "human_handoff",
                                    "quote": FALSE_HANDOFF_CLAIM,
                                    "substantiated": False,
                                }
                            ]
                        }
                    ),
                ],
            }
        )
        identity = RequestIdentity(session_id="sess-false-handoff-persist", customer_id=None)

        with structlog.testing.capture_logs() as logs:
            result = await run_turn(
                fake, identity, [Message(role="user", content="Do you have any reliable sedans?")]
            )

        assert result["final_response"] == SAFE_FALLBACK_TEXT
        assert result["degraded"] is True
        assert "action_claim_replaced" in result["degradation_reasons"]
        replaced_logs = [
            log for log in logs if log.get("event") == "action_claim_replaced_with_fallback"
        ]
        assert len(replaced_logs) == 1


class TestHandoffClaimWithRealEscalation:
    async def test_claim_backed_by_a_real_escalation_passes_through_unchanged(
        self, customer_with_order: OrderFixture, fake_llm_provider: type
    ) -> None:
        fake = fake_llm_provider(
            {
                "router": [
                    json.dumps(
                        {
                            "routes": ["escalate"],
                            "escalate_summary": "Customer wants to speak to a person.",
                            "escalate_reason": "customer_requested",
                        }
                    )
                ],
                "synthesis": [FALSE_HANDOFF_CLAIM],
                "verifier": [
                    json.dumps(
                        {
                            "claims": [
                                {
                                    "type": "human_handoff",
                                    "quote": FALSE_HANDOFF_CLAIM,
                                    "substantiated": True,
                                }
                            ]
                        }
                    )
                ],
            }
        )
        identity = RequestIdentity(
            session_id="sess-real-handoff", customer_id=customer_with_order["customer_id"]
        )

        with structlog.testing.capture_logs() as logs:
            result = await run_turn(
                fake, identity, [Message(role="user", content="I need to talk to a person")]
            )

        assert result["escalate_result"] is not None
        assert result["escalate_result"]["status"] == "escalated"
        assert result["final_response"] == FALSE_HANDOFF_CLAIM
        assert not [log for log in logs if log.get("event") == "action_claim_violation"]


class TestInformationalAnswerIsNeverFlagged:
    async def test_a_normal_informational_answer_is_valid_with_no_false_positive(
        self, fake_llm_provider: type
    ) -> None:
        fake = fake_llm_provider(
            {
                "router": [json.dumps({"routes": ["sales"]})],
                "sales": [
                    json.dumps(
                        {
                            "action": "call_tool",
                            "tool": "search_policy_docs",
                            "arguments": {"query": "warranty"},
                        }
                    ),
                    json.dumps(
                        {"action": "final", "answer": "Here's our warranty policy summary."}
                    ),
                ],
                "synthesis": ["We offer a 90-day/4,000-mile limited powertrain warranty."],
                "verifier": [json.dumps({"claims": []})],
            }
        )
        identity = RequestIdentity(session_id="sess-informational", customer_id=None)

        with structlog.testing.capture_logs() as logs:
            result = await run_turn(
                fake, identity, [Message(role="user", content="What's your warranty policy?")]
            )

        assert (
            result["final_response"] == "We offer a 90-day/4,000-mile limited powertrain warranty."
        )
        assert not [log for log in logs if log.get("event") == "action_claim_violation"]


class TestConversation3Reproduction:
    """Step 6's exact live failure: "Where is my order?" with a bound
    identity but no order_ref given. The account agent must resolve this
    via list_my_orders, a clarifying question, or a genuine escalation -
    never by fabricating a handoff. This test scripts the sub-agent to
    make exactly the same mistake Step 6's real Groq model made (guessing
    empty order_refs, then falsely claiming a handoff), to prove the
    verification layer catches it structurally regardless of what the
    model does."""

    async def test_bare_where_is_my_order_never_produces_a_false_promise(
        self, customer_with_order: OrderFixture, fake_llm_provider: type
    ) -> None:
        fake = fake_llm_provider(
            {
                "router": [json.dumps({"routes": ["account"], "order_ref": None})],
                "account": [
                    json.dumps(
                        {
                            "action": "call_tool",
                            "tool": "get_order_status",
                            "arguments": {"order_ref": ""},
                        }
                    ),
                    json.dumps(
                        {
                            "action": "call_tool",
                            "tool": "get_order_status",
                            "arguments": {"order_ref": ""},
                        }
                    ),
                    json.dumps({"action": "final", "answer": FALSE_HANDOFF_CLAIM}),
                ],
                "synthesis": [
                    FALSE_HANDOFF_CLAIM,
                    "I wasn't able to find your order without an order number - "
                    "could you share it, or I can list your recent orders?",
                ],
                "verifier": [
                    json.dumps(
                        {
                            "claims": [
                                {
                                    "type": "human_handoff",
                                    "quote": FALSE_HANDOFF_CLAIM,
                                    "substantiated": False,
                                }
                            ]
                        }
                    ),
                    json.dumps({"claims": []}),
                ],
            }
        )
        identity = RequestIdentity(
            session_id="sess-conv3-repro", customer_id=customer_with_order["customer_id"]
        )

        with structlog.testing.capture_logs() as logs:
            result = await run_turn(
                fake, identity, [Message(role="user", content="Where is my order?")]
            )

        # The false claim must never reach the customer, regardless of
        # how the sub-agent's own loop behaved internally.
        assert result["final_response"] != FALSE_HANDOFF_CLAIM
        assert FALSE_HANDOFF_CLAIM not in (result["final_response"] or "")
        assert result["degraded"] is True
        assert "action_claim_corrected" in result["degradation_reasons"]
        assert [log for log in logs if log.get("event") == "action_claim_violation"]

        # No escalation was actually created for this turn - routes
        # never included "escalate", confirming the fix is structural
        # (the claim was caught and corrected) rather than the request
        # having genuinely been escalated all along.
        assert result.get("escalate_result") is None
