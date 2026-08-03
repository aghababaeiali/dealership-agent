"""C2: sub-agent tool sets must be disjoint, matching CLAUDE.md's split by
permission scope. Runs against the real MCP stdio transport (docs/adr/0003).

Each test opens its own session directly (rather than via a shared async
fixture) - pytest-asyncio's per-function event loop and anyio's
task-affinity checks on `ClientSession`'s internal task group don't mix
well with a session yielded across fixture setup/teardown boundaries.
"""

from dealership_agent.agents.mcp_session import open_mcp_session
from dealership_agent.agents.tool_binding import (
    ACCOUNT_AGENT_TOOLS,
    SALES_AGENT_TOOLS,
    build_account_agent,
    build_sales_agent,
)
from dealership_agent.tools.identity import RequestIdentity

_IDENTITY = RequestIdentity(session_id="tool-binding-test-session", customer_id=None)


class TestSubAgentToolSetsAreDisjoint:
    async def test_sales_agent_has_exactly_the_expected_tools(self) -> None:
        async with open_mcp_session(_IDENTITY) as session:
            sales_agent = await build_sales_agent(session)
            assert sales_agent.allowed_tools == {"search_listings", "search_policy_docs"}
            assert set(sales_agent.tool_specs) == sales_agent.allowed_tools

    async def test_account_agent_has_exactly_the_expected_tools(self) -> None:
        async with open_mcp_session(_IDENTITY) as session:
            account_agent = await build_account_agent(session)
            assert account_agent.allowed_tools == {"get_order_status", "escalate_to_human"}
            assert set(account_agent.tool_specs) == account_agent.allowed_tools

    async def test_sales_and_account_tool_sets_are_disjoint(self) -> None:
        async with open_mcp_session(_IDENTITY) as session:
            sales_agent = await build_sales_agent(session)
            account_agent = await build_account_agent(session)
            assert sales_agent.allowed_tools.isdisjoint(account_agent.allowed_tools)
        # Sanity check against the module-level constants too.
        assert SALES_AGENT_TOOLS.isdisjoint(ACCOUNT_AGENT_TOOLS)

    async def test_sales_agent_cannot_call_an_account_tool(self) -> None:
        async with open_mcp_session(_IDENTITY) as session:
            sales_agent = await build_sales_agent(session)
            try:
                await sales_agent.call_tool("get_order_status", {"order_ref": "does-not-matter"})
            except PermissionError:
                pass
            else:
                raise AssertionError("sales_agent was able to call an account-scoped tool")

    async def test_account_agent_cannot_call_a_sales_tool(self) -> None:
        async with open_mcp_session(_IDENTITY) as session:
            account_agent = await build_account_agent(session)
            try:
                await account_agent.call_tool("search_listings", {"query": "does-not-matter"})
            except PermissionError:
                pass
            else:
                raise AssertionError("account_agent was able to call a sales-scoped tool")
