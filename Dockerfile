# Modular monolith, ONE FastAPI service (CLAUDE.md) - this image runs the
# whole application; the MCP tool server is spawned as a subprocess of it
# (agents/mcp_session.py), not a separate container/service.
FROM python:3.12-slim AS base

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ src/
COPY data/policies/ data/policies/
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["uvicorn", "dealership_agent.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
