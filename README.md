# AI Agent Studio — 台股期貨智能投資顧問

> 一套多 agent、可自我檢討與自我優化的台股期貨投資建議系統。
> 每個交易日盤前自動採集市場資料、由多個 LLM agent 分工分析、產出投資建議書並透過 LINE 推播。

---

## 功能概觀

- **每日自動工作流** — 盤前採集美股、台指期夜盤、三大法人籌碼、財經新聞，產出當日台股開盤研判與投資建議書。
- **多 agent 分工管線** — 資料整理 → 籌碼/技術分析 → 跨領域整合決策 → 個股持倉建議 → 格式化 → 推播。
- **個人持倉分析** — 依使用者持股計算未實現損益、技術指標與法人動向，給出逐檔操作建議。
- **自我檢討（回測 / 教訓）** — 隔日回測前一日預測命中率，萃取「策略教訓」回注下一輪決策。
- **自我優化（Shadow + Optimizer）** — 新版策略以 shadow 模式並跑比對；Optimizer Agent 每週檢視表現、提出新策略版本（人工審核後才上線）。
- **可觀測性 Dashboard** — Streamlit 儀表板呈現預測準確度、API 成本、持倉、系統健康、稽核 trace、shadow 比對、optimizer 提案等。
- **LINE Bot** — 推播建議書、查詢與管理個人持倉、產生 Dashboard 登入碼。

---

## 系統架構

### Agent 管線（每日投資工作流）

| 順序 | Agent | 模型層級 | 職責 |
|---|---|---|---|
| 1 | `data_collector` | Haiku | 將原始市場快照萃取為精簡數值摘要 |
| 2 | `chip_analyst` | Sonnet | 三大法人籌碼面分析 |
| 3 | `tech_analyst` | Sonnet | 技術面 + 開盤跳空方向/力道研判 |
| 4 | `chief_strategist` | Opus | 跨領域整合決策（注入歷史記憶與教訓）|
| 5 | `portfolio_manager` | Sonnet | 逐檔持倉操作建議 |
| 6 | `format_agent` | Haiku | 輸出格式化 |
| — | `save_to_db` / `send_notification` | — | 寫入資料庫 / LINE 推播 |

工作流以 **LangGraph** 狀態圖編排，搭配 SQLite checkpointer。

### 自我檢討與自我優化

- **回測評估** — `backtest_agent.py`（隔日回測）+ `evaluation_runner.py`（agent 品質評估），產出 `strategy_lessons`。
- **策略即資料** — 每個 agent 的 prompt / 參數 / 工具白名單 / 模型存於 `agent_strategy_profiles` 資料表，可版本化、可切換。
- **Shadow 模式** — 新版 agent（含 tool-use ReAct 迴圈）與舊版並跑，production 永遠送舊版，新版輸出寫入 `shadow_runs` 供比對。
- **Optimizer Agent** — 每週檢視 shadow 表現，在有資料佐證時提出新策略版本（`optimizer_proposals`）。所有提案僅進入 shadow，**永不自動上線**，須人工以 `scripts/promote_profile.py` 審核後才採用。

---

## 技術棧

