# Next-Stage Reality-Based Priorities
_獨立架構稽核 — 第四階段_
_稽核日期：2026-05-19_

---

## 判斷原則

優先序只依「reality assessment 的證據」決定，不依既有 roadmap。核心提問：**「這套系統現在最缺的是什麼？」** —— 答案不是更多功能，而是讓既有功能真正可靠地跑。

候選方向逐一裁決：

| 候選方向 | 現在需要嗎 | 理由 |
|----------|:--:|------|
| reliability（可靠性） | **是 — 第一順位** | cron 已壞、回饋迴路斷、checkpointer 失效 |
| runtime control（執行控制） | 是 | 服務靠 nohup、無 supervisor、無自動復原 |
| governance（治理） | 部分 | 安全邊界有 4 個真實漏洞，須補 |
| observability | 否 | 已是系統最強項；只需修告警出口 |
| architecture simplification | 是（中期） | 死碼、過度命名、未用抽象需收斂 |
| queue system | **否** | 單一每日批次，無併發壓力，不需要 |
| orchestration redesign | **否** | 線性 DAG 對此用途剛好，重設計是過度工程 |
| memory redesign | **否** | 問題不在記憶設計，在回饋迴路沒接上 |
| autonomous layer | **否** | 在連 cron 都跑不動時加自主層是本末倒置 |
| distributed execution | **否** | 單機單用途，無此需求 |

---

## 優先序（P0 → P3）

### P0 — 止血：讓系統回到「真的會自動跑」（數小時）

| # | 行動 | 依據 |
|---|------|------|
| P0-1 | `chmod +x daily_run.sh`，並改 crontab 為 `bash /…/daily_run.sh` 以免再被權限重置 | runtime 報告：cron 自 05-15 失敗 |
| P0-2 | 設定 `LINE_WEBHOOK_SECRET` 並驗證簽章生效 | security S-1：webhook 可被偽造 |
| P0-3 | TiDB 改綁 `127.0.0.1` 或加 ufw 限 4000/8501/8502 來源；TiDB 改用非 root 應用帳號 | security S-2/S-3 |
| P0-4 | 從 `.claude/settings.local.json` 移除明文 sudo 密碼；確認該檔已被 gitignore；輪換 itadmin 密碼 | security S-4 |

P0 完成後，系統才算「不靠人工維生」。

### P1 — 接回斷裂的迴路（1–2 天）

| # | 行動 | 依據 |
|---|------|------|
| P1-1 | 把 `backtest_agent.py` 排進 daily_run.sh（Step 2 後、評估前）或獨立 cron | workflow/memory 報告：backtest 未排程是最大隱形落差 |
| P1-2 | 設定 `TELEGRAM_BOT_TOKEN/CHAT_ID`，或把 WARNING/INFO 告警改走 LINE | observability：一半告警靜默 |
| P1-3 | 安裝 `langgraph-checkpoint-sqlite`，或移除 checkpointer 程式碼與「resume」宣稱（二擇一，別留半套） | workflow 報告：checkpointer illusion |

P1-1 一旦完成，market_actuals、strategy_lessons、準確率注入、A-009/A-010 會同時恢復——**一個修正解開五個空轉能力**。

### P2 — 治理與真相對齊（3–5 天）

| # | 行動 | 依據 |
|---|------|------|
| P2-1 | 更新 `tool_registry.yaml` / `agent_registry.yaml` 至現行檔名與工具；或若不打算讓 runtime 消費它們，明確標註為「設計文件」 | gap：註冊表全面過時 |
| P2-2 | 刪除死碼：`main.py`、`finance_mcp_server.py`、`system_inspector.py` | inventory：3 個明確死碼 |
| P2-3 | 決定 session_episodes 去留：要嘛接上讀回 inference 的路徑，要嘛停止寫入 | memory：唯寫死記憶 |
| P2-4 | 決定 `validate_tool_permission` 去留：要嘛真的在節點呼叫並 fail-closed，要嘛移除 | security S-6 |
| P2-5 | 為兩個常駐服務（dashboard/webhook）建立 systemd unit | runtime：nohup 無法重開機復原 |

### P3 — 架構收斂與誠實命名（持續）

| # | 行動 | 依據 |
|---|------|------|
| P3-1 | 把文件與命名對齊現實：移除或標註「AgentOS / multi-agent / autonomous」等尚未實現的語彙 | identity / gap 報告 |
| P3-2 | maintenance_agent：修 `asyncio.run` anti-pattern 後排程，或正式標為停用 | inventory |
| P3-3 | 統一 progress.md 與本稽核結論，建立「能力等級」欄位（✅/🟡/🟠） | gap 報告 |

---

## 明確「現在不要做」的事

- **不要加向量記憶 / 語意檢索** —— 記憶的問題是迴路斷了，不是檢索不夠聰明。先讓 3 筆教訓變成持續增長的教訓。
- **不要重設計 orchestration** —— 線性 DAG 對「每日一次台股分析」剛好夠用。
- **不要加 autonomous layer / multi-agent** —— 在 cron 都修不好時談自主，是把幻覺疊更高。
- **不要建 queue / 分散式執行** —— 無併發需求。

---

## 一句話結論

**這套系統下一階段最需要的是 reliability，不是新能力。** 用幾小時止血（P0）、一兩天接回斷裂迴路（P1），就能讓「文件早已宣稱完成」的東西第一次真正運作。在那之前，任何新功能都只是疊在不會自己跑的地基上。
