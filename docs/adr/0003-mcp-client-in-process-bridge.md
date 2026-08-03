# ADR 0003: Bridge LangGraph to the MCP tool server in-process, not via `langchain-mcp-adapters`

## Status

**Superseded.** The in-process bridge this ADR describes has been
replaced by a real MCP stdio transport - see the "Superseded" section at
the end of this document and ADR 0004. The historical context and
reasoning below is kept as the record of what was tried, what broke, and
why, since the next investigation (done properly, this time) built on it.

## Context

CLAUDE.md's Architecture Decisions mandate: "Tools are exposed via an MCP
server... The LangGraph agent consumes them as an MCP client," with
`langchain-mcp-adapters` and `mcp` as the two dependencies for this.

While wiring up the sub-agents in `src/dealership_agent/agents/`, this
combination turned out to be broken as installed:

- `mcp` resolved to `2.0.0`, which restructured the server API - the
  `MCPServer` class our tool server (`tools/server.py`) already uses lives
  at `mcp.server.mcpserver.MCPServer`. The pre-2.0 `mcp.server.fastmcp`
  module no longer exists in this version.
- `langchain-mcp-adapters` `0.3.1` (latest at time of writing) still
  imports `from mcp.server.fastmcp.tools import Tool as FastMCPTool` in
  its `tools.py` module. That import fails outright:
  `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`.
  `langchain_mcp_adapters.tools` - the module that provides
  `load_mcp_tools()`, the function this ADR would otherwise use - cannot
  be imported at all against `mcp` 2.0.0, regardless of which `mcp`
  version range `langchain-mcp-adapters`' metadata declares support for
  (`mcp>=1.24.0`, no upper bound - the declared constraint doesn't
  reflect the actual break).
- Downgrading `mcp` to a pre-2.0 version to satisfy `langchain-mcp-adapters`
  was rejected: `tools/server.py`, `tools/scope.py`, `tools/identity.py`,
  and their passing test suite (`tests/security/test_tool_boundary.py`)
  are all built and verified against the 2.0 `MCPServer` API. Reverting
  that working, tested code to chase a different library's compatibility
  window is a larger, riskier change than bridging the gap ourselves.
- A manual bridge using `mcp.ClientSession` over
  `mcp.shared.memory.create_client_server_memory_streams` (real MCP
  wire-protocol, no subprocess) was attempted directly, bypassing
  `langchain_mcp_adapters.tools`. It hung indeterminately in this
  environment during the initialize handshake and was not pursued further
  given time constraints - it may be a fixable stream-plumbing issue, but
  wasn't worth chasing for a skeleton.

## Decision

`src/dealership_agent/agents/tool_binding.py` calls the MCP server object
(`dealership_agent.tools.server.server`) directly and in-process:
`await server.list_tools()` and `await server.call_tool(name, arguments)`.
These are the same public methods a `ClientSession` ultimately calls
through on the server side - no different tool logic, schema generation,
identity chokepoint, or logging path is bypassed. What's skipped is only
the wire-level transport (JSON-RPC framing over a stream pair), which is
an implementation detail beneath the architectural contract, not the
contract itself:

- Tool permission scoping (the security boundary CLAUDE.md is built
  around) is enforced identically: `tool_binding.build_sub_agent()`
  fetches the full tool list from the server and asserts, at construction
  time, that a sub-agent's bound tools are exactly its allow-list - never
  more.
- Identity injection still happens exactly where CLAUDE.md requires: at
  the MCP server boundary, inside `tools/scope.py`'s
  `customer_scoped_connection()`, via the `tools/identity.py` contextvar -
  not in `tool_binding.py`, not in any graph node, and not in a tool
  argument.
- `langchain-mcp-adapters` remains a declared dependency (per CLAUDE.md);
  it is simply not the code path in use until it supports `mcp` 2.x.

## Consequences

- If `langchain-mcp-adapters` ships a release compatible with `mcp` 2.x,
  swapping `tool_binding.py`'s internals to use `load_mcp_tools()` instead
  of direct in-process calls is a contained, mechanical change - the
  `SubAgent` construction contract (allow-listed tools, asserted at
  construction) does not need to change.
- Until then, the LangGraph agent and the MCP server run in the same
  Python process by construction - there is no supported path in this
  codebase today to run the tool server as a separate process reachable
  over stdio/SSE. Revisit this if/when a real multi-process deployment is
  needed; it was not needed for this task.

## Superseded

The "revisit this" above happened the next time this codebase was
touched. Re-investigating properly (rather than re-confirming the
workaround) found a real fix:

- `langchain-mcp-adapters` has had no release since `0.3.1` - there is no
  newer version that supports `mcp` 2.x to wait for.
- But `langchain-mcp-adapters==0.3.1` **does** work correctly against the
  latest `mcp` 1.x release (`1.29.0`) - verified directly:
  `from langchain_mcp_adapters.tools import load_mcp_tools` imports
  cleanly, and a real stdio client/server round trip
  (`mcp.client.stdio.stdio_client` + `ClientSession` talking to a
  `mcp.server.fastmcp.FastMCP` server subprocess) works reliably. The
  2.0.0-vs-1.x break this ADR describes was never actually a dead end -
  the first pass just hadn't checked whether pinning `mcp<2` resolved it.
- `pyproject.toml` now pins `mcp>=1.24.0,<2`; `tools/server.py` was
  rewritten from `mcp.server.mcpserver.MCPServer` to
  `mcp.server.fastmcp.FastMCP` (a small, mechanical change - same
  `@server.tool()` decorator pattern, same `list_tools()`/`call_tool()`
  shape).
- The tool server now runs as a genuine separate OS process
  (`python -m dealership_agent.tools.server`), and the agent connects as
  a real MCP client over stdio (`agents/mcp_session.py`). The in-process
  `tool_binding.py` calls this ADR justified are gone; see ADR 0004 for
  how identity propagation was redesigned for a real process boundary.

The lesson: the original in-process bridge was a defensible call under
time pressure, but "newest version of `langchain-mcp-adapters` is broken
against newest `mcp`" and "no combination of these two libraries works"
are different claims, and only the first one was actually verified at the
time.
