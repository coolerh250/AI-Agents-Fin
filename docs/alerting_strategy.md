# Alerting Strategy
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Alert Philosophy

Three design rules for a single-user system:

1. **Alerts must be actionable**: every alert must have a defined next action. If the only response is "I'll check later," it is a log entry, not an alert.
2. **Zero-infrastructure delivery**: alerts use the existing LINE/Telegram push infrastructure — no PagerDuty, no email setup required.
3. **Alert on absence, not just failure**: the most dangerous failure mode in this system is **silent success** — the workflow completes with `status="success"` but produces a missing brief, an undelivered notification, or a stale-data analysis.

---

## Alert Severity Levels

| Level | Symbol | Meaning | Delivery | Response SLA |
|-------|--------|---------|----------|-------------|
| CRITICAL | 🚨 | Workflow produced no output; financial decision-making is impaired | LINE + Telegram immediately | Fix before next trading day |
| WARNING | ⚠️ | Workflow completed but a signal may be degraded or cost ceiling was breached | LINE within 1 hr | Investigate within 1 day |
| INFO | 📊 | Operational anomaly, no immediate impact | Telegram only | Review within 1 week |

---

## Alert Catalog

### A-001: Workflow Run Not Completed by 08:45 CST

**Type**: CRITICAL  
**Trigger**: `workflow_runs` has no `status='success'` row for `DATE(started_at) = CURDATE()` at check time  
**Root cause**: cron failed to fire, investment_workflow.py crashed before graph.invoke(), or graph raised an unhandled exception  
**Check mechanism**: Post-run health check script (see below) or daily cron at 08:45 CST  

```python
# new file: check_daily_health.py
from datetime import date
from database_tools import get_run_status
from messenger_tools import send_line

def check_daily_health():
    today = date.today()
    run = get_run_status(today)
    if run is None:
        send_line("🚨 [A-001] CRITICAL: 今日工作流未執行\n排程可能失敗，請手動執行 investment_workflow.py")
    elif run["status"] == "failed":
        send_line(f"🚨 [A-001] CRITICAL: 今日工作流失敗\n錯誤：{run.get('error_message', 'unknown')}")
```

**Next action**: SSH to server, run `uv run python investment_workflow.py`, check cron with `crontab -l`

---

### A-002: Daily Brief Not Saved to DB

**Type**: CRITICAL  
**Trigger**: `daily_briefs` has no row for today's `trade_date` after workflow completion  
**Root cause**: `save_to_db_node` failure (DB connection reset, duplicate key, schema mismatch)  
**Check mechanism**: Post-run check or dashboard manual observation

```python
from database_tools import get_brief
from datetime import date

if get_brief(date.today()) is None:
    send_line("🚨 [A-002] CRITICAL: 今日建議書未寫入 TiDB\nBacktest 無法評估，請手動檢查 save_to_db_node")
```

**Next action**: Check `workflow_events` for `node_failure` + `save_to_db`; re-run or manually insert the brief text from `investment_brief_*.txt`

---

### A-003: Notification Delivery Failure

**Type**: CRITICAL  
**Trigger**: `workflow_events` has `delivery_failure` for both LINE and Telegram in today's run  
**Root cause**: Channel token expired, network issue, LINE API rate limit

```sql
-- Check query
SELECT COUNT(*) AS success_count
FROM workflow_events
WHERE run_id = (SELECT id FROM workflow_runs WHERE DATE(started_at) = CURDATE()
                AND run_type = 'investment' ORDER BY started_at DESC LIMIT 1)
  AND event_type = 'delivery_success';
-- Alert if success_count = 0
```

**Next action**: Check LINE/Telegram token validity in `.env`; manually forward `investment_brief_*.txt`

---

### A-004: Run Cost Exceeds $0.15

**Type**: WARNING  
**Trigger**: Per-run `SUM(estimated_cost_usd)` in `cost_logs` (grouped by `run_id`) > $0.15  
**Root cause**: Opus thinking token spike (complex market signals, budget_tokens not yet capped)

```python
# investment_workflow.py — add after _print_cost_report()
COST_ALERT_THRESHOLD_USD = 0.15

def _check_cost_alert(run_id: str) -> None:
    try:
        from database_tools import get_run_cost
        total = get_run_cost(run_id)
        if total > COST_ALERT_THRESHOLD_USD:
            from messenger_tools import send_telegram
            send_telegram(
                f"⚠️ [A-004] 今日執行成本 ${total:.4f} 超過閾值 ${COST_ALERT_THRESHOLD_USD}\n"
                f"Run ID: {run_id[:8]}...\n"
                f"請檢查 cost_logs 的 thinking_tokens 欄位"
            )
    except Exception as exc:
        logger.warning(f"Cost alert check failed: {exc}")
```

