# Observability & Cost Governance — Implementation Plan
**AI Agent Studio | 2026-05-15**

---

## Scope

Implement Tier 1 (P0) and Tier 2 (P1) observability as defined in `production_observability_architecture.md`. No external infrastructure added — all storage in existing TiDB `agent_memory` database.

---

## What Was Built

### 1. New DB Tables (migration.sql)

| Table | Purpose | Priority |
|-------|---------|---------|
| `workflow_runs` | One row per workflow execution; status, cost, snapshot age | P0 |
| `workflow_events` | Structured event log: fallbacks, failures, deliveries | P0 |
| `llm_traces` | Full prompt/response/finish_reason per LLM call | P1 |
| `audit_log` | Before/after for every portfolio and brief mutation | P1 |

### 2. cost_logs Extensions

```sql
ALTER TABLE cost_logs ADD COLUMN thinking_tokens INT NOT NULL DEFAULT 0;
ALTER TABLE cost_logs ADD COLUMN run_id VARCHAR(36) DEFAULT NULL;
ALTER TABLE daily_briefs ADD UNIQUE KEY uq_trade_date (trade_date);
```

### 3. Files Created / Modified

| File | Change | Lines Δ |
|------|--------|--------|
| `migration.sql` | New — idempotent DDL for all 4 new tables + 2 column additions | +65 |
| `telemetry.py` | New — `record_usage()`, `emit_event()`, `timed_invoke()` | +88 |
| `alert_runner.py` | New — standalone cron script for A-001 through A-009 | +155 |
| `database_tools.py` | Added 15 new functions; extended `log_cost`, `get_cost_summary` | +230 |
| `market_analyst_agents.py` | Added `run_id` to `WorkflowState`; rewired all 6 nodes | +45 |
| `investment_workflow.py` | Added UUID, snapshot freshness, run lifecycle, post-alerts | +65 |
| `dashboard.py` | Added 2 new tabs (health, events); extended cost tab | +80 |

---

## Alert Catalog

| ID | Trigger | Level | Delivery | Where Checked |
|----|---------|-------|----------|--------------|
| A-001 | Workflow not completed | 🚨 CRITICAL | LINE + Telegram | `alert_runner.py` |
| A-002 | Daily brief missing from DB | 🚨 CRITICAL | LINE + Telegram | `alert_runner.py` |
| A-003 | No delivery_success event | 🚨 CRITICAL | LINE + Telegram | `alert_runner.py` |
| A-004 | Run cost > $0.15 | ⚠️ WARNING | Telegram | `investment_workflow.py` |
| A-005 | Snapshot age > 6h | ⚠️ WARNING | Telegram | `investment_workflow.py` |
| A-006 | LLM output invalid | ⚠️ WARNING | Telegram | `investment_workflow.py` |
| A-007 | Opus thinking > 2048 tok | ⚠️ WARNING | Telegram | `alert_runner.py` |
| A-008 | Fallback rate > 20% (7d) | 📊 INFO | Telegram | `alert_runner.py --weekly` |
| A-009 | Accuracy < 40% (10d) | 📊 INFO | Telegram | `alert_runner.py --weekly` |

---

## Deployment Steps

### Server-side schema migration

Schema migration runs **automatically** on the next `investment_workflow.py` invocation via `ensure_observability_tables()`. No manual SQL required.

### Cron additions (add to server crontab)

```bash
# Daily health check — 08:45 CST (00:45 UTC)
45 0 * * 1-5  cd /home/itadmin/ai_agent_studio && ~/.local/bin/uv run python alert_runner.py >> /home/itadmin/logs/alerts.log 2>&1

# Weekly digest — Sunday 09:00 CST (01:00 UTC)
0 1 * * 0  cd /home/itadmin/ai_agent_studio && ~/.local/bin/uv run python alert_runner.py --weekly >> /home/itadmin/logs/alerts.log 2>&1
```

### Dashboard

No restart needed — Streamlit hot-reloads. Two new tabs appear automatically after schema migration.

---

## Observable Signals After This Release

| Signal | Before | After |
|--------|--------|-------|
| Workflow ran | stdout only | ✅ `workflow_runs` row |
| Workflow succeeded | guessed from output | ✅ `status = 'success'` |
| Brief saved | check manually | ✅ A-002 alert |
| Notification delivered | stdout only | ✅ `delivery_success` event |
| Cost per run | no run_id → impossible | ✅ `workflow_runs.total_cost_usd` |
| Thinking tokens | folded into output_tokens | ✅ `cost_logs.thinking_tokens` |
| Fallback activated | WARNING to stdout | ✅ `workflow_events.fallback_activated` |
| LLM prompt/response | none | ✅ `llm_traces` table |
| Portfolio mutations | no record | ✅ `audit_log` before/after |
| Snapshot staleness | none | ✅ abort if >12h, warn if >6h |

**Coverage score: 2/10 → 8/10**

---

## Phase 2 (Not Implemented — Future Work)

- JSON-structured loguru output + daily log rotation (`production_observability_architecture.md` Tier 3)
- LangFuse self-hosted Docker for visual LLM trace tree (Tier 4)
- `_record_usage()` in `backtest_agent.py` and `agent_orchestrator.py` (completes 100% cost coverage)
- Output JSON schema validation with `output_invalid` events per node
