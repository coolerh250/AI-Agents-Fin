# System Identity Reassessment
_獨立架構稽核 — 第三階段_
_稽核日期：2026-05-19_

---

## 問題：這套系統「到底」是什麼？

不沿用任何既有描述（不用「AgentOS」「multi-agent」「平台」）。純以 runtime 與 source 證據重新定義。

---

## 逐一檢驗候選身分

| 候選身分 | 成立條件 | 本系統是否符合 | 判定 |
|----------|----------|----------------|------|
| **automation scripts** | 一組排程腳本，固定步驟完成例行任務 | daily_run.sh + 數個 .py，固定步驟 | ✅ 符合本質 |
| **AI workflow** | 編排多個 LLM 呼叫成一條有狀態的流程 | LangGraph 8 節點、WorkflowState 串接 | ✅ 符合 |
| **multi-agent workflow** | 多個具自主性的 agent 協作 | 無任何元件具 agent 自主性（見 agent 報告） | ❌ 不符合 |
| **orchestration system** | 有動態調度、依狀態決定路徑 | 固定 DAG、無條件邊、無動態調度 | ❌ 不符合 |
| **AI platform** | 可重用、多租戶、可擴充的基礎設施 | 單一用途、單一使用者、硬寫死領域邏輯 | ❌ 不符合 |
| **AgentOS** | agent 註冊/排程/資源治理的執行期核心 | 註冊表是不被讀取的 YAML；無 runtime 核心 | ❌ 不符合 |
| **autonomous system** | 能自我運轉、自我修正、無人介入 | cron 已壞、靠人工執行；回饋迴路斷裂 | ❌ 不符合 |

---

## 系統的真實身分

> **它是一套「以 LLM 為核心的單一用途批次分析管線」（a single-purpose, LLM-powered batch analytics pipeline），目前處於人工維生狀態。**

更精確的分層描述：

```
┌─ 本質：一支每日批次工作（batch job）
│   • 排程意圖：cron 每個交易日早晨
│   • 實際狀態：cron 已壞，靠人工執行
│
├─ 編排層：LangGraph 被當作 DAG 執行器
│   • 8 個固定節點、線性、無條件分支、無自主迴圈
│   • 6 個節點 = 各一次 LLM 呼叫；2 個 = 工具節點
│
├─ 智能層：6 次單路 LLM 推論（Haiku/Sonnet/Opus）
│   • 每次「組 prompt → invoke 一次 → 取文字」
│   • chief_strategist 會注入歷史 context（最接近 agent，仍非 agent）
│
├─ 周邊服務：2 個常駐 web 服務
│   • Streamlit 看板（觀測 + 持倉 CRUD）
│   • FastAPI LINE webhook（持倉指令）
│
└─ 支援設施：TiDB 持久化 + observability 表 + 規則式評估 + 告警腳本
    • 觀測寫入紮實；回饋迴路（backtest）斷裂
```

---

## 它「不是」什麼，以及為什麼會被誤認

| 被誤認為 | 誤認來源 | 真相 |
|----------|----------|------|
| multi-agent 系統 | 檔名 `market_analyst_agents.py`、節點皆稱「Agent」、`agent_registry.yaml` | 節點是無自主性的單次 LLM 呼叫 |
| AgentOS | `agentos_implementation_plan.md`、兩個註冊表 YAML | YAML 是描述性 metadata，runtime 不消費；無 OS 核心 |
| 自主/自我學習系統 | 「Adaptive Flywheel」「Memory Phase」命名 | 飛輪寫端未排程；記憶自 05-15 凍結 |
| 平台 | 「Enterprise AI Agent Studio」（pyproject 描述） | 單一用途、單一使用者、領域邏輯硬寫死 |

共同根因：**用「目標架構的語彙」描述「現階段的實作」。** 命名與文件投射的是願景，runtime 呈現的是現況。

---

## 一句話定性

**這是一個「做得相當用心、但被過度命名」的每日台股分析批次管線——它的真實價值在於那條穩定可重現的 LLM 分析流程，而不在於它名字裡的「Agent / OS / 平台」。**

下一份報告（priorities）將據此判斷：它現在最需要的，不是更多 agent 能力，而是讓既有的東西真正可靠地跑起來。
