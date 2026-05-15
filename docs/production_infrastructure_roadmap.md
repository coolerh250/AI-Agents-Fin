# Production Infrastructure Roadmap
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Executive Summary

This roadmap converts the current prototype infrastructure into a production-grade deployment. Four infrastructure analysis documents have been produced in this session: `infrastructure_analysis.md`, `deployment_topology.md`, `scalability_analysis.md`, `reliability_review.md`. This roadmap consolidates their findings into a phased implementation plan.

**Current infrastructure posture**: Single bare-metal Ubuntu server, manual file-copy deployment, no CI/CD, no staging, no process supervision, no backup, no LangGraph checkpointing. The daily workflow fails silently on any API error.

**Target posture after this roadmap**: Automated daily workflow that self-recovers from transient failures, persists all critical data with off-server backup, restarts all services on reboot, and can be reproduced from the Git repository in under 2 hours.

---

## Infrastructure Risk Matrix

| Risk | Severity | Frequency | Root Document |
|------|----------|-----------|---------------|
| No LangGraph checkpointer — failure wastes all computation | 🔴 CRITICAL | Several/month (Claude 529) | reliability_review.md |
| `set -euo pipefail` — data collection failure stops workflow | 🔴 CRITICAL | Monthly (TAIFEX scrape changes) | reliability_review.md |
| No TiDB backup — disk failure loses all data | 🔴 CRITICAL | Low probability, irreversible | reliability_review.md |
| No `.env` backup — key loss requires manual re-registration | 🔴 CRITICAL | Low probability, high cost | reliability_review.md |
| No LLM retry — transient 529 fails entire run | 🔴 CRITICAL | Several/month | scalability_analysis.md |
| No systemd for dashboard — requires manual restart after reboot | 🟠 HIGH | Every reboot | infrastructure_analysis.md |
| `uv.lock` not committed — builds non-reproducible | 🟠 HIGH | Every new server setup | deployment_topology.md |
| TiDB Docker config not version-controlled | 🟠 HIGH | Every new server setup | deployment_topology.md |
| No staging environment — bugs tested on production | 🟠 HIGH | Every code change | deployment_topology.md |
| `_engine()` per-call — connection pool churn | 🟠 HIGH | Every workflow run | scalability_analysis.md |
| `accuracy_report` not persisted | 🟡 MEDIUM | Daily | reliability_review.md |
| No logrotate — logs grow unbounded | 🟡 MEDIUM | Long-term | reliability_review.md |
| No CI/CD — manual file-copy deployment | 🟡 MEDIUM | Every deployment | deployment_topology.md |
| `daily_run.sh` remote diverges from repo | 🟡 MEDIUM | Ongoing | deployment_topology.md |
| No off-server backup | 🟡 MEDIUM | Long-term | reliability_review.md |

---

## Phase 0 — Immediate Reliability Fixes
**Target**: Before the next trading day | **Effort**: ~3 hours

These five changes eliminate the most common failure modes with minimal code changes.

### Fix 0-A: LangGraph Checkpointing (15 min)

**File**: `investment_workflow.py`

```python
from langgraph.checkpoint.sqlite import SqliteSaver
from datetime import date

def build_graph(checkpointer=None):
    graph = StateGraph(WorkflowState)
    # ... all add_node, add_edge calls unchanged ...
    return graph.compile(checkpointer=checkpointer)

def main():
    # ...
    checkpointer = SqliteSaver.from_conn_string("workflow_checkpoints.db")
    graph = build_graph(checkpointer=checkpointer)
    thread_id = f"daily_{date.today().isoformat()}"
    result = graph.invoke(
        initial_state,
        config={"configurable": {"thread_id": thread_id}},
    )
```

Add `workflow_checkpoints.db` to `.gitignore`. **Effect**: Transient node failures resume from the last completed node on retry.

---

### Fix 0-B: Per-LLM Retry with Exponential Jitter (30 min)

**File**: `market_analyst_agents.py`

```python
import anthropic

def _llm(model: str, max_tokens: int = 1024) -> ChatAnthropic:
    return ChatAnthropic(
        model=model,
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        max_tokens=max_tokens,
    ).with_retry(
        stop_after_attempt=3,
        wait_exponential_jitter=True,
        retry_if_exception_type=(
            anthropic.RateLimitError,
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
        ),
    )
```

