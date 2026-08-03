"""Integration tests for the FastAPI edge (Step 9, Part B6): JWT
verification, rate limiting, conversation persistence/isolation, and the
public listings endpoint. Runs against real Postgres and the real MCP
stdio transport (like tests/integration/test_agent_graph.py), with NO
live LLM calls - `fake_llm_provider` (tests/conftest.py) is monkeypatched
in wherever a route needs one.
"""

import json
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from dealership_agent.api.app import app
from dealership_agent.api.rate_limit import get_rate_limiter, reset_rate_limiter_for_tests
from dealership_agent.config import get_settings

settings = get_settings()


def _mint_token(
    customer_id: int,
    *,
    expired: bool = False,
    audience: str | None = None,
    issuer: str | None = None,
) -> str:
    """Build a token the same way scripts/mint_dev_token.py does, without
    shelling out - lets individual tests deliberately construct invalid
    tokens (expired, wrong audience) that the script's normal CLI
    wouldn't produce."""
    private_key = Path(settings.jwt_private_key_path).read_text()
    now = int(time.time())
    exp = now - 60 if expired else now + settings.jwt_access_token_expire_minutes * 60
    claims = {
        "sub": str(customer_id),
        "session_id": str(uuid.uuid4()),
        "iss": issuer if issuer is not None else settings.jwt_issuer,
        "aud": audience if audience is not None else settings.jwt_audience,
        "iat": now,
        "exp": exp,
    }
    return str(jwt.encode(claims, private_key, algorithm=settings.jwt_algorithm))


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> Iterator[None]:
    reset_rate_limiter_for_tests()
    yield
    reset_rate_limiter_for_tests()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def owner_engine() -> Iterator[Engine]:
    engine = create_engine(settings.database_migration_url)
    yield engine
    engine.dispose()


@pytest.fixture
def two_customers(owner_engine: Engine) -> Iterator[tuple[int, int]]:
    suffix = uuid.uuid4().hex[:8]
    with owner_engine.begin() as conn:
        customer_a = conn.execute(
            text(
                "INSERT INTO customers (external_ref, email, full_name) "
                "VALUES (:ref, :email, 'API Test Customer A') RETURNING id"
            ),
            {"ref": f"api-cust-a-{suffix}", "email": f"api-a-{suffix}@example.com"},
        ).scalar_one()
        customer_b = conn.execute(
            text(
                "INSERT INTO customers (external_ref, email, full_name) "
                "VALUES (:ref, :email, 'API Test Customer B') RETURNING id"
            ),
            {"ref": f"api-cust-b-{suffix}", "email": f"api-b-{suffix}@example.com"},
        ).scalar_one()

    yield customer_a, customer_b

    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM conversation_messages WHERE conversation_id IN "
                "(SELECT id FROM conversations WHERE customer_id = :a OR customer_id = :b)"
            ),
            {"a": customer_a, "b": customer_b},
        )
        conn.execute(
            text("DELETE FROM conversations WHERE customer_id = :a OR customer_id = :b"),
            {"a": customer_a, "b": customer_b},
        )
        conn.execute(
            text("DELETE FROM customers WHERE id = :a OR id = :b"),
            {"a": customer_a, "b": customer_b},
        )


class TestAuthRejection:
    def test_missing_token_returns_401(self, client: TestClient) -> None:
        response = client.post("/chat", json={"message": "hi"})
        assert response.status_code == 401

    def test_expired_token_returns_401(self, client: TestClient) -> None:
        token = _mint_token(1, expired=True)
        response = client.post(
            "/chat", json={"message": "hi"}, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401

    def test_wrong_audience_returns_401(self, client: TestClient) -> None:
        token = _mint_token(1, audience="some-other-api")
        response = client.post(
            "/chat", json={"message": "hi"}, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401


class TestListingsPublic:
    def test_listings_works_unauthenticated(self, client: TestClient) -> None:
        response = client.get("/listings", params={"query": "sedan", "limit": 2})
        assert response.status_code == 200
        assert isinstance(response.json(), list)


class TestConversationIdentityAndIsolation:
    def test_body_customer_id_is_ignored_and_conversation_is_owner_scoped(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        fake_llm_provider: type,
        two_customers: tuple[int, int],
        owner_engine: Engine,
    ) -> None:
        customer_a, customer_b = two_customers
        fake = fake_llm_provider(
            {
                "router": [json.dumps({"routes": ["sales"]})],
                "sales": [
                    json.dumps({"action": "final", "answer": "We have several sedans in stock."})
                ],
                "synthesis": ["We have several sedans in stock."],
                "verifier": [json.dumps({"claims": []})],
            }
        )
        monkeypatch.setattr("dealership_agent.api.app.get_llm_provider", lambda: fake)

        token_a = _mint_token(customer_a)
        response = client.post(
            "/chat",
            # customer_id here names customer_b - the request body is not
            # a source of identity (ChatRequest ignores unknown/extra
            # fields), so the conversation must still end up owned by
            # customer_a, whose identity came from the verified token.
            json={"message": "any sedans?", "customer_id": customer_b},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert response.status_code == 200
        conversation_ref = response.json()["conversation_id"]

        with owner_engine.connect() as conn:
            row = conn.execute(
                text("SELECT customer_id FROM conversations WHERE conversation_ref = :ref"),
                {"ref": conversation_ref},
            ).fetchone()
        assert row is not None
        assert row.customer_id == customer_a

        token_b = _mint_token(customer_b)
        cross_customer_response = client.post(
            "/chat",
            json={"message": "what did I just ask?", "conversation_id": conversation_ref},
            headers={"Authorization": f"Bearer {token_b}"},
        )
        # RLS makes "belongs to someone else" indistinguishable from
        # "doesn't exist" (db/rls.py) - 404, not 403, is the fail-closed
        # behavior here, and customer_b must not see customer_a's turn.
        assert cross_customer_response.status_code == 404


class TestRateLimit:
    def test_rate_limit_returns_429_with_retry_after(self, client: TestClient) -> None:
        customer_id = 9_999_999
        limiter = get_rate_limiter(per_key_limit=1, global_limit=1000)
        assert limiter.check(str(customer_id)).allowed

        token = _mint_token(customer_id)
        response = client.post(
            "/chat", json={"message": "hi"}, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 429
        assert "Retry-After" in response.headers