**Next action**: Query `thinking_tokens` for today's `chief_strategist` row; apply `budget_tokens=2048` fix if not yet done

---

### A-005: Stale Snapshot Detected

**Type**: WARNING  
**Trigger**: `snapshot_age_seconds > 21600` (6 hours) in `workflow_runs`  
**Root cause**: `test_collection.py` did not run, MCP data collection failed, snapshot file was stale from previous day

```python
# investment_workflow.py — main()
if snapshot_age_seconds > 21600:  # 6 hours
    logger.warning(f"Snapshot is {snapshot_age_seconds//3600:.1f}h old")
    from messenger_tools import send_telegram
    send_telegram(
        f"⚠️ [A-005] 市場快照已 {snapshot_age_seconds//3600:.1f} 小時未更新\n"
        f"分析可能基於昨日數據，請確認 test_collection.py 是否正常執行"
    )
```

**Abort condition**: If `snapshot_age_seconds > 43200` (12 hours), abort with `sys.exit(1)` and fire A-001.

---

### A-006: LLM Output Invalid (JSON Parse Failure)

**Type**: WARNING  
**Trigger**: `workflow_events` has `output_invalid` for `data_collector`, `chip_analyst`, or `tech_analyst` in today's run  
**Root cause**: Model produced non-JSON response (markdown fencing, explanation text, truncation)

```sql
-- Check query
SELECT node_name, detail->>'$.missing_fields' AS missing_fields,
       detail->>'$.error' AS error_type
FROM workflow_events
WHERE run_id = :today_run_id
  AND event_type = 'output_invalid';
```

**Alert message**:
```python
send_telegram(
    f"⚠️ [A-006] {node_name} 輸出格式異常\n"
    f"遺漏欄位：{missing_fields}\n"
    f"已使用 Fallback 路徑 — 今日分析可能不準確"
)
```

**Next action**: Check `llm_traces` for that `run_id` + `agent_name`; inspect `raw_response` field for model behavior

---

### A-007: Opus Thinking Token Spike

**Type**: WARNING  
**Trigger**: `thinking_tokens > 2048` in `cost_logs` for `chief_strategist` (after budget_tokens fix is applied)  
**Root cause**: If `budget_tokens=2048` is set and Anthropic still reports > 2048, the API cap behavior changed or the fix was not deployed  

```sql
-- Check query
SELECT thinking_tokens, estimated_cost_usd
FROM cost_logs
WHERE agent_name = 'chief_strategist'
  AND logged_at >= CURDATE()
  AND thinking_tokens > 2048;
```

**Alert message**:
```python
send_telegram(
    f"⚠️ [A-007] Opus 思考 Token 超過預算上限\n"
    f"實際 thinking_tokens: {thinking_tokens}\n"
    f"預期上限：2048（budget_tokens 設定可能未生效）"
)
```

---

### A-008: data_collector Fallback Rate > 20%

**Type**: INFO  
**Trigger**: More than 20% of runs in the last 5 trading days triggered `fallback_activated` for `data_collector`  
**Root cause**: Haiku JSON formatting instability; TAIFEX HTML structure changed; MCP server error

```sql
SELECT COUNT(*) * 100.0 / 5 AS fallback_pct
FROM workflow_events we
JOIN workflow_runs wr ON we.run_id = wr.id
WHERE we.event_type = 'fallback_activated'
  AND we.node_name = 'data_collector'
  AND wr.started_at >= NOW() - INTERVAL 7 DAY;
-- Alert if result > 20
```

**Next action**: Check `llm_traces` for data_collector; update `_COLLECTOR_SYSTEM` prompt to be more explicit about JSON formatting; check TAIFEX scraper output

---

### A-009: Backtest Accuracy Below 40%

**Type**: INFO  
**Trigger**: Rolling 10-day direction prediction accuracy drops below 40% (below random chance)  
**Root cause**: Systematic prediction error; model drift; market regime change that the system isn't adapting to