Apply the same `.with_retry(...)` to `_llm_opus()`. **Effect**: 529 errors auto-retry with jitter. Parallel branches (`chip_analyst` + `tech_analyst`) do not storm the API on simultaneous failures.

---

### Fix 0-C: Resilient `daily_run.sh` (30 min)

**File**: `daily_run.sh`

Replace `set -euo pipefail` with per-step error handling. Allow `test_collection.py` to fail gracefully (use stale snapshot if < 24 hours old):

```bash
#!/bin/bash
LOG_DIR="/home/itadmin/ai_agent_studio/logs"
mkdir -p "$LOG_DIR"
source /home/itadmin/.local/bin/env
cd /home/itadmin/ai_agent_studio

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*"; }

log "===== daily run start ====="

# Step 1: Data collection — allow failure if snapshot exists
if ! uv run test_collection.py; then
    log "WARNING: test_collection failed"
    if [ ! -f market_snapshot.json ]; then
        log "ERROR: no snapshot — aborting"
        exit 1
    fi
    SNAPSHOT_AGE=$(( $(date +%s) - $(stat -c %Y market_snapshot.json) ))
    if [ "$SNAPSHOT_AGE" -gt 86400 ]; then
        log "ERROR: snapshot > 24h old — aborting"
        exit 1
    fi
    log "Using snapshot from $(stat -c %y market_snapshot.json)"
fi

# Step 2: Analysis workflow — retry once after 5 min
if ! uv run investment_workflow.py; then
    log "WARNING: workflow failed — retrying in 300s"
    sleep 300
    uv run investment_workflow.py
fi

log "===== daily run complete ====="
```

**Effect**: TAIFEX scrape failure no longer blocks the daily brief.

---

### Fix 0-D: TiDB Backup Script (15 min)

**New file**: `/home/itadmin/ai_agent_studio/backup_db.sh`

```bash
#!/bin/bash
BACKUP_DIR="/home/itadmin/backups/tidb"
mkdir -p "$BACKUP_DIR"
DATE=$(date +%Y%m%d)

# Get TiDB container name
TIDB_CONTAINER=$(docker ps --filter "expose=4000" --format "{{.Names}}" | head -1)

docker exec "$TIDB_CONTAINER" mysqldump \
    -u root --host=127.0.0.1 agent_memory \
    > "$BACKUP_DIR/agent_memory_$DATE.sql"

# Retain 30 days
find "$BACKUP_DIR" -name "*.sql" -mtime +30 -delete

echo "[$(date)] Backup: agent_memory_$DATE.sql ($(du -h "$BACKUP_DIR/agent_memory_$DATE.sql" | cut -f1))"
```

```bash
chmod +x backup_db.sh

# Add to crontab:
# 0 22 * * * /home/itadmin/ai_agent_studio/backup_db.sh >> /home/itadmin/logs/backup.log 2>&1
```

---

### Fix 0-E: `.env` Encrypted Backup (5 min)

```bash
# Create encrypted backup (one-time):
gpg --symmetric --cipher-algo AES256 --output .env.gpg .env
# Store .env.gpg passphrase in a password manager

# To restore:
gpg --decrypt .env.gpg > .env
```

---

## Phase 1 — Process Management and IaC
**Target**: Within 1 week | **Effort**: ~4 hours

### Fix 1-A: systemd Service for Streamlit Dashboard (20 min)

```ini
# /etc/systemd/system/ai-investment-dashboard.service
[Unit]
Description=AI Investment Dashboard
After=network.target tidb.service
Wants=tidb.service

[Service]
User=itadmin
WorkingDirectory=/home/itadmin/ai_agent_studio
EnvironmentFile=/home/itadmin/ai_agent_studio/.env
ExecStart=/home/itadmin/.local/bin/uv run streamlit run dashboard.py \
    --server.port 8501 \
    --server.address 127.0.0.1
Restart=on-failure
RestartSec=10s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now ai-investment-dashboard
```

Note: `--server.address 127.0.0.1` binds Streamlit to localhost only. Access via SSH tunnel (see security roadmap). **Effect**: Dashboard auto-starts on boot; auto-restarts on crash.

---

### Fix 1-B: systemd Service for TiDB (15 min)

```ini
# /etc/systemd/system/tidb.service
[Unit]
Description=TiDB Container
After=docker.service
Requires=docker.service

[Service]
Restart=always
ExecStart=/usr/bin/docker start -a tidb_agent
ExecStop=/usr/bin/docker stop tidb_agent
StandardOutput=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now tidb
```

