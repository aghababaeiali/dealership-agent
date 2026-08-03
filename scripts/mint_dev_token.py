"""DEV-ONLY: mint a JWT for local testing of the authenticated API.

This is not part of any real auth flow - in production, tokens are issued
by a real identity provider that holds the private key; this script
exists purely so `/chat` can be exercised locally without one. It
generates its own RSA keypair on first use (written to the paths
configured by JWT_PRIVATE_KEY_PATH / JWT_PUBLIC_KEY_PATH, default
dev_keys/) and refuses to run at all when APP_ENV=production, so a real
deployment can never mint a token for itself.

Run with: uv run python scripts/mint_dev_token.py --customer-id 1
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt
from sqlalchemy import create_engine, text

from dealership_agent.config import get_settings

DEFAULT_PRIVATE_KEY_PATH = "dev_keys/dev_jwt_private.pem"
DEFAULT_PUBLIC_KEY_PATH = "dev_keys/dev_jwt_public.pem"


def _ensure_customer_exists(database_migration_url: str, customer_id: int) -> None:
    """A minted token is only useful if its subject is a real row -
    `/chat` persists conversations under `customer_id`, which has a FK
    constraint to `customers`. Synthetic dev data either way (per
    CLAUDE.md's Data Honesty section), so this creates a matching
    customer with the given id if one doesn't already exist, using the
    owner/migration role - the same role data/scripts/embed_policies.py
    and scripts/seed_ci_vehicles.py already use for setup work."""
    engine = create_engine(database_migration_url)
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM customers WHERE id = :id"), {"id": customer_id}
        ).first()
        if exists is None:
            conn.execute(
                text(
                    "INSERT INTO customers (id, external_ref, email, full_name) "
                    "VALUES (:id, :ref, :email, :name)"
                ),
                {
                    "id": customer_id,
                    "ref": f"dev-token-customer-{customer_id}",
                    "email": f"dev-token-customer-{customer_id}@example.com",
                    "name": f"Dev Token Customer {customer_id}",
                },
            )
            # Explicit ids bypass the id column's own sequence - advance it
            # past the highest existing id so a later INSERT without an
            # explicit id never collides with one minted here.
            conn.execute(
                text(
                    "SELECT setval(pg_get_serial_sequence('customers', 'id'), "
                    "(SELECT MAX(id) FROM customers))"
                )
            )
            print(f"Created customer id={customer_id} (synthetic, dev-only)", file=sys.stderr)
    engine.dispose()


def _generate_keypair(private_path: Path, public_path: Path) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    private_path.chmod(0o600)

    public_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    print(f"Generated dev JWT keypair at {private_path} / {public_path}", file=sys.stderr)


def main() -> None:
    settings = get_settings()
    if settings.app_env == "production":
        print(
            "refusing to mint a dev token: APP_ENV=production. This script "
            "is dev-only - production tokens must come from a real identity "
            "provider that holds the private key.",
            file=sys.stderr,
        )
        sys.exit(1)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--customer-id", type=int, required=True)
    parser.add_argument(
        "--expires-minutes", type=int, default=settings.jwt_access_token_expire_minutes
    )
    args = parser.parse_args()

    _ensure_customer_exists(settings.database_migration_url, args.customer_id)

    private_path = Path(settings.jwt_private_key_path or DEFAULT_PRIVATE_KEY_PATH)
    public_path = Path(settings.jwt_public_key_path or DEFAULT_PUBLIC_KEY_PATH)
    if not private_path.exists() or not public_path.exists():
        _generate_keypair(private_path, public_path)
        if not settings.jwt_public_key_path:
            print(
                f"Note: JWT_PUBLIC_KEY_PATH is not set in .env - set it to "
                f"{public_path} so the running API can verify this token.",
                file=sys.stderr,
            )

    now = int(time.time())
    claims = {
        "sub": str(args.customer_id),
        "session_id": str(uuid.uuid4()),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + args.expires_minutes * 60,
    }
    token = jwt.encode(claims, private_path.read_text(), algorithm=settings.jwt_algorithm)
    print(token)


if __name__ == "__main__":
    main()
