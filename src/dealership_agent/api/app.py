"""The FastAPI service (Step 9, Part B). CLAUDE.md: ONE FastAPI service,
no microservices - this module is the single edge every request enters
through.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import structlog
from fastapi import Depends, FastAPI, HTTPException, Response, status
from sqlalchemy import text

from dealership_agent.agents.runner import run_turn
from dealership_agent.agents.state import GraphState
from dealership_agent.api.auth import get_current_identity
from dealership_agent.api.conversations import (
    append_messages,
    create_conversation,
    load_conversation,
)
from dealership_agent.api.rate_limit import get_rate_limiter
from dealership_agent.api.schemas import ChatRequest, ChatResponse
from dealership_agent.config import get_settings
from dealership_agent.db.session import engine
from dealership_agent.llm.base import Message
from dealership_agent.llm.factory import get_llm_provider
from dealership_agent.retrieval.search import VehicleSearchResult, search_listings
from dealership_agent.tools.identity import RequestIdentity

logger = structlog.get_logger(__name__)

app = FastAPI(title="dealership-agent")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness: this process is up and able to answer HTTP requests. No
    dependency checks here - that's /readyz's job; a liveness probe that
    depends on the DB or a subprocess can cause an orchestrator to kill a
    perfectly healthy process during a transient DB blip."""
    return {"status": "ok"}


def _check_database() -> None:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


@app.get("/readyz")
async def readyz(response: Response) -> dict[str, object]:
    """Readiness: checks the DB and the MCP tool server can actually be
    reached, since those are what /chat needs to do real work."""
    checks: dict[str, str] = {}
    healthy = True

    try:
        # engine.connect() is sync (blocking) I/O - run it off the event
        # loop. Without this, a slow connection (cold pool, pool_pre_ping's
        # extra round trip) blocks the ENTIRE single-worker event loop,
        # which also delays this same request's MCP subprocess handshake
        # below and any concurrently-arriving request (e.g. the ALB's own
        # health checks land from multiple AZs near-simultaneously) - Step
        # 12's live deployment never once passed its health check until
        # this was fixed, no matter how much CPU/timeout budget was given.
        await asyncio.to_thread(_check_database)
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 -- readiness must report, never raise
        checks["database"] = f"error: {exc}"
        healthy = False

    try:
        from dealership_agent.agents.mcp_session import open_mcp_session

        probe_identity = RequestIdentity(session_id=f"readyz-{uuid.uuid4().hex}")
        async with open_mcp_session(probe_identity):
            pass
        checks["mcp_server"] = "ok"
    except Exception as exc:  # noqa: BLE001 -- readiness must report, never raise
        checks["mcp_server"] = f"error: {exc}"
        healthy = False

    response.status_code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if healthy else "unavailable", "checks": checks}


@app.get("/listings", response_model=list[VehicleSearchResult])
def listings(
    query: str,
    price_min: float | None = None,
    price_max: float | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    max_mileage: int | None = None,
    make: str | None = None,
    model: str | None = None,
    body_style: str | None = None,
    fuel_type: str | None = None,
    limit: int = 10,
) -> list[VehicleSearchResult]:
    """Public catalog search - no authentication. Vehicle listings are
    public data (CLAUDE.md's Core Security Invariant only governs
    customer-scoped tables), the same data the Sales Agent's
    search_listings tool exposes."""
    return search_listings(
        query,
        price_min=price_min,
        price_max=price_max,
        year_min=year_min,
        year_max=year_max,
        max_mileage=max_mileage,
        make=make,
        model=model,
        body_style=body_style,
        fuel_type=fuel_type,
        limit=limit,
    )


def _tool_calls_made(result: GraphState) -> list[str]:
    names: list[str] = []
    for loop_result in (result.get("sales_result"), result.get("account_result")):
        if loop_result:
            names.extend(c["tool"] for c in loop_result["tool_calls"])
    if result.get("escalate_result") is not None:
        names.append("escalate_to_human")
    return names


@app.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    identity: RequestIdentity = Depends(get_current_identity),
) -> ChatResponse:
    settings = get_settings()
    limiter = get_rate_limiter(
        per_key_limit=settings.rate_limit_per_customer_per_minute,
        global_limit=settings.rate_limit_global_per_minute,
    )
    rate_result = limiter.check(str(identity.customer_id))
    if not rate_result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(int(rate_result.retry_after_seconds) + 1)},
        )

    # customer_id is bound to this request's authenticated identity only -
    # `request.conversation_id`/`request.message` come from the body, but
    # customer_id never does (see ChatRequest.model_config, extra="ignore").
    # /chat requires authentication, so get_current_identity always sets
    # this - but fail closed rather than trust that invariant silently.
    if identity.customer_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authenticated identity required"
        )
    customer_id = identity.customer_id

    if request.conversation_id is not None:
        loaded = load_conversation(engine, customer_id, request.conversation_id)
        if loaded is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
            )
        conversation_id, prior_messages = loaded
        conversation_ref = request.conversation_id
    else:
        conversation_id, conversation_ref = create_conversation(engine, customer_id)
        prior_messages = []

    user_message = Message(role="user", content=request.message)
    messages = [*prior_messages, user_message]

    llm = get_llm_provider()
    start = time.monotonic()
    result = await run_turn(llm, identity, messages)
    latency_ms = (time.monotonic() - start) * 1000

    answer = result.get("final_response") or ""
    assistant_message = Message(role="assistant", content=answer)
    append_messages(engine, customer_id, conversation_id, [user_message, assistant_message])

    return ChatResponse(
        answer=answer,
        degraded=result.get("degraded", False),
        degradation_reasons=result.get("degradation_reasons", []),
        tool_calls_made=_tool_calls_made(result),
        conversation_id=conversation_ref,
        latency_ms=latency_ms,
    )
