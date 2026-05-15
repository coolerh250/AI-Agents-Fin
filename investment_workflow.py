"""
investment_workflow.py
Taiwan Stock Futures Analysis Team — Phase 2
Entry point: reads market_snapshot.json, runs parallel analyst workflow,
outputs 今日投報建議書 to console and file.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from loguru import logger

from market_analyst_agents import (
    WorkflowState,
    chief_strategist_node,
    chip_analyst_node,
    save_to_db_node,
    tech_analyst_node,
)

load_dotenv()

logger.remove()
logger.add(
    sys.stdout,
    format="{time:HH:mm:ss} | {level:<8} | {message}",
    level="DEBUG",
    colorize=False,
)

SNAPSHOT_FILE = Path("market_snapshot.json")


def build_graph():
    graph = StateGraph(WorkflowState)
    graph.add_node("chip_analyst", chip_analyst_node)
    graph.add_node("tech_analyst", tech_analyst_node)
    graph.add_node("chief_strategist", chief_strategist_node)
    graph.add_node("save_to_db", save_to_db_node)

    # fan-out: START → chip_analyst AND tech_analyst (parallel)
    graph.add_edge(START, "chip_analyst")
    graph.add_edge(START, "tech_analyst")
    # fan-in: both → chief_strategist (waits for both to complete)
    graph.add_edge("chip_analyst", "chief_strategist")
    graph.add_edge("tech_analyst", "chief_strategist")
    graph.add_edge("chief_strategist", "save_to_db")
    graph.add_edge("save_to_db", END)

    return graph.compile()


def main():
    logger.info("=" * 62)
    logger.info("  台灣股期分析團隊 — Investment Workflow")
    logger.info("=" * 62)

    if not os.getenv("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY not set — aborting")
        sys.exit(1)

    if not SNAPSHOT_FILE.exists():
        logger.error(f"{SNAPSHOT_FILE} not found — run test_collection.py first")
        sys.exit(1)

    snapshot = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    logger.info(f"Snapshot loaded: {snapshot['timestamp']}")

    for tool_name, tool_data in snapshot["tools"].items():
        if not tool_data.get("success"):
            logger.warning(f"Tool {tool_name} reported failure in snapshot — proceeding anyway")

    graph = build_graph()
    initial_state = WorkflowState(
        snapshot=snapshot,
        chip_report="",
        tech_report="",
        final_brief="",
        db_row_id=None,
    )

    logger.info("Invoking LangGraph parallel workflow...")
    result = graph.invoke(initial_state)

    brief = result["final_brief"]
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    brief_file = Path(f"investment_brief_{ts}.txt")
    brief_file.write_text(brief, encoding="utf-8")
    logger.success(f"建議書已寫入 → {brief_file}")
    if result.get("db_row_id"):
        logger.success(f"TiDB 記錄 row_id={result['db_row_id']}")

    print("\n" + "=" * 62)
    print("  今日投報建議書")
    print(f"  資料時間：{snapshot['timestamp'][:19]} UTC")
    print("=" * 62)
    print(brief)
    print("=" * 62 + "\n")


if __name__ == "__main__":
    main()
