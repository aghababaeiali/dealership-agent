# Policy Document Coverage

This documents what the Northgate Motors policy corpus (`data/policies/`)
does and does not answer, so we know in advance which questions
`search_policy_docs` should return low-similarity results for rather than
a confident (wrong) match, and so the agent knows when to say "I don't
know" or escalate instead of guessing from a loosely-related chunk.

## Documents in the corpus

- `warranty.md` - standard and CPO warranty coverage, claims, exclusions
- `returns.md` - the current 5-day/250-mile Buy Back Guarantee (effective
  2026-06-01)
- `returns-2024-superseded.md` - the prior 7-day/500-mile version
  (effective 2024-01-15), retained for historical reference only,
  contradicts `returns.md` on the return window by design
- `financing.md` - partner-lender financing, credit tiers, down payments,
  rescission rights
- `trade-in.md` - appraisals, offer validity, negative equity, vehicles we
  can't accept
- `delivery.md` - pickup, local/long-distance delivery, delivery
  guarantees
- `service-and-maintenance.md` - included services, rates, loaners,
  recalls
- `fees-and-taxes.md` - doc fee, title/registration, sales tax, optional
  add-ons
- `test-drive.md` - eligibility, duration, accompaniment, damage liability

## Known unanswerable questions (the refusal test set)

These are questions a real customer would plausibly ask that **no
document in this corpus addresses**, verified by rereading all 8 current
documents. They are not edge cases of an existing policy - the topic
itself is simply never discussed. This is the deliberate gap set used in
`tests/integration/test_policy_search.py` to confirm the system returns
low-similarity results (and the agent should say "I don't know" or
escalate) rather than confidently matching an unrelated chunk.

1. **"Do you offer any discounts for military members, first responders,
   veterans, or students?"** - No document mentions any discount program,
   promotional pricing, or eligibility-based price reduction of any kind.
   `fees-and-taxes.md` covers what's charged, never what's discounted.
   Top retrieval similarity: 0.279.

2. **"Do you price-match a lower price I found for the same vehicle at
   another dealership?"** - No document mentions price matching,
   competitor pricing, or any process for reconciling a lower advertised
   price found elsewhere. `fees-and-taxes.md` states there are no hidden
   markups but is silent on matching a competitor's price. Top retrieval
   similarity: 0.401.

3. **"Do you offer any loyalty program or referral bonus for referring
   friends?"** - No document mentions repeat-customer perks, loyalty
   points, or referral incentives anywhere in the corpus. Top retrieval
   similarity: 0.258.

4. **"Do you accept cryptocurrency as a form of payment?"** -
   `financing.md` and `fees-and-taxes.md` describe cash, card, check, and
   financed payment methods; cryptocurrency is never mentioned as an
   accepted or rejected payment form. Top retrieval similarity: 0.264.

Two other plausible candidates were tested and **rejected** for this set
because they scored too close to a genuine match to make a clean test
case - a reminder that lexical overlap can produce a deceptively
confident similarity score even when the underlying question truly isn't
answered:

- *"Can I finance through my own outside bank or credit union instead of
  one of your partner lenders?"* scored 0.529 against `financing.md`'s
  Overview section, purely because both share heavy financing/lender
  vocabulary - despite the document never actually addressing outside
  financing.
- *"What if I need to return the vehicle because I'm relocating out of
  state, outside the normal return window?"* scored 0.527 against
  `returns.md`'s return-window section for the same reason.

## Why these matter for retrieval design

Because `search_policy_docs` (Part B4) ranks purely by embedding
similarity to real chunk content, a question with genuinely no matching
content should score low across the board - there is no chunk about
"military discount" for the query to land near. If the system instead
returns a confident top match (e.g. the financing APR table, because it
shares vocabulary like "approval" or "credit"), that's a sign the
similarity threshold or ranking logic needs tightening, not that the
question has secretly been answered.
