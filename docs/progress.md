# AI Agent Studio — 專案進度總覽
**Taiwan Stock Futures Analysis Team**
_更新日期：2026-05-19_

---

## 專案簡介

台灣股票期貨分析 AI Agent 系統，以 LangGraph 編排 8 個 LLM 節點，每日自動採集市場資料、生成投資建議書，並透過 LINE / Telegram 推播給用戶。部署於 Ubuntu 伺服器（10.0.1.20），以 cron 排程自動觸發。

---

## Git 提交歷史（最近 25 筆）

| Hash | 說明 | 時間 |
|------|------|------|
| `9635310` | Fix dashboard not loading .env: add explicit load_dotenv() | 2026-05-19 |
| `1df7c10` | Add LINE OTP login and multi-user personalized portfolio analysis | 2026-05-19 |
| `549c225` | Fix portfolio data loss: use LINE_USER_ID for all dashboard read/write ops | 2026-05-19 |
| `8f3bdc7` | Fix LINE messages: remove double format_brief() and eliminate duplicate send | 2026-05-18 |
| `cff8794` | Fix daily_run.sh Step 3: send line_report instead of brief_text | 2026-05-18 |
| `2ede700` | Add individual stock supplementary data to portfolio manager | 2026-05-18 |
| `4e34881` | Add three analysis accuracy improvements | 2026-05-18 |
| `c6cfe71` | Fix portfolio entry_price editing: enable cost field in dashboard | 2026-05-18 |
| `c06bd9a` | Fix portfolio_manager: inject company name to prevent LLM hallucination | 2026-05-18 |
| `12cff88` | Persist line_report (LINE-formatted brief) to daily_briefs for reliable resend | 2026-05-17 |
| `d2641f9` | Fix A-003 false positive: log delivery_dedup event on dedup skip | 2026-05-17 |
| `cc72f3f` | Fix A-002 false positive; raise A-004 cost threshold to $0.25 | 2026-05-17 |
| `a14e949` | Add stock_info sync, fix A-002 MCP write token bug | 2026-05-17 |
| `a5de724` | Fix TWSE TLS, backfill session_episodes actuals, add cost logging | 2026-05-17 |
| `029c738` | Add MCP Governance Phase 1 (1A–1D): server split, auth, audit, workflow integration | 2026-05-16 |
| `69bc3c5` | Update chief_strategist prompt to use accuracy history and lessons | 2026-05-16 |
| `b930015` | Fix quality_score and direction_correct regex for markdown table output | 2026-05-16 |
| `81aeb18` | Fix daily_briefs duplicate rows and enforce unique trade_date | 2026-05-16 |
| `b04f643` | Add Flywheel Phase 2: LLM lesson quality scoring | 2026-05-16 |
| `42da7da` | Fix foreign_net/foreign_oi_net naming inconsistency | 2026-05-16 |
| `812af40` | Add Flywheel dashboard tab and stats DB functions | 2026-05-16 |
| `e67e5b1` | Wire evaluation and flywheel cleanup into daily_run.sh; add A-010 alert | 2026-05-16 |
| `fab7753` | Add Agent Evaluation Framework and Adaptive Data Flywheel Phase 1 | 2026-05-16 |
| `153becc` | Add AgentOS registry, tool governance, context injection, and memory Phase 0 | 2026-05-15 |
| `cc776a4` | Add observability & cost governance (Phase 6 telemetry) | 2026-05-15 |

---

## 完成階段

### Phase 1：MCP 資料採集
- `finance_mcp_server.py`：TAIFEX 三大法人、yfinance 美股、Anue 財經新聞
- `system_inspector.py`：psutil 系統監控
- `test_collection.py`：async MCP 客戶端，輸出 `market_snapshot.json`

### Phase 2–3：LangGraph 工作流 + 通知推播 + 儀表板
- `investment_workflow.py`：StateGraph 建構，串接 8 個節點
- `market_analyst_agents.py`：所有節點邏輯
- `messenger_tools.py`：LINE + Telegram 推播
- `dashboard.py`：Streamlit 可視化儀表板
- `portfolio_tools.py`：持倉管理工具函式

### Phase 4–5：模型路由、回測、多工作流協調
- Haiku / Sonnet / Opus 三層模型路由
- Extended Thinking for chief_strategist（Opus 4.7 + thinking tokens）
- `backtest_agent.py`：預測準確率回測
- `agent_orchestrator.py`：多 workflow 協調器
- `twse_fetcher.py`：TWSE 當日行情抓取

