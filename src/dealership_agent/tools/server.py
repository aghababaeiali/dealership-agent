"""MCP tool server: the framework-independent tool layer LangGraph consumes
as an MCP client (CLAUDE.md).

Exactly four tools, split by permission scope:
  - search_listings, search_policy_docs: public data, no identity anywhere.
  - get_order_status, escalate_to_human: customer-scoped, go through
    tools.scope.customer_scoped_connection() - the identity comes from
    server-side request context (tools/identity.py), never from a tool
    argument. Re-read CLAUDE.md's Core Security Invariant before adding a
    parameter to any tool here.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from typing import Any

import structlog
from mcp.server.mcpserver import MCPServer
from sqlalchemy import text

from dealership_agent.retrieval.search import search_listings as _search_listings
from dealership_agent.tools.identity import get_current_identity
from dealership_agent.tools.scope import customer_scoped_connection

logger = structlog.get_logger(__name__)

server = MCPServer("dealership-agent-tools")


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
    financing). Stub: policy docs have not been ingested yet."""
    return []


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
