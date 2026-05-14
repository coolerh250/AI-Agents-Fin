import asyncio
import json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from loguru import logger
import sys

logger.remove()
logger.add(sys.stdout, format="{time:HH:mm:ss} | {level} | {message}", level="DEBUG")


async def main():
    server_params = StdioServerParameters(
        command="uv",
        args=["run", "mcp_servers/system_inspector.py"],
        env=None,
    )

    logger.info("Connecting to System Inspector MCP Server...")
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            logger.success("Session initialized")

            tools_resp = await session.list_tools()
            tool_names = [t.name for t in tools_resp.tools]
            logger.info(f"Available tools: {tool_names}")

            logger.info("Calling 'get_system_stats'...")
            result = await session.call_tool("get_system_stats", {})

            raw = result.content[0].text
            stats = json.loads(raw)

            print("\n" + "=" * 50)
            print("  SYSTEM STATS REPORT")
            print("=" * 50)
            print(json.dumps(stats, indent=2))
            print("=" * 50)

            logger.success("Test completed successfully")


if __name__ == "__main__":
    asyncio.run(main())
