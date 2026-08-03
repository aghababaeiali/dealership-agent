"""Tool-boundary security tests for the MCP server, run against the REAL
stdio transport (see docs/adr/0003, docs/adr/0004): every assertion here
reads schemas and results exactly as they come over the wire from a
separate server subprocess, not from in-process method calls.

Proves CLAUDE.md's Core Security Invariant end-to-end: no tool schema
exposes an identity field, and get_order_status can only ever return the
calling customer's own data - a lookup for another customer's order_ref is
indistinguishable from a lookup for a ref that doesn't exist at all.
"""

import uuid
from collections.abc import Iterator
from typing import TypedDict

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from dealership_agent.agents.mcp_session import open_mcp_session
from dealership_agent.config import get_settings
from dealership_agent.tools.identity import RequestIdentity

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
        anonymous = RequestIdentity(session_id="schema-scan-session", customer_id=None)
        async with open_mcp_session(anonymous) as session:
            tools = await session.list_tools()

        assert len(tools.tools) == 5

        for tool in tools.tools:
            schema_properties = set(tool.inputSchema.get("properties", {}).keys())
            leaked = schema_properties & FORBIDDEN_SCHEMA_FIELDS
            assert not leaked, f"Tool {tool.name!r} exposes identity field(s): {leaked}"


class TestGetOrderStatusScoping:
    async def test_lookup_of_another_customers_order_is_not_found(
        self, two_customers_with_orders: OrderFixture
    ) -> None:
        fixtures = two_customers_with_orders
        identity_a = RequestIdentity(
            session_id="test-session-a", customer_id=fixtures["customer_a_id"]
        )
        async with open_mcp_session(identity_a) as session:
            result = await session.call_tool(
                "get_order_status", {"order_ref": fixtures["order_b_ref"]}
            )

        assert result.isError is False
        assert result.structuredContent == {"result": None}

    async def test_no_session_context_fails_closed_and_returns_no_data(
        self, two_customers_with_orders: OrderFixture
    ) -> None:
        fixtures = two_customers_with_orders
        # No customer_id bound for this session at all - the subprocess
        # never receives DEALERSHIP_MCP_CUSTOMER_ID (see
        # docs/adr/0004-mcp-identity-propagation.md).
        anonymous = RequestIdentity(session_id="anon-session", customer_id=None)
        async with open_mcp_session(anonymous) as session:
            result = await session.call_tool(
                "get_order_status", {"order_ref": fixtures["order_b_ref"]}
            )

        # Over the real transport, a tool-side failure comes back as an
        # error *result*, not a raised client-side exception - either way,
        # no order data is returned.
        assert result.isError is True
        assert result.structuredContent is None


class TestListMyOrdersScoping:
    async def test_returns_only_the_calling_customers_own_orders(
        self, two_customers_with_orders: OrderFixture
    ) -> None:
        fixtures = two_customers_with_orders
        identity_b = RequestIdentity(
            session_id="test-session-list-b", customer_id=fixtures["customer_b_id"]
        )
        async with open_mcp_session(identity_b) as session:
            result_b = await session.call_tool("list_my_orders", {})

        assert result_b.isError is False
        content_b = result_b.structuredContent
        assert content_b is not None
        refs_b = {order["order_ref"] for order in content_b["result"]}
        assert fixtures["order_b_ref"] in refs_b

        identity_a = RequestIdentity(
            session_id="test-session-list-a", customer_id=fixtures["customer_a_id"]
        )
        async with open_mcp_session(identity_a) as session:
            result_a = await session.call_tool("list_my_orders", {})

        assert result_a.isError is False
        content_a = result_a.structuredContent
        assert content_a is not None
        refs_a = {order["order_ref"] for order in content_a["result"]}
        assert fixtures["order_b_ref"] not in refs_a

    async def test_no_session_context_fails_closed_and_returns_no_data(self) -> None:
        anonymous = RequestIdentity(session_id="anon-list-orders-session", customer_id=None)
        async with open_mcp_session(anonymous) as session:
            result = await session.call_tool("list_my_orders", {})

        assert result.isError is True
        assert result.structuredContent is None


class TestSearchListingsIsPublic:
    async def test_search_listings_works_with_no_session_context(self) -> None:
        anonymous = RequestIdentity(session_id="public-sales-session", customer_id=None)
        async with open_mcp_session(anonymous) as session:
            result = await session.call_tool(
                "search_listings", {"query": "reliable family SUV", "limit": 3}
            )

        assert result.isError is False
        content = result.structuredContent
        assert content is not None
        assert len(content["result"]) > 0
