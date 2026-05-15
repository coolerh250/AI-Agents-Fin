# Reliability Review
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Executive Summary

The platform has **four reliability gaps that compound each other**: no LangGraph checkpointing, no retry strategy on LLM calls, `set -euo pipefail` in the orchestration shell script that aborts the entire daily workflow on any step failure, and no backup for the TiDB database. The most financially impactful scenario is a Claude 529 error at 08:20 CST: the workflow aborts, all upstream computation is wasted, the daily brief is not generated, LINE/Telegram receives no push, and the user has no visibility into the failure without checking logs.

---

## 1. Restart Strategy

### 1.1 Current State

| Component | Restart Policy | What Happens on Failure |
|-----------|---------------|------------------------|
| `investment_workflow.py` | None | Workflow aborts; no retry; cron does not re-fire |
| `test_collection.py` | None | `daily_run.sh` aborts at Step 1; Step 2 never runs |
| `dashboard.py` (Streamlit) | None | Dashboard unavailable until manually restarted |
| TiDB container | Unknown (`--restart=always` assumed, unverified) | May restart automatically |
| `backtest_agent.py` | None (manual only) | User must re-run manually |

### 1.2 Root Causes of Failure

**Primary failure modes for the daily workflow:**

1. **Anthropic API 529 (Overloaded)** — Most common. Occurs several times/month during peak hours. All 6 LLM nodes are vulnerable. No retry → complete abort.

2. **TAIFEX scraper failure** — TAIFEX changes HTML structure without notice (`get_tw_future_chips`). A parsing error in `test_collection.py` triggers `set -euo pipefail` and stops `daily_run.sh` before `investment_workflow.py` runs.

3. **Yahoo Finance / Anue API rate limit** — Both endpoints return occasional 429 / 503. The MCP server has a single `_retry(retries=1)` — only one retry with a 2-second delay.

4. **TiDB connection failure** — If TiDB container is restarting or Docker has a hiccup, `save_to_db_node` raises an exception and the final brief is not persisted. The `send_notification_node` may still succeed (it reads from `WorkflowState`, not TiDB), but no record exists for backtest.

5. **Network outage** — Any external API call fails. `set -euo pipefail` exits the script.

### 1.3 Missing Restart Mechanism

```bash
# Current daily_run.sh (problematic):
set -euo pipefail   # ANY failure exits immediately

uv run test_collection.py          # Step 1
uv run investment_workflow.py      # Step 2 — NEVER RUNS if Step 1 fails
python - <<'PYEOF'                  # Step 3
...
PYEOF
```

**Fix — per-step error handling with fallback:**

```bash
#!/bin/bash
LOG_DIR="/home/itadmin/ai_agent_studio/logs"
mkdir -p "$LOG_DIR"
source /home/itadmin/.local/bin/env
cd /home/itadmin/ai_agent_studio

ts() { date '+%Y-%m-%d %H:%M:%S'; }

echo "[$(ts)] ===== daily run start ====="

# Step 1: Data collection (allow failure — workflow uses stale snapshot)
if ! uv run test_collection.py; then
    echo "[$(ts)] WARNING: test_collection failed — proceeding with yesterday snapshot"
    # Check snapshot freshness; abort only if no snapshot exists
    if [ ! -f market_snapshot.json ]; then
        echo "[$(ts)] ERROR: no snapshot available — aborting"
        exit 1
    fi
fi

# Step 2: Analysis workflow (retry once after 5 min on failure)
if ! uv run investment_workflow.py; then
    echo "[$(ts)] WARNING: workflow failed — retrying in 300s"
    sleep 300
    uv run investment_workflow.py  # second attempt; checkpointer resumes from last node
fi

echo "[$(ts)] ===== daily run complete ====="
```

### 1.4 LangGraph Node-Level Restart

No LangGraph checkpointer is configured. Every node runs from scratch on retry. The fix:

```python
# investment_workflow.py
from langgraph.checkpoint.sqlite import SqliteSaver

def build_graph(checkpointer=None):
    graph = StateGraph(WorkflowState)
    # ... add_node, add_edge unchanged ...
    return graph.compile(checkpointer=checkpointer)

def main():
    checkpointer = SqliteSaver.from_conn_string("workflow_checkpoints.db")
    graph = build_graph(checkpointer=checkpointer)

    thread_id = f"daily_{date.today().isoformat()}"
    result = graph.invoke(
        initial_state,
        config={"configurable": {"thread_id": thread_id}},
    )
```

With checkpointing: if `chief_strategist` fails after spending $0.05 on prior nodes, a re-run with the same `thread_id` resumes from `chief_strategist` directly.

---

## 2. Backup Strategy

### 2.1 Current State: None

