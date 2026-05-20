# MCP Reality Assessment
_獨立架構稽核 — 第一階段 6/8_
_稽核日期：2026-05-19｜方法：追蹤 MCP server 檔案、客戶端呼叫鏈、env 隔離、tool_audit_log 實機紀錄_

---

## 1. MCP server 真實清單

| Server 檔 | FastMCP 名稱 | 工具數 | 真實被呼叫者 | 狀態 |
|-----------|--------------|:--:|--------------|------|
| `market_data_server.py` | market-data | 6 | `test_collection.py`（daily_run Step 1） | **production 使用中** |
| `persistence_server.py` | persistence | 4 | `save_to_db_node` | **production 使用中** |
| `notification_server.py` | notification | 2 | `send_notification_node` | **production 使用中** |
| `system_server.py` | system-server | 1 | `agent_orchestrator.py`、`test_mcp_client.py` | 僅手動（未排程） |
| `finance_mcp_server.py` | — | 3 | 無 | **死碼（legacy）** |
| `system_inspector.py` | — | — | 無 | **死碼（legacy）** |

實機佐證：`tool_audit_log` 有 `persistence.save_brief`（ok×9, unauthorized×2）與 `notification.push_investment_brief`（ok×2）紀錄 → persistence / notification 真的被走到。

---

## 2. MCP 是 production 使用，還是 prototype？

**是 production 使用，但屬「軟性」整合。** 三個 server 真的會在 daily workflow 被呼叫到。但每個呼叫點都有 **fallback-to-direct**：

| 節點 | MCP 呼叫 | 失敗時 |
|------|----------|--------|
| save_to_db_node | `persistence_server.save_brief` | `except` → 直接 `database_tools.save_brief()` |
| send_notification_node | `notification_server.push_investment_brief` | `except` 或 `unauthorized` → 直接 `messenger_tools.send_line/telegram` |

→ MCP 在此**不是強制路徑**。它是一層可被繞過的治理外殼，設計上就允許「MCP 掛了照樣完成工作」。

---

## 3. MCP 是 wrapper 嗎？

部分是。`persistence_server.save_brief` 的本體就是 `from database_tools import save_brief as _save_brief` —— 它把同一個 Python 函式包成 MCP 工具。`notification_server` 同理包 `messenger_tools`。**MCP 工具與 direct 函式呼叫的是同一段程式碼**，差別只在：(a) 跑在獨立子程序，(b) 多了 token 檢查與 audit。

例外：`market_data_server` 的 6 個工具是實打實的爬蟲/API 邏輯，非 wrapper。

---

## 4. MCP 是否形成 permission boundary？

**部分形成，但不是硬邊界。**

| 機制 | 實況 |
|------|------|
| Token 認證 | `persistence`（MCP_WRITE_TOKEN）、`notification`（MCP_NOTIFY_TOKEN）在 handler 入口檢查；錯誤回 `unauthorized`。✅ 實機 audit 有 2 筆 unauthorized |
| `market_data` / `system` | 無認證（讀取公開資料，可接受） |
| Env 隔離 | `utils/mcp_env.py` 為每個 server 量身給 env：market_data 只給 PATH/HOME；persistence/notification 才給 TIDB_*/LINE_*。✅ 不繼承完整父程序 env |
| 邊界可繞過 | workflow 的 fallback-to-direct 直接在主程序呼叫 `database_tools` / `messenger_tools`，**完全不經過 token 與 audit** → 邊界對「自己人」無效 |

→ MCP 的 token 邊界防的是「外部 MCP 客戶端」，但 workflow 自身永遠有一條繞過邊界的合法捷徑。

---

## 5. MCP 是否形成 isolation？

| 隔離面向 | 實況 |
|----------|------|
| Process 隔離 | ✅ 每次呼叫 `uv run <server>` 開新子程序，stdio transport，呼叫完即結束 |
| 冷啟成本 | 每次 ~2s（`mcp_call.py` docstring 自述）。每日 workflow 數次呼叫 → 可接受但浪費 |
| Credential 隔離 | ✅ 部分（env 白名單）。但 persistence 子程序仍拿到完整 TIDB root 憑證 |
| 常駐 vs 暫態 | MCP server **非常駐**；無連線池、無 server 生命週期管理 |

→ 有 process-level 隔離，但無「長駐受控服務」概念。每次都是 fork-exec-die。

---

## 6. 與既有文件的落差

| 文件聲稱 | 實況 |
|----------|------|
| `tool_registry.yaml`：server 為 `finance_mcp_server.py`，含 orphan 工具 `save_brief_to_db`/`send_brief_to_user`（critical 風險） | finance_mcp_server.py 已死碼；該兩 orphan 工具現檔已不存在。**該安全項目實際已解除，但 registry 未更新** |
| `agent_registry.yaml`：`mcp.system_inspector` | 實際為 `system_server.py` |
| 「MCP Governance Phase 1」 | token + audit + env 隔離屬實；但「治理」可被 fallback 繞過 |

---

## 結論

MCP 在此系統是 **真實、production 在用的整合層**，具備 process 隔離、env 憑證白名單、寫入/通知 token 認證、與 audit log——這些都是真的。

但它**不是硬性的權限/隔離邊界**：(a) 每個 MCP 呼叫點都有 fallback 直連同一段程式碼，繞過 token 與 audit；(b) MCP server 為暫態子程序、非受控常駐服務；(c) 註冊表描述的仍是上一代檔案。

定位：**MCP 是一層「可選的、軟性的」治理與隔離外殼，而非系統的權限骨幹。**
