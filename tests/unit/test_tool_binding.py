"""C2: sub-agent tool sets must be disjoint, matching CLAUDE.md's split by
permission scope. No live LLM or DB access needed - `list_tools()` only
returns registered schemas."""

from dealership_agent.agents.tool_binding import (
    ACCOUNT_AGENT_TOOLS,
    SALES_AGENT_TOOLS,
    build_account_agent,
    build_sales_agent,
)


class TestSubAgentToolSetsAreDisjoint:
    async def test_sales_agent_has_exactly_the_expected_tools(self) -> None:
        sales_agent = await build_sales_agent()
        assert sales_agent.allowed_tools == {"search_listings", "search_policy_docs"}
        assert set(sales_agent.tool_specs) == sales_agent.allowed_tools

    async def test_account_agent_has_exactly_the_expected_tools(self) -> None:
        account_agent = await build_account_agent()
        assert account_agent.allowed_tools == {"get_order_status", "escalate_to_human"}
        assert set(account_agent.tool_specs) == account_agent.allowed_tools

    async def test_sales_and_account_tool_sets_are_disjoint(self) -> None:
        sales_agent = await build_sales_agent()
        account_agent = await build_account_agent()
        assert sales_agent.allowed_tools.isdisjoint(account_agent.allowed_tools)
        # Sanity check against the module-level constants too.
        assert SALES_AGENT_TOOLS.isdisjoint(ACCOUNT_AGENT_TOOLS)

    async def test_sales_agent_cannot_call_an_account_tool(self) -> None:
        sales_agent = await build_sales_agent()
        try:
            await sales_agent.call_tool("get_order_status", {"order_ref": "does-not-matter"})
        except PermissionError:
            pass
        else:
            raise AssertionError("sales_agent was able to call an account-scoped tool")

    async def test_account_agent_cannot_call_a_sales_tool(self) -> None:
        account_agent = await build_account_agent()
        try:
            await account_agent.call_tool("search_listings", {"query": "does-not-matter"})
        except PermissionError:
            pass
        else:
            raise AssertionError("account_agent was able to call a sales-scoped tool")
