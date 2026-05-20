# 多 Agent 自我改善工作室 — 轉型計畫
_制定日期：2026-05-19｜基礎：本目錄 12 份 reality audit 報告_
_目標：將現況「半自動 LLM 管線」轉型為「真・多 Agent、自我改善、可經 gateway 互動的 AI 工作室」_

---

## 0. 起點與目標的差距

### 起點（稽核確認的真實現況）
一條 8 節點線性 LLM 管線。每個「Agent」實為單次 LLM 呼叫；回饋迴路（backtest）未排程而凍結；cron 已壞；無 gateway 抽象層。

### 目標（使用者定義）
1. **真・多 Agent** — agent 能用工具、能規劃、能自主決定方法
2. **自我改善** — agent 依結果自行調整策略與方法
3. **Gateway 互動** — 工作室可經多種 gateway 與人或其他工具雙向互動

### 三個核心差距

| 維度 | 現況 | 目標 | 差距本質 |
|------|------|------|----------|
| Agent | 單次 LLM 呼叫節點 | 具 tool-use 迴圈、規劃、自主 | 缺「agentic loop」 |
| 自我改善 | 回饋迴路斷、教訓僅被「注入文字」 | 結果 → 評估 → 反思 → **自動調整策略** | 缺「策略即資料 + optimizer」 |
| Gateway | LINE webhook + dashboard 各自寫死 | 統一 inbound/outbound gateway 抽象 | 缺「gateway 層」 |

---

## 1. 設計原則（決定計畫形狀）

1. **Reality-first**：先讓地基成立，再蓋多 agent。稽核已證明「在壞掉的地基上疊功能」是本專案最大問題，不重蹈。
2. **演進而非重寫**：LangGraph、TiDB、observability、MCP 都保留。它們是真實資產（稽核確認）。轉型是「升級」不是「打掉」。
3. **每階段可獨立交付價值**：每個 Phase 結束時系統都能穩定運轉，不存在「做到一半更糟」的中間態。
4. **策略即資料（strategy-as-data）**：自我改善的前提是「策略」必須是可被程式讀寫的資料，而非寫死在 source 的 prompt 字串。
5. **改變需有護欄**：自我調整必須 bounded、可 shadow 測試、可 rollback、重大變更需人工核可。自主 ≠ 失控。

---

## 2. 目標架構

```
┌────────────────────────────────────────────────────────────────┐
│  GATEWAY 層（雙向）                                              │
│  Inbound : LINE webhook · Web UI · REST API · 排程 · MCP server  │
│  Outbound: LINE/Telegram push · MCP client · outbound webhook    │
│            └─ 統一 GatewayMessage 抽象，與核心解耦                │
├────────────────────────────────────────────────────────────────┤
│  AGENT RUNTIME 層                                                │
│  Orchestrator Agent（規劃、委派、路由）                          │
│    ├─ Specialist Agents（chip / tech / chief / portfolio …）     │
│    │    每個 = LLM + tool-use 迴圈 + 自己的 strategy profile      │
│    └─ Optimizer Agent（meta：讀評估結果 → 調整 strategy profile） │
├────────────────────────────────────────────────────────────────┤
│  TOOL 層                                                         │
│  MCP tools（市場資料 · 持久化 · 通知 · 系統）+ 內部函式工具       │
│  以「工具目錄」形式提供，agent 動態選用                          │
├────────────────────────────────────────────────────────────────┤
│  MEMORY 層                                                       │
│  working（單次 run）· episodic（session_episodes）·              │
│  semantic（向量檢索）· strategy（strategy profiles ＝可調策略）   │
├────────────────────────────────────────────────────────────────┤
│  SELF-IMPROVEMENT 層（閉環）                                     │
│  predict → act → observe → evaluate → reflect → adjust → apply   │
├────────────────────────────────────────────────────────────────┤
│  GOVERNANCE / OBSERVABILITY 層                                   │
│  既有 telemetry + agent registry「runtime 強制執行」化           │
└────────────────────────────────────────────────────────────────┘
```

