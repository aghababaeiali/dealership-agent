# ADR 0007: Bedrock provider and per-provider model routing

## Status

Accepted.

## Context

CLAUDE.md specifies an `LLMProvider` interface with two implementations -
Groq for local dev, AWS Bedrock for prod - and per-node model routing: a
cheap model for classification/guardrails, a stronger model for final
synthesis. Through Step 8, only `GroqProvider` was implemented;
`BedrockProvider` was an intentional stub raising `NotImplementedError`.
Settings already had two separate pairs of model-name fields
(`LLM_MODEL_CLASSIFIER`/`LLM_MODEL_SYNTHESIS` and
`BEDROCK_MODEL_CLASSIFIER`/`BEDROCK_MODEL_SYNTHESIS`), but
`agents/nodes.py` only ever read the first pair - the Bedrock-specific
fields were dead config with no consumer. Switching `LLM_PROVIDER` from
`groq` to `bedrock` would silently keep using Groq's model names against
the Bedrock API.

## Decision

**BedrockProvider, implemented against the Converse API:**

- `llm/bedrock_provider.py::BedrockProvider` wraps a `boto3
  bedrock-runtime` client. The region comes from `Settings.aws_region`
  (default `eu-west-1`, per this step's explicit instruction). Credentials
  come from the standard boto3 credential chain (env vars, shared
  config/credentials file, or an ECS task role in prod) - never
  hardcoded, never read from `Settings`.
- `complete()` mirrors `GroqProvider.complete()` exactly: plain
  messages in, plain text out. `Message`'s `system` role entries are
  extracted into Converse's separate `system` parameter (Converse does
  not accept a `system` role inline in `messages`); everything else maps
  directly to Converse's `role`/`content` shape.
- No `toolConfig` is passed, even though the Converse API supports native
  tool use. dealership-agent's own tool-calling loop
  (`agents/tool_loop.py`) is a structured-JSON protocol carried *inside*
  the completion text - the system prompt instructs the model to emit
  `{"action": "call_tool", ...}` / `{"action": "final", ...}` as plain
  text, which the loop then parses itself. Both providers therefore speak
  the identical plain-text `LLMProvider.complete` contract; using
  Converse's native tool use would require either bypassing the existing
  loop for Bedrock only (a second, divergent tool-calling code path) or
  redesigning the loop around structured tool-call responses for every
  provider - out of scope for this step, and unnecessary given the
  existing design already works uniformly across providers.

**Per-provider model routing, made real:**

- `Settings.model_classifier` / `Settings.model_synthesis` (properties,
  `config.py`) resolve to the Bedrock-specific fields when
  `llm_provider == "bedrock"`, and to the Groq/default fields otherwise.
  `agents/nodes.py`'s six call sites (router, both sub-agent tool loops,
  synthesis, and both action-claim verifier stages) were updated to call
  these properties instead of reading `llm_model_classifier`/
  `llm_model_synthesis` directly - this is the fix for the dead-config gap
  described above.
