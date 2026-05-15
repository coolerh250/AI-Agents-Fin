# Production-Grade Observability Architecture
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Architecture Goal

Transform the current system from **"I can see it ran"** to **"I can see what it did, why, and whether it was correct"** — without adding any external infrastructure beyond what already exists (TiDB + LINE/Telegram).

The architecture is defined in four tiers. Each tier is independently deployable. Tiers 1–2 require no new infrastructure.

---

## Current Observability Stack (Baseline)

```
┌─────────────────────────────────────────────────────────────────┐
│  CURRENT STATE                                                   │
│                                                                  │
│  Logging:   loguru → stdout only (plain text, no file, no JSON) │
│  Tracing:   NONE                                                 │
│  Metrics:   NONE                                                 │
│  Alerting:  NONE                                                 │
│  Dashboards: Streamlit (cost + accuracy + portfolio, no health)  │
│                                                                  │
│  Storage:   cost_logs (6/8 nodes, no run_id, no thinking_tokens) │
│             daily_briefs (no duplicate guard)                    │
│             market_actuals (manual, no audit)                    │
│             user_portfolio (no mutation log)                     │
└─────────────────────────────────────────────────────────────────┘
```

**Coverage score: 2/10**

---

## Target Observability Stack

```
┌─────────────────────────────────────────────────────────────────────┐
│  TIER 4 (optional): External Trace Platform                         │
│  LangSmith / LangFuse — LLM span tree, prompt playground            │
├─────────────────────────────────────────────────────────────────────┤
│  TIER 3: Structured Logs + Metrics Queries                          │
│  JSON loguru → log file → grep/jq; SQL-based KPI queries            │
├─────────────────────────────────────────────────────────────────────┤
│  TIER 2: LLM Traces + Output Validation + Audit Log                 │
│  llm_traces, audit_log tables (TiDB)                                │
├─────────────────────────────────────────────────────────────────────┤
│  TIER 1: Run Correlation + Event Log + Token Telemetry (P0)         │
│  workflow_runs, workflow_events, cost_logs[+run_id, +thinking_tokens]│
└─────────────────────────────────────────────────────────────────────┘
         ↑ all stored in existing TiDB agent_memory database ↑
```

---

## Tier 1 — Run Correlation (P0, ~4 hours total)

**Goal**: Close the invisible failure gap. Know that every workflow run happened, succeeded, and delivered.

### New Data Model

```
workflow_runs
├── id (UUID)                 ← primary correlation key
├── run_type                  ← "investment" | "backtest" | "orchestrator"
├── status                    ← running | success | failed
├── started_at, ended_at
├── snapshot_age_seconds      ← freshness check result
├── total_cost_usd
└── error_message

cost_logs (extended)
├── run_id → workflow_runs.id  ← NEW correlation foreign key
└── thinking_tokens             ← NEW Opus thinking visibility

workflow_events
├── run_id → workflow_runs.id
├── event_type                  ← node_start | node_success | node_failure |
│                                  fallback_activated | output_invalid |
│                                  delivery_success | delivery_failure |
│                                  cost_alert_triggered
├── node_name
├── detail (JSON)               ← structured event payload
└── severity                    ← info | warn | error
```

### Signal Coverage After Tier 1

| Previously Invisible Failure | Now Visible As |
|------------------------------|---------------|
| Workflow crashed silently | `workflow_runs.status = 'failed'` + error_message |
| Brief not saved to DB | A-002 alert from post-run check |
| Notification not delivered | `workflow_events.delivery_failure` + A-003 alert |
| Cost tracking failure | `workflow_events.node_failure` for `_record_usage` |
| data_collector fallback | `workflow_events.fallback_activated` + detail.raw_text_length |
| Opus thinking spike | `cost_logs.thinking_tokens` > threshold |
| Stale snapshot used | `workflow_runs.snapshot_age_seconds` + A-005 alert |

### Implementation Sequence

```
Step 1 (20 min): Add workflow_runs table + create_workflow_run() / finish_workflow_run()
Step 2 (20 min): Add workflow_events table + log_event()
Step 3 (20 min): ALTER cost_logs ADD COLUMN run_id, thinking_tokens
Step 4 (30 min): Wire run_id through investment_workflow.main() + _record_usage()
Step 5 (30 min): Add thinking_tokens extraction to _record_usage()
Step 6 (30 min): Add log_event() calls to: data_collector fallback, save_to_db failure,
                  send_notification result, _record_usage failure
Step 7 (30 min): Add post-run alert runner (_run_post_alerts) for A-001 through A-006
Step 8 (20 min): Add snapshot freshness check (abort if > 12h, warn if > 6h)
Step 9 (30 min): Add _record_usage() to backtest_agent + agent_orchestrator
```