---

## 3. 關鍵架構決策

### D-1：Agent 框架 — 保留並升級 LangGraph
LangGraph 已支援 agentic pattern（tool node、ReAct、`add_conditional_edges`、subgraph、`interrupt`）。現況只是「沒用到這些能力」。
- **決定**：保留 LangGraph，啟用其 agentic 能力；不引入新框架。
- **替代方案（記錄）**：Claude Agent SDK 原生支援 agent loop，但會與既有 LangGraph 投資衝突。除非未來需求超出 LangGraph，否則不採用。

### D-2：「Agent」的明確定義（驗收標準）
一個元件要從「node」升級為「agent」，必須具備：
1. **工具目錄**：被授予一組工具，而非寫死的單一資料來源
2. **tool-use 迴圈**：LLM 自行決定呼叫哪個工具、是否再呼叫、何時結束（Anthropic tool-use API）
3. **策略 profile**：行為由 DB 中可調的 strategy profile 驅動，而非寫死的 system prompt
4. **可被評估**：每次行動產生可被 evaluation 層量化的輸出

### D-3：自我改善的機制 — 「策略即資料」+ Optimizer Agent
- 每個 specialist agent 的「策略」（system prompt 模板、權重、閾值、工具偏好）存於 DB 表 `agent_strategy_profiles`，附版本號。
- Optimizer Agent 定期讀 `eval_results` + `strategy_lessons`，產生「策略調整提案」，寫入新版 profile。
- 調整分兩種粒度：
  - **run 內自我調整**：tool-use 迴圈讓 agent 在單次任務中換方法、重試、補資料。
  - **run 間自我改善**：Optimizer 跨多次結果學習，更新 strategy profile。
- **護欄**：每次調整 bounded（限制變更幅度）→ shadow run（新舊 profile 平行跑、比對）→ 自動採用（小改善）或人工核可（大變更）→ 可 rollback 至任一版本。

### D-4：Gateway 抽象
所有 inbound 訊息正規化為 `GatewayMessage{source, actor_id, intent, payload}`；所有 outbound 經 `Gateway.send()`。核心 agent runtime 不直接認識 LINE/Telegram/HTTP。新增一種 gateway = 實作一個 adapter，核心零改動。

---

## 4. 分階段計畫

### Phase 0 — 穩固地基（數小時～2 天）｜先決條件，不可跳過
直接執行 audit 的 P0/P1。**地基不修，後面全部白做。**

| 項目 | 動作 | 依據 |
|------|------|------|
| 修 cron | `chmod +x daily_run.sh` 或 crontab 改 `bash daily_run.sh` | runtime 報告 |
| 接回 backtest | backtest_agent 排進 daily_run.sh | memory 報告 |
| 修告警出口 | 設定 Telegram，或 WARNING/INFO 改走 LINE | observability 報告 |
| 修 webhook 安全 | 設 `LINE_WEBHOOK_SECRET`；缺 secret 改為拒絕 | security S-1 |
| 收斂對外 port | TiDB 綁 127.0.0.1、加 ufw | security S-2/S-3 |
| 清明文密碼 | 移除 settings.local.json 內 sudo 密碼並輪換 | security S-4 |
| checkpointer | `uv add langgraph-checkpoint-sqlite`（後續 agent 化需要持久 state） | workflow 報告 |

**出場標準**：連續 3 個交易日 cron 自動完成、backtest 有產出新 `market_actuals`、告警能送達、無對外曝險。

---

### Phase 1 — Node → Agent（1～2 週）
把線性管線升級為具 tool-use 能力的 agent，並引入 Orchestrator。

**1.1 建立工具層**
- 把 MCP 工具 + 內部函式整理成統一「工具目錄」，每個工具有 JSON schema。
- 新增 `tools/` 模組，提供 Anthropic tool-use 格式的工具定義。

