# Security Reality Report
_獨立架構稽核 — 第一階段 8/8_
_稽核日期：2026-05-19｜方法：檢視 source、runtime env、實際 port 曝險、認證路徑_

---

## 評估標準

不看 security 文件。檢查 source code、runtime 行為、實際 env 曝險、實際權限路徑。

---

## 1. 各安全能力實況

| 宣稱能力 | 實作 | 判定 |
|----------|------|------|
| Secrets isolation | `.env` mode 600；`utils/mcp_env.py` 為 MCP 子程序做 env 白名單 | **部分存在** |
| Runtime isolation | MCP 子程序 process 隔離 | **部分存在** |
| Permission boundary | MCP token（write/notify）；`validate_tool_permission`（fail-open） | **弱** |
| MCP isolation | env 白名單 + 子程序 | **部分存在** |
| Sandboxing | 無容器化應用層、無 seccomp、服務以 itadmin 一般帳號跑 | **不存在** |
| RBAC | dashboard 認證為二元（登入/未登入）；line_user_id 僅隔離持倉資料 | **幾乎不存在** |
| Auditability | `audit_log` + `tool_audit_log` | **部分存在** |

---

## 2. 🔴 嚴重問題

### S-1（Critical）LINE Webhook 簽章驗證實際被關閉

`line_webhook.py._verify_signature()`：若 `LINE_WEBHOOK_SECRET` 未設定，直接 `return True`（記一行 warning）。

實機 `.env` **無 `LINE_WEBHOOK_SECRET`** → 簽章驗證全程跳過。

webhook 綁定 `0.0.0.0:8502`、無主機防火牆。**後果：同網段任何人都能對 `/webhook` POST 偽造的 LINE 事件**，挾帶任意 `source.userId`，即可：
- 對任意使用者的持倉執行新增/刪除/改成本（`新增 2330 ...`）
- 觸發 `create_login_token()` 為任意 user_id 產生 dashboard 登入碼

這是可遠端利用、影響使用者資產資料的漏洞。

### S-2（Critical）TiDB 以 root 對全網段開放

`TIDB_USER=root`，TiDB 綁 `0.0.0.0:4000`，無 ufw。同網段可直連 root DB。`agent_memory` 含 LLM trace、使用者 LINE ID、持倉成本等。`backup_db.sh` 亦以 root 連線。

### S-3（High）無主機防火牆，三個 port 全網段曝露

`ufw status` 無輸出（未啟用）。8501（dashboard）、8502（webhook）、4000（TiDB）皆 `0.0.0.0`。dashboard 雖有登入，但 OTP / 密碼之外無速率限制、無鎖定。

### S-4（High）`.claude/settings.local.json` 內嵌主機 sudo 密碼

該檔的已核准指令清單中多次出現明文 `echo p@ssw0rd | sudo -S ...` 與 `echo 'p@ssw0rd' | sudo -S`。`itadmin` 的 sudo 密碼 `p@ssw0rd`（同時也是弱密碼）被寫死在 repo 工作目錄的設定檔。需確認此檔是否被 `.gitignore` 排除；即使未進 git，本機明文留存仍是憑證外洩。

---

## 3. ⚠️ 中度問題

| # | 問題 | 說明 |
|---|------|------|
| S-5 | snapshot HMAC 形同虛設 | `SNAPSHOT_HMAC_KEY` 未設定 → `sign/verify_snapshot` 全 no-op。`verify_snapshot` 設計上「即使簽章不符也只 warning、不中止」，故即便啟用也非強制 |
| S-6 | `validate_tool_permission` fail-open 且幾乎沒被呼叫 | 函式 caller 不在白名單僅回 False + warning，「production 是 fail-open」；且 grep 顯示工作流節點並未實際呼叫它把關 |
| S-7 | DASH_PASSWORD 明文於 .env、無雜湊、無輪替 | 二元管理員密碼 |
| S-8 | TWSE TLS：`twse_fetcher` 用 `verify_flags=0` | 註解稱為相容 TW 政府舊 CA（移除 SKI 檢查但保留鏈/主機名驗證）——比 `verify=False` 好，屬可接受的折衷，但仍是降級 |
| S-9 | MCP fallback 繞過 token 與 audit | workflow 自身可直連 DB/LINE，不經 MCP 認證層（見 MCP 報告） |
| S-10 | 無容器化、服務無 supervisor | 以 itadmin 跑 nohup，重開機不復原；無資源/權限限制 |

---

## 4. 做對的部分（持平記錄）

- `.env` 權限 600，未發現憑證寫死於 .py（除 settings.local.json 的 sudo 密碼）。
- MCP `persistence` / `notification` 確有 token 檢查，實機 audit 抓到 2 筆 `unauthorized` → 防護真的有作用。
- `utils/mcp_env.py` 對 MCP 子程序做 env 白名單，未全量繼承父程序環境。
- LINE OTP 登入碼用 `secrets` 模組、8 碼、5 分鐘 TTL、單次使用——設計正確。
- `market_data_server` 對外部新聞標題有 `_sanitize_title()` prompt-injection 過濾。
- 持倉 CRUD 有 `audit_log` before/after 紀錄。

---

## 5. 與既有文件的落差

| 文件聲稱 | 實況 |
|----------|------|
| `tool_registry.yaml`：orphan 工具 `send_brief_to_user`（critical：不受限 LINE 推播） | 該工具於現行 finance_mcp_server.py 已不存在——**此項已解除，但 registry 未更新** |
| progress.md：「P0 安全/穩定性修復」「Prompt Injection 輸入清洗」 | 輸入清洗屬實；但 webhook 簽章、防火牆、root DB 曝險為更高風險且未解 |
| agent_registry「write_db_portfolio requires_auth: true（LINE signature）」 | LINE 簽章驗證實機被關閉 → 此宣稱不成立 |

---

## 結論

系統在「應用層細節」上做了若干正確的事（OTP、env 白名單、MCP token、injection 過濾、稽核表）。但在「**周邊與基礎設施**」上有 4 個 Critical/High 等級的真實漏洞：**webhook 簽章關閉、root DB 全網段曝露、無防火牆、sudo 密碼明文留存**。

安全姿態的真相：**精緻的門鎖裝在沒有圍牆的房子上。** 文件聚焦於已修補的應用層細項，卻未涵蓋曝險最大的網路邊界與基礎設施面。
