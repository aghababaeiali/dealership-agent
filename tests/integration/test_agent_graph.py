"""Integration tests for the supervisor graph, run against the real MCP
stdio transport, real Postgres (account_agent/escalate's tool calls), and
real vector search (sales_agent), but with NO live LLM calls - the
`fake_llm_provider` fixture (see tests/conftest.py) returns pre-scripted
responses per agent.
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


class TestSalesAgentHappyPath:
    async def test_cheap_suv_query_routes_to_sales_and_returns_listings(
        self, fake_llm_provider: type
    ) -> None:
        fake = fake_llm_provider(
            {
                "router": [json.dumps({"routes": ["sales"]})],
                "sales": [
                    json.dumps(
                        {
                            "action": "call_tool",
                            "tool": "search_listings",
                            "arguments": {"query": "cheap reliable family SUV"},
                        }
                    ),
                    json.dumps(
                        {
                            "action": "final",
                            "answer": "Here are a few affordable, reliable SUVs in stock.",
                        }
                    ),
                ],
                "synthesis": ["Here are a few affordable, reliable SUVs currently in stock."],
            }
        )
        identity = RequestIdentity(session_id="sess-sales", customer_id=None)
        result = await run_turn(
            fake, identity, [Message(role="user", content="I want a cheap reliable family SUV")]
        )

        assert result["routes"] == ["sales"]
        assert result["price_filters"] == {"price_max": 18_050.00}
        sales_result = result["sales_result"]
        assert sales_result is not None
        assert sales_result["hit_cap"] is False
        assert len(sales_result["tool_calls"]) == 1
        assert len(sales_result["tool_calls"][0]["result"]) > 0
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
            }
        )
        identity = RequestIdentity(
            session_id="sess-account", customer_id=customer_with_order["customer_id"]
        )
        result = await run_turn(
            fake,
            identity,
            [Message(role="user", content=f"What's the status of order {order_ref}?")],
        )

        assert result["routes"] == ["account"]
        account_result = result["account_result"]
        assert account_result is not None
        assert account_result["tool_calls"][0]["result"]["order_ref"] == order_ref
        assert account_result["tool_calls"][0]["result"]["status"] == "confirmed"
        assert result["final_response"] == "Your order is confirmed and on track."


class TestClarifyPath:
    async def test_ambiguous_message_routes_to_clarify_without_a_second_llm_call(
        self, fake_llm_provider: type
    ) -> None:
        fake = fake_llm_provider(
            {
                "router": [
                    json.dumps(
                        {
                            "routes": ["clarify"],
                            "clarify_question": (
                                "Are you asking about a vehicle or an existing order?"
                            ),
                        }
                    )
                ],
            }
        )
        identity = RequestIdentity(session_id="sess-clarify", customer_id=None)
        result = await run_turn(fake, identity, [Message(role="user", content="help")])

        assert result["routes"] == ["clarify"]
        assert result["final_response"] == "Are you asking about a vehicle or an existing order?"
        # clarify -> END directly; synthesis must not have been called.
        assert len(fake.calls) == 1


class TestEscalatePath:
    async def test_explicit_human_request_escalates_and_creates_a_record(
        self, customer_with_order: OrderFixture, fake_llm_provider: type
    ) -> None:
        fake = fake_llm_provider(
            {
                "router": [
                    json.dumps(
                        {
                            "routes": ["escalate"],
                            "escalate_summary": "Customer is upset about a delayed delivery.",
                            "escalate_reason": "delivery_delay",
                        }
                    )
                ],
                "synthesis": ["I've escalated this to a human agent who will follow up shortly."],
            }
        )
        identity = RequestIdentity(
            session_id="sess-escalate", customer_id=customer_with_order["customer_id"]
        )
        result = await run_turn(
            fake,
            identity,
            [Message(role="user", content="This is ridiculous, I need to talk to a real person")],
        )

        assert result["routes"] == ["escalate"]
        escalate_result = result["escalate_result"]
        assert escalate_result is not None
        assert escalate_result["status"] == "escalated"
        assert (
            result["final_response"]
            == "I've escalated this to a human agent who will follow up shortly."
        )


class TestMultiScopeRouting:
    async def test_one_turn_can_touch_both_sales_and_account(
        self, customer_with_order: OrderFixture, fake_llm_provider: type
    ) -> None:
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
                "synthesis": [
                    "Here are some cheap SUVs, and your order is confirmed and on the way!"
                ],
            }
        )
        identity = RequestIdentity(
            session_id="sess-multi-scope", customer_id=customer_with_order["customer_id"]
        )
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

        assert set(result["routes"]) == {"sales", "account"}
        sales_result = result["sales_result"]
        account_result = result["account_result"]
        assert sales_result is not None
        assert account_result is not None
        assert len(sales_result["tool_calls"][0]["result"]) > 0
        assert account_result["tool_calls"][0]["result"]["order_ref"] == order_ref
        # Each sub-agent only ever used its own tool - the boundary held
        # even though both ran in the same turn.
        assert sales_result["tool_calls"][0]["tool"] == "search_listings"
        assert account_result["tool_calls"][0]["tool"] == "get_order_status"
        assert (
            result["final_response"]
            == "Here are some cheap SUVs, and your order is confirmed and on the way!"
        )
