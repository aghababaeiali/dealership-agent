"""B3: explicit coverage for the tool-loop edge cases the bounded loop
(agents/tool_loop.py) was designed around. Each of the four paths gets
its own test:

  - Zero results: a narrow search returns nothing, so the loop must
    broaden and try again rather than answering "no results" after one
    search.
  - Tool error: a tool call that genuinely fails (invalid argument type,
    surfaced by the real MCP transport) must be caught and degrade the
    conversation, not raise out of run_turn.
  - Iteration cap: an agent that never emits "final" hits the 5-call cap
    and the graph must escalate rather than loop forever or silently
    truncate.
  - Multi-step chain: one sub-agent making two different tool calls in
    sequence (find a vehicle, then look up a related policy) within its
    own loop.

Also covers a fifth case discovered via the live smoke test
(scripts/smoke_test.py, run against real Groq): the LLM *provider* call
itself failing (e.g. the 413/rate-limit response Groq's free tier returns
once tool observations grow the conversation past its per-minute token
budget), which is a different failure mode from a tool call failing and
was not originally caught.

Runs against the real MCP stdio transport and real Postgres/pgvector, no
live LLM calls.
"""

import json
import uuid
from collections.abc import Iterator
from typing import TypedDict

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from dealership_agent.agents.runner import run_turn
from dealership_agent.config import get_settings
from dealership_agent.llm.base import LLMProvider, Message
from dealership_agent.tools.identity import RequestIdentity

settings = get_settings()

_AGENT_PROMPT_MARKERS = {
    "router": "routing classifier",
    "sales": "sales assistant",
    "synthesis": "helpful, honest assistant",
}


class _SalesLLMOutage(LLMProvider):
    """A minimal LLMProvider that raises when called for the sales agent
    specifically (simulating a provider-side failure like a rate limit or
    timeout) and otherwise behaves like a normal scripted fake."""

    def __init__(self, responses: dict[str, list[str]]) -> None:
        self._queues = {key: list(value) for key, value in responses.items()}

    def complete(self, messages: list[Message], *, model: str) -> str:
        system = messages[0].content if messages and messages[0].role == "system" else ""
        agent = next(
            (name for name, marker in _AGENT_PROMPT_MARKERS.items() if marker in system),
            "unknown",
        )
        if agent == "sales":
            raise RuntimeError("simulated Groq rate-limit error (413 Payload Too Large)")
        return self._queues[agent].pop(0)


class CustomerFixture(TypedDict):
    customer_id: int


@pytest.fixture(scope="module")
def owner_engine() -> Iterator[Engine]:
    engine = create_engine(settings.database_migration_url)
    yield engine
    engine.dispose()


@pytest.fixture
def customer(owner_engine: Engine) -> Iterator[CustomerFixture]:
    suffix = uuid.uuid4().hex[:8]
    with owner_engine.begin() as conn:
        customer_id = conn.execute(
            text(
                "INSERT INTO customers (external_ref, email, full_name) "
                "VALUES (:ref, :email, 'Edge Case Customer') RETURNING id"
            ),
            {"ref": f"edge-cust-{suffix}", "email": f"edge-{suffix}@example.com"},
        ).scalar_one()

    yield {"customer_id": customer_id}

    with owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM escalations WHERE customer_id = :c"), {"c": customer_id})
        conn.execute(text("DELETE FROM customers WHERE id = :c"), {"c": customer_id})


class TestZeroResultsBroadensTheQuery:
    async def test_a_zero_result_search_is_retried_with_broader_arguments(
        self, fake_llm_provider: type
    ) -> None:
        fake = fake_llm_provider(
            {
                "router": [json.dumps({"routes": ["sales"]})],
                "sales": [
                    # A make filter that cannot match any real row -
                    # deterministically zero results regardless of the
                    # embedding model's behavior.
                    json.dumps(
                        {
                            "action": "call_tool",
                            "tool": "search_listings",
                            "arguments": {
                                "query": "reliable SUV",
                                "make": "NonexistentMakeXYZ123",
                            },
                        }
                    ),
                    # Broaden: drop the impossible filter.
                    json.dumps(
                        {
                            "action": "call_tool",
                            "tool": "search_listings",
                            "arguments": {"query": "reliable SUV"},
                        }
                    ),
                    json.dumps(
                        {
                            "action": "final",
                            "answer": "Here's what I found once I broadened the search.",
                        }
                    ),
                ],
                "synthesis": ["Here's what I found once I broadened the search."],
            }
        )
        identity = RequestIdentity(session_id="sess-zero-results", customer_id=None)
        result = await run_turn(
            fake,
            identity,
            [Message(role="user", content="Do you have any NonexistentMakeXYZ123 SUVs?")],
        )

        sales_result = result["sales_result"]
        assert sales_result is not None
        assert sales_result["hit_cap"] is False
        assert len(sales_result["tool_calls"]) == 2
        # First call: the narrow, impossible-filter search returned nothing.
        assert sales_result["tool_calls"][0]["result"] == []
        # Second call: broadened, real results came back.
        assert len(sales_result["tool_calls"][1]["result"]) > 0
        assert sales_result["final_answer"] == "Here's what I found once I broadened the search."


