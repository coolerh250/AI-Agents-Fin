# Monitoring Strategy
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Strategy Overview

The monitoring strategy is organized around three questions:

1. **Did the workflow run?** — Run-level health: did it start, complete, and deliver?
2. **Did it run correctly?** — Quality signals: was the output well-formed, was the analysis coherent?
3. **Did it run efficiently?** — Cost and performance: tokens used, latency, model behavior

Each question maps to a specific monitoring layer with a defined data source, check frequency, and escalation path.

---

## 1. Run-Level Health Monitoring

### 1.1 Workflow Completion Check

**Data source**: `workflow_runs` table (new — see `telemetry_design.md` Layer 0)

**Check**: After each cron-triggered workflow, verify a `success` row exists in `workflow_runs` for today's date.

```python
# New function: database_tools.py
def get_run_status(run_date: date) -> Optional[dict]:
    with _engine().connect() as conn:
        row = conn.execute(
            text("""
                SELECT id, run_type, status, started_at, ended_at,
                       snapshot_age_seconds, total_cost_usd, error_message
                FROM workflow_runs
                WHERE DATE(started_at) = :d AND run_type = 'investment'
                ORDER BY started_at DESC LIMIT 1
            """),
            {"d": run_date},
        ).fetchone()
    return dict(row._mapping) if row else None
```

**Failure condition**: `get_run_status(date.today())` returns `None` or `status != "success"` at 08:45 CST.

### 1.2 Daily Brief Existence Check

**Data source**: `daily_briefs` table

```python
def check_daily_brief_exists(trade_date: date) -> bool:
    brief = get_brief(trade_date)
    return brief is not None
```

**Failure condition**: No `daily_briefs` row for today at 08:45 CST.
This check is independent of `workflow_runs` — it catches the case where the DB write appeared to succeed (`db_row_id != None`) but was actually silently rolled back.

### 1.3 Notification Delivery Check

**Data source**: `workflow_events` table with `event_type IN ('delivery_success', 'delivery_failure')`

```sql
SELECT event_type, detail->>'$.channel' AS channel, detail->>'$.http_status' AS http_status
FROM workflow_events
WHERE run_id = :run_id AND event_type IN ('delivery_success', 'delivery_failure')
```

**Failure condition**: No `delivery_success` row for LINE or Telegram after today's run.

---

## 2. Data Quality Monitoring

### 2.1 Snapshot Freshness

**Current state**: No check exists. The workflow runs against stale data silently.

**Fix** (to add in `investment_workflow.py:main()` before `graph.invoke()`):

```python
from datetime import datetime, timezone, timedelta

snap_ts = datetime.fromisoformat(snapshot["timestamp"].replace("Z", "+00:00"))
snap_age = datetime.now(timezone.utc) - snap_ts
snapshot_age_seconds = int(snap_age.total_seconds())

if snap_age > timedelta(hours=12):
    logger.error(f"Snapshot is {snap_age} old — aborting (stale data risk)")
    log_event(run_id, "node_failure", "main",
              {"reason": "stale_snapshot", "age_seconds": snapshot_age_seconds},
              severity="error")
    sys.exit(1)
elif snap_age > timedelta(hours=6):
    logger.warning(f"Snapshot is {snap_age} old — proceeding with caution")
    log_event(run_id, "fallback_activated", "main",
              {"reason": "snapshot_aging", "age_seconds": snapshot_age_seconds},
              severity="warn")
```

**Monitoring query**:
```sql
SELECT snapshot_age_seconds, started_at
FROM workflow_runs
WHERE run_type = 'investment'
ORDER BY started_at DESC
LIMIT 10;
-- Alert if snapshot_age_seconds > 43200 (12 hours)
```

### 2.2 LLM Output Validity Rate

**Data source**: `workflow_events` with `event_type = 'output_invalid'`

