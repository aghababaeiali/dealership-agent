# ADR 0004: Identity propagation across the real MCP stdio boundary

## Status

Accepted.

## Context

ADR 0003 replaced the in-process MCP bridge with a real transport: the
tool server now runs as a separate OS process, and the agent connects as
a genuine MCP client over stdio (see `agents/mcp_session.py`). The
in-process design leaned on a `contextvars.ContextVar`
(`tools/identity.py`) set by the caller immediately before a tool call.
That mechanism cannot survive a process boundary - a contextvar set in
the agent's process has no meaning in the separate tool-server process
stdio now connects to.

CLAUDE.md's Core Security Invariant is unchanged by the transport: the
LLM must never choose whose data it reads, identity must never be a tool
argument, and a tool call with no established identity must fail closed.
The question this ADR answers is *where identity now lives* so those
properties still hold with a real process boundary in between.

## Decision

**One MCP session is one OS subprocess, and identity is fixed for that
subprocess's entire lifetime, set once via environment variables at
spawn time.**

- `agents/mcp_session.py::open_mcp_session(identity)` spawns
  `python -m dealership_agent.tools.server` with two additional
  environment variables layered onto a full copy of the parent's
  environment: `DEALERSHIP_MCP_SESSION_ID` (always) and
  `DEALERSHIP_MCP_CUSTOMER_ID` (only when a real customer is
  authenticated).
- `tools/server.py::main()` reads those variables exactly once, at
  process startup, and binds them into the *same*
  `tools/identity.py` contextvar the in-process design used - but now
  bound for the process's whole life (`with bind_identity(identity):
  server.run(transport="stdio")`), not per call. Every tool invocation
  the server handles for that subprocess's lifetime sees the same
  identity, because there is only ever one identity per process.
- If `DEALERSHIP_MCP_SESSION_ID` is absent, `_identity_from_env()`
  returns `None` and the server runs with no bound identity at all -
  `tools/scope.py::customer_scoped_connection()` already raises
  `PermissionError` in that case (unchanged from ADR 0003), so a
  customer-scoped tool call arriving at a session with no established
  identity fails closed, exactly as required. This is exercised directly
  in `tests/security/test_tool_boundary.py`.
- Identity is never included in any tool's JSON schema or `arguments`
  dict - `get_order_status(order_ref)` and `escalate_to_human(summary,
  reason)` are unchanged. `argument_keys` logged per call
  (`tools/server.py`'s `_log_tool_call`) never includes it either.
- One subprocess/session is shared across every sub-agent tool call
  within a single conversation turn (see `agents/graph.py`'s
  `run_turn()`), so "per session, not per call" also means: if a turn's
  bounded tool loop calls two or three tools back to back, identity is
  established exactly once for all of them, not re-sent per call.

## Rejected alternatives

- **Per-call `_meta` sideband on each `tools/call` request.** MCP's
  JSON-RPC envelope supports an optional `_meta` field independent of
  `arguments`. This was rejected because it is, structurally, a per-call
  channel - it would satisfy "never a tool argument" but not "per
  session, not per call," and it invites future code to treat identity
  as just another per-call parameter to thread through, eroding the
  boundary this whole design exists to protect.
- **One long-lived multi-tenant server process, with an in-band session
  identifier resolved server-side to identity.** Stdio is inherently a
  single point-to-point pipe (one parent, one child); serving many
  concurrent customers from one server process would require inventing a
  custom multiplexing/session-registry protocol on top of MCP. That is
  unrequested complexity CLAUDE.md's Anti-Over-Engineering Rules would
  flag, for no real benefit given a fresh subprocess per turn is already
  cheap enough for this project's scale.
- **A session-id tool argument the server resolves against a shared
  store (DB/Redis) to look up identity.** Rejected for two reasons: (1) a
  session id passed as a tool argument is still an identity-shaped
  parameter arriving through the tool-call path, which is exactly what
  CLAUDE.md prohibits in spirit even if `customer_id` itself never
  appears; (2) it adds a mutable shared side-channel (staleness, races,
  an extra store to secure) that the environment-variable-at-spawn design
  avoids entirely by construction.

## Consequences

- A subprocess is spawned per conversation turn (not per tool call, and
  not one long-lived server for the whole application). This has a real
  cost - `sentence-transformers`/`torch` get re-imported and the
  embedding model reloaded on every turn, since each subprocess starts
  cold. Measured during Part C's smoke test: multi-second startup
  overhead per turn. Acceptable for this project's scale and explicitly
  the tradeoff CLAUDE.md's "transport-real" requirement implies; revisit
  with a model-server/warm-pool if throughput ever matters.
- Environment variables of a child process are visible to anything with
  sufficient OS privilege to inspect that process (e.g. `/proc/<pid>/environ`
  on Linux). This is a known, moderate disclosure surface, judged
  acceptable here because the subprocess is our own application's child,
  not exposed to other tenants, and ECS Fargate tasks run in their own
  isolated execution environment. A future hardening step (e.g. a Unix
  domain socket with peer-credential checks instead of env vars) is
  possible but out of scope for this project's current deployment model.
- Because identity is fixed per subprocess, a single running MCP session
  cannot be "re-identified" mid-conversation (e.g. a customer logging in
  partway through an anonymous chat) - that case requires ending the
  current session and opening a new one with the newly-established
  identity, which is what `run_turn()` naturally does on the next turn
  anyway since a fresh session is opened per turn.