class TestToolErrorDegradesGracefully:
    async def test_an_invalid_tool_argument_is_caught_and_does_not_crash_the_turn(
        self, fake_llm_provider: type
    ) -> None:
        fake = fake_llm_provider(
            {
                "router": [json.dumps({"routes": ["sales"]})],
                "sales": [
                    # year_min as a non-numeric string fails MCP-side
                    # argument validation - a genuine tool error, not a
                    # scope violation.
                    json.dumps(
                        {
                            "action": "call_tool",
                            "tool": "search_listings",
                            "arguments": {"query": "SUV", "year_min": "not-a-number"},
                        }
                    ),
                    json.dumps(
                        {
                            "action": "final",
                            "answer": (
                                "I ran into a technical issue with that search - could you "
                                "try rephrasing, e.g. with a specific year like 2020?"
                            ),
                        }
                    ),
                ],
                "synthesis": [
                    "I ran into a technical issue with that search - could you try "
                    "rephrasing, e.g. with a specific year like 2020?"
                ],
            }
        )
        identity = RequestIdentity(session_id="sess-tool-error", customer_id=None)

        result = await run_turn(
            fake, identity, [Message(role="user", content="SUVs from year not-a-number onward")]
        )

        sales_result = result["sales_result"]
        assert sales_result is not None
        assert sales_result["hit_cap"] is False
        assert len(sales_result["tool_calls"]) == 1
        failed_call = sales_result["tool_calls"][0]
        assert "error" in failed_call
        assert "result" not in failed_call
        # The customer-facing answer must be a plain-language degradation,
        # not a raised exception or a stack trace - run_turn completing at
        # all (no exception propagating out of this test) is the main
        # assertion; this checks the scripted recovery text made it through.
        assert result["final_response"] == (
            "I ran into a technical issue with that search - could you try "
            "rephrasing, e.g. with a specific year like 2020?"
        )


class TestIterationCapEscalates:
    async def test_an_agent_that_never_finalizes_hits_the_cap_and_escalates(
        self, customer: CustomerFixture, fake_llm_provider: type
    ) -> None:
        # Five call_tool responses, never a "final" - the loop must stop
        # itself at MAX_ITERATIONS (5) rather than looping forever.
        endless_tool_calls = [
            json.dumps(
                {
                    "action": "call_tool",
                    "tool": "search_listings",
                    "arguments": {"query": f"attempt {i}"},
                }
            )
            for i in range(5)
        ]
        fake = fake_llm_provider(
            {
                "router": [json.dumps({"routes": ["sales"]})],
                "sales": endless_tool_calls,
                "synthesis": ["I've escalated this to a human agent who will follow up shortly."],
            }
        )
        identity = RequestIdentity(
            session_id="sess-iteration-cap", customer_id=customer["customer_id"]
        )
        result = await run_turn(
            fake,
            identity,
            [Message(role="user", content="Find me the perfect car, don't stop looking")],
        )

        sales_result = result["sales_result"]
        assert sales_result is not None
        assert sales_result["hit_cap"] is True
        assert sales_result["final_answer"] is None
        assert len(sales_result["tool_calls"]) == 5

        # Hitting the cap must route to escalate, not straight to
        # synthesis with no result.
        escalate_result = result["escalate_result"]
        assert escalate_result is not None
        assert escalate_result["status"] == "escalated"
        assert (
            result["final_response"]
            == "I've escalated this to a human agent who will follow up shortly."
        )


