"""CI-ONLY: seed a small, synthetic vehicle catalog + real embeddings.

NOT part of the production data pipeline (data/scripts/{download,clean,
load,embed}_listings.py), which loads the real, licensed Kaggle dataset
per CLAUDE.md's Data Honesty section. That pipeline needs Kaggle
credentials that aren't available as a CI secret, so CI's Postgres
service container would otherwise have an empty `vehicles` table, which
several integration/security tests depend on returning non-empty,
structurally-varied search results (a cheap SUV, a low-mileage Ford
truck, a recent electric car, at least one row with an unreliable/masked
price). This script inserts a handful of clearly-synthetic rows covering
exactly those shapes, embedded with the same self-hosted model production
uses, so the real pgvector/RLS/MCP code paths are genuinely exercised in
CI without needing the licensed dataset. Never run against a real
dev/prod database - see CLAUDE.md: real vehicle data must stay real.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from dealership_agent.config import get_settings
from dealership_agent.retrieval.embedder import embed_texts

VEHICLES: list[dict[str, Any]] = [
    {
        "external_ref": "ci-seed-001",
        "year": 2016,
        "make": "Honda",
        "model": "CR-V",
        "trim": "EX",
        "body_style": "SUV",
        "fuel_type": "Gasoline",
        "mileage": 62000,
        "price": 17500.00,
        "price_low": 16500.00,
        "price_high": 18500.00,
        "is_price_reliable": True,
        "seller_state": "CA",
        "description_clean": "A reliable, affordable family SUV with a clean history.",
    },
    {
        "external_ref": "ci-seed-002",
        "year": 2018,
        "make": "Toyota",
        "model": "RAV4",
        "trim": "LE",
        "body_style": "SUV",
        "fuel_type": "Gasoline",
        "mileage": 48000,
        "price": 21000.00,
        "price_low": 20000.00,
        "price_high": 22000.00,
        "is_price_reliable": True,
        "seller_state": "TX",
        "description_clean": "Dependable compact SUV, great for families.",
    },
    {
        "external_ref": "ci-seed-003",
        "year": 2021,
        "make": "Ford",
        "model": "F-150",
        "trim": "XLT",
        "body_style": "TRUCK",
        "fuel_type": "Gasoline",
        "mileage": 32000,
        "price": 34000.00,
        "price_low": 33000.00,
        "price_high": 35000.00,
        "is_price_reliable": True,
        "seller_state": "TX",
        "description_clean": "Low mileage pickup truck, well maintained.",
    },
    {
        "external_ref": "ci-seed-004",
        "year": 2023,
        "make": "Tesla",
        "model": "Model 3",
        "trim": "Standard Range",
        "body_style": "SEDAN",
        "fuel_type": "Electric",
        "mileage": 8000,
        "price": 38000.00,
        "price_low": 37000.00,
        "price_high": 39000.00,
        "is_price_reliable": True,
        "seller_state": "CA",
        "description_clean": "Efficient electric car with modern technology.",
    },
    {
        "external_ref": "ci-seed-005",
        "year": 2015,
        "make": "Toyota",
        "model": "Camry",
        "trim": "LE",
        "body_style": "SEDAN",
        "fuel_type": "Gasoline",
        "mileage": 95000,
        # KBB-style $0.00 sentinel: no valuation available, masked to None
        # by search_listings regardless of the raw values stored here.
        "price": 0.00,
        "price_low": 0.00,
        "price_high": 0.00,
        "is_price_reliable": False,
        "seller_state": "FL",
        "description_clean": "Reliable sedan for daily commuting.",
    },
    {
        "external_ref": "ci-seed-006",
        "year": 2020,
        "make": "Honda",
        "model": "Civic",
        "trim": "Sport",
        "body_style": "SEDAN",
        "fuel_type": "Gasoline",
        "mileage": 41000,
        "price": 19500.00,
        "price_low": 18500.00,
        "price_high": 20500.00,
        "is_price_reliable": True,
        "seller_state": "NY",
        "description_clean": "Reliable sedan for daily commuting, low running costs.",
    },
]


def seed(engine: Engine, model_name: str) -> None:
    texts = [
        f"{v['year']} {v['make']} {v['model']} {v['trim']}. {v['description_clean']}"
        for v in VEHICLES
    ]
    vectors = embed_texts(texts)

    with engine.begin() as conn:
        for vehicle, vector in zip(VEHICLES, vectors, strict=True):
            vehicle_id = conn.execute(
                text(
                    """
                    INSERT INTO vehicles (
                        external_ref, year, make, model, trim, body_style,
                        fuel_type, mileage, price, price_low, price_high,
                        is_price_reliable, seller_state, description_clean,
                        is_available
                    ) VALUES (
                        :external_ref, :year, :make, :model, :trim, :body_style,
                        :fuel_type, :mileage, :price, :price_low, :price_high,
                        :is_price_reliable, :seller_state, :description_clean,
                        true
                    )
                    ON CONFLICT (external_ref) DO UPDATE SET
                        description_clean = EXCLUDED.description_clean
                    RETURNING id
                    """
                ),
                vehicle,
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO vehicle_embeddings (vehicle_id, embedding, model_name) "
                    "VALUES (:vehicle_id, :embedding, :model_name)"
                ),
                {
                    "vehicle_id": vehicle_id,
                    "embedding": str(vector),
                    "model_name": model_name,
                },
            )


def main() -> int:
    settings = get_settings()
    engine = create_engine(settings.database_migration_url)
    seed(engine, settings.embedding_model_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
