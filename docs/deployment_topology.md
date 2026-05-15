# Deployment Topology
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Executive Summary

Deployment is a **fully manual, single-environment, file-copy workflow** with no CI/CD, no staging, and no version-controlled infrastructure-as-code for the server configuration. The developer writes code on a Windows workstation, copies files to the Ubuntu server via `scp`, and runs scripts over SSH. There is no automated pipeline from commit to deployment.

---

## 1. Deployment Flow

### 1.1 Current Deployment Procedure

```
[Windows Dev Machine — VS Code]
    │
    │  1. Write / edit Python files locally
    │  2. scp individual files to remote server:
    │
    ▼
PowerShell:
  scp "$local\market_analyst_agents.py"  "ai-agents-server:/home/itadmin/ai_agent_studio/"
  scp "$local\investment_workflow.py"    "ai-agents-server:/home/itadmin/ai_agent_studio/"
  scp "$local\mcp_servers\*.py"         "ai-agents-server:/home/itadmin/ai_agent_studio/mcp_servers/"
    │
    │  3. SSH into server
    │  4. source /home/itadmin/.local/bin/env
    │  5. uv run <changed_script>.py   (manual smoke test)
    │
    ▼
[Ubuntu Server 10.0.1.20]
  /home/itadmin/ai_agent_studio/
```

**There is no deployment script.** Each file is deployed individually. There is no mechanism to ensure all related files are deployed atomically (e.g., if `market_analyst_agents.py` is updated but `investment_workflow.py` is not yet deployed, the server runs a mixed-version state).

### 1.2 Files Requiring Manual Deployment

| File | Deploy Command | Notes |
|------|---------------|-------|
| `market_analyst_agents.py` | `scp` | Core agents — most frequently modified |
| `investment_workflow.py` | `scp` | Graph topology |
| `backtest_agent.py` | `scp` | Backtesting graph |
| `database_tools.py` | `scp` | DB schema changes require manual `ALTER TABLE` |
| `portfolio_tools.py` | `scp` | — |
| `messenger_tools.py` | `scp` | — |
| `dashboard.py` | `scp` | Requires manual `streamlit run` restart |
| `mcp_servers/*.py` | `scp` | Effective on next invocation |
| `.env` | Manual edit | Never in version control |
| `daily_run.sh` | `scp` or manual edit | Currently diverges from local repo |

### 1.3 Dependency Installation

When a new package is added to `pyproject.toml`:

```bash
# Developer (local, Windows):
# 1. Add to pyproject.toml
# 2. uv add <package>  (updates local venv)

# Remote server:
# 1. scp pyproject.toml ai-agents-server:/home/itadmin/ai_agent_studio/
# 2. SSH into server
# 3. uv sync  (installs new dependency)
```

`uv.lock` is not committed. If the server resolves a different minor version than the developer's local venv (common with `>=` version pins), the behaviour may differ silently.

---

## 2. CI/CD

### 2.1 Current State: None

| CI/CD Component | Status |
|----------------|--------|
| GitHub Actions (or any CI runner) | ❌ Not configured |
| Automated test suite | ❌ Not present |
| Lint / type check automation | ❌ Not present |
| Automated deployment on push | ❌ Not present |
| Deployment rollback mechanism | ❌ Not present |
| Blue/green deployment | ❌ Not applicable (single server) |

**The Git repository exists** (`git init`, initial commit `72580f2`), but no `.github/workflows/` directory has been created. There is no automated validation between a `git push` and deployment.

### 2.2 What Deployment Automation Would Require

To add minimal CI/CD for this project:

```yaml
# .github/workflows/deploy.yml (proposed)
name: Deploy to ai-agents-server
on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run type check
        run: pip install pyright && pyright
      - name: Deploy via rsync
        run: |
          rsync -av --exclude='.env' --exclude='*.json' \
            ./ itadmin@10.0.1.20:/home/itadmin/ai_agent_studio/
      - name: Sync dependencies
        run: ssh itadmin@10.0.1.20 "cd ai_agent_studio && uv sync"
```

**Blocker**: The server is on a LAN IP (10.0.1.20) with no public access. A GitHub Actions runner cannot reach it directly. Options: self-hosted runner on the LAN, or VPN.

---

## 3. Environment Separation

### 3.1 Current State: Single Environment

```
┌─────────────────────────────────────────────────────────────────┐
│  PRODUCTION ONLY                                                 │
│  Ubuntu Server 10.0.1.20                                        │
│  /home/itadmin/ai_agent_studio/                                 │
│                                                                  │
│  .env:                                                           │
│    ANTHROPIC_API_KEY = real key (billed, live)                  │
│    TIDB_HOST = 127.0.0.1 (live database)                        │
│    LINE/TELEGRAM = real tokens (sends to real users)            │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  DEVELOPMENT (Windows workstation)                               │
│  c:\Users\stpadmin\Documents\VS Code\AI Agents\                 │
│                                                                  │
│  .env: depends — if developer copies production .env,           │
│        local runs use live keys and send real LINE messages     │
└─────────────────────────────────────────────────────────────────┘
```

**There is no staging environment.** Any code change tested locally that is then `scp`'d to the server goes directly to the only database, the real Anthropic API key, and the real LINE/Telegram channels. A bug in `send_notification_node` can send malformed messages to the real user.