- **Reasoning for the cheap/strong split** (unchanged in principle from
  the Groq setup, now enforced for Bedrock too): the classifier model
  handles routing, tool-loop decisions, and both action-claim verifier
  stages - high call volume per turn (every routing decision and every
  tool-loop iteration is a call), low reasoning depth per call (classify
  into a fixed small set of outcomes, or judge one draft against
  evidence). The synthesis model handles exactly one call per turn - the
  customer-facing reply - where response quality is what the customer
  actually sees and is worth paying for. Recommended Bedrock model
  classes: a Haiku-class Claude model for `BEDROCK_MODEL_CLASSIFIER`, a
  Sonnet-class Claude model for `BEDROCK_MODEL_SYNTHESIS`. The exact
  Bedrock model ID string (e.g. `anthropic.claude-haiku-...-v1:0`) is
  left for whoever deploys to confirm against the live Bedrock model
  catalog for the target region and account at deployment time, rather
  than hardcoded here - Bedrock model availability varies by region and
  the catalog changes over time, so a value baked into this ADR would
  likely go stale. This is not a theoretical concern: while wiring this
  up, `eu.anthropic.claude-sonnet-5` appeared in
  `list-foundation-models`/`list-inference-profiles` for this account but
  returned `AccessDeniedException` on an actual `Converse` call - a model
  being *listed* does not mean it is *granted*. `.env`'s
  `BEDROCK_MODEL_SYNTHESIS` currently points at
  `eu.anthropic.claude-sonnet-4-6`, the strongest Sonnet-tier model
  verified reachable with a real call on this account at the time of
  writing - confirm both listing *and* invoke access for whichever model
  a real deployment intends to use.
  - Newer Claude models on Bedrock require a region-prefixed **inference
    profile ID** (e.g. `eu.anthropic.claude-haiku-4-5-20251001-v1:0`), not
    the bare model ID - the bare ID fails with `ValidationException:
    ... on-demand throughput isn't supported`. List available profiles
    with `aws bedrock list-inference-profiles --region <region>`.

## Rejected alternatives

- **Giving `BedrockProvider` a different, tool-use-aware interface than
  `GroqProvider`.** Rejected: it would break the single `LLMProvider`
  abstraction `agents/nodes.py` and `agents/tool_loop.py` are written
  against, requiring provider-specific branches throughout the agent
  code - exactly the kind of divergence the interface exists to prevent.
  It would also make the cross-provider contract test
  (`tests/integration/test_llm_provider_contract.py`) meaningless, since
  there would be nothing left to normalize identically.
- **A single model-name pair with a provider-prefixed model string
  (e.g. `LLM_MODEL_CLASSIFIER=bedrock:anthropic.claude-...`) instead of
  separate settings fields.** Rejected as unnecessary indirection - two
  plain settings fields per provider is simpler to read, and Settings
  already had the Bedrock-specific fields defined; they just weren't
  wired up.

## Consequences

- Switching `LLM_PROVIDER` from `groq` to `bedrock` now requires setting
  `BEDROCK_MODEL_CLASSIFIER`/`BEDROCK_MODEL_SYNTHESIS` (previously it
  would have silently kept sending Groq model-name strings to the
  Bedrock API, which would fail loudly - a real bug, now fixed rather
  than merely characterized).
- boto3 is a new runtime dependency (plus `boto3-stubs[bedrock-runtime]`
  for mypy strict). Neither Terraform nor IAM policy work is in scope for
  this step - only application-level credential *usage* (the standard
  chain), not provisioning.

## Update (Step 10): response normalization closed the live-transcript gap

The Step 9 status report disclosed a live finding: a real Bedrock
conversation fell back to `clarify` because Claude wrapped the router's
JSON in a ` ```json ` fence that `nodes.py`'s `json.loads()` couldn't
parse. Step 10, Part A fixed this properly, in the provider layer rather
than in `nodes.py` - see `llm/base.py::normalize_llm_response()`, called
by both `GroqProvider.complete()` and `BedrockProvider.complete()` before
returning. Re-testing surfaced two *further* Claude-specific quirks
beyond the originally-reported fence-wrapping, both now handled by the
same function:

- Claude sometimes falls back to its own tool-call convention even with
  no native `toolConfig` offered - wrapping the intended single object in
  a `<function_calls>[...]</function_calls>` tag, with the object itself
  wrapped in a one-element JSON array.
- Claude sometimes drops this codebase's `"action"` discriminator key
  entirely, replying with just `{"tool": ..., "arguments": ...}` (its own
  native shape) or `{"answer": ...}` - both are unambiguous to infer from
  the codebase's own fixed set of JSON schemas (no other node's schema
  uses either key combination), so the discriminator is added back
  rather than left for the caller to fail on.

A live conversation re-run against real Bedrock after this fix reached
the sales sub-agent, called `search_listings` for real, and returned ten
concrete vehicles - not a `clarify` fallback. See the Step 10 status
report for the full transcript, token counts, and cost.