### Phase 6：可觀測性 + LINE 持倉管理
- 資料表：`workflow_runs`, `workflow_events`, `llm_traces`, `audit_log`, `tool_audit_log`
- `telemetry.py`：`record_usage()`, `emit_event()`, `timed_invoke()`
- `line_webhook.py`：FastAPI LINE Webhook Server（持倉 CRUD 指令）
- `alert_runner.py`：A-001 至 A-009 九個告警

### AgentOS：Registry + Tool Governance + Context Engineering + Memory Phase 0 (`153becc`)
- `agent_registry.yaml`：10 個 Agent 的 metadata
- `tool_registry.yaml`：22 個工具盤點（含孤兒工具標記）
- `tool_audit_log` 資料表 + `log_tool_call()` + `validate_tool_permission()`
- chief_strategist SQL 歷史注入（`get_recent_accuracy_context(days=14)`）
- `session_episodes` 資料表 + `log_session_episode()`（含 regime tag 自動推導）

### MCP Governance Phase 1 (`029c738`)
- MCP Server 分割：`finance_mcp_server.py` 只讀，寫入 MCP 獨立端點
- `MCP_WRITE_TOKEN` Bearer Token 認證防止未授權資料庫寫入
- MCP 呼叫稽核整合至 `tool_audit_log`
- 工作流整合測試通過

### Agent Evaluation Framework (`fab7753`)
- `evaluation_runner.py`：CLI 評估腳本（支援 `--date` 參數）
- `evaluation_metrics.py`：Rule-based per-agent 評分（無 LLM 呼叫）
- `evaluation_metrics.md`：評分方法說明文件
- 資料表：`eval_runs`, `eval_results`
- `daily_run.sh` Step 4 整合：每日自動評估

### Adaptive Data Flywheel (`fab7753`, `b04f643`, `812af40`, `e67e5b1`)
- **Phase 1**：`lesson_writer.py` — 從 backtest 報告提取策略教訓，寫入 `strategy_lessons`
- **Phase 2**：LLM 品質評分（Haiku 評分 0–10，過濾低品質教訓）
- `lesson_retriever.py`：依 regime 標記檢索相符教訓，注入 chief_strategist prompt
- Dashboard 新增 Flywheel Tab（教訓統計、品質分佈）
- `cleanup_expired_lessons()`：清理過期教訓（TTL = 90 天）
- `daily_run.sh` Step 5 整合：每日清理過期教訓
- A-010 告警：教訓品質低於閾值時觸發

### LINE ID 多使用者系統 (`549c225`, `1df7c10`, `9635310`)
- **問題**：Dashboard 使用固定密碼 + 固定 LINE_USER_ID，所有人看同份持倉
- **解決方案**：LINE OTP 登入流程

  ```
  用戶 → LINE Bot 傳「登入代碼」→ 8 碼 token（5 分鐘 TTL）
  用戶 → Dashboard 輸入代碼 → consume_login_token() → line_user_id
  Session → 各瀏覽器 session 獨立，只看到自己的持倉
  ```

- `login_tokens` 資料表（token、line_user_id、expires_at、used）
- `create_login_token()` / `consume_login_token()` / `get_all_portfolio_users()`
- Dashboard 雙 Tab 登入（LINE OTP + 管理員密碼）+ sidebar 登出按鈕
- `generate_portfolio_analysis_for_user()`：每日對非擁有者用戶生成個人化持股分析
- `send_line_to_user()`：推送至指定 LINE user_id
- `daily_run.sh` Step 2.5：向所有非擁有者用戶發送個人化分析

### 修復與品質改善（2026-05-17–19）
- **持倉資料遺失**：修正 Dashboard 寫 NULL `line_user_id` 導致 seed 後資料消失（`549c225`）
- **DASH_PASSWORD 未載入**：Dashboard 補加 `load_dotenv()`（`9635310`）
- **LINE 訊息重複推播**：移除 `send_notification_node` 中的多餘 `format_brief()`（`8f3bdc7`）
- **建議書重送**：`line_report` 欄位持久化至 DB 供 fallback 使用（`12cff88`）
- **A-002/A-003 誤報**：修正告警計算邏輯（`cc72f3f`, `d2641f9`）
- **TWSE TLS**：重新啟用 TLS 驗證，`verify=False` 已移除（`a5de724`）
- **LLM 幻覺**：股票中文名稱從 DB 注入 portfolio_manager prompt（`c06bd9a`）

