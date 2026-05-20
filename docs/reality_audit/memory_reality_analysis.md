# Memory Reality Analysis
_獨立架構稽核 — 第一階段 5/8_
_稽核日期：2026-05-19｜方法：追蹤 DB schema、寫入/讀取程式碼、execution path，並以實機 row count 驗證_

---

## 評估標準

不看名稱。對每一種「記憶」，驗證三件事：
1. execution path 是否真的寫入？
2. execution path 是否真的讀取？
3. 讀取的內容是否真的影響 inference（進入 LLM prompt）？

---

## 1. 各類記憶實況

| 宣稱的記憶類型 | 實作載體 | 寫入 | 讀取並影響 inference | 實機資料量 | 判定 |
|----------------|----------|:--:|:--:|------|------|
| Long-term / 持久記憶 | TiDB 各表 | ✅ | ⚠️部分 | — | **部分存在** |
| Episodic memory | `session_episodes` 表 | ✅ save_to_db_node | ❌ **無人讀來注入** | 3 筆（1 筆有結果） | **寫了但沒在用** |
| 預測準確率記憶 | `daily_briefs`⋈`market_actuals` | ✅ | ✅ 注入 chief_strategist | actuals 僅 3 筆且凍結 | **存在但資料枯竭** |
| 策略教訓（Flywheel） | `strategy_lessons` 表 | ⚠️ 僅 backtest 觸發 | ✅ 注入 chief_strategist | 3 筆，自 05-15 凍結 | **讀端活、寫端死** |
| Semantic memory | — | — | — | — | **不存在** |
| Vector retrieval / embedding | — | — | — | — | **不存在** |
| Memory ranking | `get_relevant_lessons` SQL CASE 評分 | — | ✅ | — | **存在（規則式，非語意）** |
| Memory governance | `strategy_lessons` 90 天 TTL + `is_active` + `cleanup_expired_lessons` | ✅ | — | — | **部分存在（僅 TTL 歸檔）** |

---

## 2. 逐項程式碼證據

### Episodic memory — 寫了，但沒有讀回路

- 寫：`save_to_db_node` → `log_session_episode()`，含 regime tag 自動推導（regime_sox / regime_foreign_oi）。
- 讀：`database_tools.get_session_episode()` 存在，呼叫者只有 `evaluation_runner`（重建 raw_market_data 用）與 `lesson_writer`（取 regime 用）。
- **關鍵**：沒有任何程式把 session_episodes 的歷史內容注入到 LLM prompt。`agent_registry.yaml` 宣稱 chief_strategist `memory_access.reads: db.session_episodes`——**程式碼中無此讀取**。
- 判定：episodic memory 是「只寫不讀回 inference」的死記憶。實機僅 3 筆、1 筆有結果。

### 準確率記憶 — 真的注入，但資料源枯竭

- `chief_strategist_node` 確實呼叫 `get_recent_accuracy_context(days=14)`，結果（≤800 字）附加到 user prompt。✅ 真的影響 inference。
- 但該函式是 `daily_briefs JOIN market_actuals`。market_actuals 由 backtest_agent 寫入，而 **backtest 未排程 → market_actuals 凍結在 05-15（僅 3 筆）**。
- 結果：對近期交易日，JOIN 無對應 actuals → 回傳空字串 → chief_strategist 實際上拿不到準確率回饋。
- 判定：機制真實存在，但因上游 backtest 斷線而「空轉」。

### 策略教訓（Adaptive Flywheel）— 半條迴圈

- 讀：`chief_strategist_node` → `lesson_retriever.get_lesson_context()` → `get_relevant_lessons()`，依 regime 比對 + 近期性 + error_type 的 SQL CASE 評分排序，取 top 3 注入 prompt（≤600 字）。✅
- 寫：`lesson_writer.write_lesson()` **只被 `backtest_agent.save_accuracy_node` 呼叫**。backtest 未排程 → 寫端不執行。
- 實機：strategy_lessons 3 筆，皆 ≤ 05-15。
- 判定：Flywheel 的「retrieval→inference」半邊是活的；「outcome→lesson」半邊是死的。**這是一個轉不動的飛輪。**

### 無語意記憶 / 無向量檢索

- 全 repo 無 embedding、無向量庫（Chroma / TiDB Vector / FAISS 皆無）、無相似度檢索。
- `get_relevant_lessons` 的「ranking」是 SQL `CASE WHEN regime=... THEN 20` 的標籤加權，屬規則式比對，非語意檢索。

---

## 3. 跨執行（cross-execution）記憶是否生效？

| 路徑 | 是否生效 |
|------|----------|
| 今日 brief / 預測 → 寫 daily_briefs → 影響未來 chief_strategist | ⚠️ 需 market_actuals 配對；目前斷線 |
| 今日 regime → 寫 session_episodes → 影響未來 | ❌ 無讀回路 |
| 過去失誤 → strategy_lessons → 注入未來 chief_strategist | ⚠️ 讀回路在，但寫端凍結；現存 3 筆教訓仍會被注入 |

**結論：理論上有跨執行記憶迴路，但目前唯一還「活著」的，是把 05-13~05-15 那 3 筆舊教訓反覆注入每天的 chief_strategist。記憶在凍結狀態下被一再重播。**

---

## 4. 與既有文件的落差

| 文件聲稱 | 實況 |
|----------|------|
| `agent_registry.yaml`：chief_strategist reads `db.session_episodes` | 程式碼無此讀取 |
| progress.md：「Memory Phase 0/1」「向量記憶預備」 | 向量記憶不存在；session_episodes 為唯寫表 |
| 「Adaptive Flywheel」 | 飛輪寫端（backtest）未排程，實際停轉 |

---

## 結論

系統的「記憶」實況：
- **真實存在**：TiDB 持久化、strategy_lessons 的讀取注入、規則式 ranking、TTL 治理。
- **存在但空轉**：準確率注入（缺 actuals）、Flywheel（缺 backtest）。
- **寫了沒用**：session_episodes（無讀回 inference 的路徑）。
- **完全不存在**：語意記憶、向量檢索、embedding。

最準確的描述是：**一套以 SQL 表為基礎的「context injection」機制，而非真正的記憶系統**。它最致命的問題不是缺向量檢索，而是 **回饋迴路（backtest→actuals→lesson）的寫端沒有被排程，導致整個記憶在 2026-05-15 之後凍結。**
