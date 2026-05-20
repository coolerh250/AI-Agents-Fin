# Agent Reality Assessment
_獨立架構稽核 — 第一階段 4/8_
_稽核日期：2026-05-19｜方法：逐一檢視每個被稱為 agent 的元件，對照「自主性」實證標準_

---

## 評估標準

一個元件要算「真 agent」，須具備下列其中數項：自主行為（autonomous）、記憶使用、工具使用迴圈、規劃（planning）、委派（delegation）、評估/自我修正。

「組 prompt → 呼叫一次 LLM → 回傳結果」= function wrapper，不是 agent。

---

## 逐元件判定

| 元件 | 命名暗示 | 自主行為 | 記憶 | 工具迴圈 | 規劃 | 委派 | 真實分類 |
|------|----------|:--:|:--:|:--:|:--:|:--:|----------|
| data_collector | agent | ✗ | ✗ | ✗ | ✗ | ✗ | **workflow node**（單次 LLM） |
| chip_analyst | agent | ✗ | ✗ | ✗ | ✗ | ✗ | **workflow node** |
| tech_analyst | agent | ✗ | ✗ | ✗ | ✗ | ✗ | **workflow node** |
| chief_strategist | agent | ✗ | ⚠️讀注入 | ✗ | ✗ | ✗ | **memory-aware workflow node** |
| portfolio_manager | agent | ✗ | ⚠️讀 DB | ✗ | ✗ | ✗ | **workflow node**（含 yfinance 工具呼叫，但非迴圈） |
| format_agent | agent | ✗ | ✗ | ✗ | ✗ | ✗ | **workflow node** |
| save_to_db | agent | ✗ | — | ✗ | ✗ | ✗ | **utility node**（無 LLM） |
| send_notification | agent | ✗ | — | ✗ | ✗ | ✗ | **utility node**（無 LLM） |
| backtest_evaluator | agent | ✗ | ⚠️讀 DB | ✗ | ✗ | ✗ | **evaluation node**（未排程） |
| maintenance_agent | agent | ✗ | ✗ | ⚠️MCP | ✗ | ✗ | **workflow node**（未排程） |
| evaluation_runner | — | ✗ | — | ✗ | ✗ | ✗ | **utility module**（規則式，無 LLM） |
| lesson_writer / retriever | — | ✗ | DB | ✗ | ✗ | ✗ | **utility module** |
| alert_runner | — | ✗ | DB | ✗ | ✗ | ✗ | **utility module**（cron 腳本） |

---

## 詳細判讀

### 沒有任何元件具備真正的 agentic 特性

1. **autonomous behavior** — 無。每個節點的行為在編譯期固定，輸入決定輸出，無「自己決定下一步做什麼」。
2. **tool usage（迴圈）** — 無。Anthropic API 的 tool-use / function-calling 機制完全未使用。`portfolio_manager` 與 `maintenance_agent` 確實呼叫外部資料源（yfinance / MCP），但那是節點程式碼「在呼叫 LLM 之前」硬寫死的資料抓取，不是 LLM 自己決定要呼叫工具。LLM 從頭到尾只負責「讀 prompt、寫 text」。
3. **planning** — 無。沒有任何元件產生「計畫」再執行。chief_strategist 產出的是分析文字，不是可執行計畫。
4. **delegation** — 無。`agent_registry.yaml` 有 `supervisor: chief_strategist` 欄位，但那是**靜態 metadata，runtime 完全不讀取**。沒有任何 supervisor/orchestrator pattern 的程式碼。節點間是固定 graph edge，不是動態委派。
5. **memory usage** — 部分。`chief_strategist` 會把 `get_recent_accuracy_context()` + `get_lesson_context()` 注入 prompt；`portfolio_manager` 讀 `user_portfolio`。這是「讀取記憶並注入 context」，是 agent 特性中最弱的一種，且為單向（讀，不寫、不依結果調整策略）。
6. **evaluation / self-correction** — `evaluation_runner` 與 `backtest_agent` 構成評估迴圈，但：(a) 評估是規則式比對，非 agent；(b) 評估結果僅寫入 DB 供「下一次」chief_strategist 注入，無 runtime 自我修正；(c) backtest 未排程 → 自我修正迴圈實際斷開。

### 「LangGraph node = agent」是命名造成的錯覺

`market_analyst_agents.py` 檔名、`agent_registry.yaml`、各 docstring 一律用「Agent」稱呼這些節點。但程式碼證據顯示它們是 **prompt template + 一次 model.invoke()**。LangGraph 在此被當作 DAG 執行器，不是 multi-agent runtime。

---

## 重新分類總表

| 真實分類 | 元件 |
|----------|------|
| **workflow node**（單次 LLM，無自主性） | data_collector, chip_analyst, tech_analyst, portfolio_manager, format_agent, maintenance「agent」 |
| **memory-aware workflow node** | chief_strategist（唯一會讀取跨執行記憶並注入者） |
| **utility node**（無 LLM 的圖節點） | save_to_db, send_notification |
| **evaluation node** | backtest_evaluator（規則 + 一次 LLM 評語；未排程） |
| **utility module**（非圖節點的支援程式） | evaluation_runner, evaluation_metrics, lesson_writer, lesson_retriever, alert_runner, telemetry |
| **real agent** | **無** |
| **orchestration agent** | **無**（LangGraph graph compile 不算；無動態調度邏輯） |
| **autonomous layer** | **無** |

---

## 結論

系統內 **沒有任何一個元件符合「agent」的實證定義**。所有被稱為 agent 的東西，實為 LangGraph DAG 上的 **單次 LLM 呼叫節點** 或 **無 LLM 的工具節點**。最接近 agent 的是 `chief_strategist`——它會讀取歷史記憶——但它仍只是「讀 context → 呼叫一次 Opus → 回傳文字」，無迴圈、無規劃、無工具自主。

**「multi-agent system」是命名與文件造成的 illusion；實況是 single-pass multi-step LLM pipeline。**
