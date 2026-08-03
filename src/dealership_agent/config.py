"""Application configuration, read from environment variables / .env.

No secrets live in code. See .env.example for the full set of keys.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"

    jwt_secret_key: str = ""
    # Step 9, Part B: RS256, not the symmetric HS256 default from earlier
    # steps - asymmetric so the API only ever needs the public key to
    # verify, and only a separate signing party (or, in dev, the
    # scripts/mint_dev_token.py script) ever holds the private key.
    jwt_algorithm: str = "RS256"
    jwt_access_token_expire_minutes: int = 30
    # PEM-encoded RSA public key used to verify incoming tokens. Read
    # from a file path (never inlined in .env - multi-line PEM content
    # in a single env var is exactly the kind of secret-handling
    # friction CLAUDE.md's "no secrets in code" is easiest to honor by
    # avoiding entirely).
    jwt_public_key_path: str = ""
    # Dev-only: the matching private key, used exclusively by
    # scripts/mint_dev_token.py, never read by the running API. That
    # script refuses to run at all when app_env == "production".
    jwt_private_key_path: str = ""
    jwt_issuer: str = "dealership-agent"
    jwt_audience: str = "dealership-agent-api"

    # Step 9, Part B4: in-process rate limiting (no Redis/external
    # service - CLAUDE.md's anti-over-engineering rules and the
    # single-process modular-monolith deployment target both point the
    # same way). Fixed-window counters, reset every window.
    rate_limit_per_customer_per_minute: int = 20
    rate_limit_global_per_minute: int = 200

    # App role: the application connects as this role. RLS policies apply to
    # it. It must never be granted BYPASSRLS. See db/rls.py.
    database_url: str = ""
    # Owner/superuser role: used only by Alembic migrations, which need to
    # create roles and enable row-level security.
    database_migration_url: str = ""
    app_db_user: str = ""
    app_db_password: str = ""

    postgres_user: str = ""
    postgres_password: str = ""
    postgres_db: str = ""
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    embedding_model_name: str = "sentence-transformers/all-MiniLM-L6-v2"

    llm_provider: str = "groq"
    # Step 9, Part C2: per-node model routing, configurable per provider -
    # LLM_MODEL_CLASSIFIER/SYNTHESIS are Groq's (and the historical
    # default's) model names; BEDROCK_MODEL_CLASSIFIER/SYNTHESIS are
    # Bedrock's. `model_classifier`/`model_synthesis` below resolve to
    # whichever pair matches `llm_provider`, so switching providers never
    # requires also editing the model-name env vars a second time.
    llm_model_classifier: str = ""
    llm_model_synthesis: str = ""

    groq_api_key: str = ""

    # Step 7, Part B: explicit, configurable retry/backoff for LLM calls -
    # replaces the provider SDK's own silent internal retry (a 49-second
    # backoff with no application-visible log was judged unacceptable in
    # Step 6's live smoke test). Exponential: base_delay * 2**attempt,
    # capped at max_delay_seconds.
    llm_retry_max_retries: int = 3
    llm_retry_base_delay_seconds: float = 1.0
    llm_retry_max_delay_seconds: float = 20.0

    # Step 7, Part B: bound on one compacted tool observation fed back
    # into the loop's message history (see agents/compaction.py).
    loop_observation_max_chars: int = 800
    # Step 7, Part B: approximate cumulative token budget for one
    # sub-agent's tool-calling loop (agents/tokens.py's estimator). Set
    # conservatively under Groq's free-tier 6000-TPM limit to leave
    # headroom for the completion and for other concurrent usage; when a
    # loop iteration's estimated prompt size would exceed this, the loop
    # stops and synthesizes with whatever it already has rather than
    # making a call likely to fail with a 413/429.
    loop_token_budget: int = 4000

    # Step 9, Part C1: default changed from us-east-1 to eu-west-1 per
    # this step's explicit instruction.
    aws_region: str = "eu-west-1"
    aws_bedrock_role_arn: str = ""
    bedrock_model_classifier: str = ""
    bedrock_model_synthesis: str = ""

    mcp_server_host: str = "localhost"
    mcp_server_port: int = 8765

    langfuse_host: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    @property
    def model_classifier(self) -> str:
        """The cheap model used for routing, guardrail detection, and
        loop decisions (agents/nodes.py's router, tool-calling loops, and
        action-claim stage 1/2) - resolved per the active provider."""
        return (
            self.bedrock_model_classifier
            if self.llm_provider == "bedrock"
            else self.llm_model_classifier
        )

    @property
    def model_synthesis(self) -> str:
        """The stronger model used for final customer-facing synthesis -
        resolved per the active provider."""
        return (
            self.bedrock_model_synthesis
            if self.llm_provider == "bedrock"
            else self.llm_model_synthesis
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
