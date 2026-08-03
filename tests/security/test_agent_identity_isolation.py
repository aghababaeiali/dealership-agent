"""C3: session identity lives in graph state and must NEVER appear in any
prompt sent to the LLM.

Runs a full sample conversation through the supervisor graph (real
Postgres for the account-lookup tool call, NO live LLM - fake_llm_provider
returns scripted responses) with a real, specific customer_id, then
serialises every `Message` sent to `FakeLLMProvider.complete()` across the
whole run and asserts that customer_id's value never appears in it, in any
form (raw int, str, or as part of any structured payload).
"""

import json
import uuid
from collections.abc import Iterator
from typing import TypedDict

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from dealership_agent.agents.graph import build_supervisor_graph
from dealership_agent.agents.state import GraphState
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
                "INSERT INTO customers (external_ref, email, full_name) "
                "VALUES (:ref, :email, 'Identity Test Customer') RETURNING id"
            ),
            {"ref": f"identity-cust-{suffix}", "email": f"identity-{suffix}@example.com"},
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
    """Flatten every Message from every LLM call into one searchable string."""
    return json.dumps([[m.model_dump() for m in call] for call in calls])


class TestNoIdentityLeaksIntoPrompts:
    async def test_customer_id_never_appears_in_any_llm_prompt(
        self, customer_with_order: OrderFixture, fake_llm_provider: type
    ) -> None:
        customer_id = customer_with_order["customer_id"]
        order_ref = customer_with_order["order_ref"]

        fake = fake_llm_provider(
            [
                json.dumps({"route": "account", "order_ref": order_ref}),
                "Your order is confirmed.",
            ]
        )
        graph = await build_supervisor_graph(fake)
        state: GraphState = {
            "messages": [Message(role="user", content=f"What's the status of order {order_ref}?")],
            "identity": RequestIdentity(session_id="sess-identity-test", customer_id=customer_id),
        }
        result = await graph.ainvoke(state)

        assert result["tool_result"]["order_ref"] == order_ref  # sanity: the run worked

        serialized = _serialize_all_calls(fake.calls)
        assert str(customer_id) not in serialized

    async def test_customer_id_never_appears_in_sales_conversation_prompts(
        self, customer_with_order: OrderFixture, fake_llm_provider: type
    ) -> None:
        """Even in an unrelated sales conversation, an authenticated
        customer_id present in state must never leak into a prompt."""
        customer_id = customer_with_order["customer_id"]

        fake = fake_llm_provider(
            [
                json.dumps({"route": "sales", "sales_intent": "listings"}),
                "Here are some options for you.",
            ]
        )
        graph = await build_supervisor_graph(fake)
        state: GraphState = {
            "messages": [Message(role="user", content="Do you have any reliable sedans?")],
            "identity": RequestIdentity(session_id="sess-identity-sales", customer_id=customer_id),
        }
        await graph.ainvoke(state)

        serialized = _serialize_all_calls(fake.calls)
        assert str(customer_id) not in serialized
