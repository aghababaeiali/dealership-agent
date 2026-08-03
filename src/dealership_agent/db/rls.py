"""Row-Level Security scope enforcement.

This is where the Session (CLAUDE.md's Core Security Invariant) crosses from
server-side state into the database layer: `customer_scope` sets
`app.customer_id` for exactly one transaction, server-side, at tool
execution time. It is never derived from a prompt, message, or tool
argument.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.engine import Connection


@contextmanager
def customer_scope(connection: Connection, customer_id: int) -> Iterator[Connection]:
    """Scope every statement on `connection` to `customer_id` for one transaction.

    Uses `set_config('app.customer_id', ..., true)` - the `true` (is_local)
    argument makes this behave like `SET LOCAL`: Postgres automatically
    clears it at COMMIT/ROLLBACK. Opening the transaction here and always
    closing it on exit guarantees the setting cannot leak into a later
    transaction on a pooled/reused connection, even if the caller forgets.

    `SET LOCAL app.customer_id = ...` is deliberately not used directly:
    Postgres does not support bind parameters in `SET` statements, and
    `set_config(...)` is a plain function call that does.
    """
    with connection.begin():
        connection.execute(
            text("SELECT set_config('app.customer_id', :customer_id, true)"),
            {"customer_id": str(customer_id)},
        )
        yield connection
