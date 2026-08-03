"""Conversation persistence for POST /chat (Step 9, Part B3).

Scoped by the same RLS mechanism as every other customer table
(db/rls.py::customer_scope). This is application code running in the
FastAPI process itself, not inside the MCP tool-server subprocess, so it
opens its own scoped connection directly rather than going through
tools/scope.py (that module is specifically the tool-execution
chokepoint used by tools/server.py).

Only authenticated conversations are persisted - POST /chat requires a
verified customer_id (api/auth.py), so there is no anonymous case here.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.engine import Engine

from dealership_agent.db.rls import customer_scope
from dealership_agent.llm.base import Message


def load_conversation(
    engine: Engine, customer_id: int, conversation_ref: str
) -> tuple[int, list[Message]] | None:
    """Return (conversation_id, prior messages) for `conversation_ref` if
    it belongs to `customer_id`, else None. RLS makes "belongs to another
    customer" and "doesn't exist at all" indistinguishable - same
    fail-closed design as get_order_status - so the caller should treat
    None as a generic 404, never as proof the ref exists for someone
    else."""
    with engine.connect() as conn, customer_scope(conn, customer_id):
        row = conn.execute(
            text("SELECT id FROM conversations WHERE conversation_ref = :ref"),
            {"ref": conversation_ref},
        ).fetchone()
        if row is None:
            return None
        conversation_id = row.id
        message_rows = conn.execute(
            text(
                "SELECT role, content FROM conversation_messages "
                "WHERE conversation_id = :cid ORDER BY id ASC"
            ),
            {"cid": conversation_id},
        ).fetchall()

    messages = [Message(role=r.role, content=r.content) for r in message_rows]
    return conversation_id, messages


def create_conversation(engine: Engine, customer_id: int) -> tuple[int, str]:
    """Start a new conversation for `customer_id`, returning
    (conversation_id, conversation_ref) - the ref is what's returned to
    the client and passed back on subsequent turns."""
    conversation_ref = f"conv-{uuid.uuid4().hex}"
    with engine.connect() as conn, customer_scope(conn, customer_id):
        conversation_id = conn.execute(
            text(
                "INSERT INTO conversations (conversation_ref, customer_id) "
                "VALUES (:ref, :customer_id) RETURNING id"
            ),
            {"ref": conversation_ref, "customer_id": customer_id},
        ).scalar_one()
    return conversation_id, conversation_ref


def append_messages(
    engine: Engine, customer_id: int, conversation_id: int, messages: list[Message]
) -> None:
    with engine.connect() as conn, customer_scope(conn, customer_id):
        for message in messages:
            conn.execute(
                text(
                    "INSERT INTO conversation_messages (conversation_id, role, content) "
                    "VALUES (:cid, :role, :content)"
                ),
                {"cid": conversation_id, "role": message.role, "content": message.content},
            )
