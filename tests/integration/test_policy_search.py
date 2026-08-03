"""Integration tests for policy document search.

Run against the real Postgres/pgvector instance with the real policy
corpus chunked and embedded (data/scripts/embed_policies.py). See
docs/POLICY_COVERAGE.md for the source of the unanswerable-question set
and the empirical similarity scores referenced below.
"""

from dealership_agent.retrieval.policy_search import search_policy_docs

# From docs/POLICY_COVERAGE.md: genuine matches score ~0.6-0.7; a query
# using the superseded document's own exact vocabulary still only reaches
# ~0.7 there. 0.45 sits well above every unanswerable question's top score
# (<=0.401) and well below a genuine match, so it cleanly separates the two.
UNANSWERABLE_SIMILARITY_CEILING = 0.45

UNANSWERABLE_QUESTIONS = [
    "Do you offer any discounts for military members, first responders, veterans, or students?",
    "Do you price-match a lower price I found for the same vehicle at another dealership?",
    "Do you offer any loyalty program or referral bonus for referring friends?",
    "Do you accept cryptocurrency as a form of payment?",
]


class TestSupersededPolicyDownRanking:
    def test_superseded_return_window_never_ranks_above_current(self) -> None:
        # This query uses the superseded document's own exact terms
        # ("7-day", "500-mile"); raw cosine similarity alone actually
        # ranks the superseded chunk higher here (see docs/POLICY_COVERAGE.md
        # / manual verification), which is exactly why down-ranking exists.
        results = search_policy_docs("7 day 500 mile return window", limit=10)
        assert len(results) > 0

        superseded_positions = [i for i, r in enumerate(results) if r.is_superseded]
        current_positions = [i for i, r in enumerate(results) if not r.is_superseded]

        if superseded_positions and current_positions:
            assert min(current_positions) < min(
                superseded_positions
            ), "A superseded chunk ranked above every non-superseded chunk"

    def test_current_return_window_chunk_is_top_result(self) -> None:
        results = search_policy_docs("how many days do I have to return a car", limit=3)
        assert len(results) > 0
        assert results[0].doc_slug == "returns"
        assert results[0].is_superseded is False

    def test_no_superseded_chunk_ever_precedes_a_current_chunk(self) -> None:
        """Structural guarantee, independent of query: for any result set,
        every superseded chunk must sort after every non-superseded chunk."""
        results = search_policy_docs("return policy return window mileage limit", limit=20)
        seen_superseded = False
        for r in results:
            if r.is_superseded:
                seen_superseded = True
            elif seen_superseded:
                raise AssertionError("Found a non-superseded chunk ranked after a superseded one")


class TestUnansweredQuestionsScoreLow:
    def test_known_unanswerable_questions_score_low(self) -> None:
        for question in UNANSWERABLE_QUESTIONS:
            results = search_policy_docs(question, limit=3)
            assert len(results) > 0
            top_similarity = results[0].similarity
            assert top_similarity < UNANSWERABLE_SIMILARITY_CEILING, (
                f"{question!r} scored {top_similarity:.3f}, expected < "
                f"{UNANSWERABLE_SIMILARITY_CEILING} (no document addresses this topic)"
            )

    def test_genuine_question_scores_above_unanswerable_ceiling(self) -> None:
        """Sanity check that the ceiling above is meaningful, not just low
        for every query regardless of relevance."""
        results = search_policy_docs("how many days do I have to return a car", limit=1)
        assert results[0].similarity > UNANSWERABLE_SIMILARITY_CEILING
