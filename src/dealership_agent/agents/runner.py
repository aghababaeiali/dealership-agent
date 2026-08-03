"""Run one supervisor-graph turn: owns the MCP session lifecycle.

One call to `run_turn()` is one MCP session (see
docs/adr/0004-mcp-identity-propagation.md): a single tool-server
subprocess is spawned, bound to `identity` for its whole lifetime, shared
by whichever sub-agent(s) this turn's routing touches, and torn down when
the turn completes.
"""

from __future__ import annotations

from dealership_agent.agents.graph import build_supervisor_graph
from dealership_agent.agents.mcp_session import open_mcp_session
from dealership_agent.agents.state import GraphState
from dealership_agent.agents.tool_binding import build_account_agent, build_sales_agent
from dealership_agent.llm.base import LLMProvider, Message
from dealership_agent.tools.identity import RequestIdentity


async def run_turn(
    llm: LLMProvider, identity: RequestIdentity, messages: list[Message]
) -> GraphState:
    async with open_mcp_session(identity) as session:
        sales_agent = await build_sales_agent(session)
        account_agent = await build_account_agent(session)
        graph = build_supervisor_graph(llm, sales_agent, account_agent)

        initial_state: GraphState = {"messages": messages, "identity": identity}
        result: GraphState = await graph.ainvoke(initial_state)  # type: ignore[assignment]
        return result
