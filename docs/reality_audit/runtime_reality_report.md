# Runtime Reality Report
_獨立架構稽核 — 第一階段 2/8_
_稽核日期：2026-05-19_

---

## 稽核方法

SSH 至 `ai-agents-server`（itadmin@10.0.1.20）實機檢查：`crontab -l`、`ps aux`、`ss -tlnp`、`ls -la`、`tail logs/`、以及透過 `uv run python` 直接查詢 TiDB 各表 row count 與內容。**所有結論以實機輸出為證據。**

---

## 1. Cron 排程實況

```
CRON_TZ=Asia/Taipei
0  8 * * 1-5  /home/itadmin/ai_agent_studio/daily_run.sh        >> logs/daily_run.log
20 8 * * 1-5  cd ... && ~/.local/bin/uv run python alert_runner.py        >> logs/alerts.log
0  9 * * 0    cd ... && ~/.local/bin/uv run python alert_runner.py --weekly >> logs/alerts.log
```

### 🔴 重大發現：daily_run.sh cron 自 2026-05-15 起失敗

`logs/daily_run.log` 末端實機輸出：

```
[2026-05-15 08:21:21] ===== daily run 完成 =====
/bin/sh: 1: /home/itadmin/ai_agent_studio/daily_run.sh: Permission denied
/bin/sh: 1: /home/itadmin/ai_agent_studio/daily_run.sh: Permission denied
```

- `ls -la daily_run.sh` → `-rw-rw-r--`（**無 execute bit**）
- crontab 直接執行 `.sh` 路徑（非 `bash daily_run.sh`），故需 +x
- 檔案於 2026-05-19 03:45 被改寫（git pull / scp 重置權限）
- **最後一次成功的自動 daily run 是 2026-05-15 08:21**

**結論：每日自動化工作流的 cron 已連續失敗約 4 個交易日。**

### alert_runner cron 仍正常

alert_runner 經 `uv run python` 直接呼叫（非 .sh），不受權限問題影響 → **每個工作日 08:20 仍會執行**。因 daily_run 已壞、08:20 前通常無當日 workflow_run → **A-001（工作流未執行）CRITICAL 告警極可能每個交易日都在發 LINE**。系統其實一直在求救。

---

## 2. 真正運行中的 process

| Process | PID | 啟動方式 | 狀態 |
|---------|-----|----------|------|
| `uvicorn line_webhook:app` :8502 | 21675 | `nohup ... &`（手動） | 執行中 |
| `streamlit run dashboard.py` :8501 | 23442 | `nohup ... &`（手動，啟動腳本含 `git pull`） | 執行中 |
| investment_workflow | — | 無常駐；經 cron/手動觸發後即結束 | 批次 |

無 systemd、無 supervisor、無 process manager。常駐服務靠 `nohup`，重開機不會自動復活。

---

## 3. 工作流實際上「怎麼」在跑

daily_briefs 有 2026-05-18、05-19 的資料、workflow_runs 在這兩天各有 4 筆 success——但 cron 自 05-15 已壞。唯一解釋：

> **使用者一直在「手動」執行 `investment_workflow.py`。**

證據：`investment_brief_20260518_0732.txt / 0740 / 0745`（同日 3 個檔，間隔數分鐘）為典型反覆手動執行。`market_snapshot.json` 與 `collection_journal.jsonl` 時間戳為 2026-05-19 03:08 → test_collection.py 亦為手動執行。

**runtime 真相：系統目前是「人工驅動的半自動腳本」，而非文件所稱的「cron 全自動」。**

---

## 4. DB 連線與資料寫入實況（TiDB agent_memory）

實測各表 row count：

