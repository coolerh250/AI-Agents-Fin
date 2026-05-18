"""
investment_workflow.py
Taiwan Stock Futures Analysis Team — Phase 4 + Observability
Graph: data_collector → (chip_analyst ∥ tech_analyst) → chief_strategist → format_agent → save_to_db
"""
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
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
    portfolio_manager_node,
    save_to_db_node,
    send_notification_node,
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
_COST_ALERT_THRESHOLD_USD = 0.25
_SNAPSHOT_WARN_HOURS      = 6
_SNAPSHOT_ABORT_HOURS     = 12


def build_graph():
    graph = StateGraph(WorkflowState)
    graph.add_node("data_collector",    data_collector_node)
    graph.add_node("chip_analyst",      chip_analyst_node)
    graph.add_node("tech_analyst",      tech_analyst_node)
    graph.add_node("chief_strategist",  chief_strategist_node)
    graph.add_node("portfolio_manager", portfolio_manager_node)
    graph.add_node("format_agent",      format_agent_node)
    graph.add_node("save_to_db",        save_to_db_node)
    graph.add_node("send_notification", send_notification_node)

    graph.add_edge(START,                  "data_collector")
    graph.add_edge("data_collector",       "chip_analyst")
    graph.add_edge("data_collector",       "tech_analyst")
    graph.add_edge("chip_analyst",         "chief_strategist")
    graph.add_edge("tech_analyst",         "chief_strategist")
    graph.add_edge("chief_strategist",     "portfolio_manager")
    graph.add_edge("portfolio_manager",    "format_agent")
    graph.add_edge("format_agent",         "save_to_db")
    graph.add_edge("save_to_db",           "send_notification")
    graph.add_edge("send_notification",    END)

    # Phase 4: checkpointer — enables resume-on-failure
    # Prefers SqliteSaver (persistent); falls back to MemorySaver (in-process)
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
        checkpointer = SqliteSaver.from_conn_string("./checkpoints.db")
        logger.debug("[build_graph] Using SqliteSaver checkpointer")
    except ImportError:
        from langgraph.checkpoint.memory import MemorySaver
        checkpointer = MemorySaver()
        logger.debug("[build_graph] langgraph-checkpoint-sqlite not installed — using MemorySaver")
    return graph.compile(checkpointer=checkpointer)


def _print_cost_report(run_id: str) -> float:
    """Print per-node cost table and return total cost for this run."""
    try:
        from database_tools import get_cost_summary

        rows = get_cost_summary(days=1)
        if not rows:
            logger.warning("無成本記錄可顯示")
            return 0.0

        _order = ["data_collector", "chip_analyst", "tech_analyst",
                  "chief_strategist", "portfolio_manager", "format_agent"]
        rows.sort(key=lambda r: _order.index(r["agent_name"]) if r["agent_name"] in _order else 99)

        haiku_input_rate  = _PRICING[_MODEL_HAIKU]["input"]
        haiku_output_rate = _PRICING[_MODEL_HAIKU]["output"]

        total_cost    = 0.0
        baseline_cost = 0.0

        print("\n" + "=" * 70)
        print("  效能與預算對比報告")
        print("=" * 70)
        print(f"{'節點':<18} {'模型':<14} {'耗時ms':>8} {'輸入Tok':>9} {'輸出Tok':>9} {'思考Tok':>9} {'成本($)':>12}")
        print("-" * 70)

        for r in rows:
            in_tok    = int(r.get("total_input",   0) or 0)
            out_tok   = int(r.get("total_output",  0) or 0)
            think_tok = int(r.get("total_thinking", 0) or 0)
            cost      = float(r.get("total_cost_usd", 0) or 0)
            latency   = int(r.get("avg_latency_ms", 0) or 0)
            model_short = r["model_name"].replace("claude-", "").replace("-20251001", "")
            total_cost    += cost
            baseline_cost += (in_tok * haiku_input_rate + out_tok * haiku_output_rate) / 1_000_000
            print(f"{r['agent_name']:<18} {model_short:<14} {latency:>8} {in_tok:>9,} {out_tok:>9,} {think_tok:>9,} ${cost:>11.6f}")

        savings_pct    = (baseline_cost - total_cost) / baseline_cost * 100 if baseline_cost > 0 else 0
        monthly_saving = (baseline_cost - total_cost) * 20

        print("-" * 70)
        print(f"{'合計':<56} ${total_cost:>11.6f}")
        print(f"{'All-Haiku 基準（同 tokens）':<56} ${baseline_cost:>11.6f}")
        print(f"{'節省幅度':<56} {savings_pct:>10.1f}%")
        print(f"{'預估每月節省（20 交易日）':<56} ${monthly_saving:>11.4f}")
        print("=" * 70 + "\n")

        # Return this run's cost for alert threshold — daily total is display-only
        from database_tools import get_run_cost
        return get_run_cost(run_id)
    except Exception as exc:
        logger.warning(f"成本報表輸出失敗: {exc}")
        return 0.0


