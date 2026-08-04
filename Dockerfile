# Modular monolith, ONE FastAPI service (CLAUDE.md) - this image runs the
# whole application; the MCP tool server is spawned as a subprocess of it
# (agents/mcp_session.py), not a separate container/service.
#
# Multi-stage: the builder stage has uv and needs network access (to
# resolve/download dependencies and bake the embedding model below); the
# runtime stage is what actually ships - no build tools, no package
# manager, no dependency cache, just the venv, the app, and the
# pre-baked model.

# ---- builder ----
FROM python:3.12-slim AS builder

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ src/
COPY data/policies/ data/policies/
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Step 11, Part A2: bake the self-hosted embedding model into the image
# at build time - cold starts must never hit the network for it (a
# Fargate task with no NAT Gateway - see infra/terraform - has no
# outbound internet path anyway, so an un-baked model would fail
# outright, not just start slowly). HF_HOME pins the cache to a project-
# relative path so it can be copied into the runtime stage explicitly,
# rather than relying on whatever the default user cache directory
# happens to be.
ENV HF_HOME=/app/.cache/huggingface
RUN python -c "from dealership_agent.retrieval.embedder import get_embedder; get_embedder()"

# ---- runtime ----
FROM python:3.12-slim AS runtime

# Non-root: a compromised app process should not run as root inside its
# own container, even though the container boundary is itself a layer of
# isolation - defense in depth, same principle as RLS's FORCE (CLAUDE.md).
RUN groupadd --system --gid 10001 appuser \
    && useradd --system --uid 10001 --gid appuser --home-dir /app --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/.cache /app/.cache
COPY --chown=appuser:appuser src/ src/
COPY --chown=appuser:appuser data/policies/ data/policies/

ENV PATH="/app/.venv/bin:$PATH"
ENV HF_HOME=/app/.cache/huggingface
# Fail loudly, not slowly: if the baked cache is ever incomplete or
# missing, sentence-transformers/huggingface_hub must raise rather than
# silently fall back to a live download - the whole point of A2.
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1

USER appuser

EXPOSE 8000

# /healthz specifically (not /readyz): a container healthcheck is a
# liveness probe - "is this process responsive at all" - not a
# dependency-readiness check (DB/MCP reachability is /readyz's job, used
# by the ALB target group instead - see infra/terraform). No curl/wget in
# this image on purpose (smaller image); urllib is already present.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=3)" || exit 1

CMD ["uvicorn", "dealership_agent.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
