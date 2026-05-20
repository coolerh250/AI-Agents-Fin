# Reality Repository Inventory
_獨立架構稽核 — 第一階段 1/8_
_稽核日期：2026-05-19｜稽核者：Independent Architecture Auditor_
_原則：以實際 source / runtime / config 為準，不引用既有文件結論_

---

## 稽核方法

- `git ls-files` + Glob 全檔案盤點
- 逐檔閱讀所有 Python 原始碼（30 個 .py）、config（yaml/sql/sh/toml）
- runtime 比對：SSH 至 `ai-agents-server`（10.0.1.20）檢查實際部署檔案
- 以「import 關係」與「cron / daily_run.sh 呼叫鏈」判定檔案是否真的進入 execution path

---

## 1. 真實存在的程式檔案（30 個 .py）

### 進入 execution path 的檔案（live）

| 檔案 | 角色 | 由誰呼叫 | 證據 |
|------|------|----------|------|
| `investment_workflow.py` | 主工作流入口 | daily_run.sh Step 2 | LangGraph 8 節點圖建構 |
| `market_analyst_agents.py` | 8 個節點邏輯 | investment_workflow import | 6 個 LLM 節點 + 2 個無 LLM 節點 |
| `database_tools.py` | TiDB 全部 CRUD（1641 行） | 幾乎所有模組 import | 15 張表的 ensure_/CRUD |
| `telemetry.py` | record_usage / emit_event | 各節點 import | 薄封裝層 |
| `messenger_tools.py` | LINE / Telegram push | send_notification、alert_runner | — |
| `portfolio_tools.py` | 持倉計算 + yfinance + TWSE T86 | portfolio_manager、dashboard | — |
| `snapshot_integrity.py` | HMAC 簽章/驗證 | investment_workflow、test_collection | **runtime 無金鑰 → 實際為 no-op** |
| `test_collection.py` | 市場快照採集 | daily_run.sh Step 1 | 呼叫 market_data_server |
| `evaluation_runner.py` | Agent 評估 CLI | daily_run.sh Step 4 | — |
| `evaluation_metrics.py` | 規則式評分 | evaluation_runner import | 無 LLM、無 DB |
| `alert_runner.py` | 告警 cron 腳本 | crontab（獨立排程） | A-001~A-010 |
| `dashboard.py` | Streamlit 看板（692 行） | 常駐 process（port 8501） | 7 個 tab |
| `line_webhook.py` | FastAPI LINE webhook | 常駐 process（port 8502） | 持倉 CRUD 指令 |
| `twse_fetcher.py` | TWSE 加權指數抓取 | backtest_agent、investment_workflow | — |
| `mcp_servers/market_data_server.py` | 市場資料 MCP | test_collection 子程序 | 6 個 @mcp.tool |
| `mcp_servers/persistence_server.py` | 持久化 MCP | save_to_db_node 子程序 | token 認證 |
| `mcp_servers/notification_server.py` | 通知 MCP | send_notification_node 子程序 | token 認證 + dedup |
| `mcp_servers/system_server.py` | 系統監控 MCP | agent_orchestrator 子程序 | — |
| `utils/mcp_call.py` `mcp_env.py` `mcp_audit.py` | MCP 呼叫/環境/稽核 helper | 上述 MCP 客戶端 | — |
| `lesson_writer.py` `lesson_retriever.py` | Flywheel 寫/讀教訓 | backtest_agent / chief_strategist | **寫端未排程，見下** |

### 條件性 / 未排程的檔案（code 存在但 runtime 不被自動觸發）

| 檔案 | 狀態 | 證據 |
|------|------|------|
| `backtest_agent.py` | **未排程** — 不在 crontab、不在 daily_run.sh | crontab 僅 3 條，無 backtest |
| `agent_orchestrator.py`（maintenance_agent） | **未排程** — 僅可手動執行 | crontab 無此項 |
| `lesson_writer.py` | 僅由 backtest_agent 呼叫 → 因 backtest 未排程而**實際凍結** | — |
| `test_mcp_client.py` | 開發測試腳本 | 無 production 呼叫 |

