"""Server-side request identity for MCP tool execution.

CLAUDE.md's Core Security Invariant: identity is authenticated at the
FastAPI edge via JWT, before the agent runs, and must never live in a
prompt, message, tool schema, or tool argument. `RequestIdentity` is bound
here, server-side, by whatever authenticates the request (the FastAPI edge
today; a test fixture in tests) - never by the LLM, and never derived from
tool call arguments.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestIdentity:
    """Everything known about the caller for one request/conversation.

    `customer_id` is None for anonymous/public traffic (e.g. a Sales Agent
    conversation with no logged-in customer) - `session_id` is always
    present, since even anonymous conversations are logged and traced.
    """

    session_id: str
    customer_id: int | None = None


_current_identity: ContextVar[RequestIdentity | None] = ContextVar("current_identity", default=None)


@contextmanager
def bind_identity(identity: RequestIdentity) -> Iterator[None]:
    """Bind `identity` for the duration of one request.

    Called by the FastAPI edge (after JWT verification) around the agent
    invocation - never by tool code, and never by the model.
    """
    token = _current_identity.set(identity)
    try:
        yield
    finally:
        _current_identity.reset(token)


def get_current_identity() -> RequestIdentity | None:
    return _current_identity.get()