---

## 目前系統架構

### LangGraph Investment Workflow（8 個節點）

```
START → data_collector → chip_analyst ─┐
                       → tech_analyst  ─┤→ chief_strategist → portfolio_manager
                                        └────────────────────────────────────────→ format_agent → save_to_db → send_notification → END
```

| 節點 | 模型 | 職責 |
|------|------|------|
| data_collector | Haiku 4.5 | 解析 snapshot，提取市場數據 |
| chip_analyst | Sonnet 4.6 | 分析三大法人籌碼 |
| tech_analyst | Sonnet 4.6 | 技術面分析 |
| chief_strategist | Opus 4.7 + Thinking | 統合分析，注入歷史準確率 + 策略教訓 |
| portfolio_manager | Sonnet 4.6 | 個人持倉損益評估 |
| format_agent | Haiku 4.5 | 格式化 LINE 推播訊息 |
| save_to_db | (無 LLM) | 寫入 TiDB、session_episodes |
| send_notification | (無 LLM) | LINE + Telegram 推播 |

### Daily Run 排程（daily_run.sh）

| 步驟 | 內容 |
|------|------|
| Step 1 | 市場資料採集（`test_collection.py`） |
| Step 2 | 分析團隊執行 + 建議書存入 DB（`investment_workflow.py`） |
| Step 2.5 | 向非擁有者用戶發送個人化持股分析 |
| Step 3 | 傳送今日建議書（fallback，workflow 已推播則略過） |
| Step 4 | Agent 品質評估（`evaluation_runner.py`） |
| Step 5 | 清除過期策略教訓（`cleanup_expired_lessons()`） |

### TiDB 資料庫（`agent_memory`）

| 資料表 | 說明 |
|--------|------|
| `daily_briefs` | 每日投資建議書（含 `line_report` 欄位、UNIQUE trade_date） |
| `market_actuals` | 實際市場結果（backtest 回填） |
| `cost_logs` | 每次 LLM 呼叫的 token 與成本記錄 |
| `user_portfolio` | 用戶持倉（`UNIQUE uq_user_stock(line_user_id, stock_id)`） |
| `workflow_runs` | 每次工作流執行記錄 |
| `workflow_events` | 結構化事件日誌 |
| `llm_traces` | LLM 完整 prompt/response 紀錄 |
| `audit_log` | DB 變更稽核軌跡 |
| `tool_audit_log` | 工具呼叫稽核記錄 |
| `session_episodes` | 每日市場情境快照（含 regime tag） |
| `eval_runs` | Agent 評估執行記錄 |
| `eval_results` | Per-agent 評估結果 |
| `strategy_lessons` | Flywheel 策略教訓（含 regime、quality_score、TTL） |
| `login_tokens` | LINE OTP 登入代碼（expires_at、used） |
| `stock_info` | 股票基本資料（名稱同步自 TWSE） |

### 程式碼檔案清單

| 檔案 | 類型 | 說明 |
|------|------|------|
| `investment_workflow.py` | 主入口 | LangGraph 圖建構與主執行流程 |
| `market_analyst_agents.py` | 核心 | 8 個節點邏輯 + `generate_portfolio_analysis_for_user()` |
| `database_tools.py` | 工具 | TiDB 所有 CRUD 與 schema 管理 |
| `telemetry.py` | 工具 | 遙測 helper（record_usage / emit_event） |
| `messenger_tools.py` | 工具 | LINE / Telegram 推播 + `send_line_to_user()` |
| `portfolio_tools.py` | 工具 | 持倉計算（get_user_portfolio / calculate_pnl） |
| `snapshot_integrity.py` | 工具 | Snapshot HMAC 簽章/驗證 |
| `evaluation_runner.py` | 評估 | Agent 評估 CLI（每日 daily_run 觸發）⚠️ 未提交 |
| `evaluation_metrics.py` | 評估 | Rule-based per-agent 評分 ⚠️ 未提交 |
| `lesson_writer.py` | Flywheel | 從 backtest 提取策略教訓 ⚠️ 未提交 |
| `lesson_retriever.py` | Flywheel | 依 regime 檢索教訓注入 chief_strategist ⚠️ 未提交 |
| `test_collection.py` | 採集 | 市場快照採集（MCP async 客戶端） |
| `backtest_agent.py` | 分析 | 預測準確率回測工作流 |
| `agent_orchestrator.py` | 分析 | 多工作流協調器 |
| `alert_runner.py` | 監控 | A-001 至 A-010 告警的獨立 cron 腳本 |
| `dashboard.py` | 介面 | Streamlit 可視化儀表板（LINE OTP 登入 + 多使用者持倉） |
| `line_webhook.py` | 介面 | FastAPI LINE Webhook Server |
| `twse_fetcher.py` | 採集 | TWSE 當日行情（backtest + stock_info 同步） |
| `mcp_servers/finance_mcp_server.py` | MCP | 財經資料 MCP Server（只讀） |
| `mcp_servers/system_inspector.py` | MCP | 系統監控 MCP Server |