### 死碼（dead code — 無任何 import / 呼叫）

| 檔案 | 證據 |
|------|------|
| `main.py` | 內容僅 `print("Hello from ai-agent-studio!")` |
| `mcp_servers/finance_mcp_server.py` | 已被 `market_data_server.py` 取代（docstring 自述「kept for one sprint as rollback target」）；無 import |
| `mcp_servers/system_inspector.py` | 已被 `system_server.py` 取代（docstring 自述「Renamed from」）；無 import |

---

## 2. 真實存在的 services

| Service | Port | 狀態 | 證據（ss -tlnp） |
|---------|------|------|------|
| Streamlit dashboard | 8501 | 常駐執行中 | pid 23442，bind `0.0.0.0` |
| FastAPI LINE webhook | 8502 | 常駐執行中 | pid 21675，bind `0.0.0.0` |
| TiDB | 4000 | 常駐執行中 | bind `0.0.0.0`（docker） |

無 systemd unit；兩個 service 由 `nohup ... &` 手動啟動（process 樹可見 `bash -c ... nohup`）。

---

## 3. 真實存在的 workflows

| Workflow | 框架 | 節點數 | 排程狀態 |
|----------|------|--------|----------|
| investment_workflow | LangGraph StateGraph | 8 | daily_run.sh（**cron 已壞，見 runtime 報告**） |
| backtest_agent | LangGraph StateGraph | 4 | **未排程** |
| agent_orchestrator（maintenance） | LangGraph StateGraph | 2 | **未排程** |

---

## 4. 真實存在的 integrations

| 外部系統 | 用途 | 認證 | 真實使用 |
|----------|------|------|----------|
| Anthropic API | 6 個 LLM 節點 + backtest + maintenance | ANTHROPIC_API_KEY | 是 |
| TiDB（self-hosted, docker） | 全部持久化 | TIDB_USER=**root** | 是 |
| LINE Messaging API | push + webhook | LINE_CHANNEL_ACCESS_TOKEN | 是 |
| Telegram | push | TELEGRAM_BOT_TOKEN | **.env 未設定 → 全部 skipped** |
| TAIFEX（爬蟲） | 三大法人、夜盤 | 無 | 是 |
| yfinance / Yahoo v8 | 美股、個股價 | 無 | 是 |
| Anue 鉅亨網 API | 財經新聞 | 無 | 是 |
| TWSE API | 加權指數、個股名稱 | 無 | 是（但 backtest 未排程 → 加權指數路徑凍結） |

---

## 5. 與既有文件的初步落差

| 既有文件聲稱 | repo / runtime 實況 |
|--------------|---------------------|
| `tool_registry.yaml` 列出 `finance_mcp_server.py` 為 active server，含 orphan 工具 `save_brief_to_db` / `send_brief_to_user` | finance_mcp_server.py 已是死碼；該兩 orphan 工具在現檔中**已不存在**（grep 無 def） |
| `agent_registry.yaml` maintenance_agent 引用 `mcp.system_inspector` | 實際檔名為 `system_server.py` |
| `progress.md` 稱系統「cron 排程自動觸發」 | daily_run.sh 的 cron 自 2026-05-15 起因權限問題失敗（見 runtime 報告） |
| docstring：investment_workflow 圖為 5 節點 | 實際 build_graph() 為 8 節點 |

---

## 結論

Repo 含 **30 個 Python 檔，其中約 20 個進入 execution path，3 個為明確死碼，2 個工作流（backtest、maintenance）雖有完整程式碼但未被任何排程觸發**。兩個註冊表 yaml 描述的是上一代檔名與工具，已與現況不符。檔案層級的「存在」不等於「運作」——下一份 runtime 報告將確認哪些 code 真的在跑。
