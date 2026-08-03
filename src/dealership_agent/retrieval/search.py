"""Hybrid vector + structured search over the public vehicle catalog.

No RLS applies here - `vehicles`/`vehicle_embeddings` are public catalog
data (CLAUDE.md's Core Security Invariant only governs customer-scoped
tables). Combines pgvector cosine similarity with SQL filters. Rows with a
NULL price, or with `is_price_reliable=false` (see docs/DATA_PRICE_AUDIT.md
- a $0.00 sentinel meaning "KBB had no valuation"), are excluded whenever a
price filter is active, and included otherwise - both cases mean "price is
not usable", never "free". Their price fields are also masked to None in
every result, filtered or not, so nothing downstream ever sees the $0.00
sentinel and mistakes it for a real price.
"""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from dealership_agent.config import get_settings
from dealership_agent.db.session import engine as default_engine
from dealership_agent.retrieval.embedder import embed_text


class VehicleSearchResult(BaseModel):
    id: int
    external_ref: str
    year: int
    make: str
    model: str
    trim: str | None
    body_style: str | None
    fuel_type: str | None
    mileage: int
    price: float | None
    price_low: float | None
    price_high: float | None
    seller_state: str | None
    description_clean: str | None
    similarity: float


def search_listings(
    query: str,
    *,
    price_min: float | None = None,
    price_max: float | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    max_mileage: int | None = None,
    make: str | None = None,
    model: str | None = None,
    body_style: str | None = None,
    fuel_type: str | None = None,
    limit: int = 10,
    connection: Connection | Engine | None = None,
) -> list[VehicleSearchResult]:
    """Search the public vehicle catalog by semantic similarity to `query`,
    narrowed by any structured filters supplied."""
    settings = get_settings()
    query_embedding = embed_text(query)

    conditions: list[str] = ["v.is_available = true"]
    params: dict[str, object] = {
        "embedding": str(query_embedding),
        "model_name": settings.embedding_model_name,
        "limit": limit,
    }

    # NULL price or is_price_reliable=false must be excluded only when a
    # price filter is active - either means "unknown"/"invalid", not
    # "free", and shouldn't silently match a max-price search, but an
    # unfiltered search should still surface these listings (with price
    # masked to None below).
    if price_min is not None or price_max is not None:
        conditions.append("v.price IS NOT NULL")
        conditions.append("v.is_price_reliable = true")
    if price_min is not None:
        conditions.append("v.price >= :price_min")
        params["price_min"] = price_min
    if price_max is not None:
        conditions.append("v.price <= :price_max")
        params["price_max"] = price_max
    if year_min is not None:
        conditions.append("v.year >= :year_min")
        params["year_min"] = year_min
    if year_max is not None:
        conditions.append("v.year <= :year_max")
        params["year_max"] = year_max
    if max_mileage is not None:
        conditions.append("v.mileage <= :max_mileage")
        params["max_mileage"] = max_mileage
    if make is not None:
        conditions.append("v.make ILIKE :make")
        params["make"] = make
    if model is not None:
        conditions.append("v.model ILIKE :model")
        params["model"] = model
    if body_style is not None:
        conditions.append("v.body_style ILIKE :body_style")
        params["body_style"] = body_style
    if fuel_type is not None:
        conditions.append("v.fuel_type ILIKE :fuel_type")
        params["fuel_type"] = fuel_type

    where_clause = " AND ".join(conditions)

    sql = text(
        f"""
        SELECT
            v.id, v.external_ref, v.year, v.make, v.model, v.trim,
            v.body_style, v.fuel_type, v.mileage, v.price, v.price_low,
            v.price_high, v.is_price_reliable, v.seller_state,
            v.description_clean,
            1 - (ve.embedding <=> (:embedding)::vector) AS similarity
        FROM vehicles v
        JOIN vehicle_embeddings ve
            ON ve.vehicle_id = v.id AND ve.model_name = :model_name
        WHERE {where_clause}
        ORDER BY ve.embedding <=> (:embedding)::vector ASC
        LIMIT :limit
        """  # noqa: S608 -- where_clause is built from a fixed set of literal
        # column conditions above, never from raw user input
    )

    engine_or_conn = connection if connection is not None else default_engine
    if isinstance(engine_or_conn, Engine):
        with engine_or_conn.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
    else:
        rows = engine_or_conn.execute(sql, params).fetchall()

    results = []
    for row in rows:
        data = dict(row._mapping)
        if not data.pop("is_price_reliable"):
            data["price"] = None
            data["price_low"] = None
            data["price_high"] = None
        results.append(VehicleSearchResult.model_validate(data))
    return results