class TestLLMProviderErrorEscalates:
    """Discovered live: a real Groq rate-limit response on iteration 2 of
    the sales loop crashed the whole turn uncaught, because the loop only
    wrapped the tool-call, not the LLM-provider call. A provider hiccup
    must degrade the same way an iteration-cap hit does: escalate, never
    propagate an exception out of run_turn."""

    async def test_a_failing_llm_call_escalates_instead_of_crashing_the_turn(
        self, customer: CustomerFixture
    ) -> None:
        outage_llm = _SalesLLMOutage(
            {
                "router": [json.dumps({"routes": ["sales"]})],
                "synthesis": ["I've escalated this to a human agent who will follow up shortly."],
            }
        )
        identity = RequestIdentity(
            session_id="sess-llm-outage", customer_id=customer["customer_id"]
        )

        result = await run_turn(
            outage_llm, identity, [Message(role="user", content="Find me a cheap SUV")]
        )

        sales_result = result["sales_result"]
        assert sales_result is not None
        assert sales_result["hit_cap"] is True
        assert sales_result["final_answer"] is None
        assert sales_result["tool_calls"] == []

        escalate_result = result["escalate_result"]
        assert escalate_result is not None
        assert escalate_result["status"] == "escalated"
        assert (
            result["final_response"]
            == "I've escalated this to a human agent who will follow up shortly."
        )


class TestEscalationWithNoAuthenticatedCustomer:
    """Discovered live: an anonymous sales conversation (no bound
    customer_id - genuinely legal per CLAUDE.md, Sales Agent is
    read-only/public) hit the iteration cap and got routed to escalate,
    which then tried to call the customer-scoped escalate_to_human tool
    and raised PermissionError, crashing the turn - escalate_to_human
    cannot persist a record with no customer_id. Must degrade to a
    "no ticket created" result instead."""

    async def test_anonymous_cap_hit_degrades_instead_of_crashing(
        self, fake_llm_provider: type
    ) -> None:
        endless_tool_calls = [
            json.dumps(
                {
                    "action": "call_tool",
                    "tool": "search_listings",
                    "arguments": {"query": f"attempt {i}"},
                }
            )
            for i in range(5)
        ]
        fake = fake_llm_provider(
            {
                "router": [json.dumps({"routes": ["sales"]})],
                "sales": endless_tool_calls,
                "synthesis": ["I'm having trouble with that request - please contact support."],
            }
        )
        identity = RequestIdentity(session_id="sess-anon-cap-hit", customer_id=None)

        result = await run_turn(
            fake, identity, [Message(role="user", content="Find me the perfect car")]
        )

        escalate_result = result["escalate_result"]
        assert escalate_result is not None
        assert escalate_result["status"] == "not_created"
        assert escalate_result["reason"] == "no_authenticated_customer"
        assert (
            result["final_response"]
            == "I'm having trouble with that request - please contact support."
        )


class TestMultiStepToolChain:
    async def test_sales_agent_finds_a_vehicle_then_looks_up_a_related_policy(
        self, fake_llm_provider: type
    ) -> None:
        fake = fake_llm_provider(
            {
                "router": [json.dumps({"routes": ["sales"]})],
                "sales": [
                    json.dumps(
                        {
                            "action": "call_tool",
                            "tool": "search_listings",
                            "arguments": {"query": "certified pre-owned SUV"},
                        }
                    ),
                    json.dumps(
                        {
                            "action": "call_tool",
                            "tool": "search_policy_docs",
                            "arguments": {"query": "warranty coverage"},
                        }
                    ),
                    json.dumps(
                        {
                            "action": "final",
                            "answer": "Here's a matching SUV and our warranty policy for it.",
                        }
                    ),
                ],
                "synthesis": ["Here's a matching SUV and our warranty policy for it."],
            }
        )
        identity = RequestIdentity(session_id="sess-multi-step", customer_id=None)
        result = await run_turn(
            fake,
            identity,
            [
                Message(
                    role="user",
                    content="Find me a certified pre-owned SUV and tell me about its warranty",
                )
            ],
        )

        sales_result = result["sales_result"]
        assert sales_result is not None
        assert sales_result["hit_cap"] is False
        assert len(sales_result["tool_calls"]) == 2
        assert sales_result["tool_calls"][0]["tool"] == "search_listings"
        assert len(sales_result["tool_calls"][0]["result"]) > 0
        assert sales_result["tool_calls"][1]["tool"] == "search_policy_docs"
        assert len(sales_result["tool_calls"][1]["result"]) > 0
        assert (
            sales_result["final_answer"] == "Here's a matching SUV and our warranty policy for it."
        )
