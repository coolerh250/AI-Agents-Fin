# 調整計畫（依稽核發現的問題）
_制定日期：2026-05-19｜基礎：本目錄 8 份 reality 報告 + 4 份綜合報告_
_範圍：僅針對「稽核已確認的現存問題」之修復；多 agent 自我改善的目標路線在 [multi_agent_transformation_plan.md](multi_agent_transformation_plan.md)_

---

## 0. 序言

### 計畫範圍
本計畫是「**現況修復**」型計畫——目標是把稽核所列、現在已壞或失真的東西修到「真的成立」。不含新增能力。

### 與其他計畫的關係
- [next_stage_reality_based_priorities.md](next_stage_reality_based_priorities.md)：高層優先序（P0~P3 概念）。本計畫是其**操作化**版本，把每個優先項目拆成「問題→修法→驗證」。
- [multi_agent_transformation_plan.md](multi_agent_transformation_plan.md)：往多 agent / 自我改善目標的演進路線。本計畫是其 **Phase 0 的精細展開**。

### 目前 working tree 狀態（透明告知）
我在前一輪曾準備執行部分 P0 修復，已在本機產生以下未提交編輯：

| 檔案 | 變更摘要 |
|------|---------|
| `daily_run.sh` | 修正 crontab 註解；新增 Step 1.5「前一交易日回測」 |
| `alert_runner.py` | `_send_warning`/`_send_info` 加 LINE fallback |
| `investment_workflow.py` | 修 SqliteSaver 使用方式（直接以 sqlite3 連線建構） |
| `pyproject.toml` | 加 `langgraph-checkpoint-sqlite>=2.0.0` |
| `.gitignore` | 排除 `checkpoints.db` / `investment_brief_*.txt` |

這些編輯**對應到下表 A-1 / B-1 / C-2 / F-1 / E-3 的修復**，等本計畫獲核可後即一併提交與部署。

---

## 1. 問題總表

### A. 排程與運維（runtime_reality_report.md）

#### A-1 daily_run.sh cron 自 05-15 起每日失敗 🔴 P0
- **現況**：crontab 直接執行 `.sh`，檔案失去 execute bit，logs/daily_run.log 連續 `Permission denied`
- **修復**：① 在 server `chmod +x daily_run.sh`；② crontab 改為 `bash /path/daily_run.sh`（治本，未來 git pull 不再受影響）
- **工時**：5 分鐘
- **驗證**：手動 `bash daily_run.sh` 跑完無錯；下一個交易日 08:00 後 logs 有「daily run 完成」

#### A-2 兩個常駐服務無 supervisor、無開機自起 🟡 P2
- **現況**：streamlit / uvicorn webhook 由 `nohup` 啟動，主機重開不會自動復活
- **修復**：建立兩個 systemd unit（`ai-agents-dashboard.service`、`ai-agents-webhook.service`），含 `Restart=on-failure`、`After=docker.service`（因依賴 TiDB 容器）
- **工時**：1 小時
- **驗證**：`systemctl restart`、`reboot` 後服務自動恢復；崩潰測試（`kill -9`）後自動重啟

#### A-3 workflow 失敗無「主動回報」 🟡 P2
- **現況**：靠隔天 08:20 alert_runner 反推「昨天沒跑」才發 A-001
- **修復**：daily_run.sh 結尾加成功通知；任一步驟非零退出時送 LINE 失敗訊息（trap ERR）
- **工時**：30 分鐘
- **驗證**：故意讓 Step 2 失敗，應收到即時 LINE 通知

---

### B. 回饋迴路與記憶（memory_reality_analysis.md / runtime §4）

#### B-1 backtest_agent 未排程，回饋迴路全斷 🔴 P0
- **現況**：backtest 不在 cron、不在 daily_run.sh → market_actuals 凍結在 05-15、strategy_lessons 凍結、session_episodes 結果欄全空
- **修復**：daily_run.sh 在 Step 2 **之前**插入 Step 1.5 跑 `backtest_agent.py`（無參 → 自動取「最近一筆 brief」即前一交易日）。順序重要：早於 Step 2 才能讓 chief_strategist 注入到剛產的新 actuals/lessons。
- **工時**：已在 working tree 修；待部署
- **驗證**：跑一輪後查 market_actuals 應有新日期、strategy_lessons 應有新 row、session_episodes 對應日的 actual_* 欄位被回填

