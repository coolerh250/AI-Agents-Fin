"""
market_analyst_agents.py
Taiwan Stock Futures Analysis Team — Phase 4
Model routing: Haiku (collector/format) · Sonnet (analysts) · Opus+Thinking (strategist)
"""
import json
import os
import time
from datetime import date
from typing import Optional, TypedDict

import anthropic as _anthropic
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger

load_dotenv()

_OWNER_LINE_ID: Optional[str] = os.getenv("LINE_USER_ID") or None

# Phase 3: context size limits (chars) to guard against runaway input
_CTX_LIMIT_CHIEF_HISTORY_CHARS  = 800   # max chars for injected SQL history
_CTX_LIMIT_CHIEF_LESSONS_CHARS  = 600   # max chars for strategy lessons context
_CTX_LIMIT_CHIEF_NEWS_CHARS     = 800   # max chars for financial news headlines
_CTX_LIMIT_PORTFOLIO_CHARS      = 3000  # max chars for portfolio PnL block

_MODEL_HAIKU  = "claude-haiku-4-5-20251001"
_MODEL_SONNET = "claude-sonnet-4-6"
_MODEL_OPUS   = "claude-opus-4-7"

_PRICING = {  # USD per 1M tokens
    _MODEL_HAIKU:  {"input": 1.00, "output":  5.00},
    _MODEL_SONNET: {"input": 3.00, "output": 15.00},
    _MODEL_OPUS:   {"input": 5.00, "output": 25.00},
}


class WorkflowState(TypedDict):
    run_id:           str             # UUID correlation key — set in investment_workflow.main()
    snapshot:         dict
    raw_market_data:  dict            # compact summary from data_collector
    chip_report:      str
    tech_report:      str
    final_brief:      str             # verbose output from chief_strategist
    final_report:     str             # LINE-formatted output from format_agent
    db_row_id:        Optional[int]
    portfolio_advice: str


# ── System prompts ────────────────────────────────────────────────────────────

_COLLECTOR_SYSTEM = """你是資料前處理 Agent。
從原始快照中提取關鍵數值，以 JSON 回應（不加 code block）：
{"foreign_oi_long": int, "foreign_oi_short": int, "foreign_oi_net": int,
 "trust_oi_long": int, "trust_oi_short": int, "trust_oi_net": int,
 "dealer_oi_long": int, "dealer_oi_short": int, "dealer_oi_net": int,
 "djia_chg_pct": float, "ndx_chg_pct": float,
 "sox_chg_pct": float, "tsm_adr_chg_pct": float,
 "night_futures_chg_pct": float_or_null,
 "data_ok": bool, "missing_fields": []}
若快照中 get_tw_night_futures 有 error 欄位或資料缺失，night_futures_chg_pct 填 null。"""

_CHIP_SYSTEM = """你是台灣期貨市場籌碼專家。根據三大法人留倉數據分析多空力道。
判斷規則（net 部位）：
- 外資 oi_net < -30,000 口 → 極度偏空
- 外資 oi_net 在 -10,000 ~ -30,000 口 → 偏空
- 外資 oi_net 在 0 ~ -10,000 口 → 輕微偏空
- 外資 oi_net > 0 → 偏多
- 若投信 oi_net > 20,000 且外資偏空 → 籌碼面反向指標（法人分歧）
空方比率分析（若有 oi_long / oi_short）：
- 外資空方比率 = oi_short / (oi_long + oi_short)
  - > 0.60 → 主動加空、空方主導（看空信心強）
  - 0.40 ~ 0.60 → 多空均衡
  - < 0.40 → 多方主導
- 多空雙增（oi_long ↑ 且 oi_short ↑）→ 分歧加劇，市場不確定性高
嚴格以 JSON 格式回應（不要加 markdown code block）：
{"sentiment": str, "foreign_oi_net": int, "foreign_short_ratio": float, "trust_net": int, "dealer_net": int, "divergence_signal": bool, "reasoning": str}"""