```sql
-- Invalid output rate per node (last 30 days)
SELECT node_name,
       COUNT(*) AS total_events,
       COUNT(*) * 100.0 / (
           SELECT COUNT(DISTINCT run_id) FROM workflow_runs
           WHERE DATE(started_at) >= CURDATE() - INTERVAL 30 DAY
       ) AS invalid_pct
FROM workflow_events
WHERE event_type = 'output_invalid'
  AND created_at >= NOW() - INTERVAL 30 DAY
GROUP BY node_name
ORDER BY invalid_pct DESC;
```

**Alert threshold**: `output_invalid` rate > 10% for any node → investigate prompt stability.

### 2.3 data_collector Fallback Rate

**Data source**: `workflow_events` with `event_type = 'fallback_activated'` and `node_name = 'data_collector'`

```sql
SELECT COUNT(*) AS fallback_count,
       COUNT(*) * 100.0 / (
           SELECT COUNT(*) FROM workflow_runs WHERE run_type = 'investment'
           AND DATE(started_at) >= CURDATE() - INTERVAL 30 DAY
       ) AS fallback_rate_pct
FROM workflow_events
WHERE event_type = 'fallback_activated'
  AND node_name = 'data_collector'
  AND created_at >= NOW() - INTERVAL 30 DAY;
```

**Alert threshold**: fallback_rate > 20% over 5 trading days → Haiku JSON parsing is unstable.

### 2.4 gap_direction NULL Rate

**Data source**: `daily_briefs` table

```sql
SELECT
    COUNT(*) AS total_briefs,
    SUM(CASE WHEN gap_direction IS NULL THEN 1 ELSE 0 END) AS null_direction_count,
    SUM(CASE WHEN gap_direction IS NULL THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS null_pct
FROM daily_briefs
WHERE trade_date >= CURDATE() - INTERVAL 30 DAY;
```

**Meaning**: `gap_direction = NULL` means `tech_analyst` produced non-parseable JSON. This silently breaks backtest accuracy tracking.

---

## 3. Cost and Performance Monitoring

### 3.1 Per-Run Cost Monitoring

