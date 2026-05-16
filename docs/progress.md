# AI Agent Studio — 專案進度總覽
**Taiwan Stock Futures Analysis Team**
_更新日期：2026-05-15_

---

## 專案簡介

台灣股票期貨分析 AI Agent 系統，以 LangGraph 編排 8 個 LLM 節點，每日自動採集市場資料、生成投資建議書，並透過 LINE / Telegram 推播給用戶。部署於 Ubuntu 伺服器（10.0.1.20），以 cron 排程自動觸發。

---

## Git 提交歷史

| Hash | 說明 | 時間 |
|------|------|------|
| `153becc` | Add AgentOS registry, tool governance, context injection, and memory Phase 0 | 2026-05-15 |
| `cc776a4` | Add observability & cost governance (Phase 6 telemetry) | 2026-05-15 |
| `ab2cf14` | Add P0 security/stability fixes and Phase 6 LINE portfolio management | 2026-05-15 |
| `d3908be` | Add Phase 5: portfolio monitoring, comprehensive analysis docs (32 docs) | 2026-05-15 |
| `b0d3ac5` | Add Phase 4: model routing, cost analytics, and extended thinking | 2026-05-15 |
| `73f1540` | Add Phase 3: messenger push, dashboard, scheduler, and analysis agents | 2026-05-15 |
| `ca9ef40` | Add AI Agent Studio: MCP servers, LangGraph orchestrator, finance data collector | 2026-05-15 |
| `72580f2` | Initial commit | 2026-05-15 |

---

## 完成階段

### Phase 1：MCP 資料採集 (`ca9ef40`)
- `finance_mcp_server.py`：TAIFEX 三大法人、yfinance 美股、Anue 財經新聞
- `system_inspector.py`：psutil 系統監控
- `test_collection.py`：async MCP 客戶端，輸出 `market_snapshot.json`
- `main.py`：簡易 MCP 呼叫測試

### Phase 2：基礎 LangGraph 工作流 (已含於後期)
- `investment_workflow.py`：StateGraph 建構，串接 8 個節點
- `market_analyst_agents.py`：所有節點邏輯（chip_analyst、tech_analyst、chief_strategist 等）
- `database_tools.py`：TiDB 連線與資料庫操作

### Phase 3：通知推播與儀表板 (`73f1540`)
- `messenger_tools.py`：LINE + Telegram 推播
- `dashboard.py`：Streamlit 可視化儀表板（成本、brief 歷史、投資組合）
- `portfolio_tools.py`：持倉管理工具函式

### Phase 4：模型路由與成本分析 (`b0d3ac5`)
- Haiku / Sonnet / Opus 三層模型路由（依節點複雜度）
- Extended Thinking for chief_strategist（Opus + thinking tokens）
- 成本分析文件 (8 份)

### Phase 5：投資組合監控與深度分析 (`d3908be`)
- `backtest_agent.py`：預測準確率回測
- `agent_orchestrator.py`：多 workflow 協調器
- `twse_fetcher.py`：TWSE 當日行情抓取（用於 backtest）
- 分析文件 32 份（安全、效能、記憶體、基礎建設、成本等）

### P0 安全/穩定性修復 + Phase 6 LINE 持倉管理 (`ab2cf14`)
- `snapshot_integrity.py`：Snapshot HMAC 簽章驗證模組
- `line_webhook.py`：FastAPI LINE Webhook Server（持倉 CRUD 指令）
- `database_tools.py`：`@lru_cache` singleton engine、多用戶 user_id 欄位
- `market_analyst_agents.py`：Opus token cap (4096)、LLM retry with exponential backoff
- `messenger_tools.py`：移除 Telegram `parse_mode: Markdown`、訊息長度截斷
- `mcp_servers/finance_mcp_server.py`：Prompt Injection 輸入清洗 (`_sanitize()`)
- `dashboard.py`：`DASH_PASSWORD` 環境變數認證
- `alert_runner.py`：獨立告警 cron 腳本

