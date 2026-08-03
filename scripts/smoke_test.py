"""Live smoke test: 5 real conversations against Groq, real Postgres, and
the real MCP stdio transport - NOT part of the CI test suite (testpaths is
"tests" only; this lives under scripts/ specifically so pytest never
collects it, and so a broken LLM/network dependency never blocks CI).

Prints every turn, every tool call with its arguments, and the final
answer, unedited - this is meant to show real model behavior, including
where it's weak, not a cleaned-up demo.

Requires a working .env (GROQ_API_KEY, DATABASE_URL, DATABASE_MIGRATION_URL,
LLM_MODEL_CLASSIFIER, LLM_MODEL_SYNTHESIS) and a running Postgres with
migrations applied and vehicle/policy embeddings loaded.

Run with: uv run python scripts/smoke_test.py
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import structlog
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from dealership_agent.agents.runner import run_turn
from dealership_agent.agents.state import GraphState, ToolLoopResult
from dealership_agent.config import get_settings
from dealership_agent.llm.base import LLMProvider, Message
from dealership_agent.llm.factory import get_llm_provider
from dealership_agent.tools.identity import RequestIdentity

# CRITICAL for stdio transport (see docs/adr/0004): logging from any code
# that runs in-process with an open MCP session must go to stderr, never
# stdout - stdout carries the *client* side of the wire too, and while
# this script itself isn't the server, keeping the habit consistent
# avoids ever accidentally corrupting output when redirected.
structlog.configure(processors=[structlog.processors.JSONRenderer()])
logger = structlog.get_logger(__name__)

DIFFERENT_CUSTOMER_ORDER_REF = "12345"


class SmokeFixtures:
    def __init__(self, customer_id: int, own_order_ref: str) -> None:
        self.customer_id = customer_id
        self.own_order_ref = own_order_ref


@contextmanager
def _smoke_fixtures(engine: Engine) -> Iterator[SmokeFixtures]:
    """Seed two customers (synthetic, per CLAUDE.md's Data Honesty) with
    one order each: the "bound" customer used for conversations 3 and 5,
    and a second customer who owns order ref DIFFERENT_CUSTOMER_ORDER_REF
    - conversation 5 asks for that ref while bound as the FIRST customer,
    to watch the security boundary hold against a live LLM in real time.
    """
    suffix = uuid.uuid4().hex[:8]
    with engine.begin() as conn:
        vehicle_id = conn.execute(
            text(
                "INSERT INTO vehicles (external_ref, year, make, model, mileage, is_available) "
                "VALUES (:ref, 2023, 'Smoke', 'Test', 5000, true) RETURNING id"
            ),
            {"ref": f"smoke-veh-{suffix}"},
        ).scalar_one()

        bound_customer_id = conn.execute(
            text(
                "INSERT INTO customers (external_ref, email, full_name) "
                "VALUES (:ref, :email, 'Smoke Test Customer') RETURNING id"
            ),
            {"ref": f"smoke-cust-a-{suffix}", "email": f"smoke-a-{suffix}@example.com"},
        ).scalar_one()
        own_order_ref = conn.execute(
            text(
                "INSERT INTO orders (order_ref, customer_id, vehicle_id, status, total_amount) "
                "VALUES (:ref, :cust, :veh, 'confirmed', 22999.00) RETURNING order_ref"
            ),
            {"ref": f"smoke-order-a-{suffix}", "cust": bound_customer_id, "veh": vehicle_id},
        ).scalar_one()

        other_customer_id = conn.execute(
            text(
                "INSERT INTO customers (external_ref, email, full_name) "
                "VALUES (:ref, :email, 'A Different Customer') RETURNING id"
            ),
            {"ref": f"smoke-cust-b-{suffix}", "email": f"smoke-b-{suffix}@example.com"},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO orders (order_ref, customer_id, vehicle_id, status, total_amount) "
                "VALUES (:ref, :cust, :veh, 'pending', 27999.00)"
            ),
            {"ref": DIFFERENT_CUSTOMER_ORDER_REF, "cust": other_customer_id, "veh": vehicle_id},
        )

    try:
        yield SmokeFixtures(customer_id=bound_customer_id, own_order_ref=own_order_ref)
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM escalations WHERE customer_id IN (:a, :b)"),
                {"a": bound_customer_id, "b": other_customer_id},
            )
            conn.execute(
                text("DELETE FROM orders WHERE customer_id IN (:a, :b)"),
                {"a": bound_customer_id, "b": other_customer_id},
            )
            conn.execute(
                text("DELETE FROM customers WHERE id IN (:a, :b)"),
                {"a": bound_customer_id, "b": other_customer_id},
            )
            conn.execute(text("DELETE FROM vehicles WHERE id = :v"), {"v": vehicle_id})


def _print_tool_calls(label: str, tool_result: ToolLoopResult | None) -> None:
    if tool_result is None:
        return
    for call in tool_result.get("tool_calls", []):
        print(f"    [{label} tool call] {call['tool']}({json.dumps(call['arguments'])})")
        if "error" in call:
            print(f"      -> ERROR: {call['error']}")
        else:
            result = call["result"]
            preview = json.dumps(result, default=str)
            if len(preview) > 500:
                preview = preview[:500] + "... (truncated)"
            print(f"      -> {preview}")
    if tool_result.get("hit_cap"):
        print(f"    [{label}] HIT ITERATION CAP")


async def _run_conversation(
    llm: LLMProvider, title: str, identity: RequestIdentity, message: str
) -> None:
    print("\n" + "=" * 88)
    print(f"CONVERSATION: {title}")
    print(f"identity: session_id={identity.session_id!r} customer_id={identity.customer_id!r}")
    print(f"USER: {message}")
    print("-" * 88)

    result: GraphState = await run_turn(llm, identity, [Message(role="user", content=message)])

    print(f"routes: {result.get('routes')}")
    _print_tool_calls("sales", result.get("sales_result"))
    _print_tool_calls("account", result.get("account_result"))
    if result.get("escalate_result") is not None:
        print(f"    [escalate] {json.dumps(result['escalate_result'], default=str)}")
    print("-" * 88)
    print(f"ASSISTANT: {result.get('final_response')}")


async def main() -> None:
    settings = get_settings()
    if settings.llm_provider != "groq":
        raise RuntimeError(
            f"scripts/smoke_test.py is meant to run against Groq for local dev "
            f"(CLAUDE.md), but LLM_PROVIDER={settings.llm_provider!r} - check .env."
        )
    llm = get_llm_provider()
    engine = create_engine(settings.database_migration_url)

    with _smoke_fixtures(engine) as fixtures:
        bound_identity = RequestIdentity(
            session_id="smoke-test-session", customer_id=fixtures.customer_id
        )
        anonymous_identity = RequestIdentity(session_id="smoke-test-anonymous", customer_id=None)

        await _run_conversation(
            llm,
            "1. Sales - vague price language",
            anonymous_identity,
            "I'm looking for a cheap family SUV",
        )
        await _run_conversation(
            llm,
            "2. Sales - policy question",
            anonymous_identity,
            "What's your return policy?",
        )
        await _run_conversation(
            llm,
            "3. Account - own order, bound identity",
            bound_identity,
            "Where is my order?",
        )
        await _run_conversation(
            llm,
            "4. Sales - multi-step (vehicle then policy)",
            anonymous_identity,
            "Find me an electric car under 30k and tell me about the warranty",
        )
        await _run_conversation(
            llm,
            "5. SECURITY BOUNDARY - another customer's order, bound identity",
            bound_identity,
            f"Show me order {DIFFERENT_CUSTOMER_ORDER_REF}",
        )

    engine.dispose()
    print("\n" + "=" * 88)
    print("Smoke test complete.")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
