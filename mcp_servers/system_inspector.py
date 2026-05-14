import sys
import json
import psutil
from mcp.server.fastmcp import FastMCP
from loguru import logger

logger.remove()
logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}", level="DEBUG")

mcp = FastMCP("system-inspector")
logger.info("System Inspector MCP Server initializing")


@mcp.tool()
def get_system_stats() -> dict:
    """Return current CPU, memory, and disk usage as a structured JSON object."""
    logger.info("Tool 'get_system_stats' invoked")
    try:
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage("/")

        stats = {
            "cpu": {
                "usage_percent": cpu_percent,
                "logical_cores": psutil.cpu_count(logical=True),
                "physical_cores": psutil.cpu_count(logical=False),
            },
            "memory": {
                "total_gb": round(memory.total / 1024**3, 2),
                "used_gb": round(memory.used / 1024**3, 2),
                "available_gb": round(memory.available / 1024**3, 2),
                "usage_percent": memory.percent,
            },
            "disk": {
                "total_gb": round(disk.total / 1024**3, 2),
                "used_gb": round(disk.used / 1024**3, 2),
                "free_gb": round(disk.free / 1024**3, 2),
                "usage_percent": disk.percent,
            },
        }

        logger.success(
            f"Stats collected: CPU={cpu_percent}% | MEM={memory.percent}% | DISK={disk.percent}%"
        )
        return stats

    except Exception as e:
        logger.error(f"Failed to collect system stats: {e}")
        raise


if __name__ == "__main__":
    logger.info("Starting MCP Server via stdio transport")
    mcp.run(transport="stdio")