**Effect**: TiDB restarts automatically after server reboot (instead of relying on `--restart=always` Docker policy, which is unverified).

---

### Fix 1-C: Version-Control Infrastructure Configuration (1 hr)

Add to Git repository:

```
ai_agent_studio/
├── server/
│   ├── tidb-compose.yml          # Docker Compose for TiDB
│   ├── sshd_hardening.conf       # SSH hardening config
│   ├── ai-investment-dashboard.service  # systemd unit
│   ├── tidb.service              # systemd unit
│   └── logrotate.conf            # log rotation config
├── migrations/
│   ├── 001_initial_schema.sql    # All CREATE TABLE statements
│   ├── 002_add_cost_logs.sql
│   ├── 003_add_portfolio.sql
│   └── README.md
├── crontab.txt                   # Authoritative crontab
└── daily_run.sh                  # Authoritative (synced from remote)
```

**`server/tidb-compose.yml`**:
```yaml
services:
  tidb:
    image: pingcap/tidb:latest
    container_name: tidb_agent
    ports:
      - "127.0.0.1:4000:4000"    # Bind to localhost only — not LAN-exposed
    volumes:
      - tidb_data:/var/lib/tidb
    restart: unless-stopped
    networks:
      - agent_sandbox

networks:
  agent_sandbox:
    driver: bridge
    ipam:
      config:
        - subnet: 172.20.0.0/16

volumes:
  tidb_data:
```

**`crontab.txt`** (deploy with `crontab crontab.txt`):
```
# AI Agent Studio — Crontab
# Deploy: crontab /home/itadmin/ai_agent_studio/crontab.txt
20 8 * * 1-5 /home/itadmin/ai_agent_studio/daily_run.sh >> /home/itadmin/ai_agent_studio/logs/daily_run.log 2>&1
0 22 * * * /home/itadmin/ai_agent_studio/backup_db.sh >> /home/itadmin/logs/backup.log 2>&1
```

---

### Fix 1-D: Commit `uv.lock` and Shared Engine (30 min)

```bash
# On remote server:
cd /home/itadmin/ai_agent_studio
uv lock
scp ai-agents-server:/home/itadmin/ai_agent_studio/uv.lock \
    "c:\Users\stpadmin\Documents\VS Code\AI Agents\"
git add uv.lock && git commit -m "Add uv.lock for reproducible builds"
```

Shared DB engine singleton in `database_tools.py`:
```python
from functools import lru_cache

@lru_cache(maxsize=1)
def _get_engine() -> Engine:
    host     = os.getenv("TIDB_HOST", "127.0.0.1")
    port     = os.getenv("TIDB_PORT", "4000")
    user     = os.getenv("TIDB_USER", "root")
    password = os.getenv("TIDB_PASSWORD", "")
    db       = os.getenv("TIDB_DB", "agent_memory")
    url = f"mysql+pymysql://{user}:{password}@{host}:{port}/{db}?charset=utf8mb4"
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=2)
```

Replace all `_engine()` calls with `_get_engine()`. **Effect**: One connection pool per process; shared across all DB calls.

---

### Fix 1-E: Log Rotation (15 min)

```
# /etc/logrotate.d/ai-agent
/home/itadmin/ai_agent_studio/logs/daily_run.log
/home/itadmin/logs/backup.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
```

---

## Phase 2 — Staging and Reproducibility
**Target**: Within 1 month | **Effort**: ~4 hours

### Fix 2-A: Minimal Staging Environment (2 hrs)

Create a staging configuration that uses:
- Separate TiDB schema: `agent_memory_staging`
- Anthropic API key with low spend cap (set $1/month limit in Anthropic console)
- Test LINE bot and Telegram chat

```bash
# .env.staging (gitignored — template in .env.staging.template)
ANTHROPIC_API_KEY=<staging_key_with_$1_cap>
TIDB_DB=agent_memory_staging
LINE_CHANNEL_ACCESS_TOKEN=<test_bot_token>
TELEGRAM_BOT_TOKEN=<test_bot_token>
TELEGRAM_CHAT_ID=<test_chat_id>
```

```bash
# Run workflow in staging:
cp .env.staging .env.active
ENV_FILE=.env.active uv run investment_workflow.py
```

**Effect**: New LLM prompts, schema migrations, and `send_notification` changes can be tested without touching production data or real user channels.

---

### Fix 2-B: Off-Server Backup (1 hr)

Choose one:

**Option A — LAN NAS (simplest)**:
```bash
# Add to backup_db.sh:
rsync -az /home/itadmin/backups/tidb/ nas-server:/ai-agent-backups/tidb/
```

**Option B — rclone to cloud storage**:
```bash
# Install rclone, configure with Google Drive or S3:
rclone copy /home/itadmin/backups/tidb/ gdrive:ai-agent-backups/
```

---

### Fix 2-C: Database Migration Tracking (1 hr)

Create `migrations/` directory with numbered SQL files. Apply with a simple script:

```bash
#!/bin/bash
# migrations/apply.sh
APPLIED_FILE="/home/itadmin/ai_agent_studio/migrations/.applied"
touch "$APPLIED_FILE"

for sql_file in /home/itadmin/ai_agent_studio/migrations/*.sql; do
    fname=$(basename "$sql_file")
    if ! grep -q "$fname" "$APPLIED_FILE"; then
        echo "Applying $fname..."
        docker exec tidb_agent mysql -u root agent_memory < "$sql_file"
        echo "$fname" >> "$APPLIED_FILE"
        echo "Applied $fname"
    fi
done
```

---

## Phase 3 — Multi-User Scalability (Optional, Future)
**Target**: When second user added | **Effort**: ~12 hours

### Fix 3-A: Per-User Data Isolation (4 hrs)

Add `user_id` column to `user_portfolio`, `daily_briefs`, `cost_logs`:

```sql
ALTER TABLE daily_briefs ADD COLUMN user_id VARCHAR(50) NOT NULL DEFAULT 'default';
ALTER TABLE cost_logs ADD COLUMN user_id VARCHAR(50) NOT NULL DEFAULT 'default';
ALTER TABLE user_portfolio ADD COLUMN user_id VARCHAR(50) NOT NULL DEFAULT 'default';

-- Add unique constraint with user scope:
ALTER TABLE daily_briefs ADD UNIQUE INDEX uq_user_trade_date (user_id, trade_date);
```

### Fix 3-B: Per-User Snapshot Files (2 hrs)

Replace `market_snapshot.json` (single shared file) with per-user snapshots:

```python
# test_collection.py
USER_ID = os.getenv("AGENT_USER_ID", "default")
SNAPSHOT_FILE = Path(f"market_snapshot_{USER_ID}.json")

# investment_workflow.py
USER_ID = os.getenv("AGENT_USER_ID", "default")
SNAPSHOT_FILE = Path(f"market_snapshot_{USER_ID}.json")
```

### Fix 3-C: Async Migration (8 hrs)

Migrate all LangGraph nodes to `async def`. Use `await graph.ainvoke()`. Enables true concurrent I/O for parallel Sonnet calls and yfinance fetches. Full migration order in `production_architecture_recommendation.md:T3-E`.

---

## Consolidated Change Table

| Fix | File(s) | Phase | Effort | Risk Addressed |
|-----|---------|-------|--------|----------------|
| LangGraph checkpointer | `investment_workflow.py` | P0 | 15 min | Failure cost / resume |
| LLM retry with jitter | `market_analyst_agents.py` | P0 | 30 min | Claude 529 errors |
| Resilient `daily_run.sh` | `daily_run.sh` | P0 | 30 min | Data collection failure |
| TiDB backup script | `backup_db.sh` (new) | P0 | 15 min | Data loss on disk failure |
| `.env` encrypted backup | `.env.gpg` | P0 | 5 min | API key loss |
| systemd for Streamlit | `server/ai-investment-dashboard.service` | P1 | 20 min | Dashboard unavailable after reboot |
| systemd for TiDB | `server/tidb.service` | P1 | 15 min | DB unavailable after reboot |
| `server/tidb-compose.yml` | New file in repo | P1 | 30 min | IaC reproducibility |
| `crontab.txt` in repo | New file | P1 | 10 min | Cron not version-controlled |
| `uv.lock` committed | Repo | P1 | 10 min | Non-reproducible builds |
| Shared DB engine (`lru_cache`) | `database_tools.py` | P1 | 20 min | Connection pool churn |
| Log rotation | `/etc/logrotate.d/ai-agent` | P1 | 15 min | Unbounded log growth |
| Migration files | `migrations/*.sql` | P1 | 30 min | Schema not tracked |
| Staging `.env` | `.env.staging.template` | P2 | 30 min | Testing on production |
| Off-server backup | `backup_db.sh` extension | P2 | 1 hr | Disk failure destroys backups |
| Migration apply script | `migrations/apply.sh` | P2 | 30 min | Manual schema management |
| `user_id` in all tables | `database_tools.py` + SQL | P3 | 4 hrs | Multi-user isolation |
| Per-user snapshot files | `test_collection.py`, `investment_workflow.py` | P3 | 2 hrs | Snapshot race condition |
| Full async migration | All node files | P3 | 8 hrs | Single-threaded I/O bottleneck |

