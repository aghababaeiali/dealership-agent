"""Integration tests for the supervisor graph, run against real Postgres
(for account_agent/escalate's tool calls) and real vector search (for
sales_agent), but with NO live LLM calls - the `fake_llm_provider` fixture
(see tests/conftest.py) returns pre-scripted responses in the exact order
the graph is expected to call `.complete()`.
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
            {"ref": f"graph-veh-{suffix}"},
        ).scalar_one()
        customer_id = conn.execute(
            text(
                "INSERT INTO customers (external_ref, email, full_name) "
                "VALUES (:ref, :email, 'Graph Test Customer') RETURNING id"
            ),
            {"ref": f"graph-cust-{suffix}", "email": f"graph-{suffix}@example.com"},
        ).scalar_one()
        order_ref = conn.execute(
            text(
                "INSERT INTO orders (order_ref, customer_id, vehicle_id, status, total_amount) "
                "VALUES (:ref, :cust, :veh, 'confirmed', 21999.00) RETURNING order_ref"
            ),
            {"ref": f"graph-order-{suffix}", "cust": customer_id, "veh": vehicle_id},
        ).scalar_one()

    yield {"customer_id": customer_id, "order_ref": order_ref, "vehicle_id": vehicle_id}

    with owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM escalations WHERE customer_id = :c"), {"c": customer_id})
        conn.execute(text("DELETE FROM orders WHERE customer_id = :c"), {"c": customer_id})
        conn.execute(text("DELETE FROM customers WHERE id = :c"), {"c": customer_id})
        conn.execute(text("DELETE FROM vehicles WHERE id = :v"), {"v": vehicle_id})


def _initial_state(message: str, identity: RequestIdentity) -> GraphState:
    return {"messages": [Message(role="user", content=message)], "identity": identity}


class TestSalesAgentHappyPath:
    async def test_cheap_suv_query_routes_to_sales_and_returns_listings(
        self, fake_llm_provider: type
    ) -> None:
        fake = fake_llm_provider(
            [
                json.dumps({"route": "sales", "sales_intent": "listings"}),
                "Here are a few affordable, reliable SUVs currently in stock.",
            ]
        )
        graph = await build_supervisor_graph(fake)
        state = _initial_state(
            "I want a cheap reliable family SUV",
            RequestIdentity(session_id="sess-sales", customer_id=None),
        )
        result = await graph.ainvoke(state)

        assert result["route"] == "sales"
        assert result["price_filters"] == {"price_max": 18_050.00}
        assert isinstance(result["tool_result"], list)
        assert len(result["tool_result"]) > 0
        assert (
            result["final_response"]
            == "Here are a few affordable, reliable SUVs currently in stock."
        )


class TestAccountAgentHappyPath:
    async def test_order_status_query_routes_to_account_and_returns_the_order(
        self, customer_with_order: OrderFixture, fake_llm_provider: type
    ) -> None:
        order_ref = customer_with_order["order_ref"]
        fake = fake_llm_provider(
            [
                json.dumps({"route": "account", "order_ref": order_ref}),
                "Your order is confirmed and on track.",
            ]
        )
        graph = await build_supervisor_graph(fake)
        state = _initial_state(
            f"What's the status of order {order_ref}?",
            RequestIdentity(
                session_id="sess-account", customer_id=customer_with_order["customer_id"]
            ),
        )
        result = await graph.ainvoke(state)

        assert result["route"] == "account"
        assert result["tool_result"]["order_ref"] == order_ref
        assert result["tool_result"]["status"] == "confirmed"
        assert result["final_response"] == "Your order is confirmed and on track."


class TestClarifyPath:
    async def test_ambiguous_message_routes_to_clarify_without_a_second_llm_call(
        self, fake_llm_provider: type
    ) -> None:
        fake = fake_llm_provider(
            [
                json.dumps(
                    {
                        "route": "clarify",
                        "clarify_question": "Are you asking about a vehicle or an existing order?",
                    }
                )
            ]
        )
        graph = await build_supervisor_graph(fake)
        state = _initial_state("help", RequestIdentity(session_id="sess-clarify", customer_id=None))
        result = await graph.ainvoke(state)

        assert result["route"] == "clarify"
        assert result["final_response"] == "Are you asking about a vehicle or an existing order?"
        # clarify -> END directly; synthesis must not have been called.
        assert len(fake.calls) == 1


class TestEscalatePath:
    async def test_explicit_human_request_escalates_and_creates_a_record(
        self, customer_with_order: OrderFixture, fake_llm_provider: type
    ) -> None:
        fake = fake_llm_provider(
            [
                json.dumps(
                    {
                        "route": "escalate",
                        "escalate_summary": "Customer is upset about a delayed delivery.",
                        "escalate_reason": "delivery_delay",
                    }
                ),
                "I've escalated this to a human agent who will follow up shortly.",
            ]
        )
        graph = await build_supervisor_graph(fake)
        state = _initial_state(
            "This is ridiculous, I need to talk to a real person right now",
            RequestIdentity(
                session_id="sess-escalate", customer_id=customer_with_order["customer_id"]
            ),
        )
        result = await graph.ainvoke(state)

        assert result["route"] == "escalate"
        assert result["tool_result"]["status"] == "escalated"
        assert (
            result["final_response"]
            == "I've escalated this to a human agent who will follow up shortly."
        )
