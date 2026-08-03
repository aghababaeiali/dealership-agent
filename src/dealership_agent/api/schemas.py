"""Pydantic v2 request/response models for the API boundary (CLAUDE.md:
Pydantic v2 for all boundaries).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ChatRequest(BaseModel):
    # extra="ignore" is pydantic v2's default, set explicitly here because
    # it matters for security, not just style: if a client sends
    # customer_id (or anything else) in the body, it is silently dropped,
    # never read. Identity comes exclusively from the verified bearer
    # token (api/auth.py) - see CLAUDE.md's Core Security Invariant.
    model_config = ConfigDict(extra="ignore")

    message: str
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    answer: str
    degraded: bool
    degradation_reasons: list[str]
    # Tool names only, never arguments - arguments may contain free-text
    # customer input and this response shape is meant for monitoring/
    # display, not to be a second channel for raw request data.
    tool_calls_made: list[str]
    conversation_id: str
    latency_ms: float
