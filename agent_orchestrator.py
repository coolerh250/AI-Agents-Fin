"""
agent_orchestrator.py
Ubuntu 26.04 Maintenance Agent — Phase 4
State Graph: act (MCP) -> think (Claude) -> END
"""
import asyncio
import json
import os
import sys
import time
from typing import TypedDict

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from loguru import logger
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from utils.mcp_env import system_env

load_dotenv()

logger.remove()
logger.add(
    sys.stdout,
    format="{time:HH:mm:ss} | {level:<8} | {message}",
    level="DEBUG",
    colorize=False,
)

SYSTEM_PROMPT = """You are an expert Ubuntu 26.04 server maintenance agent for an AI Agent Studio.
Your responsibilities:
- Monitor system health metrics in real time
- Identify bottlenecks or risks for AI workloads
- Deliver clear, actionable maintenance recommendations

Respond technically and concisely. End your response with a single status line:
STATUS: READY | WARNING | CRITICAL"""

TASK = (
    "Perform a self-diagnostic check:\n"
    "1. Disk space: if available space is below 10 GB flag as CRITICAL immediately.\n"
    "2. Otherwise evaluate current CPU load — is the system suitable for "
    "high-intensity AI computation right now?\n"
    "3. Conclude with STATUS: READY / WARNING / CRITICAL and a one-line justification."
)

MODEL_ID = "claude-haiku-4-5-20251001"


class AgentState(TypedDict):
    system_stats: dict
    final_analysis: str
    status: str


# ── ACT node ──────────────────────────────────────────────────────────────────

async def _fetch_mcp_stats() -> dict:
    """Open stdio channel to system_server MCP and call get_system_stats."""
    params = StdioServerParameters(
        command="uv",
        args=["run", "mcp_servers/system_server.py"],
        env=system_env(),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get_system_stats", {})
            return json.loads(result.content[0].text)


def act_node(state: AgentState) -> dict:
    """Fetch live system data via MCP stdio transport."""
    logger.info("[ACT] Opening MCP channel → system_server.py")
    try:
        stats = asyncio.run(_fetch_mcp_stats())
        logger.success(
            f"[ACT] Live data received — "
            f"CPU: {stats['cpu']['usage_percent']}% | "
            f"MEM: {stats['memory']['usage_percent']}% | "
            f"DISK free: {stats['disk']['free_gb']} GB / {stats['disk']['total_gb']} GB"
        )
        return {"system_stats": stats}
    except Exception as exc:
        logger.error(f"[ACT] MCP call failed: {exc}")
        raise


# ── THINK node ────────────────────────────────────────────────────────────────

def think_node(state: AgentState) -> dict:
    """Send MCP data to Claude Haiku for logical inference."""
    logger.info(f"[THINK] Invoking Claude model: {MODEL_ID}")
    try:
        llm = ChatAnthropic(
            model=MODEL_ID,
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            max_tokens=512,
        )

        stats = state["system_stats"]
        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(
                content=f"Task:\n{TASK}\n\nLive system statistics:\n{json.dumps(stats, indent=2)}"
            ),
        ]

        logger.debug("[THINK] Sending prompt to Claude...")
        start = time.monotonic()
        response = llm.invoke(messages)
        latency_ms = int((time.monotonic() - start) * 1000)
        try:
            from telemetry import record_usage
            user_msg = f"Task:\n{TASK}\n\nLive system statistics:\n{json.dumps(state['system_stats'], indent=2)}"
            record_usage("maintenance_agent", MODEL_ID, response, latency_ms,
                         system_prompt=SYSTEM_PROMPT, user_content=user_msg)
        except Exception:
            pass
        analysis: str = response.content

        status = "UNKNOWN"
        for s in ("CRITICAL", "WARNING", "READY"):
            if s in analysis.upper():
                status = s
                break

        logger.success(f"[THINK] Analysis complete — Final Status: {status}")
        return {"final_analysis": analysis, "status": status}

    except Exception as exc:
        logger.error(f"[THINK] LLM invocation failed: {exc}")
        raise


# ── Graph assembly ─────────────────────────────────────────────────────────────

def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("act", act_node)
    graph.add_node("think", think_node)
    graph.set_entry_point("act")
    graph.add_edge("act", "think")
    graph.add_edge("think", END)
    return graph.compile()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    logger.info("=" * 62)
    logger.info("  Ubuntu 26.04 Maintenance Agent  |  agent_orchestrator.py")
    logger.info("=" * 62)

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key or api_key == "your_key_here":
        logger.error("ANTHROPIC_API_KEY not configured in .env — aborting")
        sys.exit(1)

    graph = build_graph()
    result = graph.invoke(
        AgentState(system_stats={}, final_analysis="", status="")
    )

    print("\n" + "=" * 62)
    print("  AGENT THOUGHT STREAM — FINAL REPORT")
    print("=" * 62)
    print(f"\n  ► System Status: {result['status']}\n")
    print(result["final_analysis"])
    print("\n" + "=" * 62)
    logger.info(f"Agent run complete. Status: {result['status']}")


if __name__ == "__main__":
    main()
