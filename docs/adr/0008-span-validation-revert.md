# ADR 0008: Span-validation refinement to the two-stage verifier, measured and reverted

## Status

**Reverted.** Tried, measured against the labelled eval set, found worse
than the design it would have replaced, and rolled back. Kept as a record
of what was tried and why it didn't ship, per this project's own rule:
if a change measures worse, revert it and say so, rather than quietly
drop the attempt from the record.

## Context

ADR 0006 shipped a two-stage verifier: stage 1 detects whether a draft
asserts an action claim, stage 2 checks whether that claim is
substantiated by the turn's real tool evidence. A plausible-looking
further refinement: validate the *specific span* of text a verifier
flags as the claim in isolation, and discard findings that don't hold up
once re-checked out of context - the idea being that re-examining just
the flagged span, rather than the whole draft, would sharpen precision
further.

## Decision (attempted)

Add a span-validation step after stage 2: re-check the isolated span the
verifier flagged as a claim, on its own, and discard the finding if that
isolated re-check doesn't confirm it.

## Measured results (`evals/run_action_claim_eval.py`, same 71-case set)

| Verifier design | VIOLATION P / R / F1 | CLEAN P / R / F1 | Accuracy |
|---|---|---|---|
| Two-stage (ADR 0006, shipped) | 0.826 / 0.864 / 0.844 | 0.938 / 0.918 / 0.928 | 0.901 |
| Two-stage + span-validation | 0.933 / 0.636 / 0.757 | 0.857 / 0.980 / 0.914 | 0.873 |

Net accuracy dropped from 0.901 to 0.873. VIOLATION precision improved
(0.826 to 0.933), but VIOLATION recall dropped sharply (0.864 to 0.636)
and CLEAN precision dropped from 0.938 to 0.857 - the change traded away
more than it gained.

## Why it measured worse

Re-checking a claim's span in isolation lost the surrounding context that
made the claim identifiable as a claim in the first place. Passive-voice
phrasing such as "Your request has been escalated to our team" reads as
a neutral statement once isolated from the rest of the draft, so the
isolated-span re-check started waving through genuine violations that
the full-draft stage 2 check had correctly caught. The refinement made
the narrow, second-order case (validating an already-flagged span) worse
at the exact judgment the two-stage design already handled correctly
using full-draft context.

## Decision

**Reverted.** The two-stage design from ADR 0006 (without span
validation) is what's shipped. Per this project's explicit rule -
measure before and after a change, and if it measures worse, revert it
and say so rather than hide it - this result is recorded here and in the
[README's Evaluation section](../../README.md#evaluation) rather than
omitted.

## Consequences

- No code from this attempt is in the shipped path; `check_draft` still
  ends at stage 2, per ADR 0006.
- This is direct, positive evidence that the eval set is doing its job:
  a change that looked like a plausible improvement was caught measuring
  worse before it shipped, not after.
- The residual stage-2 misattribution this refinement was trying to fix
  remains a real, disclosed limitation (see README's Known Limitations) -
  reverting this attempt did not solve the underlying problem, it only
  avoided making it worse in a different way.
