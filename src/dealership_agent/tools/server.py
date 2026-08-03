"""MCP tool server: the framework-independent tool layer LangGraph consumes
as a real MCP client over stdio (CLAUDE.md).

Exactly five tools, split by permission scope:
  - search_listings, search_policy_docs: public data, no identity anywhere.
  - get_order_status, list_my_orders, escalate_to_human: customer-scoped,
    go through tools.scope.customer_scoped_connection() - the identity
    comes from server-side request context (tools/identity.py), never
    from a tool argument. Re-read CLAUDE.md's Core Security Invariant
    before adding a parameter to any tool here.

list_my_orders exists specifically so the account agent can answer a bare
"where is my order?" by looking the customer up, instead of guessing at
an order_ref it doesn't have (see docs/adr/0005-action-claim-verification.md
for the incident that motivated this - Step 6's smoke test showed the
account agent fabricating a human handoff after failing 3 guesses at an
order_ref for exactly this reason).

Run standalone (`python -m dealership_agent.tools.server`), this process
IS one MCP session: identity is read once from environment variables set
by the parent at subprocess spawn time (see agents/mcp_session.py) and
bound for the process's entire lifetime - never per call, never as a tool
argument. See docs/adr/0004-mcp-identity-propagation.md.
"""

from __future__ import annotations

import functools
import os
import sys
import time
from collections.abc import Callable
from typing import Any

import structlog
from mcp.server.fastmcp import FastMCP
from sqlalchemy import text

from dealership_agent.retrieval.policy_search import search_policy_docs as _search_policy_docs
from dealership_agent.retrieval.search import search_listings as _search_listings
from dealership_agent.tools.identity import (
    RequestIdentity,
    bind_identity,
    get_current_identity,
)
from dealership_agent.tools.scope import customer_scoped_connection

logger = structlog.get_logger(__name__)

server = FastMCP("dealership-agent-tools")

SESSION_ID_ENV_VAR = "DEALERSHIP_MCP_SESSION_ID"
CUSTOMER_ID_ENV_VAR = "DEALERSHIP_MCP_CUSTOMER_ID"


def _identity_from_env() -> RequestIdentity | None:
    """Read the identity this server process was spawned with.

    Returns None for an anonymous/public session (e.g. a Sales Agent
    conversation with no logged-in customer) - only DEALERSHIP_MCP_SESSION_ID
    is required for that; DEALERSHIP_MCP_CUSTOMER_ID is set only when a real
    customer is authenticated.
    """
    session_id = os.environ.get(SESSION_ID_ENV_VAR)
    if not session_id:
        return None
    customer_id_str = os.environ.get(CUSTOMER_ID_ENV_VAR)
    customer_id = int(customer_id_str) if customer_id_str else None
    return RequestIdentity(session_id=session_id, customer_id=customer_id)


def _row_count(result: object) -> int:
    if result is None:
        return 0
    if isinstance(result, list):
        return len(result)
    return 1


def _log_tool_call[F: Callable[..., Any]](fn: F) -> F:
    """Every tool call emits one structured log line: tool name, session
    id, argument keys (never values - arguments may include free-text
    customer input), row count returned, and duration in milliseconds."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        identity = get_current_identity()
        session_id = identity.session_id if identity is not None else None
        start = time.monotonic()
        result: object = None
        succeeded = False
        try:
            result = fn(*args, **kwargs)
            succeeded = True
            return result
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            logger.info(
                "tool_call",
                tool=fn.__name__,
                session_id=session_id,
                argument_keys=sorted(kwargs.keys()),
                row_count=_row_count(result) if succeeded else 0,
                duration_ms=round(duration_ms, 1),
            )

    return wrapper  # type: ignore[return-value]


@server.tool()
@_log_tool_call
def search_listings(
    query: str,
    price_min: float | None = None,
    price_max: float | None = None,
    year_min: int | None = None,
    max_mileage: int | None = None,
    make: str | None = None,
    body_style: str | None = None,
    fuel_type: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search the public vehicle catalog. Read-only, no customer data."""
    results = _search_listings(
        query,
        price_min=price_min,
        price_max=price_max,
        year_min=year_min,
        max_mileage=max_mileage,
        make=make,
        body_style=body_style,
        fuel_type=fuel_type,
        limit=limit,
    )
    return [r.model_dump() for r in results]


