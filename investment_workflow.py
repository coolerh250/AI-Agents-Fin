"""
investment_workflow.py
Taiwan Stock Futures Analysis Team — Phase 4
Graph: data_collector → (chip_analyst ∥ tech_analyst) → chief_strategist → format_agent → save_to_db
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
    data_collector_node,
    format_agent_node,
    save_to_db_node,
    tech_analyst_node,
    _MODEL_HAIKU,
    _PRICING,
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
    graph.add_node("data_collector",   data_collector_node)
    graph.add_node("chip_analyst",     chip_analyst_node)
    graph.add_node("tech_analyst",     tech_analyst_node)
    graph.add_node("chief_strategist", chief_strategist_node)
    graph.add_node("format_agent",     format_agent_node)
    graph.add_node("save_to_db",       save_to_db_node)

    graph.add_edge(START,              "data_collector")
    graph.add_edge("data_collector",   "chip_analyst")
    graph.add_edge("data_collector",   "tech_analyst")
    graph.add_edge("chip_analyst",     "chief_strategist")
    graph.add_edge("tech_analyst",     "chief_strategist")
    graph.add_edge("chief_strategist", "format_agent")
    graph.add_edge("format_agent",     "save_to_db")
    graph.add_edge("save_to_db",       END)

    return graph.compile()


def _print_cost_report() -> None:
    try:
        from database_tools import get_cost_summary

        rows = get_cost_summary(days=1)
        if not rows:
            logger.warning("無成本記錄可顯示")
            return

        # Order by workflow sequence
        _order = ["data_collector", "chip_analyst", "tech_analyst", "chief_strategist", "format_agent"]
        rows.sort(key=lambda r: _order.index(r["agent_name"]) if r["agent_name"] in _order else 99)

        haiku_input_rate  = _PRICING[_MODEL_HAIKU]["input"]
        haiku_output_rate = _PRICING[_MODEL_HAIKU]["output"]

        total_cost    = 0.0
        baseline_cost = 0.0

        print("\n" + "=" * 70)
        print("  效能與預算對比報告")
        print("=" * 70)
        print(f"{'節點':<18} {'模型':<14} {'耗時ms':>8} {'輸入Tok':>9} {'輸出Tok':>9} {'成本($)':>12}")
        print("-" * 70)

        for r in rows:
            in_tok  = int(r.get("total_input",  0) or 0)
            out_tok = int(r.get("total_output", 0) or 0)
            cost    = float(r.get("total_cost_usd", 0) or 0)
            latency = int(r.get("avg_latency_ms", 0) or 0)
            model_short = r["model_name"].replace("claude-", "").replace("-20251001", "")
            total_cost    += cost
            baseline_cost += (in_tok * haiku_input_rate + out_tok * haiku_output_rate) / 1_000_000
            print(f"{r['agent_name']:<18} {model_short:<14} {latency:>8} {in_tok:>9,} {out_tok:>9,} ${cost:>11.6f}")

        savings_pct    = (baseline_cost - total_cost) / baseline_cost * 100 if baseline_cost > 0 else 0
        monthly_saving = (baseline_cost - total_cost) * 20

        print("-" * 70)
        print(f"{'合計':<48} ${total_cost:>11.6f}")
        print(f"{'All-Haiku 基準（同 tokens）':<48} ${baseline_cost:>11.6f}")
        print(f"{'節省幅度':<48} {savings_pct:>10.1f}%")
        print(f"{'預估每月節省（20 交易日）':<48} ${monthly_saving:>11.4f}")
        print("=" * 70 + "\n")
    except Exception as exc:
        logger.warning(f"成本報表輸出失敗: {exc}")


def main():
    logger.info("=" * 62)
    logger.info("  台灣股期分析團隊 — Investment Workflow (Phase 4)")
    logger.info("=" * 62)

    if not os.getenv("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY not set — aborting")
        sys.exit(1)

    if not SNAPSHOT_FILE.exists():
        logger.error(f"{SNAPSHOT_FILE} not found — run test_collection.py first")
        sys.exit(1)

    from database_tools import ensure_cost_logs_table
    ensure_cost_logs_table()

    snapshot = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    logger.info(f"Snapshot loaded: {snapshot['timestamp']}")

    for tool_name, tool_data in snapshot["tools"].items():
        if not tool_data.get("success"):
            logger.warning(f"Tool {tool_name} reported failure in snapshot — proceeding anyway")

    graph = build_graph()
    initial_state = WorkflowState(
        snapshot=snapshot,
        raw_market_data={},
        chip_report="",
        tech_report="",
        final_brief="",
        final_report="",
        db_row_id=None,
    )

    logger.info("Invoking LangGraph workflow...")
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
    print("=" * 62)

    if result.get("final_report"):
        print("\n" + "=" * 62)
        print("  LINE 推播格式")
        print("=" * 62)
        print(result["final_report"])
        print("=" * 62)

    _print_cost_report()


if __name__ == "__main__":
    main()
