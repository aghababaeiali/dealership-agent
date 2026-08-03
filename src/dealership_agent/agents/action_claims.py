"""Post-generation verification that a synthesized reply doesn't make an
ACTION CLAIM unsupported by any real tool result (Step 7, Part A).

Step 6's live smoke test showed the account agent telling a customer
"this conversation will now be handed off to a human agent" when no such
handoff had happened - no escalate_to_human call, no escalations row,
nothing. Prompting alone doesn't fix this reliably: the failure is in
what the model *claims*, under context (repeated tool failures) that
made the fabrication look like a reasonable way to end the conversation.
A model that will fabricate under that pressure once cannot be trusted
to police its own instructions about the same thing a second time in the
same generation. So this is a second, independent pass over the actual
output, checked against actual state - structural, not a differently
worded prompt.

Why an LLM classifier and not regex (see docs/adr/0005 for the full
justification): the claim types below are expressed in unbounded natural
language ("I've escalated this", "connecting you with someone", "your
refund is on its way", "your order has been cancelled", and any
paraphrase of those). A keyword/regex list needs constant expansion and
still misses paraphrases; a small classification call with the cheap
model, given the actual tool evidence, generalizes across phrasing at
low cost and latency.

Fails CLOSED, matching CLAUDE.md's fail-closed philosophy for identity:
if the verifier's own output can't be parsed, the draft is treated as
unverifiable rather than waved through - the cost of an occasional
unnecessary regeneration is far lower than a false promise reaching the
customer.
"""

from __future__ import annotations

import json
from typing import Any, Literal, TypedDict

import structlog

from dealership_agent.agents.state import GraphState
from dealership_agent.llm.base import LLMProvider, Message

logger = structlog.get_logger(__name__)

ActionClaimType = Literal[
    "human_handoff", "order_cancelled", "booking_made", "refund_issued", "other_state_change"
]

ACTION_CLAIM_TYPES: tuple[ActionClaimType, ...] = (
    "human_handoff",
    "order_cancelled",
    "booking_made",
    "refund_issued",
    "other_state_change",
)


class ActionClaim(TypedDict):
    type: str
    quote: str
    substantiated: bool


VERIFIER_SYSTEM_PROMPT = """\
You are a strict fact-checker reviewing a customer service assistant's \
draft reply before it is sent to the customer. Find any ACTION CLAIM in \
the draft - a statement that some state-changing action was actually \
performed or will definitely happen:

  - human_handoff: a human agent will or has followed up, taken over, or \
    the conversation was/will be transferred or escalated to a person.
  - order_cancelled: an order was cancelled.
  - booking_made: a test drive or other booking was made, confirmed, or \
    scheduled.
  - refund_issued: a refund was issued or is being processed.
  - other_state_change: any other claim that a record was created, \
    changed, or removed on the customer's behalf.

You are given the tool evidence actually available from this turn. A \
claim is SUBSTANTIATED only if that evidence explicitly shows the action \
really happened - e.g. human_handoff is substantiated only by an \
escalation result with status "escalated", never by one that is absent, \
failed, or skipped. Intending to do something, a failed or skipped tool \
call, or no matching tool call at all does NOT substantiate a claim.

Describing a vehicle, a policy, an order's current status, a price, or \
asking a clarifying question is never an action claim - do not flag those.

Reply with ONLY a single JSON object:
  {"claims": [{"type": "<claim type>", "quote": "<exact phrase from the draft>", \
"substantiated": true|false}]}
If there are no action claims, reply {"claims": []}.
"""

SAFE_FALLBACK_TEXT = (
    "I'm not able to confirm that this specific action was completed. Please "
    "contact our team directly so a person can help with this request."
)

CORRECTION_INSTRUCTION_TEMPLATE = """\
Your previous draft made these claim(s) that are not supported by the \
actual tool results from this turn:

{violations}

Rewrite your reply WITHOUT making any of these claims. If you were unable \
to fully complete part of the customer's request, say so honestly instead \
of claiming it was done. Do not mention this correction instruction \
itself - just give the corrected reply.
"""


def build_evidence_summary(state: GraphState) -> dict[str, Any]:
    """A compact, structured summary of what actually happened this turn -
    the only evidence a claim can be substantiated against. Deliberately
    omits full tool-result payloads: not needed to judge whether an
    *action* was really taken, and keeps the verifier call cheap."""

    def _tool_summary(result: Any) -> list[dict[str, Any]] | None:
        if result is None:
            return None
        return [
            {"tool": call.get("tool"), "succeeded": "error" not in call}
            for call in result.get("tool_calls", [])
        ]

    return {
        "sales_tool_calls": _tool_summary(state.get("sales_result")),
        "account_tool_calls": _tool_summary(state.get("account_result")),
        "escalation_result": state.get("escalate_result"),
    }


def verify_action_claims(
    llm: LLMProvider, model: str, draft_text: str, evidence: dict[str, Any]
) -> list[ActionClaim] | None:
    """Return the claims found in `draft_text`, or None if the verifier's
    own response couldn't be parsed (treated as fail-closed by the
    caller - never treated as "no claims found")."""
    messages = [
        Message(role="system", content=VERIFIER_SYSTEM_PROMPT),
        Message(
            role="user",
            content=(
                f"Draft reply:\n{draft_text}\n\n"
                f"Available tool evidence:\n{json.dumps(evidence, default=str)}"
            ),
        ),
    ]
    raw = llm.complete(messages, model=model)
    try:
        parsed = json.loads(raw)
        claims = parsed["claims"]
        if not isinstance(claims, list):
            raise ValueError("claims is not a list")
        return claims
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        logger.warning("action_claim_verifier_unparseable", raw=raw[:200])
        return None


def format_violations(claims: list[ActionClaim]) -> str:
    return "\n".join(f'- [{c["type"]}] "{c["quote"]}"' for c in claims)


class VerificationOutcome(TypedDict):
    """The stable, single entry-point result shape both nodes.py and the
    eval harness (evals/run_action_claim_eval.py) consume - kept stable
    across Step 8's Part B rewrite so the same eval harness measures
    "before" and "after" without needing to change itself.
    """

    label: Literal["VIOLATION", "CLEAN"]
    claims: list[ActionClaim]
    skipped_precheck: bool
    stage1_detected: bool | None
    unparseable: bool


def check_draft(
    llm: LLMProvider, model: str, draft_text: str, evidence: dict[str, Any]
) -> VerificationOutcome:
    """Single-stage baseline (Step 7): every draft goes straight to the
    substantiation classifier. Superseded by Step 8's two-stage version -
    kept here only as the pre-Step-8 baseline this module's docstring and
    the Step 8 status report cite; call sites should use the two-stage
    `check_draft` once Part B lands (this function is replaced in place,
    not kept side by side, to avoid two diverging implementations).
    """
    claims = verify_action_claims(llm, model, draft_text, evidence)
    if claims is None:
        return {
            "label": "VIOLATION",
            "claims": [],
            "skipped_precheck": False,
            "stage1_detected": None,
            "unparseable": True,
        }
    unsubstantiated = [c for c in claims if not c.get("substantiated")]
    return {
        "label": "VIOLATION" if unsubstantiated else "CLEAN",
        "claims": unsubstantiated,
        "skipped_precheck": False,
        "stage1_detected": None,
        "unparseable": False,
    }
