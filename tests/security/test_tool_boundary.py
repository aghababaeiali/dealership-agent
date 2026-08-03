"""Tool-boundary security tests for the MCP server.

Proves CLAUDE.md's Core Security Invariant end-to-end: no tool schema
exposes an identity field, and get_order_status can only ever return the
calling customer's own data - a lookup for another customer's order_ref is
indistinguishable from a lookup for a ref that doesn't exist at all.
"""

import uuid
from collections.abc import Iterator
from typing import TypedDict

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from dealership_agent.config import get_settings
from dealership_agent.tools.identity import RequestIdentity, bind_identity
from dealership_agent.tools.server import server

settings = get_settings()

FORBIDDEN_SCHEMA_FIELDS = {"customer_id", "user_id", "tenant_id", "email", "customer_ref"}


class OrderFixture(TypedDict):
    customer_a_id: int
    customer_b_id: int
    order_b_ref: str
    vehicle_id: int


@pytest.fixture(scope="module")
def owner_engine() -> Iterator[Engine]:
    """Bypasses RLS (table owner) - used only to set up/tear down fixtures."""
    engine = create_engine(settings.database_migration_url)
    yield engine
    engine.dispose()


@pytest.fixture
def two_customers_with_orders(owner_engine: Engine) -> Iterator[OrderFixture]:
    suffix = uuid.uuid4().hex[:8]
    with owner_engine.begin() as conn:
        vehicle_id = conn.execute(
            text(
                "INSERT INTO vehicles (external_ref, year, make, model, mileage, is_available) "
                "VALUES (:ref, 2024, 'Test', 'Model', 10000, true) RETURNING id"
            ),
            {"ref": f"tool-veh-{suffix}"},
        ).scalar_one()
        customer_a_id = conn.execute(
            text(
                "INSERT INTO customers (external_ref, email, full_name) "
                "VALUES (:ref, :email, 'Customer A') RETURNING id"
            ),
            {"ref": f"tool-a-{suffix}", "email": f"tool-a-{suffix}@example.com"},
        ).scalar_one()
        customer_b_id = conn.execute(
            text(
                "INSERT INTO customers (external_ref, email, full_name) "
                "VALUES (:ref, :email, 'Customer B') RETURNING id"
            ),
            {"ref": f"tool-b-{suffix}", "email": f"tool-b-{suffix}@example.com"},
        ).scalar_one()
        order_b_ref = conn.execute(
            text(
                "INSERT INTO orders (order_ref, customer_id, vehicle_id, status, total_amount) "
                "VALUES (:ref, :cust, :veh, 'pending', 24999.00) RETURNING order_ref"
            ),
            {"ref": f"tool-order-b-{suffix}", "cust": customer_b_id, "veh": vehicle_id},
        ).scalar_one()

    yield {
        "customer_a_id": customer_a_id,
        "customer_b_id": customer_b_id,
        "order_b_ref": order_b_ref,
        "vehicle_id": vehicle_id,
    }

    with owner_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM orders WHERE customer_id IN (:a, :b)"),
            {"a": customer_a_id, "b": customer_b_id},
        )
        conn.execute(
            text("DELETE FROM customers WHERE id IN (:a, :b)"),
            {"a": customer_a_id, "b": customer_b_id},
        )
        conn.execute(text("DELETE FROM vehicles WHERE id = :v"), {"v": vehicle_id})


class TestToolSchemasContainNoIdentityFields:
    async def test_no_tool_schema_contains_identity_fields(self) -> None:
        tools = await server.list_tools()
        assert len(tools) == 4

        for tool in tools:
            schema_properties = set(tool.input_schema.get("properties", {}).keys())
            leaked = schema_properties & FORBIDDEN_SCHEMA_FIELDS
            assert not leaked, f"Tool {tool.name!r} exposes identity field(s): {leaked}"


class TestGetOrderStatusScoping:
    async def test_lookup_of_another_customers_order_is_not_found(
        self, two_customers_with_orders: OrderFixture
    ) -> None:
        fixtures = two_customers_with_orders
        with bind_identity(
            RequestIdentity(session_id="test-session-a", customer_id=fixtures["customer_a_id"])
        ):
            result = await server.call_tool(
                "get_order_status", {"order_ref": fixtures["order_b_ref"]}
            )
        assert isinstance(result, CallToolResult)
        assert result.structured_content == {"result": None}

    async def test_no_session_context_raises_and_returns_no_data(
        self, two_customers_with_orders: OrderFixture
    ) -> None:
        fixtures = two_customers_with_orders
        with pytest.raises(ToolError):
            await server.call_tool("get_order_status", {"order_ref": fixtures["order_b_ref"]})


class TestSearchListingsIsPublic:
    async def test_search_listings_works_with_no_session_context(self) -> None:
        result = await server.call_tool(
            "search_listings", {"query": "reliable family SUV", "limit": 3}
        )
        assert isinstance(result, CallToolResult)
        assert result.is_error is False
        content = result.structured_content
        assert content is not None
        assert len(content["result"]) > 0
