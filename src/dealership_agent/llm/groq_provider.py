"""Groq LLM provider - used for local dev per CLAUDE.md.

Step 7, Part B3: the Groq SDK's own default retry behaviour (max_retries=2,
exponential backoff honouring Retry-After) is silent - Step 6's live smoke
test hit a 49-second backoff with no application-visible log line at all,
which was judged unacceptable. This provider disables the SDK's built-in
retry (max_retries=0 on the client) and implements its own explicit,
configurable retry loop instead: every attempt and every backoff wait is
logged, and max attempts / base delay / delay cap are all Settings-driven
rather than hardcoded.
"""

from __future__ import annotations

import time

import groq
import structlog

from dealership_agent.llm.base import LLMProvider, Message

logger = structlog.get_logger(__name__)

# Exceptions worth retrying: transient provider-side conditions. Anything
# else (bad request, auth failure, etc.) is not retryable and raises
# immediately - retrying a request that will never succeed just delays
# the caller's own degradation path for no benefit.
_RETRYABLE_EXCEPTIONS = (
    groq.RateLimitError,
    groq.APIConnectionError,
    groq.APITimeoutError,
    groq.InternalServerError,
)


class GroqProvider(LLMProvider):
    def __init__(
        self,
        api_key: str,
        *,
        max_retries: int = 3,
        base_delay_seconds: float = 1.0,
        max_delay_seconds: float = 20.0,
    ) -> None:
        # max_retries=0: the SDK must never retry silently on its own -
        # every retry in this provider goes through the explicit,
        # logged loop below instead.
        self._client = groq.Groq(api_key=api_key, max_retries=0)
        self._max_retries = max_retries
        self._base_delay_seconds = base_delay_seconds
        self._max_delay_seconds = max_delay_seconds

    def complete(self, messages: list[Message], *, model: str) -> str:
        attempt = 0
        while True:
            try:
                response = self._client.chat.completions.create(
                    model=model,
                    messages=[{"role": m.role, "content": m.content} for m in messages],  # type: ignore[misc]
                )
                return response.choices[0].message.content or ""
            except _RETRYABLE_EXCEPTIONS as exc:
                attempt += 1
                if attempt > self._max_retries:
                    logger.warning(
                        "llm_call_retries_exhausted",
                        provider="groq",
                        model=model,
                        attempts=attempt,
                        max_retries=self._max_retries,
                        error=str(exc),
                    )
                    raise
                delay = min(
                    self._base_delay_seconds * (2 ** (attempt - 1)), self._max_delay_seconds
                )
                logger.warning(
                    "llm_call_backoff",
                    provider="groq",
                    model=model,
                    attempt=attempt,
                    max_retries=self._max_retries,
                    delay_seconds=delay,
                    error=str(exc),
                )
                time.sleep(delay)
