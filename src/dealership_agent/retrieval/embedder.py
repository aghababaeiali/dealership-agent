"""Self-hosted sentence embeddings.

CLAUDE.md: embeddings are self-hosted sentence-transformers, IDENTICAL in
dev and prod, never a cloud embedding API. The model name comes from
config (Settings.embedding_model_name), never hardcoded, so dev and prod
are guaranteed to use the same model as long as they share config.
"""

from functools import lru_cache
from typing import cast

from sentence_transformers import SentenceTransformer

from dealership_agent.config import get_settings

EMBEDDING_DIM = 384


@lru_cache(maxsize=1)
def get_embedder() -> SentenceTransformer:
    """Load (once per process) and cache the configured embedding model."""
    settings = get_settings()
    model = SentenceTransformer(settings.embedding_model_name)
    actual_dim = model.get_embedding_dimension()
    if actual_dim != EMBEDDING_DIM:
        raise ValueError(
            f"EMBEDDING_MODEL_NAME={settings.embedding_model_name!r} produces "
            f"{actual_dim}-dim vectors, but vehicle_embeddings.embedding is "
            f"vector({EMBEDDING_DIM}). Update the schema and re-embed, or "
            "point config at a 384-dim model."
        )
    return cast(SentenceTransformer, model)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts. Returns one 384-dim vector per input text."""
    model = get_embedder()
    vectors = model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
    return [vector.tolist() for vector in vectors]


def embed_text(text: str) -> list[float]:
    """Embed a single text (e.g. a search query)."""
    return embed_texts([text])[0]