```sql
SELECT COUNT(*) AS correct_count,
       10 AS total,
       COUNT(*) * 100.0 / 10 AS accuracy_pct
FROM (
    SELECT b.gap_direction, a.actual_gap_pct,
           CASE
               WHEN b.gap_direction = 'up'   AND a.actual_gap_pct > 0.3  THEN 1
               WHEN b.gap_direction = 'down' AND a.actual_gap_pct < -0.3 THEN 1
               WHEN b.gap_direction = 'flat' AND ABS(a.actual_gap_pct) <= 0.3 THEN 1
               ELSE 0
           END AS correct
    FROM daily_briefs b
    JOIN market_actuals a ON b.trade_date = a.trade_date
    WHERE b.trade_date >= CURDATE() - INTERVAL 14 DAY
    ORDER BY b.trade_date DESC LIMIT 10
) t
WHERE correct = 1;
```

---

## Alert Delivery Implementation

### Post-Run Alert Runner

```python
# investment_workflow.py — _run_post_alerts() called from main()
def _run_post_alerts(run_id: str, result: dict, run_cost: float,
                     snapshot_age_seconds: int) -> None:
    alerts = []

    # A-002: brief not saved
    if not result.get("db_row_id"):
        alerts.append("🚨 [A-002] CRITICAL: 建議書未寫入 TiDB")

    # A-003: notification not delivered
    from database_tools import get_run_events
    events = get_run_events(run_id)
    delivery_ok = any(e["event_type"] == "delivery_success" for e in events)
    if not delivery_ok:
        alerts.append("🚨 [A-003] CRITICAL: LINE/Telegram 推播失敗")

    # A-004: cost threshold
    if run_cost > 0.15:
        alerts.append(f"⚠️ [A-004] 今日成本 ${run_cost:.4f} 超出閾值")

    # A-005: stale snapshot (already handled in main(), but surface in summary)
    if snapshot_age_seconds > 21600:
        alerts.append(f"⚠️ [A-005] 快照已 {snapshot_age_seconds//3600:.1f}h 未更新")

    # A-006: output validation failures
    invalid_events = [e for e in events if e["event_type"] == "output_invalid"]
    if invalid_events:
        nodes = ", ".join(set(e["node_name"] for e in invalid_events))
        alerts.append(f"⚠️ [A-006] 節點輸出格式異常：{nodes}")

    if not alerts:
        return

    from messenger_tools import send_telegram
    message = "📋 AI Agent Studio 執行警告\n\n" + "\n".join(alerts)
    send_telegram(message)
```

### Weekly Digest (P2)

A weekly digest summarizing all INFO-level events, sent every Sunday via Telegram:

```python
# new file: weekly_digest.py
def send_weekly_digest():
    from database_tools import get_recent_events, get_cost_summary
    events = get_recent_events(days=7)
    cost_data = get_cost_summary(7)

    total_cost = sum(float(r["total_cost_usd"]) for r in cost_data)
    fallback_count = sum(1 for e in events if e["event_type"] == "fallback_activated")
    error_count = sum(1 for e in events if e["severity"] == "error")

    message = (
        f"📊 [Weekly Digest] 過去 7 天摘要\n"
        f"總 API 成本：${total_cost:.4f}\n"
        f"Fallback 觸發：{fallback_count} 次\n"
        f"錯誤事件：{error_count} 次\n"
        f"詳細請查看 Streamlit 儀表板"
    )
    send_telegram(message)
```

---

## Alert Summary Table

| ID | Alert Name | Level | Trigger | Delivery |
|----|-----------|-------|---------|---------|
| A-001 | Workflow not completed | 🚨 CRITICAL | No success run by 08:45 CST | LINE + Telegram |
| A-002 | Brief not saved to DB | 🚨 CRITICAL | No daily_briefs row today | LINE + Telegram |
| A-003 | Notification delivery failed | 🚨 CRITICAL | No delivery_success event | LINE + Telegram |
| A-004 | Run cost > $0.15 | ⚠️ WARNING | post-run cost sum | Telegram |
| A-005 | Stale snapshot | ⚠️ WARNING | snapshot_age > 6 hrs | Telegram |
| A-006 | LLM output invalid | ⚠️ WARNING | output_invalid event | Telegram |
| A-007 | Opus thinking spike | ⚠️ WARNING | thinking_tokens > 2048 | Telegram |
| A-008 | Fallback rate > 20% | 📊 INFO | weekly aggregation | Telegram |
| A-009 | Accuracy < 40% | 📊 INFO | weekly aggregation | Telegram |

**Implementation effort**: A-001 through A-006 can be added in ~2 hours. A-007 through A-009 require the `thinking_tokens` column (P0 fix) and `workflow_events` table first.
