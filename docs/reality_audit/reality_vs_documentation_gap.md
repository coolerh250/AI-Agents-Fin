# Reality vs Documentation Gap
_獨立架構稽核 — 第二階段_
_稽核日期：2026-05-19_

---

## 能力分級定義

| 等級 | 定義 |
|------|------|
| ✅ 真正存在 | code 在、runtime 在跑、產生實際效果 |
| 🟡 部分存在 | 機制存在但受限、空轉、或僅半條迴路 |
| 🟠 名稱存在未實作 | 有命名/schema/欄位，但無對應 runtime 行為 |
| 🔵 文件存在 runtime 不存在 | 文件描述為已完成，runtime 不成立 |
| ⚫ 死碼 / illusion | 程式碼存在但無人呼叫，或屬架構幻覺 |

---

## 能力總表

| 能力 | 文件聲稱 | 實況 | 等級 |
|------|----------|------|------|
| investment_workflow（8 節點 LLM 管線） | 完成 | code + runtime 皆真，結果可重現 | ✅ |
| 模型路由 Haiku/Sonnet/Opus | 完成 | 真的照設定路由 | ✅ |
| Observability 寫入（runs/events/cost/traces） | Phase 6 完成 | 真的持續寫入 | ✅ |
| Dashboard（7 tab） | 完成 | 常駐、真的查 DB | ✅ |
| LINE OTP 登入 | 完成 | 機制正確、實機有 token | ✅ |
| MCP token 認證 + env 隔離 + audit | Phase 1/2 完成 | 真實生效（抓到 unauthorized） | ✅ |
| Prompt injection 過濾 | 完成 | `_sanitize_title` 真的有 | ✅ |
| cron 每日自動化 | 「cron 排程自動觸發」 | **daily_run.sh cron 自 05-15 起權限失敗** | 🔵 |
| backtest_agent（回測） | 完成、Phase 5 | **未排程，自動 path 完全不跑** | 🔵 |
| Adaptive Flywheel | Phase 1/2 完成 | 讀端活、**寫端（backtest）凍結** → 飛輪不轉 | 🟡 |
| 準確率記憶注入 chief_strategist | Phase 3 完成 | 機制真、**market_actuals 枯竭 → 空轉** | 🟡 |
| Episodic memory（session_episodes） | Phase 4 完成 | **只寫、無讀回 inference 的路徑** | 🟠 |
| LangGraph checkpointer / resume-on-failure | Phase 4 完成 | **套件未裝 → 退化 MemorySaver；無 resume 入口** | 🔵 |
| Telegram 推播通道 | 完成 | **.env 未設定 → 全 skip** | 🔵 |
| 告警系統 A-001~A-010 | 完成 | CRITICAL（LINE）可送；**WARNING/INFO（Telegram）全靜默** | 🟡 |
| Agent 評估框架 | 完成 | 規則式評分真實運作（daily_run Step 4） | ✅ |
| AgentOS / Agent Registry | Phase 1 完成 | **2 個 YAML 純 metadata，runtime 完全不讀取** | 🟠 |
| Tool governance / 權限矩陣 | Phase 2 完成 | `validate_tool_permission` fail-open 且幾乎未被呼叫 | 🟠 |
| multi-agent system | 隱含於命名 | 無任何元件具 agent 自主性 → 為 LLM pipeline | ⚫ illusion |
| supervisor/reviewer 階層 | registry 有欄位 | runtime 無調度邏輯 | ⚫ illusion |
| 向量記憶 / 語意檢索 | 「Phase 5 預備」 | 完全不存在 | 🟠 |
| LINE webhook 簽章驗證 | registry「requires_auth」 | **未設 secret → 驗證跳過** | 🔵 |
| Snapshot HMAC 完整性 | 「P0 修復」 | 未設金鑰 → no-op；且設計上不中止 | 🟡 |
| maintenance_agent | 完成 | 未排程；含 asyncio.run anti-pattern | 🔵 |
| `finance_mcp_server.py` / `system_inspector.py` | registry 列為 active | 已被取代、無人呼叫 | ⚫ 死碼 |
| `main.py` | — | `print("Hello")` 樁 | ⚫ 死碼 |

---

## 落差的三種型態

### 型態 A — 「Phase 完成」但回饋迴路斷裂
backtest 未排程，是單一最大的隱形落差。它連帶讓 **Flywheel、準確率記憶、A-009/A-010 告警、eval 的 direction_correct** 全部空轉。文件逐一宣稱這些 Phase「完成」，但它們共用一條沒有被接上的上游。

### 型態 B — 「Phase 完成」但 config 缺一塊
checkpointer（缺套件）、Telegram（缺 env）、webhook 簽章（缺 secret）、HMAC（缺金鑰）。程式碼都寫了，但部署設定沒補齊，且程式以「優雅降級」的方式靜默吞掉——降級得太安靜，以致看起來像正常。

### 型態 C — 命名/metadata 造成的 illusion
「AgentOS」「Agent Registry」「multi-agent」「supervisor」——這些是描述性文件與 YAML，runtime 不消費。它們不是謊言，而是「設計意圖」被當成「已實現能力」來敘述。

---

## 文件最不可信的五個聲稱

1. 「cron 排程自動觸發」——cron 已壞 4 天。
2. 「resume-on-failure checkpointer」——退化為 MemorySaver，無 resume。
3. 「Adaptive Flywheel 運作中」——飛輪寫端未排程，停轉。
4. 「告警系統」——一半的告警靜默送不出。
5. 「multi-agent / AgentOS」——實為單路 LLM pipeline + 描述性 YAML。

---

## 結論

文件不是造假，而是 **以「Phase 完成」為單位累積敘述，卻沒有人回頭驗證「整體 runtime 是否仍然成立」**。每個 Phase 各自被宣告完成，但 (a) 共用的上游（backtest）斷了沒人發現，(b) 部署 config 缺口被優雅降級藏起來，(c) 設計意圖（AgentOS）被寫成既成事實。

**真相只能以 runtime 與 source 為準——而 runtime 顯示：可運作的核心比文件聲稱的小，且正處於人工維生狀態。**
