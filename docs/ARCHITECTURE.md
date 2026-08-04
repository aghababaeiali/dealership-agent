# Architecture

This is the longer technical write-up. For the locked decisions and the
non-negotiable rules this project is built to, see [CLAUDE.md](../CLAUDE.md):
that file is the constitution; this document explains how the pieces
implementing it actually fit together, and links every ADR that recorded
a specific decision along the way.

## System shape

One FastAPI service (`src/dealership_agent/api/app.py`). No
microservices, no message queue, no separate auth service, no Kubernetes:
CLAUDE.md's anti-over-engineering rules rule all of those out
explicitly. Everything below runs inside that one process, except the
MCP tool server, which is a subprocess spawned fresh per conversation
turn (more on why below).

## Why these choices, not the alternatives

The decisions above are stated as facts in CLAUDE.md; this section is
the reasoning a reviewer would otherwise have to ask about.

- **Why LangGraph, not a hand-rolled state machine or a framework like
  CrewAI/AutoGen.** The supervisor's control flow is genuinely a graph,
  not a linear pipeline: a single message can fan out to both sub-agents
  concurrently, and the router/merge/escalate/synthesis/verify_claims
  structure needs explicit conditional edges, not just a sequence of
  function calls. LangGraph gives that graph structure, concurrent
  fan-out, and typed state (`agents/state.py`) directly, without
  building a bespoke scheduler for a project this size. It is also the
  orchestration layer CLAUDE.md's Architecture Decisions specify, so
  swapping it for a different framework would be a locked-decision
  change, not a documentation fix.
- **Why pgvector, not a separate vector store (Pinecone, Weaviate,
  Qdrant).** This project has one modest-volume catalog (tens of
  thousands of vehicles) and one small policy-document corpus, both of
  which fit comfortably in Postgres alongside the transactional data
  Row-Level Security already protects. A separate vector store would
  mean a second system to run, secure, and keep consistent with the
  data in Postgres, for a scale where pgvector's IVFFlat/HNSW indexing
  already performs well. CLAUDE.md's anti-over-engineering rules rule
  out the added operational surface for a benefit this project's scale
  doesn't need.
- **Why no Kubernetes.** A single FastAPI service plus one MCP
  subprocess per turn does not need a scheduler, service mesh, or
  multi-node orchestration; ECS Fargate gives container orchestration
  (restarts, health-check-driven replacement, rolling deploys) without
  a control plane to operate. Kubernetes' benefits (multi-service
  scheduling, complex rollout strategies, horizontal scaling across
  many independent services) address problems this modular monolith
  does not have.
