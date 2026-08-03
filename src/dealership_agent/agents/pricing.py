"""Translate vague price language into explicit numeric filters.

CLAUDE.md's Data Honesty and this project's own audit both apply here:
semantic similarity does not encode price relativity (see
docs/DATA_PRICE_AUDIT.md, "Note: semantic similarity does not encode
price relativity"). Embedding the word "cheap" into a search query does
not bias vector search toward cheap vehicles - it biases toward listings
whose *text* happens to use similar marketing language. So "cheap" must be
translated into an explicit `price_max` here, in code, before it ever
reaches `search_listings`, never left for the embedding to "understand".

Thresholds are the p25/p75 of `docs/DATA_PRICE_AUDIT.md`'s
`is_price_reliable=true` price distribution (the reliable-only figures,
not the raw ones still containing the $0.00 sentinel rows) - not
hardcoded guesses:

    p25 = $18,050.00   (used as the "cheap" price_max)
    p75 = $40,956.75   (used as the "premium" price_min)

Re-run `data/scripts/audit_prices.py` and update these constants (with a
citation back to the regenerated report) if the underlying vehicle
inventory's price distribution shifts materially.
"""

from __future__ import annotations

from typing import TypedDict

# From docs/DATA_PRICE_AUDIT.md, "Distribution (is_price_reliable=true
# only)" section.
CHEAP_PRICE_MAX = 18_050.00
PREMIUM_PRICE_MIN = 40_956.75

CHEAP_KEYWORDS = frozenset(
    {"cheap", "affordable", "budget", "inexpensive", "low-cost", "low cost", "economical"}
)
PREMIUM_KEYWORDS = frozenset(
    {"premium", "luxury", "high-end", "high end", "top-of-the-line", "upscale"}
)


class PriceFilters(TypedDict, total=False):
    price_max: float
    price_min: float


def derive_price_filters(text: str) -> PriceFilters | None:
    """Return explicit numeric filters if `text` uses vague price language
    this system recognizes, else None (no price language detected)."""
    lowered = text.lower()
    if any(keyword in lowered for keyword in CHEAP_KEYWORDS):
        return {"price_max": CHEAP_PRICE_MAX}
    if any(keyword in lowered for keyword in PREMIUM_KEYWORDS):
        return {"price_min": PREMIUM_PRICE_MIN}
    return None