@server.tool()
@_log_tool_call
def search_policy_docs(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Search hand-authored dealership policy documents (warranty, returns,
    financing, trade-in, delivery, service, fees, test drives). Read-only,
    no customer data. Superseded policy versions are down-ranked below
    current ones, never returned above them."""
    results = _search_policy_docs(query, limit=limit)
    return [r.model_dump() for r in results]


@server.tool()
@_log_tool_call
def get_order_status(order_ref: str) -> dict[str, Any] | None:
    """Look up the status of one of the current customer's own orders.

    Returns None if `order_ref` doesn't exist OR belongs to a different
    customer - RLS makes those two cases indistinguishable by design, so
    this can never leak whether a ref exists for someone else.
    """
    with customer_scoped_connection() as scoped:
        row = scoped.connection.execute(
            text(
                """
                SELECT order_ref, status, total_amount, created_at,
                       expected_delivery_date, actual_delivery_date
                FROM orders
                WHERE order_ref = :order_ref
                """
            ),
            {"order_ref": order_ref},
        ).fetchone()
    return dict(row._mapping) if row is not None else None


@server.tool()
@_log_tool_call
def list_my_orders(status_filter: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """List the current customer's own orders (optionally filtered by
    status: pending, confirmed, in_preparation, ready_for_delivery,
    delivered, cancelled, refunded), most recent first.

    Never another customer's orders - same customer_scoped_connection
    chokepoint as get_order_status, so a missing/unauthenticated identity
    fails closed with no rows, not an empty-looking success.
    """
    with customer_scoped_connection() as scoped:
        rows = scoped.connection.execute(
            text(
                """
                SELECT o.order_ref, o.status, o.created_at,
                       o.expected_delivery_date, o.actual_delivery_date,
                       v.year, v.make, v.model
                FROM orders o
                JOIN vehicles v ON v.id = o.vehicle_id
                WHERE (CAST(:status_filter AS text) IS NULL OR o.status::text = :status_filter)
                ORDER BY o.created_at DESC
                LIMIT :limit
                """
            ),
            {"status_filter": status_filter, "limit": limit},
        ).fetchall()
    return [
        {
            "order_ref": row.order_ref,
            "status": row.status,
            "vehicle_summary": f"{row.year} {row.make} {row.model}",
            "created_at": row.created_at,
            "expected_delivery_date": row.expected_delivery_date,
            "actual_delivery_date": row.actual_delivery_date,
        }
        for row in rows
    ]


@server.tool()
@_log_tool_call
def escalate_to_human(summary: str, reason: str) -> dict[str, Any]:
    """Hand the current customer's conversation off to a human agent."""
    with customer_scoped_connection() as scoped:
        row = scoped.connection.execute(
            text(
                """
                INSERT INTO escalations (customer_id, conversation_id, summary, reason)
                VALUES (:customer_id, :conversation_id, :summary, :reason)
                RETURNING id, created_at
                """
            ),
            {
                "customer_id": scoped.customer_id,
                "conversation_id": scoped.session_id,
                "summary": summary,
                "reason": reason,
            },
        ).fetchone()
    assert row is not None  # noqa: S101 -- INSERT ... RETURNING always returns a row
    return {"id": row.id, "created_at": row.created_at, "status": "escalated"}


def main() -> None:
    """Entry point for `python -m dealership_agent.tools.server`.

    Binds this process's identity (if any) once, for its entire lifetime -
    one subprocess is one MCP session. Never rebinds per call.
    """
    # CRITICAL for stdio transport: stdout is the JSON-RPC wire. structlog's
    # default logger factory writes to stdout, which would silently corrupt
    # every message. All logging from this process must go to stderr.
    structlog.configure(
        processors=[structlog.processors.JSONRenderer()],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )

    identity = _identity_from_env()
    logger.info(
        "mcp_server_starting",
        transport="stdio",
        has_identity=identity is not None,
        session_id=identity.session_id if identity else None,
    )
    if identity is not None:
        with bind_identity(identity):
            server.run(transport="stdio")
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
