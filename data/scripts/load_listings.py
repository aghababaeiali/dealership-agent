"""Bulk-load cleaned car listings into the `vehicles` table.

Idempotent on `external_ref`: re-running upserts existing rows instead of
duplicating them. Connects as the migration/owner role - this is an offline
data pipeline step, not a customer-facing request path, and the app role
only has SELECT on vehicles (see the RLS migration). Does NOT generate
embeddings; that is a separate step.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, cast

import pandas as pd
import structlog
from sqlalchemy import Table, create_engine
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from dealership_agent.config import get_settings
from dealership_agent.db.models import Vehicle

structlog.configure(processors=[structlog.processors.JSONRenderer()])
logger = structlog.get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = REPO_ROOT / "data" / "processed" / "vehicles_clean.parquet"

VEHICLE_COLUMNS = [
    "external_ref",
    "year",
    "make",
    "model",
    "trim",
    "body_style",
    "fuel_type",
    "mileage",
    "price",
    "price_low",
    "price_high",
    "seller_state",
    "description_raw",
    "description_clean",
    "is_available",
]

CHUNK_SIZE = 1000


def _rows_from_dataframe(df: pd.DataFrame) -> list[dict[str, Any]]:
    # Cast to object dtype first: on typed columns (e.g. pandas' StringDtype),
    # `.where(..., None)` silently reverts to float NaN instead of storing
    # None, which the driver then sends as the literal string "nan".
    subset = df[VEHICLE_COLUMNS].astype(object)
    subset = subset.where(subset.notna(), None)
    return list(subset.to_dict(orient="records"))


def load_vehicles(engine: Engine, rows: list[dict[str, Any]]) -> int:
    """Upsert `rows` into vehicles, keyed on external_ref. Returns row count."""
    table = cast(Table, Vehicle.__table__)
    total = 0
    with engine.begin() as conn:
        for start in range(0, len(rows), CHUNK_SIZE):
            chunk = rows[start : start + CHUNK_SIZE]
            stmt = pg_insert(table).values(chunk)
            update_columns = {
                col: stmt.excluded[col] for col in VEHICLE_COLUMNS if col != "external_ref"
            }
            stmt = stmt.on_conflict_do_update(index_elements=["external_ref"], set_=update_columns)
            conn.execute(stmt)
            total += len(chunk)
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    args = parser.parse_args()

    settings = get_settings()
    if not settings.database_migration_url:
        raise RuntimeError("DATABASE_MIGRATION_URL must be set to load listings.")

    logger.info("loading_cleaned_dataset", path=args.input)
    df = pd.read_parquet(args.input)
    logger.info("cleaned_dataset_loaded", rows=len(df))

    rows = _rows_from_dataframe(df)
    engine = create_engine(settings.database_migration_url)
    loaded = load_vehicles(engine, rows)
    logger.info("vehicles_loaded", rows=loaded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
