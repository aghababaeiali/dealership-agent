"""JWT authentication at the FastAPI edge (Step 9, Part B2).

CLAUDE.md's Core Security Invariant: identity is authenticated here,
before the agent runs, and the verified customer_id becomes a
`RequestIdentity` bound server-side (agents/runner.py, tools/identity.py)
- never read from the request body or a query parameter, and never
passed to the LLM.

RS256, not HS256: the API only ever needs the public key to verify a
token here, so only whatever issues tokens (in prod, a real auth
provider; in dev, scripts/mint_dev_token.py) needs to hold the private
key. Verifies signature, expiry, issuer, and audience - `jose.jwt.decode`
enforces all four when given `algorithms`, `audience`, and `issuer`.
"""

from __future__ import annotations

import uuid
from functools import lru_cache

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import jwt
from jose.exceptions import JWTError

from dealership_agent.config import Settings, get_settings
from dealership_agent.tools.identity import RequestIdentity

_bearer_scheme = HTTPBearer(auto_error=False)


@lru_cache
def _load_public_key(path: str) -> str:
    if not path:
        raise RuntimeError(
            "JWT_PUBLIC_KEY_PATH is not set - cannot verify tokens without a public key."
        )
    with open(path) as f:
        return f.read()


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def verify_token(token: str, settings: Settings) -> RequestIdentity:
    """Verify `token`'s signature, expiry, issuer, and audience, and
    return the identity it authenticates. Raises 401 on any failure -
    never returns a partially-trusted identity."""
    try:
        public_key = _load_public_key(settings.jwt_public_key_path)
    except RuntimeError as exc:
        raise _unauthorized(str(exc)) from exc

    try:
        claims = jwt.decode(
            token,
            public_key,
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
        )
    except JWTError as exc:
        raise _unauthorized("Invalid or expired token") from exc

    subject = claims.get("sub")
    if not subject:
        raise _unauthorized("Token is missing a subject claim")
    try:
        customer_id = int(subject)
    except (TypeError, ValueError) as exc:
        raise _unauthorized("Token subject claim is not a valid customer id") from exc

    session_id = claims.get("session_id") or str(uuid.uuid4())
    return RequestIdentity(session_id=session_id, customer_id=customer_id)


async def get_current_identity(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> RequestIdentity:
    """FastAPI dependency for authenticated endpoints. Never accepts
    identity from anywhere except a verified bearer token - the request
    body and query parameters are never consulted here, so a client
    sending its own `customer_id` anywhere else in the request has no
    effect on which identity actually gets bound."""
    if credentials is None:
        raise _unauthorized("Missing bearer token")
    return verify_token(credentials.credentials, get_settings())
