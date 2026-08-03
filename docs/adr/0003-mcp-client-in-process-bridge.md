# ADR 0003: Bridge LangGraph to the MCP tool server in-process, not via `langchain-mcp-adapters`

## Status

Accepted.

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
