# AI Agent Studio — Documentation Index
**Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15_

全部文件位於 `docs/` 目錄，共 32 份，依主題分組如下。

---

## 一、基礎建設與部署

| 檔案 | 說明 |
|------|------|
| [deployment_guide.md](docs/deployment_guide.md) | Ubuntu 伺服器完整部署流程：SSH Key 設定、Python 3.14 + uv 環境建立、TiDB 連線、cron 排程設定，以及 `daily_run.sh` 自動化腳本說明 |
| [project_structure.md](docs/project_structure.md) | 程式碼庫目錄結構總覽：所有 Python 模組清單、`pyproject.toml` 依賴設定、開發環境（Windows/VS Code）與生產環境（Ubuntu `10.0.1.20`）對照，以及 cron 觸發時間（CST 08:20 工作日） |

---

## 二、系統架構與設計

| 檔案 | 說明 |
|------|------|
| [architecture_summary.md](docs/architecture_summary.md) | 執行長層級架構摘要：系統整體評估（功能完整、單人使用、~$2.54/月），最高風險清單（儀表板無認證、無 checkpointing、Opus 成本無上限），以及各風險的財務衝擊 |
| [langgraph_analysis.md](docs/langgraph_analysis.md) | LangGraph 三個圖的完整分析：圖結構清單（investment_workflow / backtest_agent / maintenance_agent）、WorkflowState TypedDict 欄位定義、節點拓撲 Mermaid 圖，以及每個 Node 的 Routing 邏輯 |
| [workflow_topology.md](docs/workflow_topology.md) | 工作流拓撲分類：investment_workflow 為「Hybrid DAG — 扇出 / 屏障合併 / 線性尾」，完整邊表（Edge Table），三個圖的編譯與執行模式對照 |
| [state_management.md](docs/state_management.md) | 三個 StateGraph 的 TypedDict schema 詳解：WorkflowState（8 欄位）、BacktestState（4 欄位）、AgentState（3 欄位），欄位生命週期、寫入節點對應關係，以及 None 值風險分析 |
| [production_architecture_recommendation.md](docs/production_architecture_recommendation.md) | 邁向生產級的三層路徑：**T1 立即修復**（Opus token cap、Dashboard 認證、checkpointer）、**T2 短期**（retry 機制、async P&L、snapshot 新鮮度驗證）、**T3 架構**（全 MCP 化、向量記憶體、多用戶支援），每項均附程式碼範例與工時估算 |

---

## 三、Agent 與工具清單

| 檔案 | 說明 |
|------|------|
| [agent_inventory.md](docs/agent_inventory.md) | 10 個 Agent 完整清冊：模型分配（Haiku × 4、Sonnet × 3、Opus × 1 + 2 無 LLM）、記憶類型、輸入輸出格式、Fallback 行為，以及 `backtest_evaluator` 與 `maintenance_agent` 的 cost 追蹤缺口 |
| [tool_inventory.md](docs/tool_inventory.md) | 所有可呼叫單元盤點：6 個 MCP Tool、14 個直接呼叫 Python 函數、7 個 LLM Invocation；**關鍵發現**：production workflow 完全繞過 MCP 層，`save_brief_to_db` 與 `send_brief_to_user` MCP 工具從未被任何 workflow client 呼叫（孤兒工具） |
| [tool_permission_matrix.md](docs/tool_permission_matrix.md) | 所有工具的權限等級矩陣（READ / WRITE / NOTIFY / EXTERNAL_READ / EXECUTE），按消費者分欄（cron 08:00、cron 08:20、backtest、dashboard、maintenance、未來 MCP client），標示今日認證狀態與建議認證需求 |
| [tool_risk_matrix.md](docs/tool_risk_matrix.md) | 所有工具的 CVSS-like 風險評分：最高風險為 `send_line()` / `send_telegram()` 憑證暴露（7.5）、財經新聞 Prompt Injection（7.0）、`_llm_opus()` token 爆炸（7.0），以及 `_engine()` 每次呼叫建立新連線池（5.5） |

---

## 四、MCP 架構

| 檔案 | 說明 |
|------|------|
| [mcp_migration_plan.md](docs/mcp_migration_plan.md) | MCP 化遷移規劃：分析哪些工具已是 MCP（維持並改善）、哪些應 MCP 化（DB 寫入、通知推播）、哪些不需要 MCP 化，附遷移優先順序與風險評估 |
| [recommended_mcp_architecture.md](docs/recommended_mcp_architecture.md) | 四個目的導向 MCP Server 的設計藍圖（市場資料、資料庫、通知、系統監控），最小權限原則（每個 Server 只持有所需憑證），寫入類工具須 internal API key 才可呼叫，附完整 `tool_audit_log` schema |