### Phase 6 可觀測性（Observability）(`cc776a4`)
- **新資料表**：`workflow_runs`, `workflow_events`, `llm_traces`, `audit_log`
- **欄位擴充**：`cost_logs` 新增 `thinking_tokens`, `run_id`；`daily_briefs` 新增 `UNIQUE KEY uq_trade_date`
- **新模組**：`telemetry.py`（`record_usage()`, `emit_event()`, `timed_invoke()`）
- **告警系統**：`alert_runner.py` 實作 A-001 至 A-009 九個告警
- **修復**：`snapshot_ts VARCHAR(30)` → `VARCHAR(40)`（修正 ISO timestamp 截斷 bug）
- **修復**：`_print_cost_report()` 改回傳 per-run cost（修正 A-004 誤報）

### AgentOS + Memory Architecture (`153becc`)

#### Phase 1：Agent Registry
- `agent_registry.yaml`：10 個 Agent 的完整 metadata（model、tools、memory access、budget、permission、supervisor/reviewer）

#### Phase 2：Tool Governance
- `tool_registry.yaml`：22 個工具盤點（6 MCP + 14 直接呼叫 + 8 LLM invocation）
- 孤兒工具標記：`save_brief_to_db`（HIGH）、`send_brief_to_user`（CRITICAL）
- `database_tools.py`：`tool_audit_log` 資料表、`log_tool_call()`、`validate_tool_permission()`（fail-open）

#### Phase 3：Context Engineering
- `market_analyst_agents.py`：
  - SQL 歷史注入：`get_recent_accuracy_context(days=14)` 注入 chief_strategist prompt
  - 上下文大小限制：`_CTX_LIMIT_CHIEF_HISTORY_CHARS = 800`、`_CTX_LIMIT_PORTFOLIO_CHARS = 3000`
  - Price Stale Flag：偵測 yfinance fallback，發出 `fallback_activated` 事件
- `database_tools.py`：`get_recent_accuracy_context()` 函式（JOIN `daily_briefs + market_actuals`）

#### Phase 4：Memory Phase 0
- `investment_workflow.py`：LangGraph `SqliteSaver` checkpointer（fallback 至 `MemorySaver`）
- `database_tools.py`：`session_episodes` 資料表、`log_session_episode()`（含 regime tag 自動推導）
- `market_analyst_agents.py`：`save_to_db_node` 在 brief 儲存後寫入 `session_episodes`

---

## 目前系統架構

### LangGraph Investment Workflow（8 個節點）

```
START → data_collector → chip_analyst ─┐
                       → tech_analyst  ─┤→ chief_strategist → portfolio_manager
                                        └──────────────────────────────────────→ format_agent → save_to_db → send_notification → END
```

| 節點 | 模型 | 職責 |
|------|------|------|
| data_collector | Haiku 4.5 | 解析 snapshot，提取市場數據 |
| chip_analyst | Sonnet 4.6 | 分析三大法人籌碼 |
| tech_analyst | Sonnet 4.6 | 技術面分析 |
| chief_strategist | Opus 4.7 + Thinking | 統合分析，生成建議書 |
| portfolio_manager | Sonnet 4.6 | 個人持倉損益評估 |
| format_agent | Haiku 4.5 | 格式化 LINE 推播訊息 |
| save_to_db | (無 LLM) | 寫入 TiDB、session_episodes |
| send_notification | (無 LLM) | LINE + Telegram 推播 |

### TiDB 資料庫（`agent_memory`）

| 資料表 | 說明 |
|--------|------|
| `daily_briefs` | 每日投資建議書（UNIQUE trade_date） |
| `market_actuals` | 實際市場結果（backtest 回填） |
| `cost_logs` | 每次 LLM 呼叫的 token 與成本記錄 |
| `user_portfolio` | 用戶持倉（含 line_user_id 多用戶隔離） |
| `workflow_runs` | 每次工作流執行記錄 |
| `workflow_events` | 結構化事件日誌 |
| `llm_traces` | LLM 完整 prompt/response 紀錄 |
| `audit_log` | DB 變更稽核軌跡 |
| `tool_audit_log` | 工具呼叫稽核記錄 |
| `session_episodes` | 每日市場情境快照（Phase 5 向量記憶預備） |

### 程式碼檔案清單