**Expected coverage after Tier 1: 6/10**

---

## Tier 2 — LLM Traces + Validation + Audit (P1, ~5 hours)

**Goal**: Enable post-hoc debugging of incorrect outputs. Know WHY the brief was wrong, not just THAT it was wrong.

### New Data Model

```
llm_traces
├── run_id → workflow_runs.id
├── agent_name
├── model_name
├── system_prompt (TEXT, truncated at 4000 chars)
├── user_content (TEXT, truncated at 4000 chars)
├── raw_response (TEXT, truncated at 8000 chars)
├── finish_reason                ← "stop" | "max_tokens" | "error"
├── input_tokens, output_tokens, thinking_tokens
└── latency_ms

audit_log
├── table_name                   ← "user_portfolio" | "daily_briefs" | "market_actuals"
├── operation                    ← INSERT | UPDATE | DELETE
├── record_id
├── actor                        ← "cron" | "dashboard" | "api"
├── before_json (JSON)           ← state before mutation
└── after_json (JSON)            ← state after mutation
```

### Output Validation Layer

Every JSON-producing node gets a validator:

```
data_collector → _validate_json_output(required=9 fields)
chip_analyst   → _validate_json_output(required=6 fields)
tech_analyst   → _validate_json_output(required=5 fields)

Validation failure → log_event(output_invalid) → downstream uses fallback
                   → alert A-006 fires
```

### Debugging Workflow (Post-Tier 2)

When a user reports "today's brief was wrong":
1. Query `workflow_runs` for today's `run_id`
2. Query `llm_traces WHERE run_id = ? AND agent_name = 'chief_strategist'` → see exact input and output
3. Query `workflow_events WHERE run_id = ? AND event_type = 'output_invalid'` → see which upstream node failed
4. Query `workflow_events WHERE event_type = 'fallback_activated'` → check if raw snapshot was injected
5. Check `cost_logs.thinking_tokens` → was Opus thinking budget exceeded?

**Expected coverage after Tier 2: 8/10**

---

## Tier 3 — Structured Logging + SQL Metrics (P2, ~3 hours)

**Goal**: Enable grep-based and SQL-based operational intelligence without a metrics platform.

### Structured Log Format

```json
{"ts": "2026-05-15T08:23:14.221+08:00", "level": "INFO",
 "run_id": "a1b2c3d4-...", "node": "data_collector",
 "message": "提取關鍵市場數值"}

{"ts": "2026-05-15T08:23:15.068+08:00", "level": "SUCCESS",
 "run_id": "a1b2c3d4-...", "node": "data_collector",
 "message": "完成 data_ok=True"}
```

**Log file destination**: `/home/itadmin/ai_agent_studio/logs/workflow_YYYYMMDD.log` (daily rotation)

```python
logger.add(
    "/home/itadmin/ai_agent_studio/logs/workflow_{time:YYYY-MM-DD}.log",
    format=lambda r: json.dumps({
        "ts": r["time"].isoformat(), "level": r["level"].name,
        "run_id": r["extra"].get("run_id", ""), "node": r["extra"].get("node", ""),
        "message": r["message"],
    }) + "\n",
    rotation="1 day",
    retention="30 days",
    level="DEBUG",
)
```

### SQL-Based KPI Queries (no Prometheus needed)

```sql
-- KPI 1: Daily run success rate (last 30 days)
SELECT DATE(started_at) AS day,
       SUM(status = 'success') AS success,
       SUM(status = 'failed') AS failed,
       SUM(status = 'success') * 100.0 / COUNT(*) AS success_rate
FROM workflow_runs
WHERE started_at >= NOW() - INTERVAL 30 DAY
GROUP BY day ORDER BY day;

-- KPI 2: Per-node error rate
SELECT node_name,
       COUNT(*) AS errors,
       COUNT(*) * 100.0 / (
           SELECT COUNT(*) FROM workflow_runs
           WHERE started_at >= NOW() - INTERVAL 30 DAY
       ) AS error_rate_pct
FROM workflow_events
WHERE event_type = 'node_failure'
  AND created_at >= NOW() - INTERVAL 30 DAY
GROUP BY node_name;

-- KPI 3: Cost efficiency trend
SELECT DATE(started_at) AS day,
       AVG(total_cost_usd) AS avg_cost,
       MAX(total_cost_usd) AS max_cost,
       SUM(CASE WHEN total_cost_usd > 0.15 THEN 1 ELSE 0 END) AS over_threshold_days
FROM workflow_runs
WHERE run_type = 'investment'
  AND started_at >= NOW() - INTERVAL 30 DAY
GROUP BY day ORDER BY day;

-- KPI 4: Opus thinking token trend
SELECT DATE(logged_at) AS day,
       AVG(thinking_tokens) AS avg_thinking,
       MAX(thinking_tokens) AS max_thinking
FROM cost_logs
WHERE agent_name = 'chief_strategist'
  AND logged_at >= NOW() - INTERVAL 30 DAY
GROUP BY day ORDER BY day;
```

