"""Unit tests for the PII redaction and near-duplicate detection used by
data/scripts/clean_listings.py."""

import pandas as pd
from clean_listings import (
    find_near_duplicates,
    normalize_make,
    normalize_model,
    redact_pii,
)


class TestRedactPii:
    def test_redacts_phone_number(self) -> None:
        redacted, counts = redact_pii("Call us today at 555-123-4567 for details.")
        assert counts["phone"] == 1
        assert "[REDACTED_PHONE]" in redacted
        assert "555-123-4567" not in redacted

    def test_redacts_email(self) -> None:
        redacted, counts = redact_pii("Reach out at sales@example.com anytime.")
        assert counts["email"] == 1
        assert "[REDACTED_EMAIL]" in redacted
        assert "sales@example.com" not in redacted

    def test_redacts_url(self) -> None:
        redacted, counts = redact_pii("Visit https://example-dealer.com for photos.")
        assert counts["url"] == 1
        assert "[REDACTED_URL]" in redacted
        assert "https://example-dealer.com" not in redacted

    def test_redacts_street_address(self) -> None:
        redacted, counts = redact_pii("Come see us at 1502 Industrial Park Dr. today.")
        assert counts["address"] == 1
        assert "[REDACTED_ADDRESS]" in redacted
        assert "1502 Industrial Park Dr." not in redacted

    def test_clean_text_is_unchanged(self) -> None:
        text = "2024 Toyota Camry with low mileage and a clean CARFAX report."
        redacted, counts = redact_pii(text)
        assert redacted == text
        assert all(count == 0 for count in counts.values())

    def test_counts_multiple_occurrences(self) -> None:
        text = "Call 555-123-4567 or 555-987-6543 for a quote."
        _, counts = redact_pii(text)
        assert counts["phone"] == 2


class TestFindNearDuplicates:
    def _row(self, **overrides: object) -> dict[str, object]:
        base = {
            "make": "Toyota",
            "model": "Camry",
            "year": 2024,
            "trim": "SE",
            "mileage": 15000,
            "description_clean": "A clean, well-maintained Camry with low mileage.",
        }
        base.update(overrides)
        return base

    def test_identical_description_same_key_is_duplicate(self) -> None:
        df = pd.DataFrame([self._row(), self._row()])
        dropped = find_near_duplicates(df)
        assert len(dropped) == 1
        assert dropped[0] == 1

    def test_different_description_same_key_is_not_duplicate(self) -> None:
        df = pd.DataFrame(
            [
                self._row(description_clean="A clean, well-maintained Camry with low mileage."),
                self._row(
                    description_clean=(
                        "Rare manual transmission model, one owner, garage kept "
                        "since new, recent major service completed."
                    )
                ),
            ]
        )
        dropped = find_near_duplicates(df)
        assert len(dropped) == 0

    def test_identical_description_different_key_is_not_duplicate(self) -> None:
        df = pd.DataFrame(
            [
                self._row(model="Camry"),
                self._row(model="Corolla"),
            ]
        )
        dropped = find_near_duplicates(df)
        assert len(dropped) == 0

    def test_near_identical_description_is_duplicate(self) -> None:
        df = pd.DataFrame(
            [
                self._row(description_clean="A clean, well-maintained Camry with low mileage!!"),
                self._row(description_clean="A clean, well-maintained Camry with low mileage."),
            ]
        )
        dropped = find_near_duplicates(df)
        assert len(dropped) == 1


class TestNormalizeMakeModel:
    def test_all_caps_make_stays_uppercase(self) -> None:
        assert normalize_make("bmw") == "BMW"
        assert normalize_make("GMC") == "GMC"

    def test_ordinary_make_is_title_cased(self) -> None:
        assert normalize_make("toyota") == "Toyota"
        assert normalize_make("  chevrolet  ") == "Chevrolet"

    def test_model_is_title_cased(self) -> None:
        assert normalize_model("camry") == "Camry"
        assert normalize_model("f-150") == "F-150"