def _run_post_alerts(run_id: str, result: dict, run_cost: float,
                     snapshot_age_seconds: int) -> None:
    """Fire A-001 through A-007 post-run alert checks."""
    from database_tools import get_run_events
    from messenger_tools import send_line, send_telegram

    alerts: list[str] = []
    try:
        events = get_run_events(run_id)

        # A-002: brief not saved to DB
        if not result.get("db_row_id"):
            alerts.append("🚨 [A-002] CRITICAL: 建議書未寫入 TiDB")

        # A-003: no successful delivery on any channel
        delivery_ok = any(e["event_type"] == "delivery_success" for e in events)
        if not delivery_ok:
            alerts.append("🚨 [A-003] CRITICAL: LINE/Telegram 推播均失敗")

        # A-004: run cost over threshold
        if run_cost > _COST_ALERT_THRESHOLD_USD:
            alerts.append(f"⚠️ [A-004] 今日成本 ${run_cost:.4f} 超出閾值 ${_COST_ALERT_THRESHOLD_USD}")

        # A-005: stale snapshot (already warned in main, surface in summary)
        if snapshot_age_seconds > _SNAPSHOT_WARN_HOURS * 3600:
            alerts.append(f"⚠️ [A-005] 快照已 {snapshot_age_seconds//3600:.1f}h 未更新")

        # A-006: LLM output invalid
        invalid = [e for e in events if e["event_type"] == "output_invalid"]
        if invalid:
            nodes = ", ".join(sorted({e["node_name"] for e in invalid if e.get("node_name")}))
            alerts.append(f"⚠️ [A-006] 節點輸出格式異常：{nodes}")

        # A-007: Opus thinking token spike
        from database_tools import get_run_events as _gre
        from sqlalchemy import text
        from database_tools import _engine
        with _engine().connect() as conn:
            think_row = conn.execute(
                text("""
                    SELECT thinking_tokens FROM cost_logs
                    WHERE run_id = :rid AND agent_name = 'chief_strategist'
                    LIMIT 1
                """),
                {"rid": run_id},
            ).fetchone()
        if think_row and think_row[0] > 2048:
            alerts.append(f"⚠️ [A-007] Opus 思考 Token {think_row[0]} 超過 2048 上限")

    except Exception as exc:
        logger.warning(f"Post-alert check failed: {exc}")

    if not alerts:
        return

    message = "📋 AI Agent Studio 執行警告\n\n" + "\n".join(alerts)
    # CRITICAL alerts → LINE + Telegram; WARNING → Telegram only
    has_critical = any("[A-00" in a and "CRITICAL" in a for a in alerts)
    if has_critical:
        send_line(message)
    send_telegram(message)
    logger.warning(f"[Alerts] {len(alerts)} 條警告已推送")


