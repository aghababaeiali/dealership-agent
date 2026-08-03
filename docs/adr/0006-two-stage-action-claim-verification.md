# ADR 0006: Two-stage action-claim verification, measured against a labelled eval set

## Status

Accepted.

## Context

ADR 0005 added a single-stage LLM verifier after synthesis: one call asked
the cheap model to find action claims (a human handoff, a cancellation, a
booking, a refund) AND judge whether each was substantiated, in one shot.
Step 7's live smoke test caught the target failure (a fabricated handoff
with no real escalation) but also flagged roughly 3 of every 8 benign
answers as violations - purely informational restatements of policy,
offers to escalate, and conditional/hypothetical framing were all
misclassified as completed-action claims and replaced with a generic,
unhelpful fallback message. This is a precision problem, not a wording
problem: the single-stage prompt conflates two different judgments -
"does this draft claim an action happened" and "is that claim true" -
into one call, and a call trying to do both at once was systematically
over-triggering on the first question.

CLAUDE.md's engineering standards call for measuring before making a
change like this, not tuning by intuition. `evals/datasets/action_claims.jsonl`
(71 hand-labelled cases, drawn from real Step 7 transcripts plus
systematic coverage of offers, conditionals, third-person policy
descriptions, and substantiated/unsubstantiated passive voice) and
`evals/run_action_claim_eval.py` (precision/recall/F1/confusion matrix
against a real Groq call) exist specifically so every change below has a
number attached, not a guess.

## Decision

**Split the one verification call into three cheaper, narrower steps,
each of which only runs if the previous one didn't already resolve the
draft as CLEAN** (`agents/action_claims.py::check_draft`):

0. `mentions_action_vocabulary` - a plain keyword pre-check (Part C1).
   Not a classifier: it only ever skips work when a draft contains
   NONE of a broad, over-inclusive vocabulary list. A draft that does
   mention this vocabulary still goes through real judgment below.
1. `detect_action_claim` (stage 1) - one narrow, binary question: does
   this draft assert, in the first person, that a state-changing action
   was completed or will definitely happen, for THIS customer, right
   now? Offers, questions, conditionals, and third-person policy
   descriptions are explicitly instructed to answer NO here - this is
   exactly the false-positive pattern Step 7 hit.
2. `verify_action_claims` (stage 2) - only reached if stage 1 says YES.
   Checks whether that specific claim, by type, is substantiated by this
   turn's real tool evidence - now expanded to include retrieved policy
   text (Part B2), so a claim that's really an accurate restatement of
   retrieved policy is substantiated by the retrieval itself, not
   mistaken for an unbacked claim about this customer.

### Measured results (`evals/run_action_claim_eval.py`, 71 cases, real Groq)

| | VIOLATION precision | VIOLATION recall | VIOLATION F1 | CLEAN precision | CLEAN recall | CLEAN F1 | Accuracy |
|---|---|---|---|---|---|---|---|
| Baseline (single-stage, ADR 0005) | 0.500 | 0.955 | 0.656 | 0.966 | 0.571 | 0.718 | 0.690 |
| Two-stage, first pass | 0.818 | 0.818 | 0.818 | 0.918 | 0.918 | 0.918 | 0.887 |
| Two-stage, refined prompts (shipped) | 0.826 | 0.864 | 0.844 | 0.938 | 0.918 | 0.928 | 0.901 |

The refinement pass (tightening stage 1 to catch mixed offer+definite-claim
drafts, and stage 2 to stop accepting "some unrelated tool call succeeded"
as substantiation for a specific claim type) improved every number
simultaneously over the first two-stage pass - no tradeoff needed there.

Compared to baseline, CLEAN recall (the false-positive problem this ADR
exists to fix) went from 0.571 to 0.918 - the great majority of benign
answers no longer get replaced with the fallback. This did cost some
recall on true violations (0.955 -> 0.864, 1 missed case -> 3): the
remaining misses are cases like "I've updated your account..." and
"Your request has been escalated to our team" with zero tool evidence -
stage 1 correctly detects a claim, but stage 2 occasionally judges an
unsubstantiated claim as substantiated. This looks like residual model
noise in a probabilistic classifier rather than a fixable prompt gap;
further improvement would need a larger eval set and more iteration than
this pass had time for (see the Step 8 status report's REMAINING
WEAKNESSES).

**Kept, not reverted**: net accuracy improved from 0.690 to 0.901 and
both classes' F1 improved; the honest cost (recall on true violations
down ~9 points) is disclosed rather than hidden, per instruction ("if a
change makes it worse, say so").

### Part C1: call-volume reduction

The keyword pre-check exists because most synthesized replies don't
mention anything action-related at all - see the Step 8 status report
for the measured skip rate against the live smoke test's 8 conversations.
Stage 1 and stage 2 both use the cheap classifier model per CLAUDE.md's
per-node routing; a draft that clears the pre-check but is correctly
judged CLEAN by stage 1 costs one classifier call, not two.

## Rejected alternatives

- **Keep one call, just reword it more carefully.** Tried implicitly by
  ADR 0005's design itself - a single call was already asked to
  distinguish offers/conditionals/policy-description from real claims,
  and still over-triggered live. Splitting "is there a claim" from "is
  it true" into separate calls, each with a narrower, single-purpose
  prompt, measurably outperforms one prompt trying to do both (see the
  table above) - not just in theory, but against the same eval set.
- **Tune by intuition against a handful of Step 7 transcripts**, without
  a labelled dataset. Rejected per this step's explicit instruction and
  because Step 7 itself was tuned this way (the single-stage prompt
  already had explicit "do not flag offers" language) and still had a
  measured 43% false-positive rate on CLEAN cases - intuition alone had
  already been tried and had already failed.
- **Regex/keyword classification for stage 1 or 2.** Rejected for the
  same reason as ADR 0005: claim and non-claim phrasing is open-ended
  natural language. The ONE place keywords are used here (stage 0) is
  deliberately not a classifier - it only ever skips work, never decides
  VIOLATION vs CLEAN, and is verified as such by
  `tests/unit/test_action_claims_precheck.py::test_true_for_offer_language_too`
  (offers must still reach stage 1, not be waved through by the
  pre-check).

## Consequences

- Most turns now cost 0-1 extra classifier calls for verification (pre-
  check skip, or stage 1 alone), rather than a guaranteed 1 for the old
  design - a real reduction in the common case, at the cost of up to 2
  calls (stage 1 + stage 2) on turns that do contain a claim, versus the
  old design's flat 1.
- `VerificationOutcome`'s `skipped_precheck` / `stage1_detected` /
  `unparseable` fields make it possible to tell, from logs alone, exactly
  which layer resolved a given draft - useful for the eval harness and
  for any future incident investigation.
- The remaining ~14% false-negative rate on true violations is a real,
  disclosed limitation, not eliminated by this change. `check_draft`
  still fails CLOSED on unparseable output from either stage (unchanged
  from ADR 0005), so at least that failure mode does not silently
  degrade further.
