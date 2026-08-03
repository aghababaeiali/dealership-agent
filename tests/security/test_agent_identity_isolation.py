"""C3/B6: session identity lives in graph state and must NEVER appear in any
prompt sent to the LLM - including tool results that flow back into the
tool loop and into the synthesis prompt.

Runs a full conversation through `run_turn()` (real MCP stdio transport,
real Postgres for the account-lookup tool call, NO live LLM -
fake_llm_provider returns scripted responses) with a real, specific
customer_id, then serialises:

  1. every `Message` sent to `FakeLLMProvider.complete()` across the whole
     turn (this now includes tool-loop observations, per B6 - tool
     results are a new leak surface now that sub-agents run multi-step
     loops with results fed back into their own message history), and
  2. the final GraphState itself (sales_result/account_result/
     escalate_result), since that also gets serialized into the
     synthesis prompt.

and asserts the customer_id's value never appears in any of it.
"""

import json
import uuid
from collections.abc import Iterator
from typing import TypedDict

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from dealership_agent.agents.runner import run_turn
from dealership_agent.config import get_settings
from dealership_agent.llm.base import Message
from dealership_agent.tools.identity import RequestIdentity

settings = get_settings()


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
    # Deliberately a large, distinctive id (9 digits) rather than letting
    # the small serial sequence assign one: a short id like "146" is a
    # realistic false-positive risk for the substring check below, since
    # it can coincidentally appear inside an unrelated price, mileage, or
    # similarity score pulled into the synthesis prompt from real search
    # results. Every price/mileage in this dataset is well under 7 digits
    # (see docs/DATA_PRICE_AUDIT.md), so a 9-digit id cannot collide.
    distinctive_customer_id = 900_000_000 + (uuid.uuid4().int % 90_000_000)
    with owner_engine.begin() as conn:
        vehicle_id = conn.execute(
            text(
                "INSERT INTO vehicles (external_ref, year, make, model, mileage, is_available) "
                "VALUES (:ref, 2024, 'Test', 'Model', 10000, true) RETURNING id"
            ),
            {"ref": f"identity-veh-{suffix}"},
        ).scalar_one()
        customer_id = conn.execute(
            text(
                "INSERT INTO customers (id, external_ref, email, full_name) "
                "VALUES (:id, :ref, :email, 'Identity Test Customer') RETURNING id"
            ),
            {
                "id": distinctive_customer_id,
                "ref": f"identity-cust-{suffix}",
                "email": f"identity-{suffix}@example.com",
            },
        ).scalar_one()
        order_ref = conn.execute(
            text(
                "INSERT INTO orders (order_ref, customer_id, vehicle_id, status, total_amount) "
                "VALUES (:ref, :cust, :veh, 'confirmed', 19999.00) RETURNING order_ref"
            ),
            {"ref": f"identity-order-{suffix}", "cust": customer_id, "veh": vehicle_id},
        ).scalar_one()

    yield {"customer_id": customer_id, "order_ref": order_ref, "vehicle_id": vehicle_id}

    with owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM orders WHERE customer_id = :c"), {"c": customer_id})
        conn.execute(text("DELETE FROM customers WHERE id = :c"), {"c": customer_id})
        conn.execute(text("DELETE FROM vehicles WHERE id = :v"), {"v": vehicle_id})


def _serialize_all_calls(calls: list[list[Message]]) -> str:
    """Flatten every Message from every LLM call (across every agent -
    router, sales, account, synthesis) into one searchable string. This
    is exactly what the LLM saw across the whole turn, tool-loop
    observations included."""
    return json.dumps([[m.model_dump() for m in call] for call in calls])