| Data | Backup | Recovery |
|------|--------|---------|
| TiDB `agent_memory` | ❌ No backup | Rebuild from scratch (data lost) |
| `market_snapshot.json` | ❌ No backup | Re-run `test_collection.py` |
| `collection_journal.jsonl` | ❌ No backup | Irreplaceable (historical latency data) |
| `investment_brief_*.txt` | ❌ No backup | Irreplaceable (historical briefs) |
| `.env` | ❌ No backup | Must re-obtain API keys manually |
| `user_portfolio` data | ❌ No backup | Must re-enter manually |

**A single NVMe disk failure or accidental `docker volume rm` destroys all historical data**: every daily brief, every cost log, every portfolio entry, every backtest accuracy score.

### 2.2 Recommended Backup Strategy

**Tier 1 — TiDB mysqldump (P0, 15 min)**

```bash
# /home/itadmin/ai_agent_studio/backup_db.sh
#!/bin/bash
BACKUP_DIR="/home/itadmin/backups/tidb"
mkdir -p "$BACKUP_DIR"
DATE=$(date +%Y%m%d)

docker exec <tidb_container_name> mysqldump \
    -u root -h 127.0.0.1 agent_memory \
    > "$BACKUP_DIR/agent_memory_$DATE.sql"

# Keep last 30 days
find "$BACKUP_DIR" -name "*.sql" -mtime +30 -delete

echo "[$(date)] Backup complete: agent_memory_$DATE.sql"
```

```bash
# Add to crontab:
# 0 22 * * * /home/itadmin/ai_agent_studio/backup_db.sh >> /home/itadmin/logs/backup.log 2>&1
```

**Tier 2 — Off-server copy (P1)**

Local disk backup does not protect against disk failure. Push daily to a secondary location:

```bash
# Option A: rsync to a NAS on the LAN
rsync -az "$BACKUP_DIR/" nas-server:/ai_agent_backups/

# Option B: rclone to Google Drive / S3
rclone copy "$BACKUP_DIR/" gdrive:ai-agent-backups/
```

**Tier 3 — `.env` backup (P0, 5 min)**

The `.env` file contains all API keys. If lost, each key must be manually re-generated from each provider's console. Store an encrypted copy:

```bash
# Encrypt with GPG (key already on server):
gpg --symmetric --cipher-algo AES256 .env
# Store .env.gpg in a password manager or secure location
```

### 2.3 Data at Risk Summary

| Data | Age | Irreplaceable? | Priority |
|------|-----|---------------|----------|
| `user_portfolio` (portfolio entries) | Since Phase 5 | Yes — manual entry | 🔴 P0 |
| `daily_briefs` (investment briefs) | Since Phase 3 | Yes — LLM-generated | 🔴 P0 |
| `cost_logs` (cost history) | Since Phase 4 | No — re-run possible | 🟠 P1 |
| `market_actuals` (TWSE data) | Since Phase 5 | No — re-fetchable from TWSE | 🟠 P1 |
| `collection_journal.jsonl` | Since Phase 3 | No — re-runnable | 🟡 P2 |

---

## 3. Recovery Strategy

### 3.1 Failure Scenarios and Recovery Paths

**Scenario A: Claude API 529 at 08:20 CST**

Current recovery: manual re-run later that day.

Improved recovery:
1. LangGraph checkpointer saves state after each completed node
2. Retry in `daily_run.sh` after 300 seconds
3. Resumed run completes from the failed node
4. User receives brief (delayed, but present)

**Scenario B: TAIFEX scraper breaks (HTML change)**

Current recovery: brief not generated that day; no fallback.

