# ADR 0001: Pin self-hosted Langfuse to v2, not v3

## Status

Accepted.

## Context

Langfuse is our observability layer (CLAUDE.md, Architecture Decisions).
Langfuse v3's self-hosted deployment requires ClickHouse (analytics store),
Redis (queues/cache), and MinIO/S3-compatible object storage in addition to
Postgres. v2's self-hosted deployment requires only Postgres.

This project is a modular monolith sized for a single dealership's customer
service workload. Running ClickHouse + Redis + MinIO alongside Postgres for
telemetry on a system this small is disproportionate infrastructure: more
containers to operate, patch, and monitor in both the local `docker-compose`
stack and the eventual ECS Fargate deployment, for a volume of traces that
Postgres alone comfortably handles. This is exactly the kind of scope creep
flagged in CLAUDE.md's Anti-Over-Engineering Rules.

## Decision

- Run `langfuse/langfuse:2` (not `:3` or `latest`) as the self-hosted
  observability server, backed by its own Postgres instance (already in
  `docker-compose.yml`).
- Pin the `langfuse` Python SDK to `>=2.53,<3` in `pyproject.toml` so the
  client major version matches the server major version. A v3 client talking
  to a v2 server (or vice versa) is not a supported combination.

## Migration path if scale demands it

If trace volume, retention needs, or query patterns eventually outgrow what
Postgres-backed Langfuse v2 can serve well, the upgrade path is:

1. Stand up ClickHouse, Redis, and object storage as new `docker-compose`
   services (and corresponding Terraform/ECS resources for prod).
2. Follow Langfuse's official v2 → v3 migration guide, which migrates
   historical trace data from Postgres into ClickHouse.
3. Bump the `langfuse` Python dependency to `>=3,<4` in the same change that
   cuts over the server image tag, so client and server stay in lockstep.

This should be a deliberate, scoped migration, not a default `latest` tag
that pulls it in silently.

## Consequences

- We give up whatever query/scale improvements v3's ClickHouse backend
  offers, in exchange for a much smaller operational footprint.
- The Docker image tag (`:2`) and the Python dependency constraint (`<3`)
  must be bumped together; bumping only one would pair a v2 server with a v3
  client or vice versa.
