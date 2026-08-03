"""Step 7, Part B: unit tests for the tool-observation compactors, plus a
concrete before/after token measurement using realistic payload shapes
(matching retrieval/search.py's VehicleSearchResult and
retrieval/policy_search.py's PolicyChunkResult) - the numbers this test
prints are what the Step 7 status report's Part B cites.
"""

import json

from dealership_agent.agents.compaction import (
    TRUNCATION_MARKER,
    compact_error,
    compact_tool_result,
)
from dealership_agent.agents.tokens import estimate_tokens

_VEHICLE_ROW = {
    "id": 6958,
    "external_ref": "784281076",
    "year": 2006,
    "make": "Chevrolet",
    "model": "Equinox",
    "trim": "LT",
    "body_style": "SUV",
    "fuel_type": "Gasoline",
    "mileage": 108024,
    "price": 3477.5,
    "price_low": 2790.0,
    "price_high": 4165.0,
    "seller_state": "KS",
    "description_clean": (
        "This is a very nice all-wheel-drive SUV with low miles for the year. "
        "Well maintained, single owner, clean title, no accidents reported. "
        "Comes with a full service history and new tires all around."
    ),
    "similarity": 0.5889744056493752,
}

_POLICY_CHUNK_ROW = {
    "doc_slug": "warranty",
    "doc_title": "Northgate Motors Warranty Policy",
    "section_heading": "Standard Used Vehicle Limited Warranty",
    "content": (
        "All non-CPO used vehicles include a complimentary 90-day / 4,000-mile "
        "limited powertrain warranty, whichever comes first, starting on the "
        "delivery date. Powertrain coverage includes the engine block and "
        "internal components, transmission and transfer case, and front/rear "
        "drive axles. It does not cover wear items (brake pads, wiper blades, "
        "belts and hoses), damage from accidents, misuse, or lack of routine "
        "maintenance. Claims must be filed within 5 business days of the "
        "issue being discovered, at an authorized service center."
    ),
    "is_superseded": False,
    "similarity": 0.61,
}


class TestCompactVehicleResults:
    def test_compacts_to_one_line_per_item_with_key_fields(self) -> None:
        compacted = compact_tool_result([_VEHICLE_ROW], max_chars=10_000)
        assert "2006 Chevrolet Equinox" in compacted
        assert "$3,477.50" in compacted
        assert "108024 mi" in compacted
        assert "ref=784281076" in compacted
        # The full free-text description is dropped, not preserved verbatim.
        assert "single owner, clean title" not in compacted

    def test_meaningfully_reduces_estimated_tokens_vs_raw_json(self) -> None:
        rows = [_VEHICLE_ROW] * 10
        raw_tokens = estimate_tokens(json.dumps(rows, default=str))
        compacted_tokens = estimate_tokens(compact_tool_result(rows, max_chars=10_000))

        # Reported in the Step 7 status report as the concrete
        # before/after measurement for a 10-row search_listings result.
        print(f"\nvehicle results: raw={raw_tokens} tokens, compacted={compacted_tokens} tokens")
        assert compacted_tokens < raw_tokens * 0.5


class TestCompactPolicyChunks:
    def test_compacts_with_source_attribution(self) -> None:
        compacted = compact_tool_result([_POLICY_CHUNK_ROW], max_chars=10_000)
        assert "Northgate Motors Warranty Policy" in compacted
        assert "Standard Used Vehicle Limited Warranty" in compacted
        assert "90-day / 4,000-mile" in compacted

    def test_superseded_chunks_are_marked(self) -> None:
        superseded = {**_POLICY_CHUNK_ROW, "is_superseded": True}
        compacted = compact_tool_result([superseded], max_chars=10_000)
        assert "[SUPERSEDED]" in compacted

    def test_long_content_is_truncated_per_chunk_not_just_overall(self) -> None:
        long_chunk = {**_POLICY_CHUNK_ROW, "content": "x" * 5000}
        compacted = compact_tool_result([long_chunk, _POLICY_CHUNK_ROW], max_chars=1000)
        assert TRUNCATION_MARKER in compacted
        # The second (short) chunk's real content must still be present -
        # per-chunk budgeting, not "first chunk eats the whole budget".
        assert "90-day / 4,000-mile" in compacted

    def test_meaningfully_reduces_estimated_tokens_vs_raw_json(self) -> None:
        rows = [_POLICY_CHUNK_ROW] * 5
        raw_tokens = estimate_tokens(json.dumps(rows, default=str))
        compacted_tokens = estimate_tokens(compact_tool_result(rows, max_chars=800))

        print(f"\npolicy chunks: raw={raw_tokens} tokens, compacted={compacted_tokens} tokens")
        assert compacted_tokens < raw_tokens * 0.5


class TestCompactionEdgeCases:
    def test_none_result(self) -> None:
        assert compact_tool_result(None) == "null"

    def test_empty_list_result(self) -> None:
        assert "zero results" in compact_tool_result([])

    def test_unrecognized_shape_falls_back_to_capped_json(self) -> None:
        result = {"order_ref": "abc-123", "status": "confirmed"}
        compacted = compact_tool_result(result, max_chars=10_000)
        assert json.loads(compacted) == result

    def test_hard_ceiling_applies_regardless_of_shape(self) -> None:
        result = {"blob": "y" * 5000}
        compacted = compact_tool_result(result, max_chars=100)
        assert len(compacted) <= 100 + len(TRUNCATION_MARKER)
        assert compacted.endswith(TRUNCATION_MARKER)


class TestCompactError:
    def test_short_error_is_unchanged(self) -> None:
        assert compact_error("boom") == "boom"

    def test_long_error_is_truncated(self) -> None:
        long_error = "x" * 2000
        compacted = compact_error(long_error, max_chars=100)
        assert len(compacted) <= 100 + len(TRUNCATION_MARKER)
        assert compacted.endswith(TRUNCATION_MARKER)
