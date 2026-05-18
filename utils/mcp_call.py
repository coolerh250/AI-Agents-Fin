"""
utils/mcp_call.py
Synchronous wrapper for MCP tool calls. Safe for use in synchronous LangGraph nodes.
One asyncio.run() per call — ~2 s cold start per subprocess. Acceptable for Phase 1.
Replace with native await after T3-E async migration.
"""
import asyncio
import json

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from utils.mcp_env import _env_for_server


def call_mcp_tool_sync(
    server_script: str,
    tool_name: str,
    arguments: dict,
    timeout: float = 30.0,
) -> dict:
    """Synchronously call a single MCP tool from a stdio server.
    Raises RuntimeError if called from inside a running event loop (use ainvoke instead).
    """
    async def _inner():
        params = StdioServerParameters(
            command="uv",
            args=["run", server_script],
            env=_env_for_server(server_script),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                return json.loads(result.content[0].text)

    return asyncio.run(_inner())