---

## 五、記憶體與檢索

| 檔案 | 說明 |
|------|------|
| [memory_analysis.md](docs/memory_analysis.md) | 記憶體架構概覽：三層記憶（WorkflowState 瞬態、TiDB 持久、`market_snapshot.json` 檔案快取），TiDB `agent_memory` 資料庫四張表的 schema，**無向量記憶體、無語意搜尋** |
| [memory_inventory.md](docs/memory_inventory.md) | 五層記憶分類法詳解（Prompt Memory → In-Process State → File System → Relational DB → External API），各層讀寫節點對應、持久化範圍、跨執行保留性分析 |
| [retrieval_flow.md](docs/retrieval_flow.md) | 四種檢索機制的流程圖（檔案讀取、精確 SQL 比對、時間窗口聚合、應用層快取），chief_strategist 無歷史資料輸入的設計缺口，無 fuzzy / embedding 搜尋 |
| [context_engineering_analysis.md](docs/context_engineering_analysis.md) | 各節點 context 組裝方式與 token 估算，陳舊 snapshot 資料風險（無新鮮度驗證），`final_brief` 重複傳遞的 context 膨脹問題，以及 P1 優先修復清單 |
| [memory_scalability_report.md](docs/memory_scalability_report.md) | 五類可擴展性風險：TiDB 資料列成長預測（`cost_logs` 年增 2,000 列）、無 LangGraph checkpointer 導致 Opus 計算浪費、無索引的 `trade_date` 查詢效能、JSON blob 無壓縮，以及跨執行記憶體洩漏風險 |
| [hybrid_memory_architecture_roadmap.md](docs/hybrid_memory_architecture_roadmap.md) | 四階段記憶體演進路徑：Phase 0（SQL 歷史注入）→ Phase 1（結構化向量嵌入）→ Phase 2（RAG pipeline）→ Phase 3（多用戶記憶隔離），附每階段程式碼範例與效益估算 |

---

## 六、安全性

| 檔案 | 說明 |
|------|------|
| [security_analysis.md](docs/security_analysis.md) | 三級嚴重度安全掃描：憑證管理（API key、TiDB 密碼、LINE token）、TLS 驗證繞過（`verify=False`）、Streamlit 儀表板無認證（LAN 可見持倉與成本）、Prompt Injection 風險於 `get_financial_news`，附每項修復建議 |
| [ai_security_review.md](docs/ai_security_review.md) | 全面 AI 安全審查：**Prompt Injection**（新聞標題注入、多跳鏈上下文劫持、已儲存注入）、**記憶體污染**（snapshot HMAC 缺失）、**Telegram Markdown 注入**、**孤兒 MCP 工具濫用**；Infrastructure 面（TiDB root 空密碼、Streamlit 無認證、API key 暴露）；Operational 面（無 audit log、無人工審核閘道）；附 14 項風險矩陣 |
| [threat_model.md](docs/threat_model.md) | STRIDE 威脅模型：5 個攻擊者側寫（外部內容、LAN 用戶、伺服器用戶、供應鏈、AI 模型誤用）；26 個威脅條目（S/T/R/I/D/E 各類）；3 個詳細攻擊情境（新聞注入改變建議、Streamlit 持久化注入、Debug log 憑證竊取）；每個威脅附可能性與衝擊評估 |
| [privilege_boundary_analysis.md](docs/privilege_boundary_analysis.md) | 最小權限分析：TiDB 所有元件共用 root（DDL 可刪表）；API Key 無模型範圍限制；LangGraph 節點信任等級（Haiku 輸出不應被 Opus 視為可信）；MCP 子程序繼承全部 `.env` 機密；Streamlit 任意 LAN 用戶可寫入；附每項建議 SQL/Python 修復 |
| [sandbox_recommendation.md](docs/sandbox_recommendation.md) | 四層沙箱設計：L1 輸入沙箱（snapshot HMAC、新聞標題清洗、LLM 輸出 Schema 強制驗證）、L2 LLM 信任降級（節點信任標籤、Markdown 移除、輸出長度上限）、L3 執行沙箱（專用 OS 用戶、最小 MCP env、systemd 安全指令）、L4 Streamlit 認證（SSH Tunnel 或 streamlit-authenticator）；附優先矩陣與工時估算 |
| [production_security_roadmap.md](docs/production_security_roadmap.md) | 四階段安全路線圖：**P0**（~2.5 小時）關閉全部 CRITICAL 風險（Dashboard 認證、Telegram Markdown、Snapshot HMAC、MCP env 隔離、新聞過濾）；**P1**（~5 小時）關閉全部 HIGH 風險（Schema 驗證、TiDB 密碼、Audit log、Opus token cap）；**P2**（~5 小時）深度防禦（OS 用戶隔離、DB RBAC、systemd 安全指令）；**P3**（~8 小時）HashiCorp Vault + CVE 掃描 + 內容政策框架；附 19 項具體程式碼變更快速參照表 |

