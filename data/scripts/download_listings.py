"""Download the real car-listings dataset used for retrieval/search.

Primary source: rebrowser/autotrader-dataset (Kaggle).
Fallback source: ananaymital/us-used-cars-dataset (Kaggle), used only if the
primary dataset cannot be downloaded or yields no files.

Requires Kaggle API credentials at ~/.kaggle/kaggle.json.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import structlog

structlog.configure(processors=[structlog.processors.JSONRenderer()])
logger = structlog.get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = REPO_ROOT / "data" / "raw"

PRIMARY_DATASET = "rebrowser/autotrader-dataset"
FALLBACK_DATASET = "ananaymital/us-used-cars-dataset"


def _dataset_dir(slug: str) -> Path:
    return RAW_DIR / slug.split("/")[1]


def _has_data_files(dest: Path) -> bool:
    if not dest.exists():
        return False
    return any(dest.rglob("*.csv")) or any(dest.rglob("*.parquet"))


def download_dataset(slug: str, dest: Path, *, force: bool = False) -> bool:
    """Download a Kaggle dataset to `dest`. Returns True on usable data present."""
    if _has_data_files(dest) and not force:
        logger.info("dataset_already_present", slug=slug, dest=str(dest))
        return True

    from kaggle.api.kaggle_api_extended import KaggleApi

    dest.mkdir(parents=True, exist_ok=True)
    logger.info("dataset_download_started", slug=slug, dest=str(dest))
    try:
        api = KaggleApi()
        api.authenticate()
        api.dataset_download_files(slug, path=str(dest), unzip=True, quiet=True)
    except Exception:
        logger.warning("dataset_download_failed", slug=slug)
        return False

    ok = _has_data_files(dest)
    logger.info("dataset_download_completed", slug=slug, dest=str(dest), usable=ok)
    return ok


def write_sample(dest: Path, sample_rows: int) -> Path | None:
    """Write a small combined CSV sample for fast local iteration."""
    import pandas as pd

    parquet_files = sorted(dest.rglob("*.parquet"))
    csv_files = sorted(dest.rglob("*.csv"))

    if parquet_files:
        frames = [pd.read_parquet(f) for f in parquet_files]
    elif csv_files:
        frames = [pd.read_csv(f) for f in csv_files]
    else:
        logger.warning("no_data_files_found_for_sample", dest=str(dest))
        return None

    combined = pd.concat(frames, ignore_index=True)
    sample = combined.head(sample_rows)
    sample_path = dest / "sample.csv"
    sample.to_csv(sample_path, index=False)
    logger.info("sample_written", path=str(sample_path), rows=len(sample))
    return sample_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        default=PRIMARY_DATASET,
        help="Kaggle dataset slug to download (default: primary listings dataset)",
    )
    parser.add_argument(
        "--sample-rows",
        type=int,
        default=None,
        help="Also write an N-row combined CSV sample for fast local iteration",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if files already exist",
    )
    args = parser.parse_args()

    dest = _dataset_dir(args.dataset)
    ok = download_dataset(args.dataset, dest, force=args.force)

    used_slug = args.dataset
    if not ok and args.dataset == PRIMARY_DATASET:
        logger.warning("falling_back_to_secondary_dataset", fallback=FALLBACK_DATASET)
        used_slug = FALLBACK_DATASET
        dest = _dataset_dir(used_slug)
        ok = download_dataset(used_slug, dest, force=args.force)

    if not ok:
        logger.error("no_usable_dataset_downloaded")
        return 1

    logger.info("dataset_ready", slug=used_slug, dest=str(dest))

    if args.sample_rows:
        write_sample(dest, args.sample_rows)

    return 0


if __name__ == "__main__":
    sys.exit(main())
