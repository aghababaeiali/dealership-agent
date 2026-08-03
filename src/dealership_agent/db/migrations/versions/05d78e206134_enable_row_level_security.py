"""enable row level security

Revision ID: 05d78e206134
Revises: cbd3d488305d
Create Date: 2026-08-03 02:00:36.294817

Creates the least-privilege `app` role that the application connects as, and
enables fail-closed Row-Level Security on every customer-scoped table.
`vehicles` and `vehicle_embeddings` are public catalog data and intentionally
excluded - see CLAUDE.md's Core Security Invariant.

Policies compare each row's owning customer_id against
`current_setting('app.customer_id', true)`. That call returns NULL (not an
error) when the setting is absent, and `column = NULL` is never true in SQL,
so an unset setting yields zero rows rather than every row - fail closed,
never fail open. `NULLIF(..., '')` additionally treats an empty-string
setting the same as absent.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from dealership_agent.config import get_settings

# revision identifiers, used by Alembic.
revision: str = "05d78e206134"
down_revision: str | Sequence[str] | None = "cbd3d488305d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE_TABLES = (
    "customers",
    "orders",
    "order_status_history",
    "test_drive_bookings",
    "escalations",
)

CUSTOMER_ID_SCOPE = "NULLIF(current_setting('app.customer_id', true), '')::integer"


def _app_role_name() -> str:
    settings = get_settings()
    if not settings.app_db_user or not settings.app_db_password:
        raise RuntimeError(
            "APP_DB_USER / APP_DB_PASSWORD must be set to create the app role. "
            "This role must never be granted BYPASSRLS."
        )
    return settings.app_db_user


def upgrade() -> None:
    """Upgrade schema."""
    settings = get_settings()
    app_role = _app_role_name()
    # Password value is bound as a literal, not interpolated into the SQL
    # text, since Postgres DDL utility statements (CREATE ROLE) don't
    # support query parameters the way DML does.
    password_literal = settings.app_db_password.replace("'", "''")

    conn = op.get_bind()
    db_name = conn.execute(sa.text("SELECT current_database()")).scalar_one()

    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{app_role}') THEN
                CREATE ROLE {app_role}
                    WITH LOGIN PASSWORD '{password_literal}'
                    NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION;
            END IF;
        END
        $$;
        """
    )

    op.execute(f'GRANT CONNECT ON DATABASE "{db_name}" TO {app_role}')
    op.execute(f"GRANT USAGE ON SCHEMA public TO {app_role}")

    # Public catalog data: read-only for the app role, no RLS.
    op.execute(f"GRANT SELECT ON vehicles, vehicle_embeddings TO {app_role}")

    # Customer-scoped tables: full CRUD, but RLS (below) restricts every row
    # to the caller's own customer_id.
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(APP_ROLE_TABLES)} TO {app_role}"
    )
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {app_role}")

    for table in APP_ROLE_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE so RLS also applies to the table owner - defense in depth in
        # case app_role or another role is ever granted ownership by mistake.
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    direct_scope_tables = ("orders", "test_drive_bookings", "escalations")
    for table in direct_scope_tables:
        op.execute(
            f"""
            CREATE POLICY {table}_isolation ON {table}
                USING (customer_id = {CUSTOMER_ID_SCOPE})
                WITH CHECK (customer_id = {CUSTOMER_ID_SCOPE})
            """
        )

    # customers: a customer's own id IS the scope key.
    op.execute(
        f"""
        CREATE POLICY customers_isolation ON customers
            USING (id = {CUSTOMER_ID_SCOPE})
            WITH CHECK (id = {CUSTOMER_ID_SCOPE})
        """
    )

    # order_status_history has no customer_id column of its own; scope via
    # the parent order.
    op.execute(
        f"""
        CREATE POLICY order_status_history_isolation ON order_status_history
            USING (
                order_id IN (
                    SELECT id FROM orders WHERE customer_id = {CUSTOMER_ID_SCOPE}
                )
            )
            WITH CHECK (
                order_id IN (
                    SELECT id FROM orders WHERE customer_id = {CUSTOMER_ID_SCOPE}
                )
            )
        """  # noqa: S608 -- CUSTOMER_ID_SCOPE is a fixed constant, not user input
    )


def downgrade() -> None:
    """Downgrade schema."""
    app_role = _app_role_name()
    conn = op.get_bind()
    db_name = conn.execute(sa.text("SELECT current_database()")).scalar_one()

    op.execute("DROP POLICY IF EXISTS order_status_history_isolation ON order_status_history")
    op.execute("DROP POLICY IF EXISTS customers_isolation ON customers")
    for table in ("orders", "test_drive_bookings", "escalations"):
        op.execute(f"DROP POLICY IF EXISTS {table}_isolation ON {table}")

    for table in APP_ROLE_TABLES:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.execute(f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {app_role}")
    op.execute(f"REVOKE ALL PRIVILEGES ON {', '.join(APP_ROLE_TABLES)} FROM {app_role}")
    op.execute(f"REVOKE ALL PRIVILEGES ON vehicles, vehicle_embeddings FROM {app_role}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {app_role}")
    op.execute(f'REVOKE CONNECT ON DATABASE "{db_name}" FROM {app_role}')
    op.execute(f"DROP ROLE IF EXISTS {app_role}")
