"""Portfolio demo: 6 scripted conversations against the real graph, real
Postgres, the real MCP stdio transport, and (for 5 of the 6) the real
Groq provider - showcasing, in order: catalog search, policy Q&A, an
authenticated order lookup, the security boundary holding against a real
customer's own bound session, the action-claim verifier catching an
unbacked promise, and honest degradation.

The 6th conversation (honest degradation) temporarily lowers
LOOP_TOKEN_BUDGET to deterministically trigger the loop's real budget
guard - the LLM call itself is never faked anywhere in this script; only
that one threshold is lowered for one conversation, restored immediately
after, so the guard fires reliably for a screen recording rather than
depending on hitting Groq's free-tier rate limit by chance.

NOT part of the CI test suite (testpaths is "tests" only) - requires a
working .env (GROQ_API_KEY, DATABASE_URL, DATABASE_MIGRATION_URL,
LLM_MODEL_CLASSIFIER, LLM_MODEL_SYNTHESIS) and a running Postgres with
migrations applied and at least some vehicle listings + embedded policy
docs loaded (see README.md's Local Setup).

Run with: uv run python scripts/demo.py
"""

from __future__ import annotations

import json
import os
import sys
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

# Structured logs go to stderr, never stdout - this script's own stdout is
# the clean, screen-recordable transcript (every print() call below), and
# keeping structlog off it means that transcript is never interleaved with
# JSON log noise. Redirect stderr away (`2>/dev/null`) for the cleanest
# possible recording, or leave it for full diagnostic detail.
structlog.configure(
    processors=[structlog.processors.JSONRenderer()],
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
)
logger = structlog.get_logger(__name__)

DIFFERENT_CUSTOMER_ORDER_REF = "demo-other-customer-order"


class DemoFixtures:
    def __init__(self, customer_id: int, own_order_ref: str) -> None:
        self.customer_id = customer_id
        self.own_order_ref = own_order_ref


@contextmanager
def _demo_fixtures(engine: Engine) -> Iterator[DemoFixtures]:
    """Two synthetic customers (per CLAUDE.md's Data Honesty), one order
    each - the "bound" customer is used for every authenticated
    conversation below; the second customer's order ref is what the
    security-boundary conversation asks for while still bound as the
    FIRST customer."""
    suffix = uuid.uuid4().hex[:8]
    with engine.begin() as conn:
        vehicle_id = conn.execute(
            text(
                "INSERT INTO vehicles (external_ref, year, make, model, mileage, is_available) "
                "VALUES (:ref, 2023, 'Demo', 'Test', 5000, true) RETURNING id"
            ),
            {"ref": f"demo-veh-{suffix}"},
        ).scalar_one()

        bound_customer_id = conn.execute(
            text(
                "INSERT INTO customers (external_ref, email, full_name) "
                "VALUES (:ref, :email, 'Demo Customer') RETURNING id"
            ),
            {"ref": f"demo-cust-a-{suffix}", "email": f"demo-a-{suffix}@example.com"},
        ).scalar_one()
        own_order_ref = conn.execute(
            text(
                "INSERT INTO orders (order_ref, customer_id, vehicle_id, status, total_amount) "
                "VALUES (:ref, :cust, :veh, 'confirmed', 22999.00) RETURNING order_ref"
            ),
            {"ref": f"demo-order-a-{suffix}", "cust": bound_customer_id, "veh": vehicle_id},
        ).scalar_one()

        other_customer_id = conn.execute(
            text(
                "INSERT INTO customers (external_ref, email, full_name) "
                "VALUES (:ref, :email, 'A Different Customer') RETURNING id"
            ),
            {"ref": f"demo-cust-b-{suffix}", "email": f"demo-b-{suffix}@example.com"},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO orders (order_ref, customer_id, vehicle_id, status, total_amount) "
                "VALUES (:ref, :cust, :veh, 'pending', 27999.00)"
            ),
            {"ref": DIFFERENT_CUSTOMER_ORDER_REF, "cust": other_customer_id, "veh": vehicle_id},
        )

    try:
        yield DemoFixtures(customer_id=bound_customer_id, own_order_ref=own_order_ref)
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
        print(f"  [{label} tool call] {call['tool']}({json.dumps(call['arguments'])})")
        if "error" in call:
            print(f"    -> ERROR: {call['error']}")
        else:
            result = call["result"]
            preview = json.dumps(result, default=str)
            if len(preview) > 400:
                preview = preview[:400] + "... (truncated)"
            print(f"    -> {preview}")
    if tool_result.get("hit_cap"):
        print(f"  [{label}] HIT ITERATION CAP")
    if tool_result.get("hit_budget_guard"):
        print(f"  [{label}] TOKEN BUDGET GUARD FIRED - stopped before an over-budget LLM call")
    if tool_result.get("llm_call_failed"):
        print(f"  [{label}] LLM CALL FAILED")


