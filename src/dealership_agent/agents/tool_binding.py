"""Bind a disjoint subset of the MCP tool server's tools to each sub-agent.

CLAUDE.md: "Sales Agent must never have order tools bound" - enforced
here in construction, not by prompting. `build_sub_agent()` fetches the
full tool list from a real MCP ClientSession (connected over stdio to a
separate server process - see agents/mcp_session.py) and asserts, before
returning, that the sub-agent's bound tools are *exactly* its allow-list.
`SubAgent.call_tool` then double-checks on every call. There is no other
way in this codebase to obtain a callable for a tool outside a sub-agent's
allow-list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp import ClientSession
from mcp.types import TextContent

SALES_AGENT_TOOLS = frozenset({"search_listings", "search_policy_docs"})
ACCOUNT_AGENT_TOOLS = frozenset({"get_order_status", "escalate_to_human"})


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class SubAgent:
    name: str
    allowed_tools: frozenset[str]
    tool_specs: dict[str, ToolSpec]
    session: ClientSession

    def __post_init__(self) -> None:
        if set(self.tool_specs) != self.allowed_tools:
            raise RuntimeError(
                f"{self.name}: bound tool specs {set(self.tool_specs)} do not "
                f"exactly match allowed_tools {set(self.allowed_tools)}"
            )

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if tool_name not in self.allowed_tools:
            raise PermissionError(
                f"{self.name} is not bound to tool {tool_name!r} "
                f"(allowed: {sorted(self.allowed_tools)})"
            )
        result = await self.session.call_tool(tool_name, arguments)
        if result.isError:
            first = result.content[0] if result.content else None
            message = first.text if isinstance(first, TextContent) else "tool call failed"
            raise RuntimeError(f"{tool_name} failed: {message}")
        structured = result.structuredContent
        if isinstance(structured, dict) and "result" in structured:
            return structured["result"]
        return structured


async def build_sub_agent(
    session: ClientSession, name: str, allowed_tools: frozenset[str]
) -> SubAgent:
    """Fetch the tool list from `session` (a real, already-initialized MCP
    ClientSession - schemas are read exactly as returned over the wire) and
    bind only `allowed_tools`."""
    registered = await session.list_tools()
    registered_by_name = {tool.name: tool for tool in registered.tools}

    missing = allowed_tools - set(registered_by_name)
    if missing:
        raise RuntimeError(f"{name}: tools not registered on the MCP server: {sorted(missing)}")

    tool_specs = {
        tool_name: ToolSpec(
            name=tool_name,
            description=registered_by_name[tool_name].description or "",
            input_schema=registered_by_name[tool_name].inputSchema,
        )
        for tool_name in allowed_tools
    }
    return SubAgent(name=name, allowed_tools=allowed_tools, tool_specs=tool_specs, session=session)


async def build_sales_agent(session: ClientSession) -> SubAgent:
    return await build_sub_agent(session, "sales_agent", SALES_AGENT_TOOLS)


async def build_account_agent(session: ClientSession) -> SubAgent:
    return await build_sub_agent(session, "account_agent", ACCOUNT_AGENT_TOOLS)