#### B-2 補回 05-16~05-18 缺漏的 actuals/lessons 🟡 P1
- **現況**：手動補跑可填補資料縫
- **修復**：B-1 部署後，手動 `uv run python backtest_agent.py 2026-05-15`、`...18` 各跑一次（注意：05-16/17 為週末跳過）
- **工時**：5 分鐘
- **驗證**：daily_briefs 與 market_actuals JOIN 後缺漏日數歸零

#### B-3 session_episodes 寫了沒人讀回 inference 🟡 P2
- **現況**：表寫入正常但無讀取注入 LLM 的程式碼，registry 宣稱卻無對應實作
- **修復**：擇一：① 新增 `get_recent_sessions_context(days=10)` 函式 + 注入 chief_strategist；② 若不打算用，把 registry 該宣稱刪除（誠實化）
- **建議**：選 ①，因為 session_episodes 是設計上的 episodic memory；做了讀回路才物盡其用
- **工時**：2 小時
- **驗證**：chief_strategist 輸入 prompt 含 sessions 區塊；A/B 比對對準確率是否提升（觀察 7 日）

#### B-4 記憶機制無語意檢索（向量） 🔵 P3（不在本計畫範圍）
- 屬「轉型」而非「修復」，留給 transformation_plan Phase 2.4

---

### C. 觀測與告警（observability_reality_report.md）

