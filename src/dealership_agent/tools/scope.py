"""The single chokepoint every customer-scoped tool must go through.

`customer_scoped_connection()` is the only function in this codebase that
yields a DB connection capable of reading customer-scoped tables
(customers, orders, order_status_history, test_drive_bookings,
escalations). It resolves identity from server-side request context
(tools/identity.py) - never from a tool argument - and refuses to open a
connection at all when no customer is authenticated, so there is no code
path where a tool can read another customer's rows: either it goes through
here and gets exactly one customer's data, or identity is missing and it
gets nothing. Callers receive the already-validated customer_id/session_id
back (see `ScopedConnection`) so they never need to re-derive identity
themselves.

This is one of two independent layers, not the only one: Postgres
Row-Level Security (see the RLS migration and db/rls.py) enforces the same
constraint at the data layer regardless of what application code does or
fails to do here.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy.engine import Connection

from dealership_agent.db.rls import customer_scope
from dealership_agent.db.session import engine as default_engine
from dealership_agent.tools.identity import get_current_identity


@dataclass(frozen=True)
class ScopedConnection:
    connection: Connection
    customer_id: int
    session_id: str


@contextmanager
def customer_scoped_connection() -> Iterator[ScopedConnection]:
    """Open a DB connection scoped to the current request's customer_id.

    Raises PermissionError if no customer is authenticated in the current
    request context - fail closed, never fail open.
    """
    identity = get_current_identity()
    if identity is None or identity.customer_id is None:
        raise PermissionError(
            "No authenticated customer in the current request context - "
            "refusing to open a customer-scoped database connection."
        )

    with default_engine.connect() as conn, customer_scope(conn, identity.customer_id):
        yield ScopedConnection(
            connection=conn,
            customer_id=identity.customer_id,
            session_id=identity.session_id,
        )