async def _run_conversation(
    llm: LLMProvider,
    number: int,
    title: str,
    showcasing: str,
    identity: RequestIdentity,
    message: str,
) -> None:
    print("\n" + "=" * 78)
    print(f"CONVERSATION {number}: {title}")
    print(f"Showcasing: {showcasing}")
    print(f"Identity: session_id={identity.session_id!r} customer_id={identity.customer_id!r}")
    print("-" * 78)
    print(f"USER: {message}")

    result: GraphState = await run_turn(llm, identity, [Message(role="user", content=message)])

    print(f"ROUTING DECISION: routes={result.get('routes')}")
    _print_tool_calls("sales", result.get("sales_result"))
    _print_tool_calls("account", result.get("account_result"))
    if result.get("escalate_result") is not None:
        print(f"  [escalate] {json.dumps(result['escalate_result'], default=str)}")
    print("-" * 78)
    print(f"ASSISTANT: {result.get('final_response')}")
    print(f"[degraded={result.get('degraded')} reasons={result.get('degradation_reasons')}]")


async def _run_degraded_conversation(
    llm: LLMProvider,
    number: int,
    title: str,
    showcasing: str,
    identity: RequestIdentity,
    message: str,
) -> None:
    """Same as _run_conversation, but temporarily forces the tool loop's
    real token-budget guard to fire deterministically - see this
    module's docstring for why. The LLM call itself is never faked."""
    original = os.environ.get("LOOP_TOKEN_BUDGET")
    os.environ["LOOP_TOKEN_BUDGET"] = "1"  # noqa: S105 -- an env var name, not a password
    get_settings.cache_clear()
    try:
        print("\n" + "=" * 78)
        print(f"CONVERSATION {number}: {title}")
        print(f"Showcasing: {showcasing}")
        print(
            "(LOOP_TOKEN_BUDGET temporarily set to 1 token for this conversation only, "
            "to make the real budget guard fire deterministically rather than waiting "
            "on Groq's free-tier rate limit by chance - the LLM call itself is real.)"
        )
        print("-" * 78)
        print(f"USER: {message}")

        result: GraphState = await run_turn(llm, identity, [Message(role="user", content=message)])

        print(f"ROUTING DECISION: routes={result.get('routes')}")
        _print_tool_calls("sales", result.get("sales_result"))
        _print_tool_calls("account", result.get("account_result"))
        print("-" * 78)
        print(f"ASSISTANT: {result.get('final_response')}")
        print(f"[degraded={result.get('degraded')} reasons={result.get('degradation_reasons')}]")
    finally:
        if original is None:
            os.environ.pop("LOOP_TOKEN_BUDGET", None)
        else:
            os.environ["LOOP_TOKEN_BUDGET"] = original
        get_settings.cache_clear()


async def main() -> None:
    settings = get_settings()
    if settings.llm_provider != "groq":
        raise RuntimeError(
            f"scripts/demo.py is meant to run against Groq for local dev "
            f"(CLAUDE.md), but LLM_PROVIDER={settings.llm_provider!r} - check .env."
        )
    llm = get_llm_provider()
    engine = create_engine(settings.database_migration_url)

    print("=" * 78)
    print("dealership-agent demo: 6 conversations against the real system")
    print("=" * 78)

    with _demo_fixtures(engine) as fixtures:
        bound_identity = RequestIdentity(
            session_id="demo-session", customer_id=fixtures.customer_id
        )
        anonymous_identity = RequestIdentity(session_id="demo-anonymous", customer_id=None)

        await _run_conversation(
            llm,
            1,
            "Catalog search",
            "public catalog search returning concrete vehicles, no login required",
            anonymous_identity,
            "I'm looking for a cheap, reliable family SUV under $25,000",
        )
        await _run_conversation(
            llm,
            2,
            "Policy Q&A",
            "search_policy_docs over the hand-authored policy corpus",
            anonymous_identity,
            "What's your return policy?",
        )
        await _run_conversation(
            llm,
            3,
            "Authenticated order lookup",
            "a real customer's order, looked up under their own verified identity",
            bound_identity,
            "Where is my order?",
        )
        await _run_conversation(
            llm,
            4,
            "SECURITY BOUNDARY",
            "the SAME bound identity from conversation 3 asks for a DIFFERENT customer's "
            "order ref - RLS + the identity-free tool schema mean this can only ever "
            "come back empty, never someone else's data",
            bound_identity,
            f"Can you show me the details for order {DIFFERENT_CUSTOMER_ORDER_REF}?",
        )
        await _run_conversation(
            llm,
            5,
            "Action-claim verifier",
            "no cancel_order tool exists anywhere in this system - watch the verifier "
            "catch a reply that claims otherwise and correct it before the customer sees it",
            bound_identity,
            "Please cancel my order",
        )
        await _run_degraded_conversation(
            llm,
            6,
            "Honest degradation",
            "the tool loop's real token-budget guard firing, and synthesis being "
            "instructed to say so honestly rather than paper over it",
            anonymous_identity,
            "I'm looking for a cheap, reliable family SUV under $25,000",
        )

    engine.dispose()
    print("\n" + "=" * 78)
    print("Demo complete.")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
