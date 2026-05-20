# Workflow Reality Analysis
_獨立架構稽核 — 第一階段 3/8_
_稽核日期：2026-05-19｜方法：直接由 investment_workflow.py / market_analyst_agents.py / daily_run.sh source 逆向_

---

## 1. 真正會執行的 workflow 只有一個

`investment_workflow.py` 是唯一被排程鏈（daily_run.sh）觸及的工作流。`backtest_agent.py`、`agent_orchestrator.py` 雖然都是完整的 LangGraph 圖，但無排程 → 不在自動 execution path。

---

## 2. daily_run.sh 真實執行順序（逐步）

```
Step 1  test_collection.py        → 開 market_data_server.py MCP 子程序，並發 4 工具
                                    → 寫 market_snapshot.json + collection_journal.jsonl
Step 2  investment_workflow.py    → LangGraph 8 節點（見下）
Step 2.5 非擁有者持股推播          → get_all_portfolio_users() 排除 owner
                                    → 實機只有 1 使用者 → non_owners 為空 → 永遠 skip
Step 3  fallback 推播             → 查 tool_audit_log 當日是否已推播，未推則補送
Step 4  evaluation_runner.py      → 規則式評分，寫 eval_runs / eval_results
Step 5  cleanup_expired_lessons() → strategy_lessons 過期歸檔
```

`set -euo pipefail` → 任一 Step 非零退出會中止整個腳本（Step 4 有 `|| echo` 保護，其餘無）。

---

## 3. investment_workflow 真實圖結構（build_graph）

docstring 寫「5 節點」，**實際 build_graph() 為 8 節點**：

```
START → data_collector → ┬→ chip_analyst ─┐
                         └→ tech_analyst ─┴→ chief_strategist → portfolio_manager
        → format_agent → save_to_db → send_notification → END
```

| # | 節點 | 模型 | 性質 |
|---|------|------|------|
| 1 | data_collector | Haiku 4.5 | 單次 LLM 呼叫，解析 snapshot → JSON |
| 2a | chip_analyst | Sonnet 4.6 | 單次 LLM 呼叫 |
| 2b | tech_analyst | Sonnet 4.6 | 單次 LLM 呼叫 |
| 3 | chief_strategist | Opus 4.7 + thinking | 單次 LLM 呼叫 |
| 4 | portfolio_manager | Sonnet 4.6 | 單次 LLM 呼叫 |
| 5 | format_agent | Haiku 4.5 | 單次 LLM 呼叫 |
| 6 | save_to_db | 無 LLM | DB 寫入 |
| 7 | send_notification | 無 LLM | 推播 |

**關鍵性質：每個節點 = 「組 prompt → 呼叫一次 LLM → 回傳 dict」。沒有迴圈、沒有條件分支（add_conditional_edges 完全未使用）、沒有工具呼叫迴圈、沒有重試以外的自主決策。這是一條固定的線性 DAG，不是 agentic workflow。**

chip_analyst 與 tech_analyst 在圖上是平行分支，但 LangGraph 同步執行 → 實際為循序執行兩次 LLM 呼叫，非真並行。

---

## 4. 真正的 state flow

`WorkflowState`（TypedDict，8 欄）為唯一狀態載體，逐節點累加：

```
snapshot ─(collector)→ raw_market_data ─(chip/tech)→ chip_report / tech_report
        ─(chief)→ final_brief ─(portfolio)→ portfolio_advice
        ─(format)→ final_report ─(save)→ db_row_id ─(notify)→ END
```

state 全程在記憶體中。checkpointer 名義上是 SqliteSaver，**實機因套件未安裝退化為 MemorySaver** → state 不落地。

---

## 5. 真正的 failure path

| 機制 | 實況 |
|------|------|
| 節點內 LLM 失敗 | `_llm()` 有 `.with_retry(stop_after_attempt=3, 指數退避)`，僅針對 RateLimit/Timeout/Connection 三類 |
| data_collector JSON 解析失敗 | catch → 回傳空 dict + emit `fallback_activated` 事件 |
| chip/tech 拿不到 collector 輸出 | fallback：直接用 raw snapshot 原始資料 |
| save_to_db MCP 失敗 | catch → fallback 直接呼叫 `database_tools.save_brief()` |
| send_notification MCP 失敗 | catch → fallback 直接呼叫 `messenger_tools.send_line/telegram` |
| 節點未捕捉的例外 | 冒泡至 `graph.invoke()` → `finish_workflow_run(failed)` → re-raise → 程序退出 |
| 快照過舊（>12h） | main() 中止 workflow，發 A-001+A-005 |

**「resume-on-failure」是名義功能**：build_graph 註解宣稱 checkpointer 可 resume，但 (a) 套件未裝退化為 MemorySaver，(b) main() 每次以新 `run_id` 作 thread_id，(c) 無 `--resume` CLI。**workflow 失敗後只能整條重跑。**

---

## 6. 真正的 retry / interrupt behavior

- **retry**：僅節點內 LLM API 層級的 3 次退避重試。節點層級無重試；workflow 層級無重試。
- **interrupt**：LangGraph 的 `interrupt()` / human-in-the-loop / `add_conditional_edges` 完全未使用。無中斷點、無人工審核關卡（`approval_required` 在 registry 中全為 false）。
- **dedup**：notification_server 以 tool_audit_log 當日紀錄去重；daily_run Step 3 亦再查一次。這是唯一的「冪等」保護。

---

## 7. 與既有文件的落差

| 文件聲稱 | 實況 |
|----------|------|
| investment_workflow docstring「5 節點」 | 實為 8 節點 |
| 「Phase 4 checkpointer — enables resume-on-failure」 | 套件未裝、無 resume 入口 → 功能不存在 |
| chip/tech「平行」 | 圖上平行、runtime 循序 |
| daily_run「cron 自動」 | cron 已壞 4 天，靠人工執行 |

---

## 結論

investment_workflow 是一條 **8 節點、固定線性、無條件分支、無自主迴圈** 的 LLM 管線。它的可靠性來自「多層 fallback + LLM 層重試 + dedup」，這部分是務實且真實的。但「checkpointer / resume」是 architectural illusion。它能跑、結果可重現，但本質是 **a deterministic LLM pipeline orchestrated by LangGraph as a DAG executor** —— 不是會自我規劃、自我調整路徑的工作流。
