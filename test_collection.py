"""
test_collection.py
Concurrent MCP client test — calls all three finance tools in parallel,
saves market_snapshot.json and appends to collection_journal.jsonl.
"""
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from utils.mcp_env import market_data_env

logger.remove()
logger.add(sys.stdout, format="{time:HH:mm:ss} | {level:<8} | {message}", level="DEBUG")

SNAPSHOT_FILE = Path("market_snapshot.json")
JOURNAL_FILE  = Path("collection_journal.jsonl")

TOOLS = ["get_tw_future_chips", "get_us_market_summary", "get_financial_news"]


async def call_tool(
    session: ClientSession, tool_name: str
) -> tuple[str, dict, float]:
    """Call one MCP tool, return (name, parsed_result, latency_s)."""
    t0 = time.monotonic()
    try:
        result = await session.call_tool(tool_name, {})
        data = json.loads(result.content[0].text)
        latency = time.monotonic() - t0
        return (tool_name, data, latency)
    except Exception as exc:
        latency = time.monotonic() - t0
        logger.error(f"[{tool_name}] Error: {exc}")
        return (tool_name, {"error": True, "message": str(exc)}, latency)


async def main() -> None:
    logger.info("=" * 62)
    logger.info("  Financial Data Collector — Concurrent MCP Test")
    logger.info("=" * 62)

    server_params = StdioServerParameters(
        command="uv",
        args=["run", "mcp_servers/market_data_server.py"],
        env=market_data_env(),
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            logger.success("MCP session initialized")

            tools_resp = await session.list_tools()
            registered = [t.name for t in tools_resp.tools]
            logger.info(f"Registered tools: {registered}")

            logger.info("Dispatching 3 tools concurrently via asyncio.gather...")
            t_start = time.monotonic()

            results = await asyncio.gather(
                *[call_tool(session, name) for name in TOOLS]
            )

            overall_latency = time.monotonic() - t_start

    # ── Build snapshot ─────────────────────────────────────────────────────
    run_ts = datetime.now(timezone.utc).isoformat()
    snapshot: dict = {
        "timestamp":        run_ts,
        "overall_latency_s": round(overall_latency, 3),
        "tools":            {},
    }

    successes = 0
    for tool_name, data, latency in results:
        is_error = isinstance(data, dict) and data.get("error")
        if not is_error:
            successes += 1
        snapshot["tools"][tool_name] = {
            "latency_s": round(latency, 3),
            "success":   not is_error,
            "data":      data,
        }

    success_rate = successes / len(results)
    snapshot["success_rate"] = round(success_rate, 3)

    # ── Save snapshot ───────────────────────────────────────────────────────
    from snapshot_integrity import sign_snapshot
    snapshot = sign_snapshot(snapshot)
    SNAPSHOT_FILE.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.success(f"Snapshot saved → {SNAPSHOT_FILE}")

    # ── Append journal ──────────────────────────────────────────────────────
    journal_entry = {
        "run_at":            run_ts,
        "overall_latency_s": snapshot["overall_latency_s"],
        "success_rate":      success_rate,
        "per_tool": {
            name: {"latency_s": snap["latency_s"], "success": snap["success"]}
            for name, snap in snapshot["tools"].items()
        },
    }
    with JOURNAL_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(journal_entry, ensure_ascii=False) + "\n")
    logger.success(f"Journal appended → {JOURNAL_FILE}")

    # ── Console report ──────────────────────────────────────────────────────
    print("\n" + "=" * 62)
    print("  FINANCIAL DATA COLLECTION REPORT")
    print("=" * 62)
    print(f"  Timestamp      : {run_ts}")
    print(f"  Overall latency: {overall_latency:.2f}s")
    print(f"  Success rate   : {success_rate*100:.0f}% ({successes}/{len(results)})")
    print("-" * 62)
    for tool_name, data, latency in results:
        is_error = isinstance(data, dict) and data.get("error")
        status = "FAIL" if is_error else " OK "
        msg = f" — {data['message']}" if is_error else ""
        print(f"  [{status}] {tool_name:<32} {latency:.2f}s{msg}")
    print("=" * 62 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
