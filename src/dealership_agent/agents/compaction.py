"""Compact raw tool results into loop-history-safe observation strings.

Step 7, Part B: feeding full tool-result JSON back into the tool loop's
message history is what blew through Groq's free-tier token-per-minute
budget in Step 6's live smoke test, after just 1-2 real tool calls (see
docs/adr/0005-action-claim-verification.md and the Step 7 status report
for the actual before/after token counts). Two payload shapes get a
tailored compactor - vehicle search results become one line per item;
policy chunks truncate to a source-attributed excerpt. Anything else
falls back to a generic char-capped JSON dump, so a tool added later
degrades gracefully (verbose but bounded) instead of silently
reintroducing unbounded growth.

Compaction only ever touches what gets fed back into the LLM's own
message history - `ToolLoopResult.tool_calls` still keeps the raw,
uncompacted result for synthesis, tests, and logging.
"""

from __future__ import annotations

import json
from typing import Any

DEFAULT_MAX_OBSERVATION_CHARS = 800
TRUNCATION_MARKER = "...[truncated]"


def _compact_vehicle(item: dict[str, Any]) -> str:
    price = f"${item['price']:,.2f}" if item.get("price") is not None else "price unavailable"
    similarity = item.get("similarity")
    similarity_str = f"{similarity:.2f}" if isinstance(similarity, int | float) else "n/a"
    return (
        f"#{item.get('id')} {item.get('year')} {item.get('make')} {item.get('model')} "
        f"{item.get('trim') or ''} ({item.get('body_style') or 'n/a'}, "
        f"{item.get('fuel_type') or 'n/a'}), {item.get('mileage')} mi, {price}, "
        f"ref={item.get('external_ref')}, similarity={similarity_str}"
    ).strip()


def _compact_policy_chunk(item: dict[str, Any], max_chars: int) -> str:
    content = item.get("content", "")
    if len(content) > max_chars:
        content = content[:max_chars].rstrip() + TRUNCATION_MARKER
    superseded = " [SUPERSEDED]" if item.get("is_superseded") else ""
    return (
        f"[{item.get('doc_title')} - {item.get('section_heading') or 'general'}"
        f"{superseded}] {content}"
    )


def _is_vehicle_list(result: Any) -> bool:
    return (
        isinstance(result, list)
        and bool(result)
        and isinstance(result[0], dict)
        and "external_ref" in result[0]
        and "year" in result[0]
    )


def _is_policy_chunk_list(result: Any) -> bool:
    return (
        isinstance(result, list)
        and bool(result)
        and isinstance(result[0], dict)
        and "doc_slug" in result[0]
        and "content" in result[0]
    )


def compact_tool_result(result: Any, *, max_chars: int = DEFAULT_MAX_OBSERVATION_CHARS) -> str:
    """Return a compact, bounded summary of `result`, safe to feed back
    into the loop's message history - never the raw JSON dump.

    `max_chars` is a hard ceiling on the returned string regardless of
    payload shape, applied last, so an unrecognized or unexpectedly large
    result still can't grow the loop's history unboundedly.
    """
    if result is None:
        return "null"
    if isinstance(result, list) and not result:
        return "[] (zero results)"

    if _is_vehicle_list(result):
        body = "\n".join(_compact_vehicle(item) for item in result)
    elif _is_policy_chunk_list(result):
        # Leave headroom per chunk so N chunks together respect max_chars
        # overall, not max_chars each.
        per_chunk_budget = max(100, max_chars // max(1, len(result)))
        body = "\n".join(_compact_policy_chunk(item, per_chunk_budget) for item in result)
    else:
        body = json.dumps(result, default=str)

    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + TRUNCATION_MARKER
    return body


def compact_error(error_text: str, *, max_chars: int = DEFAULT_MAX_OBSERVATION_CHARS) -> str:
    """Same bound applied to tool-error text - provider/DB error messages
    can themselves be long enough to matter (observed in Step 6's smoke
    test: a Pydantic validation error alone ran to several hundred
    characters)."""
    if len(error_text) > max_chars:
        return error_text[:max_chars].rstrip() + TRUNCATION_MARKER
    return error_text
