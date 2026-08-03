# ADR 0005: Post-generation verification of action claims

## Status

Accepted.

## Context

Step 6's live smoke test (against real Groq, real Postgres, the real MCP
transport) surfaced a customer-facing failure: after the account agent
failed three attempts to resolve a bare "Where is my order?" (guessing
empty/missing `order_ref` values), its own tool-calling loop emitted a
"final" answer claiming *"this conversation will now be handed off to a
human agent who will follow up shortly."* No `escalate_to_human` call had
been made. No `escalations` row existed. Nobody was following up. The
customer was told a specific, verifiable thing had happened, and it had
not.

This is a different class of bug from the ones Step 6 also found and
fixed (an uncaught LLM-provider exception, an unauthenticated escalation
attempt) - those crashed the process; this one produced a confident,
plausible-sounding, *wrong* answer that would sail straight through any
test that only checks "did the turn complete without an exception."
Tightening the account agent's system prompt (`prompts.py`) to say "never
claim a handoff you didn't perform" would not fix this reliably: the
model already had an instruction not to fabricate results in general
(`SYNTHESIS_SYSTEM_PROMPT`'s "do not invent ... that are not present in
them"), under the exact conditions (three failed tool calls, no other way
to end the conversation) that made fabrication look like the path of
least resistance. A model that will not reliably follow an instruction
once, under pressure, cannot be trusted to police the same instruction a
second time in the same generation just because the wording is more
specific.

## Decision

**A second, independent LLM call - the cheap classifier model, not the
model that wrote the reply - reviews synthesis's drafted text against
the turn's actual tool-call evidence, specifically for ACTION CLAIMS.**

`agents/action_claims.py` defines five claim types: `human_handoff`,
`order_cancelled`, `booking_made`, `refund_issued`, `other_state_change`
(a catch-all for "some record was created/changed/removed"). Everything
else - describing a vehicle, a policy, an order's current status, a
price, or asking a clarifying question - is explicitly out of scope and
must not be flagged.

`agents/nodes.py::make_verify_claims_node` runs after `synthesis` in the
graph (`synthesis -> verify_claims -> END`):

1. Call the verifier with the drafted text and a compact evidence summary
   (`build_evidence_summary`: which tools were called and whether each
   succeeded, plus the escalation result if any - not full tool-result
   payloads, since only "did this action really happen" needs to be
   judged, not the full contents of a search).
2. If every claim found is substantiated (or there are no claims at all),
   pass the draft through unchanged.
3. If any claim is unsubstantiated, log the violation (claim, draft text,
   evidence) and regenerate synthesis **once**, with an explicit
   correction naming the specific unsubstantiated claim(s).
4. Re-verify the regenerated draft. If it is now clean, use it
   (`degradation_reasons` gets `"action_claim_corrected"`). If it still
   has an unsubstantiated claim, replace the reply entirely with a fixed,
   deterministic fallback string (`SAFE_FALLBACK_TEXT`) that makes no
   claims of its own (`"action_claim_replaced"`).
5. If the verifier's *own* output can't be parsed, the draft is treated
   as unverifiable and pushed through the same correct-then-fallback
   path - fail **closed**, the same philosophy CLAUDE.md already applies
   to identity: an occasional unnecessary regeneration is a far smaller
   cost than a false promise reaching a customer.

`clarify`'s response is a hardcoded, deterministic string (never
LLM-generated free text), so it is wired directly to `END` and skips
verification entirely - there is nothing there to check.

### Why an LLM classifier, not regex

The claim types above are expressed in open-ended natural language: "I've
escalated this," "connecting you with someone," "a team member will
reach out," "your refund is on its way," "your order has been
cancelled," and any paraphrase or combination of those. A keyword or
regex list would need continuous expansion as new phrasings appear, would
still miss paraphrases regex authors didn't anticipate, and - critically
- cannot cross-reference "is this claim actually true" against
structured evidence the way a model given both the text and the evidence
can. A short classification call with the cheap model, given the actual
tool-call evidence, generalizes across phrasing at low cost and latency,
and it is the same architecture the router and tool-loop decisions
already use (CLAUDE.md's per-node model routing - cheap model for
classification/guardrails).

## Rejected alternatives

- **Prompt-only fix** (strengthen `ACCOUNT_AGENT_SYSTEM_PROMPT` /
  `SYNTHESIS_SYSTEM_PROMPT` wording alone). Rejected as the sole fix: this
  is exactly the failure mode observed live - an existing, reasonable
  instruction not to fabricate was already in place and was not followed
  under pressure. Prompt wording was still tightened (`prompts.py` now
  explicitly tells the account agent it has no cancel/refund/booking
  tools and must not claim those actions), but as a second layer, not a
  replacement for structural verification.
- **Regex/keyword matching** on the drafted text. Rejected per the
  justification above - open-ended phrasing and no way to check
  substantiation against evidence without also understanding the text's
  meaning.
- **Have the model self-report its own claims** as a structured field
  alongside the natural-language reply (e.g. `{"answer": "...", "claims":
  [...]}` from the same synthesis call). Rejected because it relies on
  the same model, in the same generation, to honestly flag its own
  fabrication - which is precisely the trust Step 6 showed is misplaced
  under pressure. An independent second pass, by construction, does not
  share that blind spot.
- **Regenerate until clean, uncapped**, instead of a single correction
  attempt before falling back. Rejected: an uncapped retry loop here
  would reintroduce the same "silent, unbounded work" problem Part B
  fixes for the tool loop, and for a different reason - a model that
  fabricates once under pressure may do so again. One correction attempt
  plus a deterministic, always-true fallback bounds the cost and
  guarantees termination without ever risking a second fabrication
  reaching the customer.

## Consequences

- Every synthesized reply costs at least one extra cheap-model call
  (the verifier), and a violation costs three more (regenerate, recheck,
  and the logging/bookkeeping around them). Measured in Part D's smoke
  test transcripts. This is judged acceptable: correctness of what the
  customer is told outweighs the marginal latency/cost of a small,
  cheap-model classification call.
- `degraded`/`degradation_reasons` (Part C) now also carries
  `action_claim_corrected` / `action_claim_replaced` as first-class
  reasons, so monitoring can alert specifically on "the model tried to
  fabricate an action" separately from "a tool failed" or "the loop
  capped" - these are different failure modes worth distinguishing.
- The safe fallback text is deliberately generic ("contact our team
  directly") rather than attempting to be helpful about *what* went
  wrong, because any more specific fallback text risks making its own
  unverifiable claim. This is a real UX cost, accepted deliberately: a
  vague-but-true answer beats a specific-but-false one.
- This only catches claims of *completed or certain* actions. A model
  saying "I can try to escalate this for you" (an offer, not a claim) is
  correctly out of scope and unaffected - matching the taxonomy's intent.
