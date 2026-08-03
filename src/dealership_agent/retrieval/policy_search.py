"""Search over the hand-authored policy document corpus (policy_chunks).

No RLS - policy documents are public data. Ranks by pgvector cosine
similarity, then re-sorts so `is_superseded` chunks always sort after
non-superseded ones regardless of raw similarity score: a superseded
policy can share 80-90% of its wording with the current version (see
data/policies/returns-2024-superseded.md vs. returns.md), so similarity
alone cannot be trusted to rank the current version first.
"""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from dealership_agent.config import get_settings
from dealership_agent.db.session import engine as default_engine
from dealership_agent.retrieval.embedder import embed_text

# Fetch more candidates than requested before re-sorting on is_superseded,
# so a superseded chunk that happens to score higher on raw similarity
# doesn't crowd out a slightly-less-similar current chunk before the
# down-rank step gets to see it.
CANDIDATE_POOL_MULTIPLIER = 4
MIN_CANDIDATE_POOL = 20


class PolicyChunkResult(BaseModel):
    doc_slug: str
    doc_title: str
    section_heading: str | None
    content: str
    is_superseded: bool
    similarity: float


def search_policy_docs(
    query: str,
    *,
    limit: int = 5,
    connection: Connection | Engine | None = None,
) -> list[PolicyChunkResult]:
    """Search policy chunks by semantic similarity to `query`.

    Superseded chunks are down-ranked below every non-superseded chunk in
    the returned results, never returned above a current chunk on the
    same topic.
    """
    settings = get_settings()
    query_embedding = embed_text(query)
    pool_size = max(limit * CANDIDATE_POOL_MULTIPLIER, MIN_CANDIDATE_POOL)

    sql = text(
        """
        SELECT
            doc_slug, doc_title, section_heading, content, is_superseded,
            1 - (embedding <=> (:embedding)::vector) AS similarity
        FROM policy_chunks
        WHERE model_name = :model_name
        ORDER BY embedding <=> (:embedding)::vector ASC
        LIMIT :pool_size
        """
    )
    params = {
        "embedding": str(query_embedding),
        "model_name": settings.embedding_model_name,
        "pool_size": pool_size,
    }

    engine_or_conn = connection if connection is not None else default_engine
    if isinstance(engine_or_conn, Engine):
        with engine_or_conn.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
    else:
        rows = engine_or_conn.execute(sql, params).fetchall()

    candidates = [PolicyChunkResult.model_validate(dict(row._mapping)) for row in rows]
    candidates.sort(key=lambda c: (c.is_superseded, -c.similarity))
    return candidates[:limit]