---

## 待辦事項

### 立即：未提交的新檔案（⚠️ 需要 commit）

| 檔案 | 說明 |
|------|------|
| `evaluation_runner.py` | 已整合至 daily_run.sh Step 4，需提交 |
| `evaluation_metrics.py` | 評估核心邏輯，需提交 |
| `lesson_writer.py` | Flywheel 教訓提取，需提交 |
| `lesson_retriever.py` | Flywheel 教訓檢索，需提交 |
| `evaluation_metrics.md` | 評估指標說明文件 |
| `evaluation_change_report.md` | 評估系統變更說明 |
| `evaluation_framework_design.md` | 評估框架設計文件 |
| `adaptive_flywheel_design.md` | Flywheel 設計文件 |
| `chief_strategist_context_update_report.md` | Chief Strategist 更新說明 |

### Memory Phase 1（部分完成）

| 項目 | 狀態 | 說明 |
|------|------|------|
| Backtest agent 回填 `session_episodes.actual_*` | ✅ 完成（`a5de724`） | |
| `get_recent_sessions_context(days=10)` | ✅ 完成（`153becc`） | chief_strategist 接收 regime 歷史 |
| 策略教訓注入 chief_strategist | ✅ 完成（`69bc3c5`） | lesson_retriever 整合 |

### Memory Phase 2（向量記憶，路線圖）

| 項目 | 預期效益 | 工時 |
|------|----------|------|
| `session_episodes` 向量嵌入（TiDB Vector / Chroma） | 語意相似場景檢索 | 12 小時 |
| 5 筆最相似歷史場景注入 chief_strategist | +10–15% direction accuracy | 3 小時 |

### 其他技術債

| 項目 | 說明 | 優先度 |
|------|------|--------|
| `asyncio.run()` anti-pattern in `maintenance_agent` | 在已有事件迴圈的環境中會崩潰 | MEDIUM |
| `investment_workflow.py --resume <run_id>` CLI flag | 目前手動 resume 需修改程式碼 | LOW |
| `backtest_evaluator` cost logging | LLM 呼叫未寫入 `cost_logs` | LOW |
| `langgraph-checkpoint-sqlite` 套件 | Server 目前 fallback 至 MemorySaver（非持久化） | LOW |
| `send_brief_to_user` MCP handler 孤兒工具 | 已由 MCP Governance 記錄，確認是否已移除 | LOW |

---

## 驗證狀態（2026-05-19）

- LINE OTP 登入流程：`login_tokens` 表已建立，`create_login_token()` / `consume_login_token()` 正常
- 多使用者持倉隔離：`user_portfolio.line_user_id` UNIQUE KEY 保護，各 session 獨立
- DASH_PASSWORD admin 登入：`load_dotenv()` 修復後正常載入
- Evaluation Framework：`evaluation_runner.py` 整合至 daily_run.sh Step 4
- Adaptive Flywheel：`strategy_lessons` 表已建立，lesson_writer / retriever 運作中
- A-010 告警：教訓品質低於閾值時觸發，已整合至 alert_runner.py
- TWSE TLS：`verify=False` 已移除（`a5de724`）

---

## 文件索引

詳細分析文件位於 `docs/` 目錄，參見 [AI_Agent_DOC_index.md](AI_Agent_DOC_index.md)。

實作計畫與設計文件（專案根目錄）：
- [agentos_implementation_plan.md](../agentos_implementation_plan.md)
- [adaptive_flywheel_design.md](../adaptive_flywheel_design.md) ⚠️ 未提交
- [evaluation_framework_design.md](../evaluation_framework_design.md) ⚠️ 未提交
- [evaluation_change_report.md](../evaluation_change_report.md) ⚠️ 未提交
- [chief_strategist_context_update_report.md](../chief_strategist_context_update_report.md) ⚠️ 未提交