_TECH_SYSTEM = """你是台股技術面專家，根據前一日美股表現與台指期夜盤數據預測今日台股開盤跳空方向與力道。

【若有台指期夜盤資料（night_futures_chg_pct 不為 null）】
使用以下權重（夜盤為最直接開盤信號）：
- 台指期夜盤 change_pct（權重 40%）← 最直接的台股開盤前信號
- PHLX SOX change_pct（權重 20%）
- NASDAQ 100 change_pct（權重 18%）
- TSMC ADR change_pct（權重 14%）
- DJIA change_pct（權重 8%）
若夜盤與美股方向相反，應以夜盤為主並在 reasoning 說明背離原因。

【若無夜盤資料（night_futures_chg_pct 為 null）】
使用以下權重：
- PHLX SOX change_pct（權重 30%）
- NASDAQ 100 change_pct（權重 25%）
- TSMC ADR change_pct（權重 25%）
- DJIA change_pct（權重 20%）

跳空判斷基準（加權平均）：
- 加權平均 > +1.5% → 強力跳空高開（預估 +1%~+2%）
- 加權平均 +0.5%~+1.5% → 溫和高開（預估 +0.3%~+1%）
- 加權平均 -0.5%~+0.5% → 平開（預估 ±0.3%）
- 加權平均 < -1.5% → 跳空低開
嚴格以 JSON 格式回應（不要加 markdown code block）：
{"gap_direction": "up"|"flat"|"down", "estimated_gap_pct": float, "key_driver": str, "tsm_signal": str, "night_futures_used": bool, "reasoning": str}"""

