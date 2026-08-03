"""Clean the downloaded car-listings dataset into a load-ready shape.

Reads the raw parquet files from download_listings.py, computes a price
proxy from the KBB fair-price range (salePrice itself is redacted in this
data tier - see docs/DATA_PROFILE.md), redacts PII found in free-text
descriptions, removes near-duplicate listings, normalises make/model casing,
strips recurring marketing boilerplate, and writes the result plus a
cleaning report.
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import TypedDict

import pandas as pd
import structlog

structlog.configure(processors=[structlog.processors.JSONRenderer()])
logger = structlog.get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = REPO_ROOT / "data" / "raw" / "autotrader-dataset"
PROCESSED_DIR = REPO_ROOT / "data" / "processed"
DOCS_DIR = REPO_ROOT / "docs"

PHONE_RE = re.compile(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")
EMAIL_RE = re.compile(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]+")
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"')]+", re.IGNORECASE)
# Abbreviated street suffixes with a trailing period only - this dataset's
# genuine addresses are formatted "1502 Industrial Park Dr.", while the
# unabbreviated/no-period forms collide constantly with ordinary prose
# ("your daily drive") and "Hwy" collides with "24 MPG Hwy." boilerplate.
ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+(?:[A-Z][a-zA-Z'.]*\s+){1,3}(?:St|Ave|Blvd|Rd|Dr|Ln|Ct|Pl|Pkwy|Cir|Ter)\."
)

PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "phone": PHONE_RE,
    "email": EMAIL_RE,
    "url": URL_RE,
    "address": ADDRESS_RE,
}

DEDUPE_KEY_COLUMNS = ["make", "model", "year", "trim", "mileage"]
DEDUPE_SIMILARITY_THRESHOLD = 0.90

# Makes whose correct display form is all-caps, not Title Case.
ALL_CAPS_MAKES = {"BMW", "GMC", "RAM", "MINI", "FIAT"}

BOILERPLATE_MIN_OCCURRENCES = 50
BOILERPLATE_MIN_LENGTH = 20


class CleaningStats(TypedDict):
    rows_before: int
    rows_with_null_price: int
    pii_counts: dict[str, int]
    duplicates_removed: int
    boilerplate_segments_found: int
    boilerplate_segments_examples: list[str]
    boilerplate_removals: int
    rows_after: int


def compute_price(df: pd.DataFrame) -> pd.DataFrame:
    """Derive `price` as the KBB fair-price midpoint.

    NULL when either bound is missing (~8% of rows) - never imputed.
    """
    df = df.copy()
    df["price_low"] = df["kbbFairPriceLow"]
    df["price_high"] = df["kbbFairPriceHigh"]
    df["price"] = (df["price_low"] + df["price_high"]) / 2
    return df


def redact_pii(text: str) -> tuple[str, dict[str, int]]:
    """Redact phone numbers, emails, URLs, and street addresses in `text`.

    Returns the redacted text and a per-category count of matches found.
    """
    counts = dict.fromkeys(PII_PATTERNS, 0)
    redacted = text
    for category, pattern in PII_PATTERNS.items():
        matches = pattern.findall(redacted)
        counts[category] = len(matches)
        if matches:
            redacted = pattern.sub(f"[REDACTED_{category.upper()}]", redacted)
    return redacted, counts


def redact_descriptions(descriptions: pd.Series) -> tuple[pd.Series, dict[str, int]]:
    """Apply `redact_pii` to a Series, returning redacted text + total counts."""
    totals = dict.fromkeys(PII_PATTERNS, 0)
    redacted_values = []
    for value in descriptions.fillna(""):
        redacted, counts = redact_pii(str(value))
        redacted_values.append(redacted)
        for category, count in counts.items():
            totals[category] += count
    return pd.Series(redacted_values, index=descriptions.index), totals


def find_near_duplicates(df: pd.DataFrame, text_column: str = "description_clean") -> pd.Index:
    """Return the index of rows to drop as near-duplicates.

    Groups by exact (make, model, year, trim, mileage), then within each
    group compares description text pairwise; a pair above the similarity
    threshold is a near-duplicate and only the first occurrence is kept.
    """
    to_drop: list[int] = []
    for _, group in df.groupby(DEDUPE_KEY_COLUMNS, dropna=False):
        if len(group) < 2:
            continue
        texts = group[text_column].fillna("").str.lower().str.strip()
        kept_indices: list[int] = []
        for idx, text in texts.items():
            is_duplicate = False
            for kept_idx in kept_indices:
                ratio = SequenceMatcher(None, text, texts[kept_idx]).ratio()
                if ratio >= DEDUPE_SIMILARITY_THRESHOLD:
                    is_duplicate = True
                    break
            if is_duplicate:
                to_drop.append(idx)
            else:
                kept_indices.append(idx)
    return pd.Index(to_drop)


def normalize_make(make: str) -> str:
    stripped = make.strip()
    if stripped.upper() in ALL_CAPS_MAKES:
        return stripped.upper()
    return stripped.title()


def normalize_model(model: str) -> str:
    return model.strip().title()


def _boilerplate_segments(texts: pd.Series) -> list[str]:
    """Split each description into segments and find ones repeated verbatim
    across many listings - a data-driven stand-in for a fixed phrase list."""
    segment_counts: Counter[str] = Counter()
    for text in texts.fillna(""):
        segments = re.split(r"<br\s*/?>\s*<br\s*/?>|(?<=[.!])\s+", str(text))
        for segment in segments:
            normalized = segment.strip()
            if len(normalized) >= BOILERPLATE_MIN_LENGTH:
                segment_counts[normalized] += 1

    return [
        segment for segment, count in segment_counts.items() if count >= BOILERPLATE_MIN_OCCURRENCES
    ]


def strip_boilerplate(texts: pd.Series) -> tuple[pd.Series, list[str], int]:
    """Remove recurring boilerplate segments from `texts`.

    Returns the cleaned Series, the list of boilerplate segments found, and
    the total number of removals performed across all rows.
    """
    boilerplate_segments = _boilerplate_segments(texts)
    if not boilerplate_segments:
        return texts, [], 0

    removal_count = 0
    cleaned_values = []
    for text in texts.fillna(""):
        cleaned = str(text)
        for segment in boilerplate_segments:
            if segment in cleaned:
                cleaned = cleaned.replace(segment, "")
                removal_count += 1
        cleaned = re.sub(r"<br\s*/?>", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        cleaned_values.append(cleaned)

    return pd.Series(cleaned_values, index=texts.index), boilerplate_segments, removal_count


def _load_raw(dataset_dir: Path) -> pd.DataFrame:
    files = sorted(dataset_dir.rglob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"No parquet files found under {dataset_dir}")
    frames = [pd.read_parquet(f) for f in files]
    return pd.concat(frames, ignore_index=True)


def clean(df: pd.DataFrame) -> tuple[pd.DataFrame, CleaningStats]:
    """Run the full cleaning pipeline. Returns the cleaned frame and a
    dict of stats for the cleaning report."""
    rows_before = len(df)

    df = compute_price(df)
    rows_with_null_price = int(df["price"].isna().sum())

    df["description_raw"] = df["description"]
    redacted, pii_counts = redact_descriptions(df["description"])
    df["description_clean"] = redacted

    df["make"] = df["makeName"]
    df["model"] = df["modelName"]

    duplicate_index = find_near_duplicates(df)
    duplicates_removed = len(duplicate_index)
    df = df.drop(index=duplicate_index)

    cleaned_descriptions, boilerplate_segments, removal_count = strip_boilerplate(
        df["description_clean"]
    )
    df["description_clean"] = cleaned_descriptions

    df["make"] = df["make"].apply(normalize_make)
    df["model"] = df["model"].apply(normalize_model)

    df["external_ref"] = df["listingId"]
    df["body_style"] = df["bodyStyle"]
    df["fuel_type"] = df["fuelType"]
    df["seller_state"] = df["sellerState"]
    df["is_available"] = True

    stats: CleaningStats = {
        "rows_before": rows_before,
        "rows_with_null_price": rows_with_null_price,
        "pii_counts": pii_counts,
        "duplicates_removed": duplicates_removed,
        "boilerplate_segments_found": len(boilerplate_segments),
        "boilerplate_segments_examples": boilerplate_segments[:5],
        "boilerplate_removals": removal_count,
        "rows_after": len(df),
    }
    return df, stats


def _write_report(stats: CleaningStats, out_path: Path) -> None:
    pii_counts = stats["pii_counts"]

    lines = [
        "# Data Cleaning Report: Car Listings\n",
        "Generated by `data/scripts/clean_listings.py`.\n",
        "## Row counts\n",
        f"- Rows before cleaning: **{stats['rows_before']:,}**",
        f"- Rows after cleaning: **{stats['rows_after']:,}**",
        f"- Near-duplicates removed: **{stats['duplicates_removed']:,}**\n",
        "## Price\n",
        f"- Rows with a null price (KBB range missing): "
        f"**{stats['rows_with_null_price']:,}** "
        f"({stats['rows_with_null_price'] / stats['rows_before'] * 100:.1f}%)\n",
        "## PII found in `description` (before redaction)\n",
        "| Category | Occurrences |",
        "| --- | --- |",
    ]
    for category, count in pii_counts.items():
        lines.append(f"| {category} | {count:,} |")

    lines += [
        "",
        "All occurrences were replaced with a `[REDACTED_<CATEGORY>]` marker in "
        "`description_clean`; `description_raw` preserves the original text.\n",
        "## Marketing boilerplate\n",
        f"- Recurring boilerplate segments identified "
        f"(appearing verbatim in >= {BOILERPLATE_MIN_OCCURRENCES} listings): "
        f"**{stats['boilerplate_segments_found']}**",
        f"- Total removals performed: **{stats['boilerplate_removals']:,}**\n",
    ]
    examples = stats["boilerplate_segments_examples"]
    if examples:
        lines.append("Examples:\n")
        for example in examples:
            lines.append(f"- `{example[:120]}`")
        lines.append("")

    out_path.write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", default=str(RAW_DIR))
    parser.add_argument("--out", default=str(PROCESSED_DIR / "vehicles_clean.parquet"))
    parser.add_argument("--report", default=str(DOCS_DIR / "DATA_CLEANING.md"))
    args = parser.parse_args()

    logger.info("loading_raw_dataset", raw_dir=args.raw_dir)
    df = _load_raw(Path(args.raw_dir))
    logger.info("raw_dataset_loaded", rows=len(df))

    cleaned, stats = clean(df)
    logger.info("cleaning_complete", **{k: v for k, v in stats.items() if k != "pii_counts"})

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cleaned.to_parquet(out_path, index=False)
    logger.info("cleaned_dataset_written", path=str(out_path), rows=len(cleaned))

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    _write_report(stats, report_path)
    logger.info("report_written", path=str(report_path))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