---

## 七、效能

| 檔案 | 說明 |
|------|------|
| [workflow_performance.md](docs/workflow_performance.md) | investment_workflow 壁鐘時間分析：各節點延遲估算（`chief_strategist` 為主要瓶頸 15–45 秒）、關鍵路徑、`calculate_pnl()` 序列 yfinance 呼叫（5 檔 = 5–15 秒）、async 化預期改善，以及並行化機會 |

---

## 八、執行風險

| 檔案 | 說明 |
|------|------|
| [execution_risk_report.md](docs/execution_risk_report.md) | 執行期風險靜態分析：無限迴圈確認（三個圖均為純 DAG，無 back-edge）、API 超時導致整個 pipeline 中止風險、無 retry 機制、無 checkpointer 造成 Opus 計算浪費的量化估算 |

---

## 九、成本與模型分析

| 檔案 | 說明 |
|------|------|
| [cost_analysis.md](docs/cost_analysis.md) | 基於 2026-05-14 實測資料的每節點成本明細：Opus 佔 41%（`chief_strategist` ~$0.05），月成本 ~$2.54，並附各模型定價（Haiku $1/$5、Sonnet $3/$15、Opus $5/$25 per 1M tokens） |
| [model_usage_report.md](docs/model_usage_report.md) | 四個腳本的模型使用清冊：8 次 LLM 呼叫（Haiku × 4、Sonnet × 3、Opus × 1）、靜態路由邏輯、無 Fallback 機制、成本追蹤覆蓋率 6/8（75%），以及 `_llm_opus()` 三個配置問題 |
| [token_cost_analysis.md](docs/token_cost_analysis.md) | 每節點 token 分佈與成本表、thinking token 計費機制（以 output token 計費 $25/M）、複雜市場日 worst-case ~$0.25+、backtest 與 orchestrator 每月 $0.09–$0.15 不可見、`final_brief` 重複傳遞 token 浪費分析 |
| [optimization_opportunities.md](docs/optimization_opportunities.md) | 10 項優化機會（P0–P3），含完整程式碼：`@lru_cache` singleton `_engine()`、`_llm()` factory 快取、Opus token cap、async `calculate_pnl()`、`_record_usage()` 補漏、`final_brief` 壓縮、Anthropic prompt caching、歷史 context 注入 |
| [cost_risk_report.md](docs/cost_risk_report.md) | 四項成本風險詳細分析：① Opus thinking token 無上限（最壞 $0.25+/run）② backtest/orchestrator 成本不可見（$0.09–$0.15/月）③ data_collector 失敗觸發 raw snapshot fallback（~5× token 膨脹）④ `final_brief` 重複傳遞，每項均附修復程式碼 |
| [cost_optimization_roadmap.md](docs/cost_optimization_roadmap.md) | 四階段成本優化路線圖：**Phase 0**（1 小時）cap Opus tokens + singleton DB engine + 補 cost logging → 月成本降 40–70%；**Phase 1**（3 小時）async P&L + snapshot freshness + 並行 final nodes；**Phase 2**（2 小時）thinking_tokens 欄位 + 成本告警；**Phase 3**（4 小時）checkpointer + 歷史 context 注入，附 12 項變更快速參照表 |

---

## 十一、基礎建設分析

