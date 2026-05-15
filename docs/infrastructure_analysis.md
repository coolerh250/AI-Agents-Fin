# Infrastructure Analysis
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Executive Summary

The platform runs on a single bare-metal Ubuntu server as an owner-operated, single-user investment analysis pipeline. The infrastructure is functional but built for a prototype: **no container isolation for Python workloads, no process supervisor, no systemd service, no CI/CD, and no staging environment**. TiDB is the only containerised component. All Python processes run directly on the OS as the `itadmin` user via cron.

---

## 1. Runtime Environment

### 1.1 Host Hardware

| Item | Specification |
|------|---------------|
| IP | 10.0.1.20 (LAN) |
| OS | Ubuntu Server 26.04 |
| CPU | AMD Ryzen 7 4800U — 8 cores / 16 threads |
| RAM | 30 GB |
| Disk | NVMe SSD 238.5 GB (LVM partition: 232 GB usable) |
| Network | LAN only; outbound HTTPS via ISP |

**No GPU.** All inference is cloud-delegated to the Anthropic API. The Ryzen 7 4800U provides adequate CPU for concurrent Python threads and TiDB query serving; it is not a compute bottleneck for the current single-user workload.

### 1.2 Python Runtime

| Component | Detail |
|-----------|--------|
| Python version | 3.14 (pin: `.python-version` + `pyproject.toml >= 3.13`) |
| Package manager | `uv` (astral.sh) — lockfile-free |
| Virtual env | `uv venv` under `/home/itadmin/ai_agent_studio/.venv` |
| Invocation | `uv run <script>.py` — resolves env automatically |
| No `uv.lock` committed | Builds are not reproducible; `uv` re-resolves on each server setup |

Key runtime dependencies:

| Package | Version | Purpose |
|---------|---------|---------|
| `langgraph >= 1.2.0` | Workflow orchestration (3 StateGraph instances) |
| `langchain-anthropic >= 1.4.3` | Anthropic API client |
| `mcp >= 1.27.1` | MCP stdio child process transport |
| `sqlalchemy >= 2.0` | TiDB/MySQL connection layer |
| `streamlit >= 1.45` | Web dashboard (persistent process) |
| `yfinance >= 1.3.0` | TSMC ADR, TAIEX price fetches |
| `httpx >= 0.28.1` | Async HTTP (TAIFEX scrape, Anue API) |
| `psutil >= 7.2.2` | System stats (system_inspector MCP) |

### 1.3 Docker Usage

Docker Engine 29.4.3 / Compose v5.1.3 is installed and functional. However, **Docker is used for exactly one purpose**: running the TiDB database container.

```
┌─────────────────────────────────────────────┐
│  Docker                                      │
│  ┌───────────────────────────────────────┐   │
│  │  TiDB container (port 4000)           │   │
│  │  Volume: tidb data                    │   │
│  └───────────────────────────────────────┘   │
│  Network: agent_sandbox (172.20.0.0/16)      │
│           bridge: br-agent                   │
│  (Python workloads: NOT containerised)        │
└─────────────────────────────────────────────┘
```

**Finding:** A Docker network `agent_sandbox (172.20.0.0/16, br-agent)` was created during Phase 2 of deployment, but **none of the Python application processes are connected to it**. The network exists but provides no isolation benefit for the application tier. The Python scripts connect to TiDB on `127.0.0.1:4000` directly from the host process.

**No Docker Compose file exists in the repository.** TiDB was started manually with `docker run`. The container configuration (bind mount paths, port bindings, restart policy) is not version-controlled.

### 1.4 Kubernetes Usage

**None.** No Kubernetes, Helm, or container orchestration of any kind.

### 1.5 GPU Usage

**None.** All LLM inference is remote (Anthropic API). No local model serving, no CUDA, no GPU drivers.

### 1.6 Process Management

This is the most significant gap in the runtime environment.

| Process | How Started | Restart Policy | Supervisor |
|---------|------------|----------------|------------|
| `investment_workflow.py` | `cron` (daily 08:20 CST) | None — cron does not retry | None |
| `test_collection.py` | `cron` (daily 08:00 CST, implicit) | None | None |
| `backtest_agent.py` | Manual CLI | None | None |
| `agent_orchestrator.py` | Manual CLI | None | None |
| `dashboard.py` (Streamlit) | Manual `streamlit run` | None — dies on SSH disconnect | None |
| TiDB | `docker run` (manual) | `--restart=always` (assumed, unverified) | Docker daemon |
| MCP servers | Python child via `asyncio`/stdio | None — dies with parent | Parent process |

**No systemd service units exist for any Python component.** If the server reboots:
- TiDB comes back (if `--restart=always`)
- All Python processes are dead
- The Streamlit dashboard is inaccessible
- The cron job will resume on next scheduled time but the dashboard requires manual restart

**Current cron entry** (`/home/itadmin/ai_agent_studio/daily_run.sh`):
```bash
# Crontab: 20 8 * * 1-5 /home/itadmin/ai_agent_studio/daily_run.sh >> logs/daily_run.log 2>&1
set -euo pipefail
Step 1: uv run test_collection.py
Step 2: uv run investment_workflow.py
Step 3: Python inline — read from DB + send_brief()
```

`set -euo pipefail` means **any step failure immediately exits the shell script**. If `test_collection.py` fails (e.g., TAIFEX is down), Step 2 never executes. The daily brief is not generated.

---

## 2. Storage Architecture

### 2.1 TiDB Database (Persistent)

