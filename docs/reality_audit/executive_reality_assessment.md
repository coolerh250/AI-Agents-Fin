# Executive Reality Assessment
_獨立架構稽核 — 總結論_
_稽核日期：2026-05-19｜稽核者：Independent Architecture Auditor_
_稽核基礎：30 個原始碼檔全文閱讀 + ai-agents-server 實機 runtime 檢查 + TiDB 實機資料查詢_

---

## 這套系統真正是什麼

> **它是一條「以 LLM 為核心、單一用途的每日台股期貨分析批次管線」。**
> 它做的事是真的、結果可重現；但它**不是** multi-agent 系統、不是 AgentOS、不是自主平台——這些是命名與文件投射的願景，不是 runtime 的事實。
> 而且，它目前**沒有在自動運轉**：每日自動化的 cron 自 2026-05-15 起已失效，過去約 4 個交易日的產出全靠人工手動執行維持。

---

## 三句話總結

1. **核心是真的、且做得用心**：8 節點 LangGraph LLM 管線、模型分層路由、observability 寫入、TiDB 持久化、Streamlit 看板、LINE OTP、MCP token 治理——這些都真的在跑、有實機資料佐證。
2. **但它正在「人工維生」**：cron 壞了、回饋迴路（backtest）從未排程、checkpointer 因缺套件失效、Telegram 從未設定——一連串部署層缺口被程式的「優雅降級」安靜地藏了起來。
3. **而文件描述的是願景、不是現況**：「AgentOS」「multi-agent」「Adaptive Flywheel 運作中」「resume-on-failure」皆與 runtime 不符。

---

## 能力真相速覽

| 真正在運作 ✅ | 空轉 / 半殘 🟡 | 幻覺 / 死碼 ⚫ |
|---------------|----------------|----------------|
| 8 節點 LLM 分析管線 | cron 自動化（已壞 4 天） | multi-agent（實為單路 LLM 呼叫） |
| 模型路由 H/S/O | Adaptive Flywheel（寫端凍結） | AgentOS（YAML 不被 runtime 讀） |
| Observability 寫入 | 準確率記憶注入（actuals 枯竭） | supervisor 階層 |
| Dashboard / LINE webhook | 告警系統（一半靜默） | finance_mcp_server / system_inspector / main.py |
| MCP token + audit + env 隔離 | checkpointer（退化 MemorySaver） | 向量/語意記憶（從未存在） |
| 規則式評估框架 | session_episodes（只寫不讀回） | — |

---

## 最關鍵的 5 個發現

1. **🔴 cron 已壞 4 天**：`daily_run.sh` 失去執行權限，crontab 直呼 `.sh` 路徑 → 每日 `Permission denied`。最後一次自動成功是 2026-05-15。系統靠人工手動執行續命。
2. **🔴 一個斷點癱瘓五個能力**：`backtest_agent.py` 從未被排程 → `market_actuals` 凍結 → 準確率記憶、Adaptive Flywheel、A-009/A-010 告警、eval 的 direction_correct 全部一起空轉。
3. **🔴 安全周邊有 4 個真實漏洞**：LINE webhook 簽章驗證被關閉（可遠端偽造持倉指令）、TiDB 以 root 對 `0.0.0.0:4000` 開放、無主機防火牆、sudo 密碼明文存在 `.claude/settings.local.json`。
4. **🟡 告警出口一半是斷的**：Telegram 從未設定 → 所有 WARNING/INFO 告警靜默；唯一還通的 LINE A-001 正每天無人理會地觸發——系統其實一直在求救。
5. **⚫ 「Agent / AgentOS」是命名造成的 illusion**：無任何元件具備自主性、工具迴圈、規劃或委派。兩個註冊表 YAML 是描述性 metadata，runtime 從不讀取。

---

## 系統健康總評

| 面向 | 評級 | 一句話 |
|------|------|--------|
| 功能正確性 | 良 | LLM 管線跑得出合理結果 |
| 可靠性 | **差** | cron 壞、回饋迴路斷、靠人工維生 |
| 可觀測性 | 良 | 寫入紮實，但告警出口半斷 |
| 安全性 | **差** | 應用層細緻、基礎設施與網路邊界有 Critical 漏洞 |
| 文件可信度 | **差** | 以「Phase 完成」累積敘述，未驗證整體 runtime |
| 架構合理性 | 中 | 線性 DAG 對用途夠用；但過度命名、有死碼 |

---

## 給決策者的建議

**下一階段不要加任何新能力。** 證據明確指向 reliability：

- **數小時內（P0）**：修 cron 執行權限、補 webhook 簽章、收斂網路曝險、移除明文密碼 → 讓系統不再靠人工維生、堵住可遠端利用的漏洞。
- **1–2 天內（P1）**：把 backtest 排進每日流程 → 一次修正解開五個空轉能力；補 Telegram 或改 LINE 告警；checkpointer 二擇一（裝套件或拔程式）。
- **接著（P2/P3）**：更新註冊表、刪死碼、建 systemd、讓文件與命名對齊現實。

**完整優先序見 `next_stage_reality_based_priorities.md`。**

---

## 最終定性

> 這不是一個失敗的系統——它的 LLM 分析核心是紮實且有價值的。
> 它是一個 **「被過度命名、且正處於靜默故障狀態」的每日批次分析管線**。
>
> 它最需要的不是更聰明的 agent，而是有人把地基上那幾根鬆掉的螺絲鎖回去——
> 然後，誠實地用它真正的名字稱呼它。

---

### 本稽核產出文件（docs/reality_audit/）

第一階段（Reality Discovery）：
`reality_repository_inventory.md`、`runtime_reality_report.md`、`workflow_reality_analysis.md`、`agent_reality_assessment.md`、`memory_reality_analysis.md`、`mcp_reality_assessment.md`、`observability_reality_report.md`、`security_reality_report.md`

第二～四階段：
`reality_vs_documentation_gap.md`、`system_identity_reassessment.md`、`next_stage_reality_based_priorities.md`、`executive_reality_assessment.md`（本檔）
