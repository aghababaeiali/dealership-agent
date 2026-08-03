"""Chunk, embed, and store the policy document corpus in policy_chunks.

Splits each markdown file in data/policies/ into one chunk per H2 section
(plus a preamble chunk for any text between the H1 title and the first
H2, e.g. the effective-date line). Re-embeds and replaces all chunks for a
given (doc_slug, model_name) on each run - the corpus is small and static,
so a full replace per document is simpler than an incremental upsert and
just as safe. Connects as the migration/owner role - the app role only has
SELECT on policy_chunks (see the policy_chunks migration).
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

import structlog
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from dealership_agent.config import get_settings
from dealership_agent.retrieval.embedder import embed_texts

structlog.configure(processors=[structlog.processors.JSONRenderer()])
logger = structlog.get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICIES_DIR = REPO_ROOT / "data" / "policies"

H1_RE = re.compile(r"^#\s+(.*)$")
H2_RE = re.compile(r"^##\s+(.*)$")


@dataclass
class Chunk:
    doc_slug: str
    doc_title: str
    section_heading: str | None
    chunk_index: int
    content: str
    is_superseded: bool


def parse_document(path: Path) -> list[Chunk]:
    """Split one markdown file into per-H2-section chunks."""
    doc_slug = path.stem
    is_superseded = "superseded" in doc_slug
    lines = path.read_text().splitlines()

    doc_title = doc_slug
    sections: list[tuple[str | None, list[str]]] = []
    current_heading: str | None = None
    current_lines: list[str] = []

    for line in lines:
        h1_match = H1_RE.match(line)
        h2_match = H2_RE.match(line)
        if h1_match:
            doc_title = h1_match.group(1).strip()
            continue
        if h2_match:
            if current_lines:
                sections.append((current_heading, current_lines))
            current_heading = h2_match.group(1).strip()
            current_lines = []
            continue
        current_lines.append(line)
    if current_lines:
        sections.append((current_heading, current_lines))

    chunks = []
    for index, (heading, body_lines) in enumerate(sections):
        content = "\n".join(body_lines).strip()
        if not content:
            continue
        chunks.append(
            Chunk(
                doc_slug=doc_slug,
                doc_title=doc_title,
                section_heading=heading,
                chunk_index=index,
                content=content,
                is_superseded=is_superseded,
            )
        )
    return chunks


def _embedding_text(chunk: Chunk) -> str:
    """Compact structured prefix (doc title + section heading) plus body,
    same pattern as embed_listings.py's year/make/model prefix."""
    parts = [chunk.doc_title]
    if chunk.section_heading:
        parts.append(chunk.section_heading)
    parts.append(chunk.content)
    return "\n".join(parts)


def load_chunks(policies_dir: Path) -> list[Chunk]:
    all_chunks: list[Chunk] = []
    for path in sorted(policies_dir.glob("*.md")):
        all_chunks.extend(parse_document(path))
    return all_chunks


def store_chunks(engine: Engine, chunks: list[Chunk], model_name: str) -> int:
    if not chunks:
        return 0

    texts = [_embedding_text(chunk) for chunk in chunks]
    vectors = embed_texts(texts)
    doc_slugs = sorted({chunk.doc_slug for chunk in chunks})

    with engine.begin() as conn:
        for doc_slug in doc_slugs:
            conn.execute(
                text(
                    "DELETE FROM policy_chunks "
                    "WHERE doc_slug = :doc_slug AND model_name = :model_name"
                ),
                {"doc_slug": doc_slug, "model_name": model_name},
            )
        for chunk, vector in zip(chunks, vectors, strict=True):
            conn.execute(
                text(
                    """
                    INSERT INTO policy_chunks
                        (doc_slug, doc_title, section_heading, chunk_index,
                         content, is_superseded, embedding, model_name)
                    VALUES
                        (:doc_slug, :doc_title, :section_heading, :chunk_index,
                         :content, :is_superseded, :embedding, :model_name)
                    """
                ),
                {
                    "doc_slug": chunk.doc_slug,
                    "doc_title": chunk.doc_title,
                    "section_heading": chunk.section_heading,
                    "chunk_index": chunk.chunk_index,
                    "content": chunk.content,
                    "is_superseded": chunk.is_superseded,
                    "embedding": str(vector),
                    "model_name": model_name,
                },
            )
    return len(chunks)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policies-dir", default=str(POLICIES_DIR))
    args = parser.parse_args()

    settings = get_settings()
    if not settings.database_migration_url:
        raise RuntimeError("DATABASE_MIGRATION_URL must be set to embed policy docs.")

    logger.info("loading_policy_documents", dir=args.policies_dir)
    chunks = load_chunks(Path(args.policies_dir))
    logger.info(
        "chunks_parsed",
        count=len(chunks),
        docs=len({chunk.doc_slug for chunk in chunks}),
    )

    engine = create_engine(settings.database_migration_url)
    stored = store_chunks(engine, chunks, settings.embedding_model_name)
    logger.info("chunks_stored", count=stored, model_name=settings.embedding_model_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