| Table | Purpose | Writes | Reads |
|-------|---------|--------|-------|
| `daily_briefs` | LLM-generated investment briefs | `investment_workflow.py` | `backtest_agent.py`, dashboard |
| `market_actuals` | TAIEX actual open/close/gap | `backtest_agent.py`, dashboard | dashboard, `backtest_agent` |
| `cost_logs` | Per-node token/cost/latency | `market_analyst_agents.py` | dashboard |
| `user_portfolio` | Stock holdings with entry price | `dashboard.py`, seed | `portfolio_manager` node |

**All tables are in the `agent_memory` schema under a single TiDB `root` account with DDL privileges.**

### 2.2 Filesystem (Ephemeral)

| File | Lifetime | Notes |
|------|----------|-------|
| `market_snapshot.json` | Overwritten daily | Single file, no versioning, no locking |
| `collection_journal.jsonl` | Accumulates indefinitely | Append-only, never cleaned up |
| `investment_brief_*.txt` | Accumulates indefinitely | Timestamped, never cleaned up |
| `logs/daily_run.log` | Accumulates indefinitely | No log rotation configured |
| `.env` | Permanent | Plaintext secrets, chmod not enforced |

**Disk growth risk**: `collection_journal.jsonl`, `investment_brief_*.txt`, and `logs/daily_run.log` accumulate indefinitely. On a 232 GB NVMe with ~20 trading days/month, file growth is negligible today. However, if log verbosity increases or multiple users are added, this becomes a concern without rotation.

---

## 3. Network Architecture

### 3.1 Outbound Connections (All HTTPS)

| Destination | Protocol | Auth | Rate Limit |
|------------|----------|------|------------|
| `api.anthropic.com` | HTTPS | Bearer token | Anthropic tier limits |
| `www.taifex.com.tw` | HTTPS | None (POST scrape) | Unknown; may IP-block |
| `api.cnyes.com` | HTTPS | None (public API) | 20 articles/page |
| `query1.finance.yahoo.com` | HTTPS | None | Informal; may 429 |
| `api.line.me` | HTTPS | Bearer (channel token) | LINE API limits |
| `api.telegram.org` | HTTPS | Bot token | 30 msg/s |

### 3.2 Inbound Connections

| Port | Service | Bound To | Auth |
|------|---------|----------|------|
| 22 | SSH | `0.0.0.0` (hardened) | Ed25519 key |
| 4000 | TiDB | Unknown — `0.0.0.0` or `127.0.0.1` (unverified) | `root` / empty |
| 8501 | Streamlit | `0.0.0.0` | **NONE** |

**The Streamlit dashboard (`dashboard.py`) binds to `0.0.0.0:8501` by default, accessible to all LAN hosts at `10.0.1.x:8501` without authentication.** This is the highest-severity runtime exposure.

---

## 4. Component Architecture Map

```
Ubuntu Server 10.0.1.20
┌─────────────────────────────────────────────────────────────┐
│                                                              │
│  [cron] 08:20 CST (Mon–Fri)                                  │
│    └─► daily_run.sh                                          │
│         ├─ test_collection.py ──► finance_mcp_server.py     │
│         │     (async MCP stdio)     (subprocess, env=None)  │
│         │     writes: market_snapshot.json                  │
│         │                                                    │
│         └─ investment_workflow.py                            │
│               ├─ data_collector    (Haiku API call)         │
│               ├─ chip_analyst ─┐   (Sonnet API call)        │
│               ├─ tech_analyst  ┘   (Sonnet API call, ∥)     │
│               ├─ chief_strategist  (Opus+Thinking API call)  │
│               ├─ portfolio_manager (Sonnet API call)        │
│               ├─ format_agent     (Haiku API call)          │
│               ├─ save_to_db       (TiDB write)              │
│               └─ send_notification (LINE/Telegram HTTPS)    │
│                                                              │
│  [manual] dashboard.py (Streamlit :8501)                     │
│     reads: all TiDB tables                                   │
│     writes: market_actuals, user_portfolio                   │
│                                                              │
│  [manual] backtest_agent.py                                  │
│     reads: TiDB daily_briefs                                 │
│     writes: TiDB market_actuals                              │
│                                                              │
│  [manual] agent_orchestrator.py                              │
│     └─► system_inspector.py (MCP stdio, env=None)           │
│                                                              │
│  [Docker] TiDB (:4000)                                       │
│     agent_memory: daily_briefs, market_actuals,              │
│                   cost_logs, user_portfolio                  │
└─────────────────────────────────────────────────────────────┘
          ↑ LAN 10.0.1.x — port 8501 OPEN, no auth
```

---

## 5. Infrastructure Gap Summary

| Gap | Severity | Area |
|-----|----------|------|
| No systemd service for workflow; cron cannot restart on failure | 🔴 HIGH | Process management |
| Streamlit binds to 0.0.0.0, no auth | 🔴 HIGH | Network / Security |
| `set -euo pipefail` stops workflow if data collection fails | 🔴 HIGH | Reliability |
| No LangGraph checkpointing — failures lose all computation | 🔴 HIGH | Reliability |
| TiDB container config not version-controlled | 🟠 MEDIUM | IaC / Reproducibility |
| No `uv.lock` — builds are non-reproducible | 🟠 MEDIUM | Reproducibility |
| Python processes not containerised — share `itadmin` OS user | 🟠 MEDIUM | Isolation / Security |
| `docker run` restart policy for TiDB unverified | 🟠 MEDIUM | Reliability |
| `collection_journal.jsonl` + brief txt files never rotated | 🟡 LOW | Disk management |
| `agent_sandbox` Docker network unused by app tier | 🟡 LOW | Dead configuration |
| No health check endpoint for any service | 🟡 LOW | Observability |
| Dashboard requires manual restart after server reboot | 🟡 LOW | Availability |
