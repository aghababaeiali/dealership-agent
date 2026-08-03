"""Integration tests for hybrid vector + structured search.

Run against the real Postgres/pgvector instance with real listings and
real embeddings loaded (data/scripts/{clean,load,embed}_listings.py). These
assert on structural properties - filters respected, NULL price handling,
result count, similarity ordering - never on exact vehicle ids, since the
underlying dataset can change.
"""

from dealership_agent.retrieval.search import search_listings


def _assert_ordered_by_descending_similarity(similarities: list[float]) -> None:
    assert similarities == sorted(similarities, reverse=True)


class TestSearchListings:
    def test_family_suv_respects_price_and_body_style_filters(self) -> None:
        results = search_listings(
            "cheap reliable family SUV",
            body_style="SUV",
            price_max=25000,
            limit=10,
        )
        assert len(results) > 0
        assert len(results) <= 10
        for r in results:
            assert r.price is not None
            assert r.price <= 25000
            assert r.body_style is not None
            assert r.body_style.lower() == "suv"
        _assert_ordered_by_descending_similarity([r.similarity for r in results])

    def test_pickup_truck_respects_make_and_mileage_filters(self) -> None:
        results = search_listings(
            "low mileage pickup truck",
            make="Ford",
            max_mileage=50000,
            limit=10,
        )
        assert len(results) <= 10
        for r in results:
            assert r.make.lower() == "ford"
            assert r.mileage <= 50000
        _assert_ordered_by_descending_similarity([r.similarity for r in results])

    def test_electric_car_respects_fuel_type_and_year_filters(self) -> None:
        results = search_listings(
            "efficient electric car with modern technology",
            fuel_type="Electric",
            year_min=2022,
            limit=10,
        )
        assert len(results) <= 10
        for r in results:
            assert r.fuel_type is not None
            assert r.fuel_type.lower() == "electric"
            assert r.year >= 2022
        _assert_ordered_by_descending_similarity([r.similarity for r in results])

    def test_unfiltered_search_can_include_null_price_rows(self) -> None:
        """No price filter active -> NULL price rows are eligible results."""
        results = search_listings("reliable sedan for daily commuting", limit=5000)
        assert len(results) > 0
        assert any(r.price is None for r in results)

    def test_price_filtered_search_excludes_null_price_rows(self) -> None:
        """Any active price filter -> NULL price rows must never appear."""
        results = search_listings(
            "reliable sedan for daily commuting",
            price_min=0,
            limit=5000,
        )
        assert len(results) > 0
        assert all(r.price is not None for r in results)
