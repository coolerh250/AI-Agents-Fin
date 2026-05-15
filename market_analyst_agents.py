"""
market_analyst_agents.py
Taiwan Stock Futures Analysis Team — Phase 2
Agent nodes: ChipAnalyst, TechnicalAnalyst, ChiefStrategist, SaveToDB
"""
import json
import os
from datetime import date
from typing import Optional, TypedDict

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger

load_dotenv()

MODEL_ID = "claude-haiku-4-5-20251001"


class WorkflowState(TypedDict):
    snapshot:         dict
    chip_report:      str
    tech_report:      str
    final_brief:      str
    db_row_id:        Optional[int]   # set by save_to_db_node after DB write


_CHIP_SYSTEM = """你是台灣期貨市場籌碼專家。根據三大法人留倉數據分析多空力道。
判斷規則：
- 外資 oi_net < -30,000 口 → 極度偏空
- 外資 oi_net 在 -10,000 ~ -30,000 口 → 偏空
- 外資 oi_net 在 0 ~ -10,000 口 → 輕微偏空
- 外資 oi_net > 0 → 偏多
- 若投信 oi_net > 20,000 且外資偏空 → 籌碼面反向指標（法人分歧）
嚴格以 JSON 格式回應（不要加 markdown code block）：
{"sentiment": str, "foreign_net": int, "trust_net": int, "dealer_net": int, "divergence_signal": bool, "reasoning": str}"""

_TECH_SYSTEM = """你是台股技術面專家，根據前一日美股表現預測今日台股開盤跳空方向與力道。
參考指標（對台股的影響權重）：
- DJIA change_pct（權重 20%）
- NASDAQ 100 change_pct（權重 25%）
- PHLX SOX change_pct（權重 30%）
- TSMC ADR change_pct（權重 25%）
跳空判斷基準（加權平均）：
- 加權平均 > +1.5% → 強力跳空高開（預估 +1%~+2%）
- 加權平均 +0.5%~+1.5% → 溫和高開（預估 +0.3%~+1%）
- 加權平均 -0.5%~+0.5% → 平開（預估 ±0.3%）
- 加權平均 < -1.5% → 跳空低開
嚴格以 JSON 格式回應（不要加 markdown code block）：
{"gap_direction": "up"|"flat"|"down", "estimated_gap_pct": float, "key_driver": str, "tsm_signal": str, "reasoning": str}"""

_CHIEF_SYSTEM = """你是台灣股期分析團隊的總合規劃師（Chief Strategist）。
你將收到籌碼面報告與技術面報告，整合為一份「今日投報建議書」。
嚴格遵守以下格式輸出：

【盤勢定調】
（一段話，綜合籌碼與技術面，定義今日台股整體多空方向）

【操作策略】
- 多單策略：（何時進場、目標區間）
- 空單策略：（何時進場、目標區間）
- 觀望條件：（哪些情況應避免操作）

【關鍵防守點】
- 多方防守：（具體指數點位或條件）
- 空方防守：（具體指數點位或條件）

【風險提示】
（一句話提醒主要風險因素）"""


def _llm() -> ChatAnthropic:
    return ChatAnthropic(
        model=MODEL_ID,
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        max_tokens=1024,
    )


def chip_analyst_node(state: WorkflowState) -> dict:
    logger.info("[ChipAnalyst] 開始籌碼分析")
    chip_data = state["snapshot"]["tools"]["get_tw_future_chips"]["data"]
    user_content = f"三大法人台指期留倉數據：\n{json.dumps(chip_data, ensure_ascii=False, indent=2)}"

    response = _llm().invoke([
        SystemMessage(content=_CHIP_SYSTEM),
        HumanMessage(content=user_content),
    ])

    result = response.content.strip()
    logger.success(f"[ChipAnalyst] 完成：{result[:80]}...")
    return {"chip_report": result}


def tech_analyst_node(state: WorkflowState) -> dict:
    logger.info("[TechnicalAnalyst] 開始技術面分析")
    markets = state["snapshot"]["tools"]["get_us_market_summary"]["data"]["markets"]
    user_content = f"昨日美股收盤數據：\n{json.dumps(markets, ensure_ascii=False, indent=2)}"

    response = _llm().invoke([
        SystemMessage(content=_TECH_SYSTEM),
        HumanMessage(content=user_content),
    ])

    result = response.content.strip()
    logger.success(f"[TechnicalAnalyst] 完成：{result[:80]}...")
    return {"tech_report": result}


def chief_strategist_node(state: WorkflowState) -> dict:
    logger.info("[ChiefStrategist] 整合兩份報告，撰寫投報建議書")
    user_content = (
        f"籌碼面報告：\n{state['chip_report']}\n\n"
        f"技術面報告：\n{state['tech_report']}"
    )

    response = _llm().invoke([
        SystemMessage(content=_CHIEF_SYSTEM),
        HumanMessage(content=user_content),
    ])

    result = response.content.strip()
    logger.success("[ChiefStrategist] 建議書撰寫完成")
    return {"final_brief": result}


def save_to_db_node(state: WorkflowState) -> dict:
    """Persist the final brief to TiDB (agent_memory.daily_briefs)."""
    logger.info("[SaveToDB] 寫入 TiDB agent_memory.daily_briefs")
    try:
        from database_tools import save_brief

        brief = state["final_brief"]
        trade_date = date.fromisoformat(
            state["snapshot"]["timestamp"][:10]
        )

        # Extract gap info from tech_report JSON (strip markdown fences if present)
        gap_pct: Optional[float] = None
        gap_dir: Optional[str] = None
        try:
            raw = state["tech_report"].strip()
            if raw.startswith("```"):
                raw = raw.split("```", 2)[1]           # drop opening fence + lang tag
                raw = raw[raw.index("\n") + 1:] if "\n" in raw else raw
            tech = json.loads(raw)
            gap_pct = float(tech.get("estimated_gap_pct", 0))
            gap_dir = tech.get("gap_direction")
        except Exception:
            pass

        row_id = save_brief(trade_date, brief, gap_pct, gap_dir)
        logger.success(f"[SaveToDB] 寫入成功 row_id={row_id}, date={trade_date}")
        return {"db_row_id": row_id}
    except Exception as exc:
        logger.error(f"[SaveToDB] 寫入失敗: {exc}")
        return {"db_row_id": None}
