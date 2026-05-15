"""
backtest_agent.py
Taiwan Stock Futures Analysis Team — Self-Reflection Agent
Reads yesterday's brief from TiDB, fetches actual TAIEX data from TWSE,
compares prediction vs reality, and outputs an accuracy report.
"""
import json
import os
import sys
from datetime import date, timedelta
from typing import Optional, TypedDict

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from loguru import logger

load_dotenv()

logger.remove()
logger.add(
    sys.stdout,
    format="{time:HH:mm:ss} | {level:<8} | {message}",
    level="DEBUG",
    colorize=False,
)

MODEL_ID = "claude-haiku-4-5-20251001"

_EVAL_SYSTEM = """你是台灣股期分析團隊的績效評估官。
你將收到昨日的投報建議書（預測）與當日真實走勢數據（實際），進行準確度評估。
請輸出「準確度評估報告」，格式如下：

【預測回顧】
- 預測跳空方向：（up/flat/down）
- 預測跳空幅度：（%）

【實際走勢】
- 實際開盤跳空：（%，相對前日收盤）
- 收盤表現：（%，相對前日收盤）

【準確度評分】
- 方向準確：（是/否）
- 幅度誤差：（絕對值 %）
- 綜合評分：（0~100分）

【反省與學習】
（指出預測失誤的原因，或成功的關鍵，以改進下次預測）"""


class BacktestState(TypedDict):
    trade_date:      str           # YYYY-MM-DD，欲回測的交易日
    brief_record:    Optional[dict]  # daily_briefs row from TiDB
    actual_data:     Optional[dict]  # TWII yfinance data
    accuracy_report: str


def load_brief_node(state: BacktestState) -> dict:
    """Load yesterday's brief from TiDB."""
    logger.info(f"[LoadBrief] 讀取 {state['trade_date']} 建議書")
    try:
        from database_tools import get_brief
        d = date.fromisoformat(state["trade_date"])
        record = get_brief(d)
        if record is None:
            logger.warning(f"[LoadBrief] 找不到 {state['trade_date']} 的建議書")
        else:
            logger.success(f"[LoadBrief] 找到建議書 row_id={record['id']}")
        return {"brief_record": record}
    except Exception as exc:
        logger.error(f"[LoadBrief] 失敗: {exc}")
        return {"brief_record": None}


def fetch_actual_node(state: BacktestState) -> dict:
    """Fetch actual TAIEX close data for trade_date from TWSE and save to DB."""
    logger.info(f"[FetchActual] 抓取 TWSE 加權指數盤後資訊 ({state['trade_date']})")
    try:
        from twse_fetcher import get_taiex_actuals
        trade_date = date.fromisoformat(state["trade_date"])
        actual = get_taiex_actuals(trade_date)
        if actual is None:
            raise ValueError(f"TWSE 無法取得 {trade_date} 資料（假日或查詢失敗）")

        try:
            from database_tools import save_actual
            save_actual(
                trade_date,
                actual["open_price"],
                actual["close_price"],
                actual["actual_gap_pct"],
                notes="source=TWSE",
            )
            logger.success(f"[FetchActual] 已寫入 market_actuals: 收盤={actual['close_price']} 漲跌={actual['actual_gap_pct']:+.2f}%")
        except Exception as db_exc:
            logger.warning(f"[FetchActual] 寫入 DB 失敗（繼續）: {db_exc}")

        return {"actual_data": actual}

    except Exception as exc:
        logger.error(f"[FetchActual] 失敗: {exc}")
        return {"actual_data": {"error": str(exc)}}


def evaluate_node(state: BacktestState) -> dict:
    """Use Claude to compare prediction vs actual and generate accuracy report."""
    logger.info("[Evaluate] 產生準確度評估報告")

    brief_record = state["brief_record"]
    actual_data  = state["actual_data"]

    if brief_record is None:
        return {"accuracy_report": "⚠️  找不到當日建議書，無法進行回測。請先執行 investment_workflow.py。"}
    if actual_data is None or "error" in actual_data:
        err = actual_data.get("error", "unknown") if actual_data else "unknown"
        return {"accuracy_report": f"⚠️  無法取得真實市場數據：{err}"}

    user_content = (
        f"交易日：{state['trade_date']}\n\n"
        f"【昨日投報建議書（預測）】\n"
        f"預測跳空幅度：{brief_record.get('predicted_gap_pct')}%\n"
        f"預測跳空方向：{brief_record.get('gap_direction')}\n\n"
        f"建議書全文：\n{brief_record.get('brief_text', '')}\n\n"
        f"【真實走勢數據】\n{json.dumps(actual_data, ensure_ascii=False, indent=2)}"
    )

    llm = ChatAnthropic(
        model=MODEL_ID,
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        max_tokens=1024,
    )
    response = llm.invoke([
        SystemMessage(content=_EVAL_SYSTEM),
        HumanMessage(content=user_content),
    ])
    report = response.content.strip()
    logger.success("[Evaluate] 評估報告完成")
    return {"accuracy_report": report}


def build_graph():
    graph = StateGraph(BacktestState)
    graph.add_node("load_brief",   load_brief_node)
    graph.add_node("fetch_actual", fetch_actual_node)
    graph.add_node("evaluate",     evaluate_node)

    graph.add_edge(START,         "load_brief")
    graph.add_edge("load_brief",  "fetch_actual")
    graph.add_edge("fetch_actual","evaluate")
    graph.add_edge("evaluate",    END)

    return graph.compile()


def main():
    logger.info("=" * 62)
    logger.info("  台灣股期分析團隊 — Backtest Agent (自我反省)")
    logger.info("=" * 62)

    if not os.getenv("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY not set — aborting")
        sys.exit(1)

    if len(sys.argv) > 1:
        trade_date = sys.argv[1]
    else:
        # Auto-select most recent brief date (handles weekends / holidays)
        try:
            from database_tools import get_recent_accuracy
            recent = get_recent_accuracy(7)
            trade_date = str(recent[0]["trade_date"]) if recent else (date.today() - timedelta(days=1)).isoformat()
        except Exception:
            trade_date = (date.today() - timedelta(days=1)).isoformat()

    logger.info(f"回測日期：{trade_date}")

    graph = build_graph()
    result = graph.invoke(BacktestState(
        trade_date=trade_date,
        brief_record=None,
        actual_data=None,
        accuracy_report="",
    ))

    print("\n" + "=" * 62)
    print(f"  準確度評估報告 — {trade_date}")
    print("=" * 62)
    print(result["accuracy_report"])
    print("=" * 62 + "\n")


if __name__ == "__main__":
    main()
