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
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 30

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

    aws_region: str = "us-east-1"
    aws_bedrock_role_arn: str = ""

    mcp_server_host: str = "localhost"
    mcp_server_port: int = 8765

    langfuse_host: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
