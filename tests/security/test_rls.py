"""Row-Level Security tests, run against real Postgres.

These connect as the same least-privilege app role the application uses
(never the migration/owner role, which bypasses RLS as table owner) and rely
on Postgres itself to enforce scoping - not application code. If RLS is ever
disabled or dropped from these tables, every test below must fail.
"""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from dealership_agent.config import get_settings
from dealership_agent.db.rls import customer_scope

settings = get_settings()


@pytest.fixture(scope="module")
def owner_engine() -> Iterator[Engine]:
    """Bypasses RLS (table owner) - used only to set up/tear down fixtures."""
    engine = create_engine(settings.database_migration_url)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def app_engine() -> Iterator[Engine]:
    """The same role the application connects as. RLS applies to it."""
    engine = create_engine(settings.database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def two_customers_with_orders(owner_engine: Engine) -> Iterator[dict[str, int]]:
    """Two customers, each with one order, inserted via the owner role."""
    suffix = uuid.uuid4().hex[:8]
    with owner_engine.begin() as conn:
        vehicle_id = conn.execute(
            text(
                "INSERT INTO vehicles (external_ref, year, make, model, mileage, is_available) "
                "VALUES (:ref, 2024, 'Test', 'Model', 10000, true) RETURNING id"
            ),
            {"ref": f"test-vehicle-{suffix}"},
        ).scalar_one()

        customer_a_id = conn.execute(
            text(
                "INSERT INTO customers (external_ref, email, full_name) "
                "VALUES (:ref, :email, 'Customer A') RETURNING id"
            ),
            {"ref": f"cust-a-{suffix}", "email": f"a-{suffix}@example.com"},
        ).scalar_one()
        customer_b_id = conn.execute(
            text(
                "INSERT INTO customers (external_ref, email, full_name) "
                "VALUES (:ref, :email, 'Customer B') RETURNING id"
            ),
            {"ref": f"cust-b-{suffix}", "email": f"b-{suffix}@example.com"},
        ).scalar_one()

        order_a_id = conn.execute(
            text(
                "INSERT INTO orders (order_ref, customer_id, vehicle_id, status, total_amount) "
                "VALUES (:ref, :cust, :veh, 'pending', 19999.00) RETURNING id"
            ),
            {"ref": f"order-a-{suffix}", "cust": customer_a_id, "veh": vehicle_id},
        ).scalar_one()
        order_b_id = conn.execute(
            text(
                "INSERT INTO orders (order_ref, customer_id, vehicle_id, status, total_amount) "
                "VALUES (:ref, :cust, :veh, 'pending', 24999.00) RETURNING id"
            ),
            {"ref": f"order-b-{suffix}", "cust": customer_b_id, "veh": vehicle_id},
        ).scalar_one()

    yield {
        "customer_a_id": customer_a_id,
        "customer_b_id": customer_b_id,
        "order_a_id": order_a_id,
        "order_b_id": order_b_id,
        "vehicle_id": vehicle_id,
    }

    with owner_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM orders WHERE id IN (:a, :b)"),
            {"a": order_a_id, "b": order_b_id},
        )
        conn.execute(
            text("DELETE FROM customers WHERE id IN (:a, :b)"),
            {"a": customer_a_id, "b": customer_b_id},
        )
        conn.execute(text("DELETE FROM vehicles WHERE id = :v"), {"v": vehicle_id})


def test_customer_cannot_read_other_customers_orders(
    app_engine: Engine, two_customers_with_orders: dict[str, int]
) -> None:
    fixtures = two_customers_with_orders
    with app_engine.connect() as conn, customer_scope(conn, fixtures["customer_a_id"]):
        rows = conn.execute(text("SELECT id, customer_id FROM orders")).fetchall()

    ids = {row.id for row in rows}
    assert fixtures["order_a_id"] in ids
    assert fixtures["order_b_id"] not in ids
    assert all(row.customer_id == fixtures["customer_a_id"] for row in rows)


def test_no_customer_id_set_returns_zero_rows_not_all_rows(
    app_engine: Engine, two_customers_with_orders: dict[str, int]
) -> None:
    """Fail closed: an absent app.customer_id must yield 0 rows, never every row."""
    with app_engine.connect() as conn, conn.begin():
        rows = conn.execute(text("SELECT * FROM orders")).fetchall()
    assert rows == []


def test_setting_does_not_leak_between_sequential_transactions(
    app_engine: Engine, two_customers_with_orders: dict[str, int]
) -> None:
    fixtures = two_customers_with_orders
    with app_engine.connect() as conn:
        with customer_scope(conn, fixtures["customer_a_id"]):
            scoped_rows = conn.execute(text("SELECT id FROM orders")).fetchall()
        assert any(row.id == fixtures["order_a_id"] for row in scoped_rows)

        # Same underlying connection, a new transaction, scope not re-set.
        with conn.begin():
            leaked_rows = conn.execute(text("SELECT * FROM orders")).fetchall()
        assert leaked_rows == []


def test_raw_query_without_where_clause_still_scoped(
    app_engine: Engine, two_customers_with_orders: dict[str, int]
) -> None:
    fixtures = two_customers_with_orders
    with app_engine.connect() as conn, customer_scope(conn, fixtures["customer_b_id"]):
        rows = conn.execute(text("SELECT * FROM orders")).fetchall()

    assert len(rows) == 1
    assert rows[0].id == fixtures["order_b_id"]