_CHIEF_SYSTEM = """你是台灣股期分析團隊的總合規劃師（Chief Strategist）。
你將收到籌碼面報告與技術面報告，整合為一份「今日投報建議書」。

輸入訊息末尾可能附帶以下補充資訊，請主動運用：
- 【近期預測準確率】：過去 14 天方向預測記錄。若準確率低於 50%，須在盤勢定調中降低方向確信度，並說明謹慎原因。
- 【歷史策略教訓】：過去相似市場環境下的失誤分類（方向誤判、過度自信、資料陳舊等）。若有相關教訓，請在分析中明確點出「本次注意：……」並調整判斷，避免重蹈相同錯誤。
- 【今日財經新聞標題】：台灣股票相關新聞標題列表。用於識別重大事件（法說會、政策公告、產業消息）對盤面的潛在影響，但不得逐條列舉，應整合進分析論述中。

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

_PORTFOLIO_SYSTEM = """你是一個私人資產顧問，請結合 ChiefStrategist 的今日展望與使用者的 Portfolio 資料。
如果大盤看空且個股跌破 stop_loss_level，請產出明確的『今日賣出建議』。
如果一切正常，請產出『建議續抱，壓力位在 X，支撐位在 Y』。
請針對每筆持股給出具體建議，格式如下：
【股票代碼：XXXX】
- 現價：XXX 元（成本：XXX 元，損益：±X.X%）
- 建議動作：（買入/續抱/減碼/賣出）
- 原因：（一句話）"""

_FORMAT_SYSTEM = """你是 LINE 推播格式化 Agent。
將投報建議書重新排版為適合手機閱讀的 LINE 訊息：
- 總長度不超過 2000 字
- 每個段落前加上合適的 emoji（📊 盤勢、⚔️ 策略、🛡️ 防守、⚠️ 風險）
- 保留原文核心內容，去除冗餘文字
- 直接輸出格式化後的訊息，不加任何說明
- 若使用者持股診斷內容不為空，請在訊息末尾加入【個人持股診斷】段落（emoji: 💼），並原文放入診斷內容
- 若持股診斷內容為空，略過【個人持股診斷】段落"""


# ── LLM factories ─────────────────────────────────────────────────────────────

_RETRY_ERRORS = (
    _anthropic.RateLimitError,
    _anthropic.APITimeoutError,
    _anthropic.APIConnectionError,
)


def _llm(model: str, max_tokens: int = 1024) -> ChatAnthropic:
    return ChatAnthropic(
        model=model,
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        max_tokens=max_tokens,
    ).with_retry(
        stop_after_attempt=3,
        wait_exponential_jitter=True,
        retry_if_exception_type=_RETRY_ERRORS,
    )


def _llm_opus() -> ChatAnthropic:
    return ChatAnthropic(
        model=_MODEL_OPUS,
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        max_tokens=4096,           # reduced from 16000 for cost control
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
    ).with_retry(
        stop_after_attempt=3,
        wait_exponential_jitter=True,
        retry_if_exception_type=_RETRY_ERRORS,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_text(response) -> str:
    c = response.content
    if isinstance(c, str):
        return c.strip()
    return "\n".join(
        b["text"] for b in c if isinstance(b, dict) and b.get("type") == "text"
    ).strip()


def _record_usage(
    agent_name: str,
    model: str,
    response,
    latency_ms: int,
    run_id: Optional[str] = None,
    system_prompt: str = "",
    user_content: str = "",
) -> None:
    from telemetry import record_usage
    record_usage(
        agent_name=agent_name,
        model=model,
        response=response,
        latency_ms=latency_ms,
        run_id=run_id,
        system_prompt=system_prompt,
        user_content=user_content,
        pricing=_PRICING,
    )


# ── Node: DataCollector (Haiku) ───────────────────────────────────────────────

def data_collector_node(state: WorkflowState) -> dict:
    from telemetry import emit_event
    run_id = state.get("run_id")
    logger.info("[DataCollector] 提取關鍵市場數值")
    snapshot = state["snapshot"]
    user_content = f"原始市場快照：\n{json.dumps(snapshot['tools'], ensure_ascii=False, indent=2)}"

    start = time.monotonic()
    response = _llm(_MODEL_HAIKU).invoke([
        SystemMessage(content=_COLLECTOR_SYSTEM),
        HumanMessage(content=user_content),
    ])
    latency_ms = int((time.monotonic() - start) * 1000)
    _record_usage("data_collector", _MODEL_HAIKU, response, latency_ms,
                  run_id=run_id, system_prompt=_COLLECTOR_SYSTEM, user_content=user_content)

    raw_text = _extract_text(response)
    try:
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```", 2)[1]
            raw_text = raw_text[raw_text.index("\n") + 1:] if "\n" in raw_text else raw_text
        raw_market_data = json.loads(raw_text)
    except Exception:
        logger.warning("[DataCollector] JSON 解析失敗，使用空 dict")
        raw_market_data = {}
        emit_event(run_id, "fallback_activated", "data_collector",
                   {"reason": "json_parse_failed", "raw_text_length": len(raw_text)},
                   severity="warn")

    logger.success(f"[DataCollector] 完成 data_ok={raw_market_data.get('data_ok', '?')}")
    return {"raw_market_data": raw_market_data}


# ── Node: ChipAnalyst (Sonnet) ────────────────────────────────────────────────

def chip_analyst_node(state: WorkflowState) -> dict:
    from telemetry import emit_event
    run_id = state.get("run_id")
    logger.info("[ChipAnalyst] 開始籌碼分析")
    raw = state.get("raw_market_data") or {}
    _chip_keys = (
        "foreign_oi_long", "foreign_oi_short", "foreign_oi_net",
        "trust_oi_long", "trust_oi_short", "trust_oi_net",
        "dealer_oi_long", "dealer_oi_short", "dealer_oi_net",
    )
    chip_data = {k: raw[k] for k in _chip_keys if k in raw}
    if not chip_data:
        chip_data = state["snapshot"]["tools"]["get_tw_future_chips"]["data"]
        emit_event(run_id, "fallback_activated", "chip_analyst",
                   {"reason": "data_collector_empty",
                    "raw_bytes": len(json.dumps(chip_data))}, severity="warn")
        logger.warning(f"[ChipAnalyst] Fallback: raw snapshot {len(json.dumps(chip_data))} bytes")
    user_content = f"三大法人台指期留倉數據：\n{json.dumps(chip_data, ensure_ascii=False, indent=2)}"

    start = time.monotonic()
    response = _llm(_MODEL_SONNET).invoke([
        SystemMessage(content=_CHIP_SYSTEM),
        HumanMessage(content=user_content),
    ])
    latency_ms = int((time.monotonic() - start) * 1000)
    _record_usage("chip_analyst", _MODEL_SONNET, response, latency_ms,
                  run_id=run_id, system_prompt=_CHIP_SYSTEM, user_content=user_content)

    result = _extract_text(response)
    logger.success(f"[ChipAnalyst] 完成：{result[:80]}...")
    return {"chip_report": result}


# ── Node: TechnicalAnalyst (Sonnet) ──────────────────────────────────────────

def tech_analyst_node(state: WorkflowState) -> dict:
    from telemetry import emit_event
    run_id = state.get("run_id")
    logger.info("[TechnicalAnalyst] 開始技術面分析")
    raw = state.get("raw_market_data") or {}
    us_keys = ("djia_chg_pct", "ndx_chg_pct", "sox_chg_pct", "tsm_adr_chg_pct", "night_futures_chg_pct")
    us_data = {k: raw[k] for k in us_keys if k in raw and raw[k] is not None}
    if not us_data:
        us_data = state["snapshot"]["tools"]["get_us_market_summary"]["data"]["markets"]
        emit_event(run_id, "fallback_activated", "tech_analyst",
                   {"reason": "data_collector_empty",
                    "raw_bytes": len(json.dumps(us_data))}, severity="warn")
        logger.warning(f"[TechnicalAnalyst] Fallback: raw snapshot {len(json.dumps(us_data))} bytes")
    user_content = f"技術面數據：\n{json.dumps(us_data, ensure_ascii=False, indent=2)}"

    start = time.monotonic()
    response = _llm(_MODEL_SONNET).invoke([
        SystemMessage(content=_TECH_SYSTEM),
        HumanMessage(content=user_content),
    ])
    latency_ms = int((time.monotonic() - start) * 1000)
    _record_usage("tech_analyst", _MODEL_SONNET, response, latency_ms,
                  run_id=run_id, system_prompt=_TECH_SYSTEM, user_content=user_content)

    result = _extract_text(response)
    logger.success(f"[TechnicalAnalyst] 完成：{result[:80]}...")
    return {"tech_report": result}


# ── Node: ChiefStrategist (Opus + Extended Thinking) ─────────────────────────

def chief_strategist_node(state: WorkflowState) -> dict:
    run_id = state.get("run_id")
    logger.info("[ChiefStrategist] 整合兩份報告，撰寫投報建議書（Opus + Extended Thinking）")
    user_content = (
        f"籌碼面報告：\n{state['chip_report']}\n\n"
        f"技術面報告：\n{state['tech_report']}"
    )

    # Phase 3: inject recent accuracy history so strategist can self-correct
    try:
        from database_tools import get_recent_accuracy_context
        history = get_recent_accuracy_context(days=14)
        if history:
            history = history[:_CTX_LIMIT_CHIEF_HISTORY_CHARS]
            user_content += f"\n\n{history}"
    except Exception as exc:
        logger.debug(f"[ChiefStrategist] 歷史上下文載入失敗（略過）: {exc}")

    # Adaptive Flywheel Phase 1: inject regime-matched strategy lessons
    try:
        from lesson_retriever import get_lesson_context
        lessons = get_lesson_context(
            state.get("raw_market_data") or {},
            limit=3,
            max_chars=_CTX_LIMIT_CHIEF_LESSONS_CHARS,
        )
        if lessons:
            user_content += f"\n\n{lessons}"
    except Exception as exc:
        logger.debug(f"[ChiefStrategist] strategy lessons 載入失敗（略過）: {exc}")

    # 方案一: inject financial news headlines from snapshot
    try:
        news_data = (state.get("snapshot") or {}).get("tools", {}).get("get_financial_news", {})
        news_items = (news_data.get("data") or {}).get("news", [])
        if news_items and not news_data.get("data", {}).get("error"):
            headlines = "\n".join(f"・{item['title']}" for item in news_items[:10] if item.get("title"))
            if headlines:
                news_block = f"【今日財經新聞標題】\n{headlines}"
                if len(news_block) > _CTX_LIMIT_CHIEF_NEWS_CHARS:
                    news_block = news_block[:_CTX_LIMIT_CHIEF_NEWS_CHARS].rsplit("\n", 1)[0]
                user_content += f"\n\n{news_block}"
    except Exception as exc:
        logger.debug(f"[ChiefStrategist] 新聞資料載入失敗（略過）: {exc}")

    start = time.monotonic()
    response = _llm_opus().invoke([
        SystemMessage(content=_CHIEF_SYSTEM),
        HumanMessage(content=user_content),
    ])
    latency_ms = int((time.monotonic() - start) * 1000)
    _record_usage("chief_strategist", _MODEL_OPUS, response, latency_ms,
                  run_id=run_id, system_prompt=_CHIEF_SYSTEM, user_content=user_content)

    result = _extract_text(response)
    logger.success("[ChiefStrategist] 建議書撰寫完成")
    return {"final_brief": result}


# ── Node: PortfolioManager (Sonnet) ──────────────────────────────────────────

def portfolio_manager_node(state: WorkflowState) -> dict:
    run_id = state.get("run_id")
    logger.info("[PortfolioManager] 載入持倉並計算損益")
    from portfolio_tools import get_user_portfolio, calculate_pnl

    holdings = get_user_portfolio(user_id=_OWNER_LINE_ID)
    if not holdings:
        logger.info("[PortfolioManager] 無持倉資料，略過分析")
        return {"portfolio_advice": ""}

    # Phase 4: emit price_stale event if yfinance fails for any holding
    try:
        from telemetry import emit_event
        enriched = calculate_pnl(holdings)
        stale = [h["stock_id"] for h in enriched
                 if h.get("current_price") is None or h.get("current_price") == h.get("entry_price")]
        if stale:
            logger.warning(f"[PortfolioManager] 現價可能為舊資料：{stale}")
            emit_event(run_id, "fallback_activated", "portfolio_manager",
                       {"reason": "price_stale", "stocks": stale}, severity="warn")
    except Exception:
        enriched = holdings  # bare fallback if calculate_pnl itself raises

    from database_tools import get_stock_name
    pnl_lines = [
        f"股票代碼: {h['stock_id']} {get_stock_name(h['stock_id']) or ''} | 成本: {h['entry_price']} | 現價: {h.get('current_price', h['entry_price']):.2f} | "
        f"持股數: {h['quantity']} 股 | 損益: {h.get('unrealized_pnl', 0):.2f} ({h.get('pnl_pct', 0):.2f}%) | "
        f"止損觸發: {'是' if h.get('stop_loss_triggered') else '否'} | 策略: {h['strategy_type']}"
        for h in enriched
    ]
    portfolio_block = "\n".join(pnl_lines)[:_CTX_LIMIT_PORTFOLIO_CHARS]
    user_content = (
        f"今日市場展望：\n{state['final_brief']}\n\n"
        f"使用者持倉損益：\n{portfolio_block}"
    )

    start = time.monotonic()
    response = _llm(_MODEL_SONNET, max_tokens=1024).invoke([
        SystemMessage(content=_PORTFOLIO_SYSTEM),
        HumanMessage(content=user_content),
    ])
    latency_ms = int((time.monotonic() - start) * 1000)
    _record_usage("portfolio_manager", _MODEL_SONNET, response, latency_ms,
                  run_id=run_id, system_prompt=_PORTFOLIO_SYSTEM, user_content=user_content)

    result = _extract_text(response)
    logger.success("[PortfolioManager] 持股診斷完成")
    return {"portfolio_advice": result}


# ── Node: FormatAgent (Haiku) ─────────────────────────────────────────────────

def format_agent_node(state: WorkflowState) -> dict:
    run_id = state.get("run_id")
    logger.info("[FormatAgent] 格式化為 LINE 推播格式")
    portfolio_section = state.get("portfolio_advice", "")
    user_content = f"原始建議書：\n{state['final_brief']}"
    if portfolio_section:
        user_content += f"\n\n使用者持股診斷：\n{portfolio_section}"

    start = time.monotonic()
    response = _llm(_MODEL_HAIKU, max_tokens=2048).invoke([
        SystemMessage(content=_FORMAT_SYSTEM),
        HumanMessage(content=user_content),
    ])
    latency_ms = int((time.monotonic() - start) * 1000)
    _record_usage("format_agent", _MODEL_HAIKU, response, latency_ms,
                  run_id=run_id, system_prompt=_FORMAT_SYSTEM, user_content=user_content)

    result = _extract_text(response)
    logger.success("[FormatAgent] LINE 格式化完成")
    return {"final_report": result}


# ── Node: SendNotification (no LLM) ──────────────────────────────────────────

def send_notification_node(state: WorkflowState) -> dict:
    from telemetry import emit_event
    run_id = state.get("run_id")
    logger.info("[SendNotification] 推播 LINE / Telegram")

    report = state.get("final_report", "")
    if not report:
        logger.warning("[SendNotification] final_report 為空，略過推播")
        emit_event(run_id, "node_failure", "send_notification",
                   {"reason": "empty_final_report"}, severity="error")
        return {}

    try:
        from utils.mcp_call import call_mcp_tool_sync
        result = call_mcp_tool_sync(
            server_script="mcp_servers/notification_server.py",
            tool_name="push_investment_brief",
            arguments={
                "brief_text": report,
                "api_key":    os.getenv("MCP_NOTIFY_TOKEN", ""),
            },
        )
        if result.get("dedup_skipped"):
            logger.info("[SendNotification] 今日已推播（dedup），略過")
            emit_event(run_id, "delivery_dedup", "send_notification",
                       {"reason": "already_sent_today"}, severity="info")
            return {}
        if result.get("error") == "unauthorized":
            logger.warning("[SendNotification] MCP_NOTIFY_TOKEN 未設定或錯誤，改用直接呼叫")
            raise RuntimeError("unauthorized")
        for channel in ("line", "telegram"):
            res = result.get(channel, {})
            status = res.get("status")
            if status == "ok":
                logger.success(f"[SendNotification] {channel} 推播成功")
                emit_event(run_id, "delivery_success", "send_notification",
                           {"channel": channel}, severity="info")
            elif status == "skipped":
                logger.info(f"[SendNotification] {channel} 略過（{res.get('reason', '')}）")
            else:
                logger.warning(f"[SendNotification] {channel} 推播失敗：{res.get('error', '')}")
                emit_event(run_id, "delivery_failure", "send_notification",
                           {"channel": channel, "error": res.get("error", "")}, severity="error")
    except Exception as mcp_exc:
        logger.warning(f"[SendNotification] MCP call 失敗，改用直接呼叫: {mcp_exc}")
        from messenger_tools import send_line, send_telegram
        results = {"line": send_line(report), "telegram": send_telegram(report)}
        for channel, res in results.items():
            status = res.get("status")
            if status == "ok":
                logger.success(f"[SendNotification] {channel} 推播成功（直接呼叫）")
                emit_event(run_id, "delivery_success", "send_notification",
                           {"channel": channel}, severity="info")
            elif status != "skipped":
                logger.warning(f"[SendNotification] {channel} 推播失敗：{res.get('error', '')}")
                emit_event(run_id, "delivery_failure", "send_notification",
                           {"channel": channel, "error": res.get("error", "")}, severity="error")
    return {}


# ── Node: SaveToDB (no LLM) ───────────────────────────────────────────────────

def save_to_db_node(state: WorkflowState) -> dict:
    from telemetry import emit_event
    run_id = state.get("run_id")
    logger.info("[SaveToDB] 寫入 TiDB agent_memory.daily_briefs")
    try:
        brief = state["final_brief"]
        trade_date = date.fromisoformat(state["snapshot"]["timestamp"][:10])

        gap_pct: Optional[float] = None
        gap_dir: Optional[str] = None
        try:
            raw = state["tech_report"].strip()
            if raw.startswith("```"):
                raw = raw.split("```", 2)[1]
                raw = raw[raw.index("\n") + 1:] if "\n" in raw else raw
            tech = json.loads(raw)
            gap_pct = float(tech.get("estimated_gap_pct", 0))
            gap_dir = tech.get("gap_direction")
        except Exception:
            emit_event(run_id, "output_invalid", "tech_analyst",
                       {"reason": "json_parse_failed_in_save_to_db"}, severity="warn")

        row_id = 0
        line_report = state.get("final_report") or None
        try:
            from utils.mcp_call import call_mcp_tool_sync
            result = call_mcp_tool_sync(
                server_script="mcp_servers/persistence_server.py",
                tool_name="save_brief",
                arguments={
                    "trade_date":        str(trade_date),
                    "brief_text":        brief,
                    "predicted_gap_pct": gap_pct,
                    "gap_direction":     gap_dir,
                    "line_report":       line_report,
                    "api_key":           os.getenv("MCP_WRITE_TOKEN", ""),
                },
            )
            if not result.get("success"):
                logger.warning(f"[SaveToDB] persistence_server 回傳錯誤: {result.get('error')}")
            row_id = result.get("row_id", 0) or 0
        except Exception as mcp_exc:
            logger.warning(f"[SaveToDB] MCP call 失敗，改用直接呼叫: {mcp_exc}")
            from database_tools import save_brief as _save_brief
            row_id = _save_brief(trade_date, brief, gap_pct, gap_dir, line_report)

        logger.success(f"[SaveToDB] 寫入成功 row_id={row_id}, date={trade_date}")
        emit_event(run_id, "node_success", "save_to_db",
                   {"row_id": row_id, "trade_date": str(trade_date)}, severity="info")

        # Phase 4: log structured session episode for future context injection
        try:
            from database_tools import log_session_episode
            raw = state.get("raw_market_data") or {}
            chip_div: Optional[bool] = None
            try:
                chip_parsed = json.loads(state.get("chip_report", "{}").strip())
                chip_div = bool(chip_parsed.get("divergence_signal", False))
            except Exception:
                pass
            log_session_episode(
                run_id=run_id or "",
                trade_date=trade_date,
                brief_id=row_id,
                predicted_direction=gap_dir,
                predicted_gap_pct=gap_pct,
                foreign_oi_net=raw.get("foreign_oi_net"),
                trust_oi_net=raw.get("trust_oi_net"),
                dealer_oi_net=raw.get("dealer_oi_net"),
                djia_chg_pct=raw.get("djia_chg_pct"),
                ndx_chg_pct=raw.get("ndx_chg_pct"),
                sox_chg_pct=raw.get("sox_chg_pct"),
                tsm_adr_chg_pct=raw.get("tsm_adr_chg_pct"),
                divergence_signal=chip_div,
            )
        except Exception as exc:
            logger.debug(f"[SaveToDB] session_episode 寫入略過: {exc}")

        return {"db_row_id": row_id}
    except Exception as exc:
        logger.error(f"[SaveToDB] 寫入失敗: {exc}")
        emit_event(run_id, "node_failure", "save_to_db",
                   {"exception": str(exc)}, severity="error")
        return {"db_row_id": None}