---

## Infrastructure Production Readiness Checklist

| Category | Check | P0 | P1 | P2 | P3 |
|----------|-------|----|----|----|----|
| **Reliability** | LangGraph checkpointer | ✅ | — | — | — |
| **Reliability** | LLM retry with jitter | ✅ | — | — | — |
| **Reliability** | `daily_run.sh` tolerates step failure | ✅ | — | — | — |
| **Reliability** | TiDB daily backup | ✅ | — | — | — |
| **Reliability** | `.env` backup | ✅ | — | — | — |
| **Process Mgmt** | systemd for Streamlit | — | ✅ | — | — |
| **Process Mgmt** | systemd for TiDB | — | ✅ | — | — |
| **IaC** | TiDB Docker config in repo | — | ✅ | — | — |
| **IaC** | Crontab in repo | — | ✅ | — | — |
| **IaC** | `uv.lock` committed | — | ✅ | — | — |
| **IaC** | Schema migrations tracked | — | ✅ | — | — |
| **Performance** | Shared DB engine singleton | — | ✅ | — | — |
| **Ops** | Log rotation | — | ✅ | — | — |
| **Testing** | Staging environment | — | — | ✅ | — |
| **Backup** | Off-server backup copy | — | — | ✅ | — |
| **Scalability** | Per-user data isolation | — | — | — | ✅ |
| **Performance** | Full async migration | — | — | — | ✅ |

---

## Implementation Timeline

```
TODAY (2026-05-15)
    └─► P0 fixes (3 hours — before next trading day 08:20 CST)
          ├── Fix 0-A: Checkpointer          15 min
          ├── Fix 0-B: LLM retry             30 min
          ├── Fix 0-C: Resilient shell       30 min
          ├── Fix 0-D: TiDB backup           15 min
          └── Fix 0-E: .env backup            5 min

WEEK 1 (2026-05-19 → 2026-05-23)
    └─► P1 fixes (4 hours)
          ├── Fix 1-A: systemd Streamlit     20 min
          ├── Fix 1-B: systemd TiDB          15 min
          ├── Fix 1-C: IaC in repo           60 min
          ├── Fix 1-D: uv.lock + engine      30 min
          └── Fix 1-E: logrotate             15 min

MONTH 1 (2026-06)
    └─► P2 fixes (4 hours)
          ├── Fix 2-A: Staging environment    2 hrs
          ├── Fix 2-B: Off-server backup      1 hr
          └── Fix 2-C: Migration tracking     1 hr

FUTURE (when second user added)
    └─► P3 fixes (14 hours)
          ├── Fix 3-A: Per-user DB isolation  4 hrs
          ├── Fix 3-B: Per-user snapshots     2 hrs
          └── Fix 3-C: Async migration        8 hrs
```

**Total effort to production-grade infrastructure (P0–P2): ~11 hours**

---

## Cross-Roadmap Dependencies

This roadmap addresses infrastructure concerns. For a complete production deployment, apply in conjunction with:

| Document | Focus | Interaction |
|----------|-------|-------------|
| `production_security_roadmap.md` | Prompt injection, HMAC, Streamlit auth | P0 security fixes should be applied alongside P0 infrastructure fixes |
| `production_architecture_recommendation.md` | LLM error boundaries, conditional routing | Fix 0-B (retry) + T1-B (error boundaries) are complementary |
| `production_observability_architecture.md` | Structured logging, cost alerting | P1 infrastructure improvements enable better observability |

**Recommended combined P0 session (2026-05-15, before 08:20 CST):**
1. Infrastructure: Fix 0-A (checkpointer), Fix 0-B (retry), Fix 0-C (shell)
2. Security: Remove `parse_mode=Markdown` from `messenger_tools.py`
3. Architecture: Cap Opus `budget_tokens=5000` in `_llm_opus()`
4. Reliability: TiDB backup script + crontab entry

**Combined effort: ~2 hours for highest-impact changes across all three roadmaps.**
