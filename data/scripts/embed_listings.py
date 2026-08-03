"""Generate vehicle listing embeddings and store them in vehicle_embeddings.

Idempotent: a vehicle already embedded with the currently-configured model
(EMBEDDING_MODEL_NAME) is skipped via an anti-join, so re-running only
embeds vehicles that are new or embedded with a different model. Connects
as the migration/owner role - the app role only has SELECT on
vehicle_embeddings (see the RLS migration).
"""

from __future__ import annotations

import argparse
import time
from typing import Any

import structlog
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from dealership_agent.config import get_settings
from dealership_agent.retrieval.embedder import embed_texts

structlog.configure(processors=[structlog.processors.JSONRenderer()])
logger = structlog.get_logger(__name__)

BATCH_SIZE = 256

SELECT_UNEMBEDDED = text(
    """
    SELECT v.id, v.year, v.make, v.model, v.trim, v.description_clean
    FROM vehicles v
    LEFT JOIN vehicle_embeddings ve
        ON ve.vehicle_id = v.id AND ve.model_name = :model_name
    WHERE ve.id IS NULL
    ORDER BY v.id
    """
)

INSERT_EMBEDDING = text(
    """
    INSERT INTO vehicle_embeddings (vehicle_id, embedding, model_name)
    VALUES (:vehicle_id, :embedding, :model_name)
    """
)


def _embedding_text(row: Any) -> str:
    prefix_parts = [str(row.year), row.make, row.model, row.trim or ""]
    prefix = " ".join(part for part in prefix_parts if part).strip()
    description = row.description_clean or ""
    return f"{prefix} {description}".strip()


def embed_pending_vehicles(engine: Engine, model_name: str, batch_size: int = BATCH_SIZE) -> int:
    """Embed every vehicle not yet embedded with `model_name`. Returns count embedded."""
    total_embedded = 0
    with engine.begin() as conn:
        rows = conn.execute(SELECT_UNEMBEDDED, {"model_name": model_name}).fetchall()

    logger.info("vehicles_pending_embedding", count=len(rows))

    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        texts = [_embedding_text(row) for row in batch]
        vectors = embed_texts(texts)

        with engine.begin() as conn:
            for row, vector in zip(batch, vectors, strict=True):
                conn.execute(
                    INSERT_EMBEDDING,
                    {"vehicle_id": row.id, "embedding": str(vector), "model_name": model_name},
                )

        total_embedded += len(batch)
        logger.info("batch_embedded", batch_size=len(batch), total_so_far=total_embedded)

    return total_embedded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = parser.parse_args()

    settings = get_settings()
    if not settings.database_migration_url:
        raise RuntimeError("DATABASE_MIGRATION_URL must be set to embed listings.")

    engine = create_engine(settings.database_migration_url)

    start_time = time.monotonic()
    total_embedded = embed_pending_vehicles(engine, settings.embedding_model_name, args.batch_size)
    elapsed_seconds = time.monotonic() - start_time

    throughput = total_embedded / elapsed_seconds if elapsed_seconds > 0 else 0.0
    logger.info(
        "embedding_complete",
        model_name=settings.embedding_model_name,
        total_embedded=total_embedded,
        elapsed_seconds=round(elapsed_seconds, 2),
        rows_per_second=round(throughput, 1),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