| 表 | rows | 判讀 |
|----|------|------|
| daily_briefs | 5 | 05-13/14/15/18/19 |
| market_actuals | **3** | **僅 05-13/14/15 — 自 backtest 停跑後無新資料** |
| cost_logs | 142 | LLM 成本持續寫入中 |
| llm_traces | 96 | LLM trace 持續寫入中 |
| workflow_runs | 15 | 05-18、05-19 各 4 筆 success |
| workflow_events | 31 | 事件持續寫入 |
| audit_log | 6 | dashboard 變更稽核 |
| tool_audit_log | 13 | MCP 呼叫稽核（save_brief ok×9 / unauthorized×2、push ok×2） |
| session_episodes | 3 | **僅 1 筆有 actual 結果** |
| eval_runs | 4 | 05-13/14/15/19（**缺 05-18**） |
| eval_results | 42 | — |
| strategy_lessons | 3 | **自 05-15 後無新增** |
| stock_info | 2 | — |
| user_portfolio | 2 | **僅 1 個 line_user_id** |
| login_tokens | 1 | — |

### 由 row count 推導的 runtime 真相

1. **observability 表（cost_logs / llm_traces / workflow_runs / events）持續有寫入** → 觀測寫入鏈是真的。
2. **market_actuals 凍結在 05-15** → backtest_agent 確實沒在跑（佐證 cron 缺項）。
3. **strategy_lessons 凍結在 05-15、僅 3 筆** → Adaptive Flywheel 的「寫端」實質停擺。
4. **session_episodes 僅 3 筆、1 筆有結果** → episodic memory 幾乎空。
5. **user_portfolio 僅 1 個使用者** → 多使用者功能已建好但實際只有 owner 一人 → daily_run Step 2.5（非擁有者推播）永遠 skip。
6. **eval_runs 缺 05-18** → 因 cron daily_run 壞掉、手動執行 workflow 不含 Step 4。

---

## 5. 真正的 API / 模型使用

- **Anthropic**：每次 workflow run 約 $0.07–0.08（workflow_runs.total_cost_usd 實測）。15 次累計 cost_logs 142 筆。模型路由（Haiku/Sonnet/Opus）真的有照 market_analyst_agents.py 的設定執行。
- **MCP**：tool_audit_log 顯示 `persistence.save_brief` 與 `notification.push_investment_brief` 都有 `ok` 紀錄 → MCP 路徑真的有被走到（非總是 fallback）。
- **Telegram**：`.env` 無 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` → 所有 `send_telegram()` 回傳 `skipped` → Telegram 通道實際上不存在。
- **langgraph checkpointer**：`uv pip list` 僅有 `langgraph-checkpoint 4.1.0`（基礎套件），**無 `langgraph-checkpoint-sqlite`**；`checkpoints.db` 檔案不存在 → `build_graph()` 的 `from langgraph.checkpoint.sqlite import SqliteSaver` 必定 ImportError → **實際使用 MemorySaver（in-process，不持久化）**。

---

## 6. 網路曝險（ss -tlnp）

| Port | 服務 | 綁定 | 風險 |
|------|------|------|------|
| 8501 | streamlit | `0.0.0.0` | 全網段可達 |
| 8502 | uvicorn webhook | `0.0.0.0` | 全網段可達 |
| 4000 | TiDB | `0.0.0.0` | **root 帳號 DB 全網段可達** |

`ufw status` 無輸出 → **無主機防火牆**。詳見 security 報告。

---

## 結論

| 元件 | code 存在 | runtime 真的在跑 |
|------|:--------:|:----------------:|
| investment_workflow | ✅ | ⚠️ 僅靠人工手動執行（cron 已壞 4 天） |
| alert_runner | ✅ | ✅（且因此每日發 A-001） |
| backtest_agent | ✅ | ❌ 完全未跑 |
| maintenance_agent | ✅ | ❌ 完全未跑 |
| dashboard / webhook | ✅ | ✅ 常駐（nohup，無 supervisor） |
| observability 寫入 | ✅ | ✅ |
| SqliteSaver checkpointer | ✅(code) | ❌ 套件未裝 → 退化為 MemorySaver |
| Telegram 通道 | ✅(code) | ❌ 未設定 → 全 skip |

**runtime 真相：這是一套「人工驅動、cron 已半壞、observability 仍運作」的批次腳本系統，而非自我運轉的自動化平台。**