**1.2 改造 specialist agent（先從 2 個試點）**
- 試點：`tech_analyst` 與 `portfolio_manager`（資料需求最明確、最易驗證）。
- 從「節點程式碼預先抓資料 → 餵給 LLM」改為「給 LLM 工具目錄 → LLM 自行決定抓什麼」。
- 實作 tool-use 迴圈（LLM 回 tool_use → 執行 → 回 tool_result → 續呼叫，直到 end_turn）。
- 影響檔案：`market_analyst_agents.py`、新增 `agent_runtime.py`（tool-use 迴圈共用邏輯）。

**1.3 引入 Orchestrator Agent**
- 新節點：接收任務 → 規劃要呼叫哪些 specialist、順序、是否需要補充分析。
- LangGraph 用 `add_conditional_edges` 實作動態路由（取代現在的固定 edge）。
- 初期 Orchestrator 可保守（沿用現有順序），重點是建立「可被調整的調度點」。

**1.4 strategy profile 表（為 Phase 2 鋪路）**
- 新增 `agent_strategy_profiles` 表：`agent_name, version, system_prompt, params(JSON), is_active, created_at`。
- 各 agent 改為啟動時讀取自己的 active profile，而非用寫死字串。

**出場標準**：≥2 個 agent 具 tool-use 迴圈並通過評估；Orchestrator 能動態決定路由；所有 agent 行為由 DB profile 驅動。

---

### Phase 2 — 自我改善閉環（2～3 週）｜本計畫的核心
讓系統「依結果自行調整策略」。

**2.1 閉環資料流**
```
workflow 產出預測 → backtest 取得真實結果 → evaluation 量化
  → Reflection（LLM 反思「為何對/錯、該調什麼」）
  → Optimizer Agent 產生 strategy profile 調整提案
  → 護欄（bounded + shadow + 核可）→ 寫入新版 profile
  → 下次 run 自動套用新 profile
```

**2.2 Optimizer Agent（新增 meta agent）**
- 輸入：近 N 次 `eval_results`、`strategy_lessons`、各 agent 的 profile 歷史。
- 輸出：針對特定 agent 的 profile 調整（prompt 片段增刪、權重/閾值微調、工具偏好）。
- 排程：每週或累積 5 筆評估後觸發（非每日，避免過擬合雜訊）。

**2.3 護欄機制（關鍵，避免自主失控）**
- **Bounded change**：單次調整限制（如閾值 ±20%、prompt 僅可增刪標記區塊）。
- **Shadow run**：新 profile 先以「平行影子模式」跑，不影響正式輸出，累積 N 次比對。
- **採用規則**：影子表現顯著優於現役 → 自動升版；接近或退步 → 丟棄；重大結構性變更 → 推一則 LINE 給人工核可。
- **Rollback**：profile 有版本號，任何時候可一鍵回退；每次升版記錄於 `audit_log`。

**2.4 強化記憶層支撐**
- 補 `session_episodes` 的「讀回路」（注入 Orchestrator/chief）。
- 導入向量檢索：把歷史 episode/lesson 向量化（TiDB Vector 或 Chroma），讓 Optimizer 與 chief 能取「語意相似的歷史情境」，而非只靠 regime 標籤比對。

**出場標準**：完成 ≥1 次「評估 → 提案 → shadow → 升版」完整閉環；profile 版本歷史可查；rollback 可用。

---

### Phase 3 — Gateway 層（1～2 週）
把「與人/工具互動」抽象化、可擴充。

**3.1 統一 Gateway 抽象**
- `gateways/` 模組：`GatewayMessage` 正規化結構 + `BaseGateway` 介面。
- 既有 LINE webhook、dashboard 改為 gateway adapter。

**3.2 Inbound gateways**
- LINE（既有，改造）、Web UI（既有 dashboard，改造）、**REST API**（新增，供外部系統觸發/查詢）、排程觸發、**MCP server**（新增：把工作室自身能力以 MCP 工具對外暴露，讓「其他 AI/工具」能呼叫本工作室）。