**Expected coverage after Tier 3: 9/10**

---

## Tier 4 — External LLM Trace Platform (P3, optional)

**Goal**: Rich visual trace UI for LLM call debugging, prompt comparison, and evaluation.

### Option A: LangSmith (Anthropic-compatible)

```python
# market_analyst_agents.py — add to environment setup
import os
os.environ["LANGCHAIN_TRACING_V2"] = "true"
os.environ["LANGCHAIN_API_KEY"] = os.getenv("LANGSMITH_API_KEY", "")
os.environ["LANGCHAIN_PROJECT"] = "taiwan-futures-agent"
```

With LangChain's native tracing enabled, every `ChatAnthropic.invoke()` call is automatically traced with:
- Full prompt tree (system + human)
- Raw response with metadata
- Token counts including thinking tokens
- Latency breakdown (queue vs. compute)
- Parent-child trace tree (workflow run → each node → each LLM call)

**Limitation**: Requires LANGSMITH_API_KEY; data sent to Anthropic/LangSmith servers. Not suitable if brief content is confidential.

### Option B: LangFuse (self-hosted)

LangFuse can be deployed on the existing Ubuntu server (Docker):
```bash
docker run -d -p 3000:3000 langfuse/langfuse
```

```python
from langfuse.callback import CallbackHandler
langfuse_handler = CallbackHandler(
    public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
    secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
    host="http://localhost:3000",
)
# Pass as callback to graph.invoke()
result = graph.invoke(initial_state, config={"callbacks": [langfuse_handler]})
```

**Advantage**: Self-hosted, no data leaves the local network. Provides a visual trace UI at `http://10.0.1.20:3000`.

**Expected coverage after Tier 4: 10/10**

---