| 檔案 | 說明 |
|------|------|
| [infrastructure_analysis.md](docs/infrastructure_analysis.md) | 基礎建設完整分析：Ubuntu 10.0.1.20 硬體規格（Ryzen 7 4800U / 30GB / 232GB NVMe）；Python 3.14 + uv 環境；Docker 僅用於 TiDB 容器（Python 應用未容器化）；**無 GPU、無 Kubernetes**；Process Management 現況（cron 無重啟策略、Streamlit 需手動重啟、無 systemd）；儲存架構（TiDB 4 張表、5 個 ephemeral 檔案）；13 項基礎建設缺口清單 |
| [deployment_topology.md](docs/deployment_topology.md) | 部署拓撲分析：完全手動 `scp` 逐檔部署流程（無 CI/CD）；**單一環境**（無 staging/production 分離）；部署風險（混合版本狀態、Bugs 直接進 production）；**IaC 現況**（TiDB Docker 指令未入版控、Crontab 未入版控、schema migration 未追蹤）；建議 GitHub Actions rsync + staging `.env` 方案 |
| [scalability_analysis.md](docs/scalability_analysis.md) | 可擴展性全面分析：**無水平擴展能力**（`market_snapshot.json` 競態、`user_portfolio` 無 `user_id`、`daily_briefs` 無 unique 約束）；LangGraph fan-out 並行現況（threading，未驗證）；Async 化現況（僅 `test_collection.py` 全 async，其餘同步）；**無 Queue 系統**；連線池問題（`_engine()` 每次建新 pool）；三種 Queue 方案比較（tenacity / MemorySaver retry / Redis+Celery） |
| [reliability_review.md](docs/reliability_review.md) | 可靠性深度審查：5 種主要失敗模式（Claude 529、TAIFEX 解析失敗、TiDB 斷線等）；`set -euo pipefail` 導致 Step 1 失敗即中止整個工作流；**無備份策略**（TiDB 無 dump schedule、`.env` 無備份、歷史 brief 無保留）；Recovery 策略（checkpointer + 重試 + stale snapshot fallback）；恢復時間目標（RTO）估算；10 項可靠性缺口清單 |
| [production_infrastructure_roadmap.md](docs/production_infrastructure_roadmap.md) | 基礎建設生產化路線圖：**P0**（~3 小時）LangGraph checkpointer + LLM retry + 彈性 shell + TiDB backup script + `.env` 加密備份；**P1**（~4 小時）systemd 服務（Streamlit + TiDB）+ IaC 入版控（tidb-compose.yml、crontab.txt、migration SQL）+ `uv.lock` + 共享 DB engine；**P2**（~4 小時）Staging 環境 + Off-server 備份 + Migration 追蹤；**P3**（~14 小時）多用戶隔離 + 全 Async 遷移；附跨路線圖（安全 / 架構 / 可觀測性）聯合 P0 實作建議 |

---

## 十、可觀測性（Observability）

| 檔案 | 說明 |
|------|------|
| [observability_gap_analysis.md](docs/observability_gap_analysis.md) | 現有可觀測性缺口全面分析：現狀覆蓋率評分 2/10；盤點現有 logs/token 追蹤/dashboard；識別五類缺口（Agent trace、LLM trace、Tool trace、Memory trace、Cost trace）；列出四類監控風險（不可見失敗、靜默幻覺、隱藏 token 爆炸、缺失稽核軌跡），附 14 項優先矩陣 |
| [telemetry_design.md](docs/telemetry_design.md) | 五層遙測資料模型設計（全部寫入現有 TiDB）：Layer 0 `workflow_runs` + `run_id`（執行關聯）、Layer 1 `thinking_tokens` 欄位、Layer 2 `workflow_events` 結構化事件日誌、Layer 3 `llm_traces` 完整 prompt/response 記錄、Layer 4 `audit_log` 所有 DB 變更稽核，附每層 SQL schema 與完整 Python 程式碼 |
| [monitoring_strategy.md](docs/monitoring_strategy.md) | 三維監控策略：① 執行層健康（workflow 完成度、brief 存在、通知交付）② 資料品質（snapshot 新鮮度、LLM 輸出合規率、fallback 觸發率、gap_direction NULL 率）③ 成本與效能（per-run 成本、thinking token 趨勢、節點延遲 P95、錯誤率），含完整 SQL KPI 查詢與 Streamlit 擴充方案 |
| [alerting_strategy.md](docs/alerting_strategy.md) | 九個 Alert 的完整定義（A-001 至 A-009）：觸發條件、嚴重度（CRITICAL/WARNING/INFO）、推播通道（LINE/Telegram）、對應行動；含 `_run_post_alerts()` 後執行告警器程式碼與每週 Digest 實作 |
| [production_observability_architecture.md](docs/production_observability_architecture.md) | 生產級可觀測性完整架構：四層架構圖（T1 執行關聯→T2 LLM Trace→T3 結構化日誌→T4 LangFuse）；Before/After 覆蓋率對照表（2/10 → 7/10 → 9/10 → 10/10）；14 步驟 Phase 0 實作序列；含完整資料流圖與 SQL KPI 查詢庫 |