**3.3 Outbound gateways**
- LINE/Telegram push（既有）、**MCP client**（呼叫外部工具）、**outbound webhook**（事件推給外部系統）。

**3.4 意圖路由**
- inbound 訊息 → Orchestrator 判定 intent（查詢／下單分析／調整持倉／觸發 workflow）→ 分派。

**出場標準**：新增一種 gateway 不需改核心；工作室能力可經 MCP/REST 被外部呼叫。

---

### Phase 4 — 多 Agent 協作與治理 runtime 化（持續）
- Orchestrator 支援真正的委派：依任務動態組裝 agent 團隊、可遞迴（agent 產生子任務）。
- `agent_registry.yaml` 從「文件」升級為 **runtime 強制執行**：budget 上限、權限、工具白名單在執行期被檢查（取代現在 fail-open 的 `validate_tool_permission`）。
- 加入 `interrupt()` 人工審核關卡於高風險動作（如真實下單、大額策略變更）。

---

## 5. 自我改善機制 — 設計細節

這是「會自我改善」的實質。三層學習：

| 層級 | 時間尺度 | 機制 | 範例 |
|------|----------|------|------|
| **Run 內** | 秒～分 | tool-use 迴圈：agent 發現資料不足 → 自行補抓、換工具、重試 | tech_analyst 發現夜盤資料缺 → 自行改抓備援來源 |
| **Run 間** | 日～週 | Optimizer 依評估結果調整 strategy profile | 連續高估跳空 → 下修 tech_analyst 的夜盤權重 |
| **結構性** | 月 | 人工 + Optimizer 共審：新增 agent、改調度拓樸 | 準確率長期低 → 新增「總經 agent」 |

**「策略即資料」是一切的前提**：若 prompt 與參數寫死在 `.py`，agent 永遠無法自我調整。Phase 1.4 的 `agent_strategy_profiles` 表是自我改善的物理基礎。

---

## 6. 風險與「不要做的事」

| 風險 | 對策 |
|------|------|
| 自主失控（agent 亂改策略） | bounded change + shadow + 人工核可 + rollback（D-3 護欄） |
| 過擬合近期雜訊 | Optimizer 觸發需累積足量樣本；shadow 比對而非直接上線 |
| tool-use 迴圈成本爆炸 | 每 agent 設 max tool-call 次數 + token 預算；沿用 A-004/A-007 告警 |
| 在壞地基上疊功能（本專案歷史問題） | Phase 0 為硬性先決條件 |

**現在不要做**：跳過 Phase 0 直接做 agent 化；在自我改善閉環未驗證前擴張 agent 數量；過早追求「全自主無人核可」。

---

## 7. 里程碑與驗收

| 里程碑 | 驗收標準 | 預估 |
|--------|----------|------|
| M0 地基穩固 | cron 連 3 日自動完成、回饋迴路通、無對外曝險 | 2 天 |
| M1 首批 Agent | ≥2 agent 具 tool-use 迴圈、行為由 DB profile 驅動 | 2 週 |
| M2 自我改善閉環 | 完成 1 次完整「評估→提案→shadow→升版」、可 rollback | 3 週 |
| M3 Gateway 層 | 新增 gateway 不改核心；能力可經 MCP/REST 對外 | 2 週 |
| M4 多 Agent 協作 | Orchestrator 動態委派；registry runtime 強制執行 | 持續 |

---

## 8. 一句話總結

> 轉型路徑是 **「先修地基 → 把 node 升級為會用工具的 agent → 接上策略即資料的自我改善閉環 → 抽象出 gateway 層 → 最後才是多 agent 協作」**。
>
> 順序不可顛倒：沒有穩固地基，agent 化只是更複雜的故障；沒有「策略即資料」，agent 永遠無法自我改善；沒有 gateway 抽象，每接一個介面都是一次硬寫死。