Improved recovery:
1. `daily_run.sh` detects `test_collection.py` failure
2. Checks if `market_snapshot.json` is < 24 hours old (yesterday's snapshot)
3. If fresh enough, proceeds with `investment_workflow.py` using stale data
4. `data_collector_node` notes `data_ok=false` in state
5. Optional: conditional routing (from `production_architecture_recommendation.md:T3-A`) sends "資料取得失敗" notification instead of a potentially inaccurate brief

**Scenario C: TiDB container not running**

Current recovery: `save_to_db_node` fails silently; brief is generated and pushed but not saved; backtest cannot run.

Improved recovery:
1. Add TiDB health check before `investment_workflow.py`:
   ```bash
   if ! docker exec tidb mysql -u root -e "SELECT 1" > /dev/null 2>&1; then
       docker start tidb && sleep 10
   fi
   ```
2. `save_to_db_node` catches exception but does not propagate — brief is still pushed
3. Alert via Telegram: "TiDB unreachable — brief pushed but not saved"

**Scenario D: Server reboot**

Current recovery: cron resumes on schedule; Streamlit must be manually restarted; no data loss if TiDB has `--restart=always`.

Improved recovery (with systemd):
```ini
# /etc/systemd/system/ai-investment-dashboard.service
[Unit]
Description=AI Investment Dashboard
After=network.target

[Service]
User=itadmin
WorkingDirectory=/home/itadmin/ai_agent_studio
ExecStart=/home/itadmin/.local/bin/uv run streamlit run dashboard.py --server.port 8501
Restart=on-failure
RestartSec=10s

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now ai-investment-dashboard
```

**Scenario E: NVMe disk failure**

Current recovery: all data lost, server must be rebuilt from documentation.

Improved recovery (with backup in place):
1. Provision new server per `deployment_guide.md`
2. Restore TiDB: `mysql -u root agent_memory < backup.sql`
3. Restore `.env` from encrypted backup
4. Re-copy application files from Git repository

### 3.2 Recovery Time Objectives (Estimated)

| Scenario | Current RTO | With Improvements |
|----------|------------|------------------|
| Claude 529 transient | Manual re-run (~minutes) | Automatic retry (~5 min) |
| Data collection failure | Skip day | Resume with stale snapshot (~0 min delay) |
| TiDB restart | Manual intervention | Auto-restart + health check |
| Server reboot | Dashboard: manual; cron: next schedule | Dashboard: systemd autostart |
| Disk failure | Days (rebuild from docs) | Hours (restore from backup) |

---

## 4. Persistence Strategy

### 4.1 Durable Storage (TiDB)

| Table | Data Type | Retention | Growth Rate |
|-------|-----------|-----------|-------------|
| `daily_briefs` | LLM text, predictions | Forever | ~1 row/day (trading days) |
| `market_actuals` | TWSE prices | Forever | ~1 row/day |
| `cost_logs` | Token counts, costs | Forever | ~6 rows/run |
| `user_portfolio` | Holdings | Until deleted | Stable (manual entries) |

No retention policy is defined. At current growth rate (~160 rows/year for `cost_logs`), disk usage is trivial. No cleanup needed until multi-user scale.

**Missing:** No `accuracy_logs` table. The `backtest_agent` generates a 0–100 accuracy score and full evaluation text but it is only printed to stdout — never persisted. This is the only historical analysis that cannot be reconstructed from existing data.

### 4.2 Ephemeral Storage (Filesystem)

| File | Created | Cleaned Up | Disk Risk |
|------|---------|------------|-----------|
| `market_snapshot.json` | Daily (overwritten) | — | None |
| `investment_brief_*.txt` | Each run | Never | Low — ~2KB/file |
| `collection_journal.jsonl` | Each run (appended) | Never | Low — ~1KB/entry |
| `logs/daily_run.log` | Appended daily | Never | Medium — grows unbounded |
| `workflow_checkpoints.db` (proposed) | Each run | Never | Low — SQLite, ~100KB/day |

**Recommendation**: Add `logrotate` for `daily_run.log`:

```
# /etc/logrotate.d/ai-agent
/home/itadmin/ai_agent_studio/logs/daily_run.log {
    daily
    rotate 30
    compress
    missingok
    notifempty
}
```

### 4.3 State Persistence Gap: LangGraph Checkpointing

The most important persistence gap is the absence of LangGraph checkpointing. Every workflow run starts from a clean state. If any node fails:

- All inputs paid for (Haiku, Sonnet calls before failure) are discarded
- The new run must re-execute all prior nodes
- For the worst case (Opus failure after ~$0.05 of prior nodes): $0.05 wasted per failure

With `SqliteSaver`:
- State is written to `workflow_checkpoints.db` after every completed node
- A re-run with the same `thread_id` (one per calendar day) skips completed nodes
- The checkpointer DB is ephemeral (can be deleted after 7 days)

---

## 5. Reliability Gap Summary

| Gap | Impact | Effort | Priority |
|-----|--------|--------|----------|
| No LangGraph checkpointer | Wasted cost + data loss on failure | 15 min | 🔴 P0 |
| `set -euo pipefail` stops workflow on data collection failure | Brief not generated if TAIFEX is down | 30 min | 🔴 P0 |
| No LLM retry (transient 529) | Silent daily failure several times/month | 30 min | 🔴 P0 |
| No TiDB backup | All historical data lost on disk failure | 15 min | 🔴 P0 |
| No `.env` backup | All API keys lost; must re-obtain from all providers | 5 min | 🔴 P0 |
| No systemd service for Streamlit | Dashboard unavailable after reboot | 15 min | 🟠 P1 |
| `accuracy_report` not persisted | Historical LLM evaluation lost | 30 min | 🟠 P1 |
| TiDB `--restart=always` unverified | DB may not come back after reboot | 5 min | 🟠 P1 |
| No off-server backup copy | Disk failure destroys local backup | 1 hr | 🟠 P1 |
| No logrotate for `daily_run.log` | Log file grows unbounded | 15 min | 🟡 P2 |
| No `accuracy_logs` table | Accuracy trend not queryable | 30 min | 🟡 P2 |