| 類別 | 技術 |
|---|---|
| 語言 / 套件管理 | Python ≥ 3.13、[uv](https://github.com/astral-sh/uv) |
| Agent 編排 | LangGraph、Anthropic SDK（ReAct tool-use 迴圈）|
| 資料庫 | TiDB v8.5（單節點，docker compose）|
| 工具協定 | MCP（Model Context Protocol）servers |
| 儀表板 | Streamlit |
| Webhook | FastAPI + uvicorn |
| 訊息推播 | LINE Messaging API |

---

## 部署方式

> 以下指令以 Linux 主機為例。所有機密值（API key、資料庫密碼、LINE token 等）一律寫入本機 `.env`，**切勿提交至版本庫** —— `.env` 已列入 `.gitignore`。

### 前置需求

- Python ≥ 3.13
- [uv](https://github.com/astral-sh/uv)
- Docker + Docker Compose
- Git

### 1. 取得程式碼

```bash
git clone https://github.com/coolerh250/AI-Agents-Fin.git
cd AI-Agents-Fin
```

### 2. 安裝相依套件

```bash
uv sync
```

### 3. 啟動 TiDB

```bash
docker compose -f docker/docker-compose.yml up -d
```

TiDB 僅綁定 `127.0.0.1`，不對外網開放。資料持久化於 `docker/data/`。

### 4. 設定環境變數

```bash
cp .env.template .env
```

接著編輯 `.env`，填入你自己的值。需設定的項目分為以下類別（**實際值請勿寫入文件或版本庫**）：

| 類別 | 變數 | 說明 |
|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY` | Claude API 金鑰 |
| TiDB | `TIDB_HOST` / `TIDB_PORT` / `TIDB_USER` / `TIDB_PASSWORD` / `TIDB_DB` | 資料庫連線 |
| LINE | `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_USER_ID` / `LINE_WEBHOOK_SECRET` | 推播與 webhook |
| MCP | `MCP_WRITE_TOKEN` / `MCP_NOTIFY_TOKEN` | MCP server 驗證 token |
| Shadow / Optimizer | `SHADOW_AGENTS` / `OPTIMIZER_ENABLED` / `OPTIMIZER_AGENTS` / `OPTIMIZER_COST_CAP_USD` | 自我優化開關 |

完整清單與註解見 [`.env.template`](.env.template)。

### 5. 初始化資料庫

建立資料庫並建表（資料表定義為冪等，可重複執行）：

```bash
uv run python -c "
from database_tools import ensure_observability_tables
ensure_observability_tables()
print('tables ready')
"
```

（可選）將現有 agent prompt 寫入策略資料表作為 v1：

```bash
uv run python -c "
from strategy_profile import seed_initial_profiles, seed_pilot_shadow_profiles
seed_initial_profiles()
seed_pilot_shadow_profiles()
"
```

資料表 schema 參考 [`migration.sql`](migration.sql)。

### 6. （選用）系統服務

Dashboard 與 LINE webhook 建議以 systemd 服務常駐：

- `ai-agent-dashboard` — `streamlit run dashboard.py`
- `ai-agent-webhook` — `uvicorn` 執行 `line_webhook.py`

### 7. （選用）排程

每日工作流與相關維運以 cron 排定。**注意：cron 排程欄位請使用 UTC 時間**（部分 Linux cron 不會套用 `CRON_TZ`）：

```cron
# 每日投資工作流 — 08:00 台北 = 00:00 UTC，週一至週五
0 0 * * 1-5  /path/to/ai_agent_studio/daily_run.sh >> logs/daily_run.log 2>&1
# 每日告警檢查 — 08:20 台北 = 00:20 UTC
20 0 * * 1-5 cd /path/to/ai_agent_studio && uv run python alert_runner.py >> logs/alerts.log 2>&1
# 每日資料庫備份 — 22:00 台北 = 14:00 UTC
0 14 * * *   /path/to/ai_agent_studio/backup_db.sh >> logs/backup.log 2>&1
# 每週 Optimizer — 週日 03:00 台北 = 週六 19:00 UTC
0 19 * * 6   cd /path/to/ai_agent_studio && uv run python scripts/optimizer_run.py >> logs/optimizer.log 2>&1
# 每日 Optimizer 回歸監測 — 02:00 台北 = 18:00 UTC
0 18 * * *   cd /path/to/ai_agent_studio && uv run python scripts/optimizer_revert_check.py >> logs/optimizer.log 2>&1
```

> `logs/` 目錄須存在（版本庫內已含 `logs/.gitkeep`），否則 cron 的輸出重導向會失敗。

---

## 使用方式

### 每日工作流

完整流程（採集 → 回測 → 分析 → 推播 → 評估）：

```bash
./daily_run.sh
```

或單獨執行核心分析工作流：

```bash
uv run investment_workflow.py
```

### Dashboard 儀表板

```bash
uv run streamlit run dashboard.py
```

登入採 LINE OTP 一次性驗證碼。若 LINE webhook 尚未啟用，可用 break-glass 腳本直接產生登入碼：

```bash
uv run python scripts/mint_login_token.py
```

代碼有效期 5 分鐘，輸入 Dashboard 登入欄即可。

### LINE Bot

設定 LINE Messaging API channel 並將 webhook URL 指向 `line_webhook.py` 服務後，可在 LINE 中：

- 接收每日投資建議書推播
- 查詢 / 新增 / 刪除個人持倉
- 傳送「登入代碼」取得 Dashboard 登入碼

### CLI 工具

| 指令 | 用途 |
|---|---|
| `uv run python scripts/promote_profile.py status <agent>` | 查看 agent 的 active / shadow 版本與比對統計 |
| `uv run python scripts/promote_profile.py promote <agent> <ver>` | 將某版本切為 active（人工升版）|
| `uv run python scripts/promote_profile.py revert <agent> <ver>` | 回滾至指定版本 |
| `uv run python scripts/promote_profile.py proposals <agent>` | 查看 Optimizer 提案紀錄 |
| `uv run python scripts/optimizer_run.py` | 手動執行一次 Optimizer |
| `uv run python scripts/mint_login_token.py` | 產生 Dashboard 登入碼 |

---

## 專案結構

```
investment_workflow.py     每日投資分析工作流（LangGraph）
market_analyst_agents.py   各分析 agent 節點實作
daily_run.sh               每日自動化腳本（採集→回測→分析→推播→評估）
backtest_agent.py          隔日回測 / 自我檢討
evaluation_runner.py       agent 品質評估
alert_runner.py            告警檢查與推播

strategy_profile.py        策略即資料 — 從 DB 讀取 agent prompt/參數
tool_catalog.py            ReAct 工具註冊與執行（含權限管控）
agent_runtime.py           ReAct tool-use 迴圈
shadow_compare.py          shadow 新舊版輸出差異計分

optimizer_agent.py         Optimizer Agent（ReAct 迴圈）
optimizer_tools.py         propose_strategy_version 寫入工具與有界變更守門
optimizer_scoring.py       策略版本評分

database_tools.py          資料庫存取、資料表建立、工具權限
messenger_tools.py         LINE / Telegram 推播
telemetry.py               成本與事件遙測
dashboard.py               Streamlit 觀測儀表板
line_webhook.py            LINE webhook（FastAPI）

mcp_servers/               MCP servers（市場資料 / 持久化 / 通知 / 系統）
scripts/                   維運 CLI（升版、optimizer、登入碼）
docker/docker-compose.yml  TiDB 單節點叢集
.env.template              環境變數範本
```

---

## 安全性

- **機密管理** — 所有金鑰 / token / 密碼僅存於本機 `.env`（已 `.gitignore`），不進版本庫。
- **資料庫隔離** — TiDB 僅綁定 `127.0.0.1`，不對外網開放。
- **工具權限管控** — 高風險工具採 fail-closed 權限檢查，未授權的呼叫者一律拒絕。
- **登入** — Dashboard 採 LINE OTP 一次性驗證碼，無常駐密碼。
- **Optimizer 護欄** — Optimizer 僅能提出有界變更（限定參數範圍、不可新增工具、不可改模型），且永不自動上線，須人工審核升版。

---

## 授權

私有專案，未開放對外授權。
