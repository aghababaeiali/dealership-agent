"""LLM provider interface.

CLAUDE.md: provider interface with two implementations - Groq (local dev)
and AWS Bedrock (prod). Per-node model routing: a cheap model for
classification/guardrails, a stronger model for final synthesis. Callers
pass the model name explicitly per call (from Settings.llm_model_classifier
/ llm_model_synthesis) - this interface never hardcodes a model.

Step 10, Part A: response normalization lives here, not in nodes.py.
Different providers wrap structured-JSON completions differently - Claude
on Bedrock tends to wrap JSON in ```json markdown fences and sometimes
adds a sentence of prose before/after it, Groq/Llama usually returns bare
JSON. Every node that parses JSON out of a completion (the router, both
sub-agent tool loops, the two-stage action-claim verifier) calls
`llm.complete()` through this one interface, so normalizing here once
fixes it for all of them, rather than duplicating cleanup logic (or
forgetting it) at every call site.
"""

import json
import re
from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?(.*?)\n?```$", re.DOTALL)
# Claude on Bedrock sometimes emits its own tool-call convention even when
# no native toolConfig is offered - wrapping the response in a single XML
# -ish tag pair, e.g. <function_calls>[...]</function_calls> - observed
# live in Step 10, Part A3 when the sales tool loop's single-object
# protocol got wrapped as a one-element array inside this tag. Generic
# (not hardcoded to "function_calls") since the exact tag name isn't
# part of any documented contract.
_XML_WRAPPER_RE = re.compile(r"^<(\w+)>(.*)</\1>$", re.DOTALL)


def _find_balanced_json_span(text: str) -> str | None:
    """Return the first balanced top-level `{...}` or `[...]` span in
    `text`, or None if no balanced span exists (e.g. truncated/malformed
    JSON, or plain text with no braces at all). Tracks whether we're
    inside a quoted string so a brace inside a string value never throws
    off the depth count."""
    start = None
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            break
    if start is None:
        return None

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def normalize_llm_response(raw: str) -> str:
    """Best-effort cleanup of a raw completion before any caller tries to
    `json.loads()` it (or, for plain-language replies, before returning
    it to the customer).

    Passes, all safe no-ops on ordinary natural-language text:

    1. Strip a single markdown code fence wrapping the whole response
       (```json ... ``` or bare ``` ... ```), with or without a language
       tag.
    2. Strip a single enclosing XML-ish tag pair wrapping the whole
       response (e.g. `<function_calls>...</function_calls>`) - Claude on
       Bedrock sometimes falls back to its own tool-call convention here
       even with no native toolConfig offered.
    3. If a balanced `{...}`/`[...]` span exists anywhere in what's left,
       extract just that span - this drops any leading prose ("Here's
       the JSON:") and trailing prose ("Let me know if that helps!")
       around it. If no balanced span exists (plain text with no braces,
       or genuinely malformed/truncated JSON), the text is returned as-is
       and left to fail downstream exactly as before - this function
       never raises and never masks malformed JSON as valid.
    4. If that span parses as a JSON array containing exactly one object,
       unwrap it to just that object - every call site in this codebase
       (router, tool loops, verifier) expects a single JSON object, never
       a top-level array, so a model wrapping its one call in a list (the
       same Claude tool-call convention as step 2) is unwrapped rather
       than left for the caller to fail on.
    5. If the (unwrapped) object has no "action" key, infer one from its
       shape: `tool` + `arguments` present -> "call_tool"; `answer`
       present -> "final". Observed live (Step 10, Part A3): Claude
       sometimes replies with its own bare `{"tool": ..., "arguments":
       ...}` shape, silently dropping our schema's "action" discriminator
       entirely. No other node's JSON schema in this codebase uses either
       key combination, so this inference is unambiguous - it never
       fires on a router, verifier, or plain-text response.
    """
    text = raw.strip()

    fence_match = _FENCE_RE.match(text)
    if fence_match:
        text = fence_match.group(1).strip()

    wrapper_match = _XML_WRAPPER_RE.match(text)
    if wrapper_match:
        text = wrapper_match.group(2).strip()

    json_span = _find_balanced_json_span(text)
    if json_span is not None:
        text = json_span

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text

    if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
        parsed = parsed[0]

    if isinstance(parsed, dict) and "action" not in parsed:
        if "tool" in parsed and "arguments" in parsed:
            parsed = {"action": "call_tool", **parsed}
        elif "answer" in parsed:
            parsed = {"action": "final", **parsed}

    if isinstance(parsed, dict):
        return json.dumps(parsed)
    return text


class LLMProvider(ABC):
    @abstractmethod
    def complete(self, messages: list[Message], *, model: str) -> str:
        """Return the assistant's completion text for `messages`,
        already normalized via `normalize_llm_response`."""