def main():
    run_id = str(uuid.uuid4())

    logger.info("=" * 62)
    logger.info("  台灣股期分析團隊 — Investment Workflow (Phase 4)")
    logger.info("=" * 62)
    logger.info(f"Run ID: {run_id}")

    if not os.getenv("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY not set — aborting")
        sys.exit(1)

    if not SNAPSHOT_FILE.exists():
        logger.error(f"{SNAPSHOT_FILE} not found — run test_collection.py first")
        sys.exit(1)

    from database_tools import (
        ensure_cost_logs_table,
        ensure_observability_tables,
        ensure_portfolio_table,
        seed_test_portfolio,
        create_workflow_run,
        finish_workflow_run,
        log_event,
    )
    ensure_cost_logs_table()
    ensure_observability_tables()
    ensure_portfolio_table()
    seed_test_portfolio()

    # Sync company names for portfolio items not yet in stock_info
    try:
        from database_tools import get_portfolio_missing_names, upsert_stock_info
        from twse_fetcher import lookup_company_name
        missing = get_portfolio_missing_names()
        if missing:
            logger.info(f"[StockSync] 同步 {len(missing)} 筆股票名稱: {missing}")
            for sid in missing:
                name = lookup_company_name(sid)
                if name:
                    upsert_stock_info(sid, name)
                    logger.success(f"[StockSync] {sid} → {name}")
                else:
                    logger.warning(f"[StockSync] 無法取得 {sid} 公司名稱（TWSE 查無資料）")
        else:
            logger.debug("[StockSync] 所有持倉股票名稱均已同步")
    except Exception as exc:
        logger.warning(f"[StockSync] 股票名稱同步失敗（略過）: {exc}")

    snapshot = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    from snapshot_integrity import verify_snapshot
    verify_snapshot(snapshot)

    # Snapshot freshness check
    snap_ts  = datetime.fromisoformat(snapshot["timestamp"].replace("Z", "+00:00"))
    snap_age = datetime.now(timezone.utc) - snap_ts
    snapshot_age_seconds = int(snap_age.total_seconds())

    if snap_age > timedelta(hours=_SNAPSHOT_ABORT_HOURS):
        logger.error(f"Snapshot is {snap_age} old — aborting (stale data risk)")
        log_event(run_id, "node_failure", "main",
                  {"reason": "stale_snapshot", "age_seconds": snapshot_age_seconds},
                  severity="error")
        from messenger_tools import send_line, send_telegram
        msg = (f"🚨 [A-001+A-005] 市場快照過舊（{snapshot_age_seconds//3600:.0f}h），"
               f"工作流已中止。請手動執行 test_collection.py")
        send_line(msg)
        send_telegram(msg)
        sys.exit(1)

    if snap_age > timedelta(hours=_SNAPSHOT_WARN_HOURS):
        logger.warning(f"Snapshot is {snap_age} old — proceeding with caution")
        log_event(run_id, "fallback_activated", "main",
                  {"reason": "snapshot_aging", "age_seconds": snapshot_age_seconds},
                  severity="warn")
        from messenger_tools import send_telegram
        send_telegram(
            f"⚠️ [A-005] 市場快照已 {snapshot_age_seconds//3600:.1f}h 未更新\n"
            f"分析可能基於舊數據，請確認 test_collection.py 是否正常執行"
        )

    logger.info(f"Snapshot loaded: {snapshot['timestamp']} (age {snapshot_age_seconds}s)")

    for tool_name, tool_data in snapshot["tools"].items():
        if not tool_data.get("success"):
            logger.warning(f"Tool {tool_name} reported failure in snapshot — proceeding anyway")

    create_workflow_run(
        run_id=run_id,
        run_type="investment",
        snapshot_ts=snapshot["timestamp"],
        snapshot_age_seconds=snapshot_age_seconds,
    )

    graph = build_graph()
    initial_state = WorkflowState(
        run_id=run_id,
        snapshot=snapshot,
        raw_market_data={},
        chip_report="",
        tech_report="",
        final_brief="",
        final_report="",
        db_row_id=None,
        portfolio_advice="",
    )

    logger.info("Invoking LangGraph workflow...")
    _graph_config = {"configurable": {"thread_id": run_id}}
    try:
        result = graph.invoke(initial_state, config=_graph_config)
        finish_workflow_run(run_id, "success")
    except Exception as exc:
        finish_workflow_run(run_id, "failed", str(exc))
        raise

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

    run_cost = _print_cost_report(run_id)
    _run_post_alerts(run_id, result, run_cost, snapshot_age_seconds)


if __name__ == "__main__":
    main()
