"""Real MCP client transport: spawns the tool server as a separate process
and connects over stdio.

CLAUDE.md + docs/adr/0003: the tool boundary must be transport-real, not
an in-process shortcut. One subprocess = one MCP session = one
conversation turn. Identity travels via subprocess environment variables,
established once per session at spawn time - never per call, never as a
tool argument. See docs/adr/0004-mcp-identity-propagation.md for the full
design and rejected alternatives.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from dealership_agent.tools.identity import RequestIdentity
from dealership_agent.tools.server import CUSTOMER_ID_ENV_VAR, SESSION_ID_ENV_VAR


@asynccontextmanager
async def open_mcp_session(identity: RequestIdentity) -> AsyncIterator[ClientSession]:
    """Spawn the MCP tool server bound to `identity` and yield a connected,
    initialized ClientSession for its lifetime.

    `mcp`'s stdio client only forwards a small safe allowlist of env vars
    (PATH etc.) to the child by default, not the parent's full
    environment - the child would not be able to reach Postgres or load
    Settings otherwise, so the full parent environment is forwarded
    explicitly, with the identity vars layered on top.
    """
    env = {
        **os.environ,
        SESSION_ID_ENV_VAR: identity.session_id,
    }
    if identity.customer_id is not None:
        env[CUSTOMER_ID_ENV_VAR] = str(identity.customer_id)

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "dealership_agent.tools.server"],
        env=env,
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session