- **Why no NAT Gateway.** Fargate tasks run in public subnets with a
  public IP assigned directly to the ENI, reachable outbound via the
  Internet Gateway with no NAT device in between (`infra/terraform/vpc.tf`).
  This is safe because the task's own security group only accepts
  inbound traffic from the ALB's security group; a public IP on the ENI
  does not make the task reachable from the internet outside that rule.
  A NAT Gateway would cost roughly $33/month fixed plus per-GB data
  processing, for a benefit (hiding the task's outbound IP) that does
  not matter at this project's scale, since RDS itself stays in a
  private subnet with no route to the internet at all, needing no NAT
  either (it only ever accepts inbound connections from within the VPC).
- **Why SSM Parameter Store, not Secrets Manager.** Parameter Store's
  Standard tier is free for any number of parameters; Secrets Manager
  charges roughly $0.40/secret/month plus API call charges, for
  automatic-rotation features this project's low-rotation-frequency
  values (a handful of database and API credentials, rotated manually
  if ever) don't use either way. For a cost-conscious deployment, that
  ongoing per-secret charge buys nothing this project needs (see
  `infra/terraform/ssm.tf`).
- **Why Langfuse v2, not v3.** See [ADR 0001](adr/0001-langfuse-v2.md):
  v3's self-hosted deployment needs ClickHouse, Redis, and object
  storage in addition to Postgres, versus v2's Postgres-only footprint.
- **Why self-hosted sentence-transformers, not a cloud embedding API.**
  Embeddings must be byte-for-byte identical between local development
  and production, since a query embedded with one model and vectors
  stored with a different model would silently degrade similarity
  search with no error to signal it. A self-hosted model pinned to one
  version guarantees that identity without depending on a cloud
  provider's model version staying fixed over time, and it avoids a
  per-call API cost and an external network dependency for every search
  and every conversation turn (`retrieval/embedder.py`).

## Request lifecycle

1. **Edge (`api/app.py`, `api/auth.py`).** `POST /chat` requires a bearer
   JWT. `api/auth.py::verify_token` checks the RS256 signature against a
   public key, plus expiry, issuer, and audience, using
   [`python-jose`](https://github.com/mpdavis/python-jose). A successful
   verification produces a `RequestIdentity` (session id + customer id).
   This is the *only* place identity is established for the whole
   request; nothing downstream ever re-derives or re-checks it from the
   request body.
2. **Rate limiting (`api/rate_limit.py`).** An in-process fixed-window
   counter, keyed per customer plus one global window. Deliberately not
   Redis or any external service: a single process's own memory is
   sufficient state at this project's scale, and CLAUDE.md's
   anti-over-engineering rules point the same way.
3. **Conversation persistence (`api/conversations.py`).** If the request
   names a `conversation_id`, prior turns are loaded, scoped by the same
   RLS mechanism as every other customer table, via
   `db/rls.py::customer_scope`. A request for someone else's conversation
   id returns 404, indistinguishable from "doesn't exist": RLS makes
   those two cases genuinely the same at the data layer, so the API
   doesn't have to fake the distinction.
4. **The graph (`agents/runner.py::run_turn`).** Owns one MCP session's
   lifecycle for the whole turn (see below).
5. **Response.** `answer`, `degraded`, `degradation_reasons`,
   `tool_calls_made` (names only, never arguments), `conversation_id`,
   `latency_ms`.

## The supervisor graph (`agents/graph.py`, `agents/nodes.py`)

```
intake -> router -> fan-out to {sales_agent, account_agent} -> merge -> (escalate | synthesis) -> verify_claims -> END
router -> clarify -> END
router -> escalate -> synthesis -> verify_claims -> END
```

- **intake** decides whether the message is too thin to route at all
  (empty/near-empty) before spending an LLM call on it.
- **router** (cheap model) classifies the message into one or more of
  `sales`, `account`, `clarify`, `escalate`. A single message can need
  both sub-agents at once ("find me a cheap SUV and tell me if my order
  shipped"): the router can return `routes: ["sales", "account"]`, and
  LangGraph runs both nodes concurrently.
- **sales_agent / account_agent** each run a bounded tool-calling loop
  (`agents/tool_loop.py`, capped at `MAX_ITERATIONS = 5`) against their
  own disjoint tool set, see [Security boundary](#security-boundary-and-mcp)
  below. Three distinct non-answer terminal states are tracked
  separately (`hit_cap`, `llm_call_failed`, `hit_budget_guard`) so
  downstream nodes can be honest about *which* kind of failure happened,
  never collapsed into one generic "it broke."
- **merge** is the unconditional convergence point after either or both
  sub-agents run, so the cap-hit/escalation check downstream always sees
  the full picture regardless of which branch(es) executed.
- **escalate** hands off to a human when either sub-agent's loop hit its
  cap, or the router itself detected an explicit escalation request.
- **synthesis** (stronger model) turns whatever sub-agent result(s) exist
  into one customer-facing reply, and is fed an explicit degradation note
  when anything upstream didn't go cleanly (a tool error, a capped loop,
  a failed LLM call), see `agents/degradation.py::compute_degradation`.
  It is instructed to say so honestly rather than paper over it.
- **verify_claims** (`agents/nodes.py::make_verify_claims_node`, backed by
  `agents/action_claims.py`) is an independent second pass over the
  drafted reply, see [Action-claim verification](#action-claim-verification)
  below. `clarify`'s response is a deterministic, hardcoded question,
  never LLM-generated free text, so it skips this pass entirely.

### Action-claim verification

The concrete risk this guards against: a synthesized reply asserting, in
the first person, that something happened for this customer right now (a
cancellation, a booking, a refund, a handoff to a human) when no real
tool result actually backs that claim. A three-layer pipeline
(`agents/action_claims.py::check_draft`), each layer only running if the
previous one didn't already resolve the draft as clean:

0. `mentions_action_vocabulary`: a cheap, deliberately over-inclusive
   keyword pre-check. It only ever *skips* work (drafts mentioning none
   of a broad vocabulary list never reach an LLM call at all); it never
   itself decides violation vs. clean.
1. `detect_action_claim` (stage 1): one narrow binary question: does
   this draft assert, in the first person, that a state-changing action
   was completed or will definitely happen? Offers, questions,
   conditionals, and third-person policy descriptions are explicitly
   instructed to answer no.
2. `verify_action_claims` (stage 2): only reached if stage 1 says yes.
   Checks whether that specific claim is substantiated by this turn's
   real tool evidence (including retrieved policy text, so an accurate
   restatement of policy isn't mistaken for an unbacked claim).

See [ADR 0005](adr/0005-action-claim-verification.md) for the original
single-stage design, [ADR 0006](adr/0006-two-stage-action-claim-verification.md)
for why it was split and the measured numbers behind that decision,
[ADR 0008](adr/0008-span-validation-revert.md) for a further refinement
that was tried, measured, and reverted, and the
[README's Evaluation section](../README.md#evaluation) for the headline
results.

Unparseable output from *either* stage is treated as a violation, not
waved through: modeled explicitly on the same fail-closed philosophy as
identity/RLS. An ambiguous verifier result should never silently degrade
into "assume it's fine."

## Security boundary and MCP

Tools are exposed through an MCP server (`tools/server.py`) so the agent
layer stays framework-independent, per CLAUDE.md. Two sub-agents, split
by tool-permission scope, enforced at construction
(`agents/tool_binding.py`):

| Sub-agent | Tools | Scope |
|---|---|---|
| Sales | `search_listings`, `search_policy_docs` | read-only, public data, no identity involved at all |
| Account | `get_order_status`, `list_my_orders`, `escalate_to_human` | customer-scoped |

**The split is itself the security boundary**: the Sales Agent must
never have order tools bound, full stop, and `tool_binding.py` enforces
this structurally rather than by convention.

One MCP session is one OS subprocess, spawned fresh per conversation turn
(`agents/mcp_session.py`), bound to the caller's identity for that
subprocess's entire lifetime via environment variables set once at
spawn, never per tool call, never as a `tools/call` argument. See
[ADR 0003](adr/0003-mcp-client-in-process-bridge.md) for the earlier,
superseded in-process bridge design and why it was replaced, and
[ADR 0004](adr/0004-mcp-identity-propagation.md) for the real-transport
design that replaced it, including the two alternatives it explicitly
rejected (a per-call `_meta` sideband, and a session-id-as-tool-argument
scheme) and why both would have reintroduced identity into the tool-call
path.

Every tool executes inside exactly one chokepoint,
`tools/scope.py::customer_scoped_connection()`: the only place a
database connection is opened for a tool call. There is no second code
path into Postgres from tool code.

## Data layer

PostgreSQL with pgvector. One database, no separate vector store (`db/`).
Every customer-scoped table (`customers`, `orders`,
`order_status_history`, `test_drive_bookings`, `escalations`,
`conversations`, `conversation_messages`) has Row-Level Security enabled
and `FORCE`d, comparing `customer_id` against
`current_setting('app.customer_id', true)`: absent-setting-yields-NULL
means an unscoped query returns zero rows, never every row. Tables
without their own `customer_id` column (`order_status_history`,
`conversation_messages`) are scoped via a subquery on their parent table.
Public catalog data (`vehicles`, `vehicle_embeddings`, `policy_chunks`) is
intentionally excluded from RLS: it isn't customer data, and CLAUDE.md's
invariant only governs what is.

`db/rls.py::customer_scope` sets `app.customer_id` with `SET LOCAL`
semantics inside a transaction (`set_config(..., true)`), so the setting
auto-clears on commit/rollback and can never leak into a later,
unrelated transaction on a pooled/reused connection: verified directly
by `tests/security/test_rls.py::test_setting_does_not_leak_between_sequential_transactions`.

Migrations run via Alembic as a separate owner/superuser role
(`DATABASE_MIGRATION_URL`) that can `CREATE ROLE` and
`ENABLE ROW LEVEL SECURITY`; the application connects as a distinct
least-privilege role (`APP_DB_USER`) that RLS actually applies to and
that must never be granted `BYPASSRLS`.

Embeddings are self-hosted `sentence-transformers`
(`retrieval/embedder.py`), identical in dev and prod, never a cloud
embedding API, per CLAUDE.md.

## LLM provider layer

One `LLMProvider` interface (`llm/base.py`), two implementations:
`GroqProvider` (local dev, explicit logged retry/backoff rather than the
SDK's silent default) and `BedrockProvider` (prod, against the Converse
API). Per-node model routing is provider-aware
(`Settings.model_classifier` / `Settings.model_synthesis`): a cheap model
for routing/tool-loop decisions/verification, a stronger model for the
one customer-facing synthesis call per turn. See
[ADR 0007](adr/0007-bedrock-provider-and-model-routing.md) for the full
reasoning and a real, disclosed access-control finding (a model listed as
available was not actually granted on the account that built this).

Neither provider uses native tool-calling: both speak a
structured-JSON-in-plain-text convention the agent's own tool loop
parses itself (`agents/tool_loop.py`). This is what makes
`llm/base.py::normalize_llm_response` load-bearing: Claude on Bedrock, in
particular, has been observed live wrapping that JSON in markdown fences,
in its own `<function_calls>[...]</function_calls>` convention with the
payload as a one-element array, and dropping this project's `"action"`
discriminator key entirely in favor of its own bare tool-call shape.
Normalization lives once, in the provider layer, rather than duplicated
(or forgotten) at every JSON-parsing call site in `nodes.py`, see
`tests/unit/test_llm_response_normalization.py` for the exact fixtures
this handles, run identically against both providers.

## Observability

Self-hosted Langfuse (`docker-compose.yml`), pinned to v2, see
[ADR 0001](adr/0001-langfuse-v2.md) for why v3 was rejected at the time
this was set up. Structured JSON logging throughout (`structlog`), never
`print`.

## ADR index

| ADR | Status | Decision |
|---|---|---|
| [0001](adr/0001-langfuse-v2.md) | Accepted | Pin self-hosted Langfuse to v2, not v3 |
| [0002](adr/0002-enum-migration-hand-edit.md) | Accepted | Hand-edited the initial migration's enum literals |
| [0003](adr/0003-mcp-client-in-process-bridge.md) | **Superseded** by 0004 | The original in-process MCP bridge, and why it was replaced |
| [0004](adr/0004-mcp-identity-propagation.md) | Accepted | Identity propagation across the real MCP stdio boundary |
| [0005](adr/0005-action-claim-verification.md) | Accepted | Original single-stage post-generation action-claim verification |
| [0006](adr/0006-two-stage-action-claim-verification.md) | Accepted | Splitting detection from substantiation, measured against a labelled eval set |
| [0007](adr/0007-bedrock-provider-and-model-routing.md) | Accepted | Bedrock provider, per-provider model routing, and a live access-control finding |
| [0008](adr/0008-span-validation-revert.md) | **Reverted** | Span-validation refinement to the two-stage verifier, measured worse, rolled back |

Also relevant, outside the ADR series: [docs/DATA_PRICE_AUDIT.md](DATA_PRICE_AUDIT.md)
(the KBB `$0.00`-sentinel handling referenced by `retrieval/search.py`)
and [docs/POLICY_COVERAGE.md](POLICY_COVERAGE.md) (the empirical
similarity-score analysis behind `retrieval/policy_search.py`'s
unanswerable-question handling).