| 檔案 | 類型 | 說明 |
|------|------|------|
| `investment_workflow.py` | 主入口 | LangGraph 圖建構與主執行流程 |
| `market_analyst_agents.py` | 核心 | 8 個節點邏輯 |
| `database_tools.py` | 工具 | TiDB 所有 CRUD 與 schema 管理 |
| `telemetry.py` | 工具 | 遙測 helper（record_usage / emit_event） |
| `messenger_tools.py` | 工具 | LINE / Telegram 推播 |
| `portfolio_tools.py` | 工具 | 持倉計算（get_user_portfolio / calculate_pnl） |
| `snapshot_integrity.py` | 工具 | Snapshot HMAC 簽章/驗證 |
| `test_collection.py` | 採集 | 市場快照採集（MCP async 客戶端） |
| `backtest_agent.py` | 分析 | 預測準確率回測工作流 |
| `agent_orchestrator.py` | 分析 | 多工作流協調器 |
| `alert_runner.py` | 監控 | 9 個告警的獨立 cron 腳本 |
| `dashboard.py` | 介面 | Streamlit 可視化儀表板 |
| `line_webhook.py` | 介面 | FastAPI LINE Webhook Server |
| `twse_fetcher.py` | 採集 | TWSE 當日行情（用於 backtest） |
| `mcp_servers/finance_mcp_server.py` | MCP | 財經資料 MCP Server |
| `mcp_servers/system_inspector.py` | MCP | 系統監控 MCP Server |

---

## 待辦事項

### 安全（高優先）

| 項目 | 風險等級 | 工時 | 說明 |
|------|----------|------|------|
| 移除 `send_brief_to_user` MCP handler | CRITICAL | 2 分鐘 | 孤兒工具，任意 MCP 客戶端可推送訊息至 LINE |
| 加入 `MCP_WRITE_TOKEN` 到 `save_brief_to_db` | HIGH | 5 分鐘 | 防止未授權 MCP 客戶端寫入資料庫 |
| 重新啟用 TWSE fetcher TLS 驗證 | MEDIUM | 15 分鐘 | `twse_fetcher.py` 目前 `verify=False` |

### 成本控制

| 項目 | 說明 |
|------|------|
| 加入 `langgraph-checkpoint-sqlite` 套件 | Server 目前 fallback 至 MemorySaver（in-process，非持久化） |

### Memory Phase 1（尚未實作）

| 項目 | 預期效益 | 工時 |
|------|----------|------|
| Backtest agent 回填 `session_episodes.actual_*` | 啟用歷史準確率計算 | 2 小時 |
| `get_recent_sessions_context(days=10)` | chief_strategist 接收 regime 標記歷史 | 1 小時 |
| 預期準確率提升 | +5–8% direction accuracy | — |

### Memory Phase 2（向量記憶，路線圖）

| 項目 | 預期效益 | 工時 |
|------|----------|------|
| `session_episodes` 向量嵌入（TiDB Vector / Chroma） | 語意相似場景檢索 | 12 小時 |
| 5 筆最相似歷史場景注入 chief_strategist | +10–15% direction accuracy | 3 小時 |

### 其他技術債

| 項目 | 說明 |
|------|------|
| `asyncio.run()` anti-pattern in `maintenance_agent` | 在已有事件迴圈的環境中會崩潰 |
| `investment_workflow.py --resume <run_id>` CLI flag | 目前手動 resume 需修改程式碼 |
| `backtest_evaluator` / `maintenance_agent` cost logging | 這兩個 agent 的 LLM 呼叫未寫入 `cost_logs` |

---

## 驗證狀態（2026-05-15 最後一次完整執行）

- `session_episodes` 資料：`trade_date=2026-05-15, predicted_direction='flat', predicted_gap_pct=0.380, regime_sox='neutral', regime_foreign_oi='bearish', divergence_signal=1`
- 工作流成功完成，無 WARNING 以上事件
- LINE / Telegram 推播正常
- 告警系統：無誤報觸發

---

## 文件索引

詳細分析文件 42 份位於 `docs/` 目錄，參見 [AI_Agent_DOC_index.md](AI_Agent_DOC_index.md)。

實作計畫文件（專案根目錄）：
- [agentos_implementation_plan.md](../agentos_implementation_plan.md) — AgentOS Phase 1–4 實作計畫與 Mermaid 架構圖
- [context_engineering_change_report.md](../context_engineering_change_report.md) — Context Engineering 變更說明
- [memory_phase0_change_report.md](../memory_phase0_change_report.md) — Memory Phase 0 變更說明
- [observability_implementation_plan.md](../observability_implementation_plan.md) — Observability 實作計畫
- [observability_change_report.md](../observability_change_report.md) — Observability 變更說明