**Current state**: Cost is aggregated by `agent_name` across all runs. Per-run cost requires summing rows with the same `run_id` (which doesn't exist yet).

**After `run_id` addition**:

```sql
-- Per-run cost for the last 30 days
SELECT run_id,
       DATE(wr.started_at) AS trade_date,
       SUM(cl.estimated_cost_usd) AS total_run_cost,
       SUM(cl.thinking_tokens) AS total_thinking_tokens,
       MAX(CASE WHEN cl.agent_name = 'chief_strategist'
               THEN cl.thinking_tokens END) AS opus_thinking_tokens
FROM workflow_runs wr
JOIN cost_logs cl ON wr.id = cl.run_id
WHERE wr.run_type = 'investment'
  AND wr.started_at >= NOW() - INTERVAL 30 DAY
GROUP BY wr.id, trade_date
ORDER BY trade_date DESC;
```

**Alert threshold**: `total_run_cost > 0.15` → trigger cost alert (see `alerting_strategy.md`).

### 3.2 Thinking Token Monitoring

**After `thinking_tokens` column addition**:

```sql
-- Opus thinking token trend (last 20 trading days)
SELECT DATE(logged_at) AS day,
       thinking_tokens,
       output_tokens,
       thinking_tokens * 100.0 / NULLIF(output_tokens, 0) AS thinking_pct
FROM cost_logs
WHERE agent_name = 'chief_strategist'
  AND logged_at >= NOW() - INTERVAL 30 DAY
ORDER BY logged_at DESC;
```

**Alert threshold**: `thinking_tokens > 2048` after `budget_tokens` fix is applied → investigate model behavior.

### 3.3 Latency Monitoring

```sql
-- Node latency trend
SELECT agent_name, model_name,
       AVG(latency_ms) AS avg_ms,
       MAX(latency_ms) AS max_ms,
       PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms
FROM cost_logs
WHERE logged_at >= NOW() - INTERVAL 7 DAY
GROUP BY agent_name, model_name
ORDER BY avg_ms DESC;
```

**P95 baselines** (from `workflow_performance.md`):

| Node | Expected P95 | Alert if > |
|------|-------------|------------|
| `data_collector` | 1,500ms | 3,000ms |
| `chip_analyst` | 2,500ms | 5,000ms |
| `tech_analyst` | 2,500ms | 5,000ms |
| `chief_strategist` | 45,000ms | 90,000ms |
| `portfolio_manager` | 3,000ms | 6,000ms |
| `format_agent` | 1,500ms | 3,000ms |

### 3.4 Error Rate Monitoring

```sql
-- Failed nodes (last 30 days)
SELECT node_name,
       COUNT(*) AS failure_count,
       MAX(created_at) AS last_failure,
       COUNT(*) * 100.0 / (
           SELECT COUNT(*) FROM workflow_runs
           WHERE run_type = 'investment'
           AND DATE(started_at) >= CURDATE() - INTERVAL 30 DAY
       ) AS failure_rate_pct
FROM workflow_events
WHERE event_type = 'node_failure'
  AND created_at >= NOW() - INTERVAL 30 DAY
GROUP BY node_name
ORDER BY failure_count DESC;
```

---

## 4. Dashboard Monitoring Extensions

The existing Streamlit dashboard should be extended with two new monitoring tabs.

### 4.1 Run Health Tab (new)

```python
# dashboard.py — new tab
with tab_health:
    st.subheader("工作流執行狀態（最近 30 天）")

    from database_tools import get_workflow_runs
    runs = get_workflow_runs(30)
    if not runs:
        st.warning("尚無 workflow_runs 資料 — 請升級 telemetry schema")
    else:
        df = pd.DataFrame(runs)
        # Color status: success=green, failed=red, running=yellow
        status_color = {"success": "🟢", "failed": "🔴", "running": "🟡"}
        df["狀態"] = df["status"].map(status_color)

        col1, col2, col3 = st.columns(3)
        col1.metric("執行次數（30 天）", len(df))
        col2.metric("成功率", f"{df['status'].eq('success').mean()*100:.1f}%")
        col3.metric("平均成本/run", f"${df['total_cost_usd'].mean():.4f}")

        st.dataframe(df[["狀態", "trade_date", "total_cost_usd",
                          "snapshot_age_seconds", "error_message"]])
```

### 4.2 Event Log Tab (new)

```python
with tab_events:
    st.subheader("最近系統事件")
    from database_tools import get_recent_events
    events = get_recent_events(days=7, severity_filter=["warn", "error"])

    if events:
        event_df = pd.DataFrame(events)
        # Highlight errors in red
        st.dataframe(
            event_df[["created_at", "run_id", "event_type", "node_name",
                       "severity", "detail"]],
            use_container_width=True,
        )
    else:
        st.success("最近 7 天無警告或錯誤事件")
```

---

## 5. Monitoring Implementation Phases

| Phase | Changes | Effort | Value |
|-------|---------|--------|-------|
| **P0 — Run Correlation** | `workflow_runs` table, `run_id` in `cost_logs`, `workflow_events` table | 3 hrs | Closes invisible failure gaps |
| **P0 — Token Tracking** | `thinking_tokens` column, backtest/orchestrator `_record_usage()` | 1 hr | Closes 25% cost gap, Opus visibility |
| **P0 — Snapshot Guard** | Freshness check in `main()` | 20 min | Prevents stale-data silent runs |
| **P1 — LLM Traces** | `llm_traces` table, prompt/response storage | 2 hrs | Post-hoc hallucination debugging |
| **P1 — Output Validation** | JSON schema validation per node | 2 hrs | NULL gap_direction prevention |
| **P1 — Audit Log** | `audit_log` table, portfolio mutation wrappers | 1 hr | Write accountability |
| **P2 — Dashboard Extensions** | Run health tab, event log tab | 2 hrs | Operational visibility |
| **P2 — Structured Logging** | JSON loguru format | 1 hr | Log queryability |
| **P3 — External Tracing** | LangSmith or LangFuse integration | 4 hrs | Deep LLM trace with UI |
