# Observability Reality Report
_獨立架構稽核 — 第一階段 7/8_
_稽核日期：2026-05-19｜方法：檢視 schema、寫入碼、查詢碼，並以實機 row count 與 log 驗證_

---

## 評估標準

不只看 table/schema 有沒有建。對每一項，驗證：是否真的寫入？是否真的被查詢？是否真的被 operational workflow 使用？

---

## 1. 各觀測能力實況

| 宣稱能力 | 實作 | 真的寫入 | 真的被查詢 | 判定 |
|----------|------|:--:|:--:|------|
| Structured logs（事件） | `workflow_events` 表 + `emit_event()` | ✅ 31 筆 | ✅ dashboard / alert_runner | **存在** |
| Execution correlation | `run_id`（UUID）貫穿 runs/events/cost/traces | ✅ | ✅ | **存在（單程序級）** |
| LLM tracing | `llm_traces` 表（prompt/response/token/finish） | ✅ 96 筆 | ⚠️ 僅 evaluation_runner 讀 | **存在** |
| Runtime metrics | `cost_logs`（token/cost/latency） | ✅ 142 筆 | ✅ dashboard 成本頁 | **存在（批次）** |
| Cost governance | cost_logs + 閾值 + `_print_cost_report` + A-004/A-007 | ✅ | ✅ | **存在** |
| Workflow run 生命週期 | `workflow_runs`（status/cost/duration） | ✅ 15 筆 | ✅ | **存在** |
| DB 變更稽核 | `audit_log` | ✅ 6 筆 | ⚠️ 無 UI 查詢 | **存在但少用** |
| 工具呼叫稽核 | `tool_audit_log` | ✅ 13 筆 | ✅ notification dedup 用 | **存在** |
| Alerting | `alert_runner.py`（A-001~A-010） | ✅ 排程執行 | — | **部分存在，見下** |
| Distributed tracing | — | — | — | **不存在** |
| 即時監控 / live dashboard | Streamlit（手動刷新、cache TTL 300s） | — | — | **不存在（屬批次查詢）** |
| Metrics 系統（Prometheus 等） | — | — | — | **不存在** |
| 日誌聚合 | loguru → stdout → `logs/*.log` 純文字檔 | ✅ | ⚠️ 僅人工 tail | **原始等級** |

---

## 2. 觀測「寫入鏈」是真的

實機 row count 證明寫入持續發生：cost_logs 142、llm_traces 96、workflow_runs 15、workflow_events 31、tool_audit_log 13。這不是空殼 schema——**telemetry 寫入是系統真實且運作中的部分**。

寫入設計上「fail-silent」（`record_usage` / `log_event` / `log_tool_call` 皆 try/except 吞例外），觀測失敗不會打斷 workflow。這是合理的取捨。

---

## 3. 觀測「被使用」是真的，但僅止於 post-hoc 批次

- `dashboard.py` 7 個 tab 直接 SQL 查詢上述表（成本、事件、評估、Flywheel、健康）。✅
- `alert_runner.py` 查 workflow_runs / events / cost_logs / eval_runs 產生告警。✅
- `investment_workflow._print_cost_report` 跑完印成本表。✅

但全部是**事後批次查詢**：無串流、無即時、無 trace span、無跨服務關聯。`run_id` 是唯一關聯鍵，且僅在單一程序內有意義。

---

## 4. 🔴 告警鏈有一半是斷的

`alert_runner` 的分流邏輯：
- CRITICAL（A-001/002/003）→ `send_line` + `send_telegram`
- WARNING / INFO（A-007/008/009/010、weekly digest）→ **僅 `send_telegram`**

但 `.env` **無 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`** → `send_telegram()` 一律回傳 `{"status":"skipped"}`。

**後果：所有 WARNING 與 INFO 等級的告警（成本異常、fallback 率過高、準確率過低、品質下滑、週報）實際上從未送達任何人。** 只有 3 個 CRITICAL 告警靠 LINE 還能送出。

附帶效應：因 daily_run cron 已壞，alert_runner 每個工作日 08:20 跑 `check_a001` → 偵測到當日無 workflow_run → 發 A-001 CRITICAL 到 LINE。**系統其實每天都在用 LINE 求救，只是沒人處理。**

---

## 5. 觀測資料的「可信度」問題

- A-004 成本閾值、A-007 thinking token —— 計算正確（per-run cost）。
- A-009 準確率、A-010 品質週對週 —— 依賴 `market_actuals` / `eval_runs`。因 backtest 未排程、market_actuals 凍結 → 這些告警**即使送得出去也是基於枯竭資料**，可能誤判或永不觸發。
- `eval_runs` 缺 05-18 → 觀測資料本身有洞（手動跑 workflow 不含評估）。

---

## 6. 與既有文件的落差

| 文件聲稱 | 實況 |
|----------|------|
| progress.md：「Phase 6 可觀測性」「告警系統 A-001~A-009」 | 表與寫入屬實；但 WARNING/INFO 告警因 Telegram 未設定而全數靜默 |
| 「distributed tracing」（隱含於 observability 命名） | 不存在；僅單程序 run_id 關聯 |

---

## 結論

Observability 是這套系統**做得最紮實的一塊**：結構化事件、run_id 關聯、LLM trace、成本與延遲、稽核表——全部真的有寫、真的被 dashboard 與 alert_runner 用。

但它有兩個真實缺口：
1. **它是 post-hoc 批次觀測，不是即時/分散式 tracing**——對單程序批次系統而言尚可接受。
2. **🔴 告警出口一半是斷的**：Telegram 未設定使所有非 CRITICAL 告警靜默；而唯一還通的 LINE 告警（A-001）正每天無人理會地觸發。**觀測得到，但告警送不到、沒人看——這讓 observability 的營運價值大打折扣。**