#### C-1 Telegram 未設定 → WARNING/INFO 告警全靜默 🔴 P0
- **現況**：`.env` 無 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`，send_telegram 一律 skipped
- **修復（兩擇一，建議②）**：
  - ① **配置 Telegram**：用戶自行建 bot、取 chat_id、補 .env
  - ② **程式 fallback**：`_send_warning`/`_send_info` 在 Telegram skipped 時改走 LINE（已在 working tree）
- **工時**：① 30 分；② 已修
- **驗證**：手動觸發 A-007 / A-009 → 至少一個通道收得到

#### C-2 A-001「workflow 未跑」每日無人理會 🟡 P1
- **現況**：cron 已壞，每日 08:20 alert_runner 都會送 A-001 到 LINE，但訊息被忽略
- **修復**：A-1 修好 cron 後此告警自然消失。但建議補一個「靜默期偵測」：若同一 alert_id 24h 內已發送，不重複送（除非升級）
- **工時**：1 小時
- **驗證**：A-001 不再每天打擾

#### C-3 A-009/A-010 依賴枯竭資料、易誤判 🟡 P1
- **現況**：依賴 market_actuals / eval_runs，B-1 修復前資料不全
- **修復**：B-1 + B-2 完成後自然解除；額外加最小樣本門檻（A-010 已有「≥2」，A-009 可加類似）
- **工時**：15 分鐘
- **驗證**：B-1 上線一週後 A-009/A-010 應發出有意義數字

#### C-4 audit_log / llm_traces 無 UI 規律檢視 🔵 P3
- **修復**：dashboard 加 tab 顯示近期 audit_log + 高成本 / 異常 finish_reason 的 llm_traces
- **工時**：3 小時
- **驗證**：能在看板看到「最近 24h 的 portfolio 變更稽核」

---

### D. 安全（security_reality_report.md）

#### D-1 LINE webhook 簽章驗證實際被關閉 🔴 P0 Critical
- **現況**：`LINE_WEBHOOK_SECRET` 未設 → `_verify_signature` 直接 `return True`；webhook 0.0.0.0:8502 任何同網段可偽造事件
- **修復**：① 程式改為「缺 secret 即拒絕」；② 從 LINE Developers Console 取 Channel Secret，寫入 `.env`；③ 重啟 webhook
- **🔴 部署順序強制**：必須 ② 先做、再部署 ①、最後重啟。順序顛倒會讓 webhook 拒收所有訊息
- **工時**：15 分鐘（程式已備）
- **驗證**：用錯誤簽章 POST `/webhook` → 應回 400；正常 LINE 訊息仍可處理

#### D-2 TiDB 以 root 對 0.0.0.0:4000 開放 🔴 P0 Critical
- **現況**：DB 容器 bind 全網段、無防火牆
- **修復**：① 修改 `docker/tidb-compose.yml` port mapping 從 `4000:4000` 改為 `127.0.0.1:4000:4000`；② docker compose down / up（**短暫 downtime**）；③ 應用端 `.env` 確認 `TIDB_HOST=127.0.0.1`（如為其他值需同步調整）
- **工時**：30 分鐘
- **驗證**：`ss -tlnp | grep 4000` 應僅見 `127.0.0.1`；應用端可正常連線；同網段他機 `mysql -h 10.0.1.20 -P 4000` 應拒絕

#### D-3 無主機防火牆，3 個 port 全網段曝險 🔴 P0 High
- **現況**：ufw 未啟用
- **🔴 風險警示**：啟用 ufw 前**必須先放行 SSH 22**，否則 SSH 鎖死自己
- **修復順序（嚴格遵守）**：
  1. `sudo ufw allow 22/tcp`（**第一步絕對不可省略**）
  2. `sudo ufw allow from 10.0.1.0/24 to any port 8501`（dashboard 僅 LAN）
  3. `sudo ufw allow from 0.0.0.0/0 to any port 8502`（webhook 需 LINE 平台外連——確認 LINE webhook IP 範圍後可收斂）
  4. `sudo ufw default deny incoming`
  5. `sudo ufw enable`
  6. 從另一台機器 SSH 測試**仍可登入**才算成功；登不進去也別緊張，主控台直接 `sudo ufw disable`
- **工時**：30 分鐘（含驗證）
- **驗證**：`ufw status` 顯示 active；4000 不可從外部達；22/8501/8502 規則正確

#### D-4 sudo 密碼弱且需輪換 🟠 P0 High（使用者執行）
- **現況**：`itadmin` 的 sudo 密碼為 `p@ssw0rd`；曾以明文出現在 `.claude/settings.local.json`（檔本身於 `.gitignore`，未進 git，已清理）
- **修復**：使用者於 server 執行 `passwd` 設新強密碼；同步檢查所有腳本不含密碼字面值
- **工時**：5 分鐘
- **驗證**：`sudo -k && sudo whoami` 須輸入新密碼始能通過

#### D-5 snapshot HMAC 為 no-op 🟡 P2
- **現況**：`SNAPSHOT_HMAC_KEY` 未設；且程式設計即便不符也只 warning、不中止
- **修復**：① `openssl rand -hex 32` 產生金鑰寫入 `.env`；② `snapshot_integrity.verify_snapshot` 在簽章不符時改為 `raise`；③ test_collection 改寫的快照也會被簽
- **工時**：30 分鐘
- **驗證**：手動篡改 market_snapshot.json 一個欄位 → investment_workflow 應中止並發 A 級告警

#### D-6 `validate_tool_permission` fail-open 且幾乎沒被呼叫 🟡 P2
- **現況**：函式存在但 workflow 從未實際呼叫；即使呼叫也只 warning、不阻擋
- **修復**：擇一：① 在敏感工具（save_brief / push）入口呼叫並改 fail-closed；② 若不打算強制，直接刪函式 + 對應 registry 欄位
- **建議**：選 ①——這是邁向 transformation_plan「registry runtime 強制」的第一步
- **工時**：2 小時
- **驗證**：未授權 caller 呼叫 → 拋例外、寫 tool_audit_log

#### D-7 MCP fallback 繞過 token + audit 🟡 P2
- **現況**：MCP 呼叫失敗時直接呼叫底層函式，跳過 token 與 audit
- **修復**：把 fallback path 也經過一個 `local_invoke()` wrapper，內含 audit 寫入（無需 token）；保留功能可用但留下軌跡
- **工時**：1 小時
- **驗證**：故意斷 MCP（移除 `MCP_WRITE_TOKEN`）跑一輪 → workflow 成功 + tool_audit_log 多一筆 `caller='workflow_fallback'`

#### D-8 DASH_PASSWORD 明文於 .env、無雜湊 🔵 P3
- 屬一般 admin 後門；LINE OTP 已是主要登入管道；可考慮移除 DASH_PASSWORD，僅留 LINE OTP

#### D-9 TWSE TLS 降級（verify_flags=0） 🔵 P3
- 已是「保留 CA / hostname 驗證、僅移除 SKI 檢查」的可接受折衷；待 TWSE 更新 CA 即可恢復

---

### E. 文件與架構誠實化

#### E-1 註冊表（agent_registry / tool_registry）指向已死的舊檔 🟡 P3
- **現況**：兩個 yaml 仍引用 `finance_mcp_server.py` / `system_inspector.py` 與已不存在的 orphan 工具
- **修復**：擇一：① 把名稱更新到現役檔（market_data_server / system_server）並移除 orphan 條目；② 若 registry 不打算 runtime 消費，明確標示「設計文件（非 runtime）」並改放 docs/
- **建議**：先做 ①，等 D-6 把 registry 變 runtime 強制時即派上用場
- **工時**：1 小時
- **驗證**：grep 不再出現 finance_mcp_server / system_inspector

#### E-2 progress.md / docstring 描述失真 🟡 P3
- **修復項目**：
  - `investment_workflow.py` docstring「5 節點」→ 「8 節點」（已在註解中提及實況）
  - `progress.md` 「cron 自動」→ 「cron 排程（執行成功率見 logs/daily_run.log）」
  - 「resume-on-failure」改述為「state 持久化於 checkpoints.db，目前無 CLI resume 入口」
  - 移除「multi-agent」「AgentOS」字眼，統一用「LLM workflow」「analysis pipeline」
- **工時**：2 小時
- **驗證**：grep 不再出現失真字眼

#### E-3 死碼存在 🟡 P3
- **目標**：刪 `main.py`、`mcp_servers/finance_mcp_server.py`、`mcp_servers/system_inspector.py`、舊 investment_brief_*.txt（已加 .gitignore）
- **前置**：確認 `finance_mcp_server.py` 的「rollback target」聲明仍需要嗎？目前已上線新 server 數週，可移除
- **工時**：15 分鐘
- **驗證**：依然能跑完整工作流；測試 `test_collection.py`、`investment_workflow.py`、`agent_orchestrator.py`

---

### F. 工程瑕疵（workflow_reality_analysis.md）

#### F-1 SqliteSaver checkpointer 即便裝套件也壞 🟡 P1
- **現況**：`SqliteSaver.from_conn_string()` 是 context manager；現程式碼當常駐 saver 使用必失敗。雙重失靈：套件未裝 + 即便裝了也壞
- **修復**：改為 `sqlite3.connect(..., check_same_thread=False)` 直接建構（已在 working tree）；加套件依賴
- **工時**：已修
- **驗證**：workflow 跑完後 `checkpoints.db` 檔案出現且大小成長；不再 ImportError

#### F-2 chip / tech 名為平行實為循序 🔵 P3
- **現況**：LangGraph 同步執行器循序跑兩支路。對總耗時影響有限（Sonnet 各 ~3s）
- **修復**：改用 async invoke + `add_node(..., parallel=True)` 或自己 thread pool
- **工時**：3 小時
- **驗證**：chip/tech 總耗時 ≈ max 而非 sum

#### F-3 `asyncio.run()` anti-pattern in agent_orchestrator 🔵 P3
- 因 maintenance_agent 未排程，影響低；轉型至 agent 化時一併重構

---

## 2. 時序執行階段

| 階段 | 包含問題 | 工時上限 | 出場標準 |
|------|----------|---------|---------|
| **P0 止血** | A-1, B-1, C-1, D-1, D-2, D-3, D-4 | 1 天 | cron 連 3 日自動完成；市場 actuals 持續更新；對外曝險收斂；無弱密碼 |
| **P1 接迴路 / 修工程** | B-2, C-2, C-3, F-1 | 1 天 | 歷史 actuals 補完；A-001/A-009/A-010 不再誤報；checkpoints.db 持久化生效 |
| **P2 韌性 + 治理收斂** | A-2, A-3, B-3, D-5, D-6, D-7 | 1 週 | 服務 systemd 化；workflow 主動回報；snapshot HMAC 強制；MCP fallback 留 audit |
| **P3 誠實化 + 清理** | C-4, D-8, D-9, E-1, E-2, E-3, F-2, F-3 | 持續 | 文件與 runtime 一致；死碼清除；命名收斂；架構敘述不再領先實況 |

---

## 3. 部署 & 回滾策略

### 部署順序（P0 整批）
```
本機 → 提交 working tree 編輯（A-1/B-1/C-1/F-1/E-3 部分）→ push
server →
  1. (D-4) 使用者先輪換 sudo 密碼 → 取得新密碼後再做其他 sudo 動作
  2. (D-1.②) 從 LINE Console 取 Channel Secret 寫入 .env
  3. git pull（D-1 程式同時部署）
  4. chmod +x daily_run.sh（A-1）
  5. crontab -e 把 .sh 直接路徑改為 `bash /path/daily_run.sh`（A-1 治本）
  6. uv sync（F-1 安裝 langgraph-checkpoint-sqlite）
  7. (D-3) ufw 規則設定→啟用（嚴格按 D-3 步驟順序）
  8. (D-2) docker-compose down → 改 compose → up（**短暫 DB 不可用 ~30s**，會影響 dashboard / webhook）
  9. 重啟 webhook：先確認 .env 有 LINE_WEBHOOK_SECRET → 才 kill + 重啟 uvicorn
 10. 重啟 streamlit dashboard
 11. 觸發一次 `bash daily_run.sh` 試跑