class TestNoIdentityLeaksIntoPrompts:
    async def test_customer_id_never_appears_in_any_llm_prompt(
        self, customer_with_order: OrderFixture, fake_llm_provider: type
    ) -> None:
        customer_id = customer_with_order["customer_id"]
        order_ref = customer_with_order["order_ref"]

        fake = fake_llm_provider(
            {
                "router": [json.dumps({"routes": ["account"], "order_ref": order_ref})],
                "account": [
                    json.dumps(
                        {
                            "action": "call_tool",
                            "tool": "get_order_status",
                            "arguments": {"order_ref": order_ref},
                        }
                    ),
                    json.dumps({"action": "final", "answer": "Your order is confirmed."}),
                ],
                "synthesis": ["Your order is confirmed and on track."],
                "verifier": [json.dumps({"claims": []})],
            }
        )
        identity = RequestIdentity(session_id="sess-identity-test", customer_id=customer_id)
        result = await run_turn(
            fake,
            identity,
            [Message(role="user", content=f"What's the status of order {order_ref}?")],
        )

        # Sanity: the run actually worked and touched the real order -
        # the tool result (which does carry customer_id server-side, in
        # the raw DB row shape) is exactly the leak surface B6 worries
        # about once it's serialized back into a subsequent prompt.
        account_result = result["account_result"]
        assert account_result is not None
        assert account_result["tool_calls"][0]["result"]["order_ref"] == order_ref

        serialized_prompts = _serialize_all_calls(fake.calls)
        assert str(customer_id) not in serialized_prompts

        # B6: tool results are a new leak surface - check the raw
        # GraphState fields that get serialized into the synthesis
        # prompt too, not just what was already sent to the LLM.
        serialized_state = json.dumps(
            {
                "sales_result": result.get("sales_result"),
                "account_result": result.get("account_result"),
                "escalate_result": result.get("escalate_result"),
            },
            default=str,
        )
        assert str(customer_id) not in serialized_state

    async def test_customer_id_never_appears_in_sales_conversation_prompts(
        self, customer_with_order: OrderFixture, fake_llm_provider: type
    ) -> None:
        """Even in an unrelated sales conversation, an authenticated
        customer_id present in state must never leak into a prompt."""
        customer_id = customer_with_order["customer_id"]

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
                    json.dumps({"action": "final", "answer": "Here are some options for you."}),
                ],
                "synthesis": ["Here are some options for you."],
                "verifier": [json.dumps({"claims": []})],
            }
        )
        identity = RequestIdentity(session_id="sess-identity-sales", customer_id=customer_id)
        result = await run_turn(
            fake, identity, [Message(role="user", content="Do you have any reliable sedans?")]
        )

        serialized_prompts = _serialize_all_calls(fake.calls)
        assert str(customer_id) not in serialized_prompts

        serialized_state = json.dumps({"sales_result": result.get("sales_result")}, default=str)
        assert str(customer_id) not in serialized_state

    async def test_customer_id_never_appears_in_multi_scope_prompts_or_results(
        self, customer_with_order: OrderFixture, fake_llm_provider: type
    ) -> None:
        """A cross-scope turn (sales + account in one turn) is the
        highest-risk case for leakage: the merge/synthesis step
        serializes both sub-agents' tool_calls into one payload."""
        customer_id = customer_with_order["customer_id"]
        order_ref = customer_with_order["order_ref"]

        fake = fake_llm_provider(
            {
                "router": [json.dumps({"routes": ["sales", "account"], "order_ref": order_ref})],
                "sales": [
                    json.dumps(
                        {
                            "action": "call_tool",
                            "tool": "search_listings",
                            "arguments": {"query": "cheap SUV", "price_max": 18050.0},
                        }
                    ),
                    json.dumps({"action": "final", "answer": "Found some cheap SUVs."}),
                ],
                "account": [
                    json.dumps(
                        {
                            "action": "call_tool",
                            "tool": "get_order_status",
                            "arguments": {"order_ref": order_ref},
                        }
                    ),
                    json.dumps({"action": "final", "answer": "Your order is confirmed."}),
                ],
                "synthesis": ["Here are some cheap SUVs, and your order is confirmed!"],
                "verifier": [json.dumps({"claims": []})],
            }
        )
        identity = RequestIdentity(session_id="sess-identity-multi", customer_id=customer_id)
        result = await run_turn(
            fake,
            identity,
            [
                Message(
                    role="user",
                    content="find me a cheap SUV and tell me if my order shipped",
                )
            ],
        )

        account_result = result["account_result"]
        assert account_result is not None
        assert account_result["tool_calls"][0]["result"]["order_ref"] == order_ref

        serialized_prompts = _serialize_all_calls(fake.calls)
        assert str(customer_id) not in serialized_prompts

        serialized_state = json.dumps(
            {
                "sales_result": result.get("sales_result"),
                "account_result": result.get("account_result"),
            },
            default=str,
        )
        assert str(customer_id) not in serialized_state