## Complete Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    PRODUCTION OBSERVABILITY ARCHITECTURE                     │
│                                                                             │
│  cron 08:20 CST                                                             │
│       │                                                                     │
│       ▼                                                                     │
│  investment_workflow.main()                                                 │
│  ├── generate run_id (UUID)                    ── T1 ──────────────────────│
│  ├── validate snapshot freshness               ── T1: A-005 alert if stale │
│  ├── create_workflow_run(run_id)               ── T1: workflow_runs INSERT  │
│  │                                                                          │
│  ├── graph.invoke()                                                         │
│  │   ├── data_collector_node                                                │
│  │   │   ├── log_event(node_start)             ── T1: workflow_events      │
│  │   │   ├── llm.invoke() + _record_usage()    ── T1: cost_logs+run_id    │
│  │   │   │                                     ── T2: llm_traces          │
│  │   │   ├── _validate_json_output()           ── T2: output_invalid event│
│  │   │   └── log_event(node_success | fallback_activated)                  │
│  │   │                                                                      │
│  │   ├── chip_analyst_node ──────────────────── (same pattern) ────────── │
│  │   ├── tech_analyst_node ──────────────────── (same pattern) ────────── │
│  │   ├── chief_strategist_node ─────────────── (+ thinking_tokens) ─────  │
│  │   ├── portfolio_manager_node ──────────────── (same pattern) ─────────  │
│  │   ├── format_agent_node ───────────────────── (same pattern) ─────────  │
│  │   ├── save_to_db_node                                                    │
│  │   │   ├── log_event(node_success | node_failure)  ── T1               │
│  │   │   └── audit_log INSERT                         ── T2               │
│  │   └── send_notification_node                                             │
│  │       └── log_event(delivery_success | delivery_failure) ── T1         │
│  │                                                                          │
│  ├── finish_workflow_run(run_id, status)       ── T1: workflow_runs UPDATE │
│  ├── _run_post_alerts(run_id)                  ── Alerts A-001 to A-006   │
│  └── _print_cost_report()                      ── stdout summary           │
│                                                                             │
│  ─────────────────────────────────────────────────────────────────────     │
│                                                                             │
│  TiDB agent_memory                                                          │
│  ├── workflow_runs        (T1) run-level health                             │
│  ├── workflow_events      (T1) structured event log                         │
│  ├── cost_logs            (T1) +run_id +thinking_tokens                    │
│  ├── llm_traces           (T2) full prompt/response per LLM call           │
│  ├── audit_log            (T2) before/after for every DB mutation          │
│  ├── daily_briefs         existing (+UNIQUE KEY on trade_date)              │
│  ├── market_actuals       existing                                          │
│  └── user_portfolio       existing (+mutation triggers to audit_log)        │
│                                                                             │
│  ─────────────────────────────────────────────────────────────────────     │
│                                                                             │
│  Streamlit Dashboard (extended)                                             │
│  ├── 📊 預測準確度          existing                                        │
│  ├── 💰 API 成本分析        existing (+thinking_tokens chart)               │
│  ├── 💼 個人持倉管理        existing                                        │
│  ├── 🟢 系統健康狀態        NEW (T1): workflow_runs table                  │
│  └── 📋 事件日誌            NEW (T1): workflow_events table                │
│                                                                             │
│  ─────────────────────────────────────────────────────────────────────     │
│                                                                             │
│  Alert Delivery (existing infrastructure)                                   │
│  ├── LINE Messaging API   → CRITICAL alerts (A-001, A-002, A-003)          │
│  └── Telegram Bot         → WARNING + INFO alerts (A-004 to A-009)         │
│                                                                             │
│  ─────────────────────────────────────────────────────────────────────     │
│                                                                             │
│  Optional: Tier 4                                                           │
│  └── LangFuse (self-hosted Docker on 10.0.1.20:3000)                       │
│      └── Visual LLM trace tree, prompt comparison, evaluation runs         │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Implementation Roadmap

| Phase | Tier | Changes | Effort | Outcome |
|-------|------|---------|--------|---------|
| **Phase 0** | T1 | `workflow_runs` + `workflow_events` tables, `run_id` + `thinking_tokens` to `cost_logs`, snapshot freshness check, `_record_usage()` in backtest + orchestrator | 4 hrs | Closes all invisible failure gaps; 100% cost visibility |
| **Phase 1** | T1 | Post-run alert runner (A-001 to A-006), `workflow_events` log_event() calls in each node | 2 hrs | 9 alerts active; delivery confirmation stored |
| **Phase 2** | T2 | `llm_traces` table, output validation per node, `audit_log` table with portfolio mutation wrappers | 5 hrs | Full LLM trace; hallucination debuggable; portfolio changes traceable |
| **Phase 3** | T3 | JSON loguru format, daily log rotation, Streamlit health + event tabs | 3 hrs | Operational intelligence via grep/SQL; no external tools |
| **Phase 4** | T4 | LangFuse Docker on server, LangChain callback integration | 4 hrs | Visual LLM trace tree; prompt comparison UI |

**Total to reach production-grade (Tiers 1–3): ~14 hours over 4 phases**

---

## Observability Coverage Before vs After

| Signal | Before | After Phase 0 | After Phase 2 | After Phase 4 |
|--------|--------|--------------|--------------|--------------|
| Workflow ran | stdout only | ✅ workflow_runs | ✅ | ✅ |
| Workflow succeeded | guessed from output files | ✅ status column | ✅ | ✅ |
| Brief saved to DB | check manually | ✅ alert A-002 | ✅ | ✅ |
| Notification delivered | stdout only | ✅ event log | ✅ | ✅ |
| Cost per run | none | ✅ run_id join | ✅ | ✅ |
| Thinking tokens | none | ✅ thinking_tokens | ✅ | ✅ |
| Fallback activated | WARNING only | ✅ event log | ✅ | ✅ |
| LLM prompt/response | none | none | ✅ llm_traces | ✅ |
| Output schema valid | none | none | ✅ validators | ✅ |
| Portfolio mutations | none | none | ✅ audit_log | ✅ |
| Visual LLM trace | none | none | none | ✅ LangFuse |

**Overall score: 2/10 → 7/10 (Phase 0) → 9/10 (Phase 2) → 10/10 (Phase 4)**