```

### 回滾策略
| 變更 | 回滾方法 |
|------|---------|
| 程式 git pull | `git revert <sha>` 或 `git reset --hard <prev_sha>` |
| crontab 修改 | 保留 `crontab -l > /tmp/cron.bak` 後再改；異常時 `crontab /tmp/cron.bak` |
| TiDB bind 變更 | `docker-compose down` → 還原 compose → `up` |
| ufw | 異常時主控台（非 SSH）`sudo ufw disable` |
| webhook secret | 移除 .env 該行 + 還原 line_webhook.py |

### 高風險步驟的雙重防線
- **D-3 ufw**：先在主控台（KVM / IPMI / 雲端 Web Console）開好 root shell，再用 SSH 設規則。若鎖死可從主控台救援
- **D-2 TiDB 重啟**：先 `pg_dump`-等價的 `backup_db.sh` 跑一次備份
- **D-1 webhook**：先 disable LINE webhook URL（webhook 暫不收訊息），更新完再 enable

---

## 4. 驗收矩陣

| 驗證項 | 通過條件 | 測法 |
|--------|---------|------|
| cron 自動執行 | 連續 3 個工作日 logs/daily_run.log 有「daily run 完成」 | `tail logs/daily_run.log` |
| 回饋迴路活 | market_actuals 每個交易日有新 row | `SELECT MAX(trade_date) FROM market_actuals` |
| 教訓持續生成 | strategy_lessons 每週 ≥ 3 新 row | `SELECT COUNT(*) WHERE created_at > NOW()-7d` |
| Telegram/LINE 告警通 | 手動觸發 A-007 至少一通道收到 | A-007 假資料注入 |
| webhook 拒絕未簽章請求 | curl 未簽章 POST → 400 | `curl -X POST .../webhook -d '{}'` |
| TiDB 內網拒絕 | LAN 他機 mysql 連線失敗 | `mysql -h 10.0.1.20 -P 4000` |
| ufw active 且 SSH 通 | `ufw status` + 新 SSH session 可建立 | 從另一台機器測試 |
| checkpointer 持久化 | workflow 跑完 `checkpoints.db` 存在且增長 | `ls -la checkpoints.db` |
| sudo 密碼已換 | 舊密碼 `p@ssw0rd` 失效 | `echo old | sudo -S whoami` 應拒絕 |
| 文件不再失真 | grep 不見 failed claims | `grep -r "cron 自動觸發\|resume-on-failure\|multi-agent" docs/` |

---

## 5. 明確不做的事

- ❌ 在 P0 完成前做任何「轉型」工作（agent 化、向量記憶、新 LLM 節點）
- ❌ 把 webhook 簽章硬化單獨部署（必須與 secret 一起）
- ❌ 啟用 ufw 而不先放行 SSH 22
- ❌ 在無備份下變更 TiDB bind
- ❌ 重寫 LangGraph 圖、引入新 framework
- ❌ 增加新告警規則（先讓既有的真的能送達）

---

## 6. 待你決定的選項

| 議題 | 選項 | 我的建議 |
|------|------|---------|
| C-1 告警通道 | ① 配置 Telegram ② 程式 LINE fallback | ②（簡單、不依賴外部設定） |
| B-3 session_episodes | ① 補讀回路 ② 移除 registry 宣稱 | ①（為自我改善鋪路） |
| D-6 tool_permission | ① 改 fail-closed 並實際呼叫 ② 刪除 | ①（小步邁向 runtime governance） |
| D-8 DASH_PASSWORD | ① 保留 ② 移除（僅留 LINE OTP） | ② |
| E-1 registry 定位 | ① 更新為現役檔 ② 改放 docs/ 標示為文件 | ①（與 D-6 配套） |

---

## 7. 一句話

> **這份計畫的承諾很小：把稽核確認壞掉的事修到真的成立、把失真的描述改回實況。它不引入任何新功能。** 完成後系統終於名實相符——這是任何「往多 agent 自我改善前進」的硬性先決條件。