### 3.2 Risks of Single-Environment Deployment

| Risk | Impact |
|------|--------|
| Schema migration tested on production DB | Accidental data loss |
| New LLM prompt tested with real API key | Incurs real billing before validation |
| Bug in `send_notification_node` deployed | Real LINE/Telegram messages sent with bad content |
| Incorrect `.env` change on server | Breaks live cron job silently until next 08:20 |
| `scp` of wrong file version | Partial update; mixed-version state |

### 3.3 Staging/Production Separation

**Not present.** No secondary server, no Docker Compose override for local vs. production, no environment-scoped `.env` files.

A minimal staging environment for this project would be a second `.env` pointing to:
- `ANTHROPIC_API_KEY` → test key with low spend cap
- `TIDB_DB=agent_memory_staging` → separate schema
- `LINE_CHANNEL_ACCESS_TOKEN` → test bot
- `TELEGRAM_CHAT_ID` → test chat

At minimum, a local Docker Compose with a test TiDB would reduce the risk of schema migration testing on production data.

---

## 4. Infrastructure as Code (IaC)

### 4.1 Version-Controlled

| Item | In Repo | Format |
|------|---------|--------|
| Python application code | ✅ | `.py` files |
| Dependency declaration | ✅ | `pyproject.toml` |
| `.env` template | ✅ | `.env.template` |
| `.gitignore` | ✅ | — |
| `daily_run.sh` (local, stale) | ✅ (stale) | bash |

### 4.2 NOT Version-Controlled

| Item | Location | Risk |
|------|----------|------|
| `daily_run.sh` (remote, authoritative) | Remote server only | Diverged from repo; new server deployment uses wrong version |
| TiDB Docker run command / Compose | Not recorded anywhere | Cannot reproduce TiDB setup |
| TiDB schema DDL | Not recorded | Table creation in `database_tools.py` (partial), but no migration files |
| Crontab entry | Remote `crontab -l` only | Not in repo; lost on server rebuild |
| SSH hardening config | `/etc/ssh/sshd_config.d/hardening.conf` on remote | Not in repo |
| `uv.lock` | Remote venv only | Dependency versions not locked |

**The authoritative server state exists only on the server.** A server rebuild requires:
1. Re-reading `deployment_guide.md` and re-running every command manually
2. Re-creating the TiDB container from memory (no Compose file)
3. Re-creating the crontab from memory
4. Re-copying `.env` from a backup

---

## 5. Deployment Topology Diagram

```
┌──────────────────────────────────────────────────────────────────────┐
│  Developer Workstation (Windows 11)                                   │
│  c:\Users\stpadmin\Documents\VS Code\AI Agents\                      │
│                                                                       │
│  [ VS Code ] ─── git commit ──► GitHub (origin/main)                │
│                                  (no CI/CD connected)                │
│  [ PowerShell ]                                                       │
│    scp *.py → ai-agents-server:/home/itadmin/ai_agent_studio/        │
│    ssh ai-agents-server "uv sync && uv run <test>"                   │
└──────────────────────────────────────────────────────────────────────┘
         │ scp (manual, file-by-file)
         ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Production Server — Ubuntu 10.0.1.20                                 │
│  /home/itadmin/ai_agent_studio/                                       │
│                                                                       │
│  [cron 08:20 Mon-Fri]  daily_run.sh                                  │
│    │── test_collection.py  ──► finance_mcp_server.py (stdio)         │
│    └── investment_workflow.py                                         │
│            ├── LLM calls → api.anthropic.com                         │
│            ├── Data calls → TAIFEX, Yahoo Finance, Anue              │
│            ├── DB writes → TiDB :4000                                │
│            └── Push → LINE / Telegram                                │
│                                                                       │
│  [manual] streamlit run dashboard.py --server.port 8501              │
│  [manual] uv run backtest_agent.py                                   │
│  [manual] uv run agent_orchestrator.py                               │
│                                                                       │
│  [Docker] TiDB container (port 4000 → 127.0.0.1)                     │
│           agent_memory DB (4 tables)                                  │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 6. Deployment Gap Summary

| Gap | Severity | Recommended Fix |
|-----|----------|----------------|
| No CI/CD pipeline | 🟠 MEDIUM | GitHub Actions + rsync (requires LAN runner or VPN) |
| No staging environment | 🟠 MEDIUM | Second `.env` + `TIDB_DB=staging` + test messenger tokens |
| No atomic deployment | 🟠 MEDIUM | Deploy script that copies all files then restarts services |
| TiDB container not in Compose file | 🟠 MEDIUM | Create `docker/tidb-compose.yml` with bind mount and restart policy |
| Crontab not version-controlled | 🟠 MEDIUM | Add `crontab.txt` to repo; deploy with `crontab crontab.txt` |
| `daily_run.sh` remote diverges from repo | 🟠 MEDIUM | Sync and commit; deploy from repo only |
| Schema migrations not tracked | 🟠 MEDIUM | Add `migrations/` directory with dated SQL files |
| `uv.lock` not committed | 🟠 MEDIUM | `uv lock && git add uv.lock` |
| SSH hardening config not in repo | 🟡 LOW | Store in `server/sshd_hardening.conf` |
| No deployment rollback mechanism | 🟡 LOW | Git tags for each deployment; rollback = `scp` previous version |
