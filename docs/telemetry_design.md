# Telemetry Design
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Design Principles

1. **Zero new infrastructure for P0**: All P0 telemetry writes to the existing TiDB `agent_memory` database. No Prometheus, no Grafana, no external service required.
2. **Additive, not disruptive**: Every change is an addition or a decorator. No existing node logic is rewritten.
3. **Structured at the source**: Log lines that need to be queryable use JSON. Human-readable lines remain plain text.
4. **Fail-open**: Telemetry failures never crash the workflow.
5. **run_id as the correlation primitive**: Every table that tracks a workflow event gains a `run_id` column.

---

## Layer 0 — Run Correlation (P0, ~30 min)

The single highest-value change in this document. A `run_id` (UUID) generated at `main()` entry and threaded through every node call ties every log row, cost row, and delivery record to one workflow execution.

### Schema Change

```sql
-- Add run_id to cost_logs (backfill NULL for historical rows)
ALTER TABLE cost_logs ADD COLUMN run_id VARCHAR(36) DEFAULT NULL;
ALTER TABLE cost_logs ADD INDEX idx_run_id (run_id);

-- New table: one row per workflow execution
CREATE TABLE IF NOT EXISTS workflow_runs (
    id           VARCHAR(36)   PRIMARY KEY,        -- UUID
    run_type     VARCHAR(20)   NOT NULL,           -- "investment" | "backtest" | "orchestrator"
    started_at   TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at     TIMESTAMP     NULL,
    status       VARCHAR(20)   NOT NULL DEFAULT 'running', -- running | success | failed
    snapshot_ts  VARCHAR(30)   NULL,               -- snapshot["timestamp"] value
    snapshot_age_seconds INT   NULL,               -- age at run start
    total_cost_usd DECIMAL(10,6) DEFAULT 0.000000,
    error_message TEXT          NULL
);
```

### Code Change

```python
# investment_workflow.py — main()
import uuid

def main():
    run_id = str(uuid.uuid4())
    ...
    from database_tools import create_workflow_run, finish_workflow_run
    create_workflow_run(run_id, "investment", snapshot["timestamp"])

    try:
        result = graph.invoke(initial_state, config={"run_id": run_id})
        finish_workflow_run(run_id, "success")
    except Exception as exc:
        finish_workflow_run(run_id, "failed", str(exc))
        raise
```

```python
# market_analyst_agents.py — _record_usage()
def _record_usage(agent_name: str, model: str, response, latency_ms: int,
                  run_id: str = None) -> None:
    try:
        from database_tools import log_cost
        usage = response.usage_metadata or {}
        in_tok       = usage.get("input_tokens", 0)
        out_tok      = usage.get("output_tokens", 0)
        thinking_tok = usage.get("thinking_tokens", 0)
        cost         = _calc_cost(model, in_tok, out_tok)
        log_cost(agent_name, model, in_tok, out_tok, thinking_tok, cost, latency_ms, run_id)
    except Exception as exc:
        logger.warning(f"[{agent_name}] cost logging failed: {exc}")
```

---

## Layer 1 — Token Telemetry (P0, ~20 min)

### Add `thinking_tokens` to `cost_logs`

```sql
ALTER TABLE cost_logs ADD COLUMN thinking_tokens INT DEFAULT 0;
```

```python
# database_tools.py — log_cost()
def log_cost(
    agent_name: str,
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    thinking_tokens: int = 0,          # NEW
    estimated_cost_usd: float = 0.0,
    latency_ms: Optional[int] = None,
    run_id: Optional[str] = None,      # NEW
) -> None:
    with _engine().begin() as conn:
        conn.execute(
            text("""
                INSERT INTO cost_logs
                    (agent_name, model_name, input_tokens, output_tokens, thinking_tokens,
                     estimated_cost_usd, latency_ms, run_id)
                VALUES (:agent, :model, :in_tok, :out_tok, :think_tok, :cost, :lat, :run_id)
            """),
            {"agent": agent_name, "model": model_name,
             "in_tok": input_tokens, "out_tok": output_tokens,
             "think_tok": thinking_tokens, "cost": estimated_cost_usd,
             "lat": latency_ms, "run_id": run_id},
        )
```

### Thinking token extraction

```python
# market_analyst_agents.py — _record_usage()
usage = response.usage_metadata or {}
in_tok       = usage.get("input_tokens", 0)
out_tok      = usage.get("output_tokens", 0)
thinking_tok = usage.get("thinking_tokens", 0)  # Anthropic API field for extended thinking
```

---

## Layer 2 — Workflow Event Log (P0, ~30 min)

Captures every event that currently has no persistent record.

### Schema

```sql
CREATE TABLE IF NOT EXISTS workflow_events (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_id       VARCHAR(36)   NOT NULL,
    event_type   VARCHAR(50)   NOT NULL,
    -- event_type values:
    --   node_start, node_success, node_failure
    --   fallback_activated, output_invalid
    --   delivery_success, delivery_failure
    --   cost_alert_triggered
    node_name    VARCHAR(50)   NULL,
    detail       JSON          NULL,       -- structured payload per event_type
    severity     VARCHAR(10)   NOT NULL DEFAULT 'info',  -- info | warn | error
    created_at   TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_run (run_id),
    INDEX idx_event_type (event_type),
    INDEX idx_created_at (created_at)
);
```

### Python helper

```python
# database_tools.py — new function
def log_event(
    run_id: str,
    event_type: str,
    node_name: Optional[str] = None,
    detail: Optional[dict] = None,
    severity: str = "info",
) -> None:
    import json as _json
    try:
        with _engine().begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO workflow_events
                        (run_id, event_type, node_name, detail, severity)
                    VALUES (:run_id, :etype, :node, :detail, :sev)
                """),
                {"run_id": run_id, "etype": event_type, "node": node_name,
                 "detail": _json.dumps(detail or {}, ensure_ascii=False),
                 "sev": severity},
            )
    except Exception as exc:
        logger.warning(f"[telemetry] log_event failed: {exc}")
```

### Usage examples

```python
# data_collector_node — fallback activation
except Exception:
    logger.warning("[DataCollector] JSON 解析失敗，使用空 dict")
    raw_market_data = {}
    log_event(run_id, "fallback_activated", "data_collector",
              {"reason": "json_parse_failed", "raw_text_length": len(raw_text)},
              severity="warn")

# send_notification_node — delivery result
log_event(run_id, "delivery_success" if status == "ok" else "delivery_failure",
          "send_notification",
          {"channel": channel, "http_status": res.get("http_status"), "error": res.get("error")},
          severity="info" if status == "ok" else "error")

# save_to_db_node — write failure
except Exception as exc:
    logger.error(f"[SaveToDB] 寫入失敗: {exc}")
    log_event(run_id, "node_failure", "save_to_db",
              {"exception": str(exc)}, severity="error")
    return {"db_row_id": None}
```

---

## Layer 3 — LLM Call Trace (P1, ~1 hr)

Captures the full request/response for every LLM invocation. Stored in a separate table to avoid polluting `cost_logs` with large text blobs.

### Schema

```sql
CREATE TABLE IF NOT EXISTS llm_traces (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_id          VARCHAR(36)   NOT NULL,
    agent_name      VARCHAR(50)   NOT NULL,
    model_name      VARCHAR(100)  NOT NULL,
    system_prompt   TEXT          NULL,
    user_content    TEXT          NULL,
    raw_response    TEXT          NULL,
    finish_reason   VARCHAR(30)   NULL,     -- "stop" | "max_tokens" | "error"
    input_tokens    INT           NULL,
    output_tokens   INT           NULL,
    thinking_tokens INT           NULL,
    latency_ms      INT           NULL,
    created_at      TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_run_agent (run_id, agent_name),
    INDEX idx_created_at (created_at)
) ROW_FORMAT=COMPRESSED;    -- text-heavy table; compression recommended
```

### Python helper

```python
# database_tools.py — new function
def log_llm_trace(
    run_id: str,
    agent_name: str,
    model_name: str,
    system_prompt: str,
    user_content: str,
    raw_response: str,
    finish_reason: Optional[str],
    input_tokens: int,
    output_tokens: int,
    thinking_tokens: int,
    latency_ms: int,
) -> None:
    try:
        with _engine().begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO llm_traces
                        (run_id, agent_name, model_name, system_prompt, user_content,
                         raw_response, finish_reason, input_tokens, output_tokens,
                         thinking_tokens, latency_ms)
                    VALUES (:run_id, :agent, :model, :sys, :usr, :resp,
                            :fin, :in_tok, :out_tok, :think_tok, :lat)
                """),
                {"run_id": run_id, "agent": agent_name, "model": model_name,
                 "sys": system_prompt[:4000],   # truncate very long prompts
                 "usr": user_content[:4000],
                 "resp": raw_response[:8000],
                 "fin": finish_reason,
                 "in_tok": input_tokens, "out_tok": output_tokens,
                 "think_tok": thinking_tokens, "lat": latency_ms},
            )
    except Exception as exc:
        logger.warning(f"[telemetry] log_llm_trace failed: {exc}")
```

### Integration into `_record_usage()`

```python
def _record_usage(agent_name, model, response, latency_ms, run_id=None,
                  system_prompt="", user_content="") -> None:
    try:
        from database_tools import log_cost, log_llm_trace
        usage        = response.usage_metadata or {}
        in_tok       = usage.get("input_tokens", 0)
        out_tok      = usage.get("output_tokens", 0)
        thinking_tok = usage.get("thinking_tokens", 0)
        cost         = _calc_cost(model, in_tok, out_tok)
        raw_text     = _extract_text(response)
        finish_reason = getattr(response, "stop_reason", None)

        log_cost(agent_name, model, in_tok, out_tok, thinking_tok, cost, latency_ms, run_id)
        log_llm_trace(run_id, agent_name, model, system_prompt, user_content,
                      raw_text, finish_reason, in_tok, out_tok, thinking_tok, latency_ms)
    except Exception as exc:
        logger.warning(f"[{agent_name}] telemetry failed: {exc}")
```

---

## Layer 4 — Output Validation Trace (P1, ~2 hrs)

Captures structured validation results for every LLM output.

### Validators per node

```python
# market_analyst_agents.py — new validation helpers

_CHIP_REQUIRED_FIELDS = {"sentiment", "foreign_net", "trust_net", "dealer_net",
                          "divergence_signal", "reasoning"}
_TECH_REQUIRED_FIELDS = {"gap_direction", "estimated_gap_pct", "key_driver",
                          "tsm_signal", "reasoning"}
_COLLECTOR_REQUIRED_FIELDS = {"foreign_oi_net", "trust_oi_net", "dealer_oi_net",
                               "djia_chg_pct", "ndx_chg_pct", "sox_chg_pct",
                               "tsm_adr_chg_pct", "data_ok"}

def _validate_json_output(raw: str, required_fields: set, agent_name: str,
                           run_id: str = None) -> tuple[dict, bool]:
    """Parse JSON output and validate required fields. Returns (parsed_dict, is_valid)."""
    try:
        parsed = json.loads(raw)
        missing = required_fields - set(parsed.keys())
        if missing:
            logger.warning(f"[{agent_name}] Missing fields: {missing}")
            if run_id:
                log_event(run_id, "output_invalid", agent_name,
                          {"missing_fields": list(missing)}, severity="warn")
            return parsed, False
        return parsed, True
    except json.JSONDecodeError as e:
        logger.warning(f"[{agent_name}] JSON decode failed: {e}")
        if run_id:
            log_event(run_id, "output_invalid", agent_name,
                      {"error": "json_decode_failed", "detail": str(e)}, severity="error")
        return {}, False
```

---

## Layer 5 — Audit Log (P1, ~1 hr)

Captures every mutation to persistent data.

### Schema

```sql
CREATE TABLE IF NOT EXISTS audit_log (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    table_name   VARCHAR(50)   NOT NULL,
    operation    VARCHAR(10)   NOT NULL,   -- INSERT | UPDATE | DELETE
    record_id    BIGINT        NULL,
    actor        VARCHAR(50)   NOT NULL DEFAULT 'system',  -- "cron" | "dashboard" | "api"
    before_json  JSON          NULL,
    after_json   JSON          NULL,
    created_at   TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_table_op (table_name, operation),
    INDEX idx_created_at (created_at)
);
```

### Portfolio mutation wrapper

```python
# database_tools.py — wrap existing mutation functions
def delete_portfolio_item(item_id: int) -> None:
    with _engine().begin() as conn:
        # Capture before state
        row = conn.execute(
            text("SELECT * FROM user_portfolio WHERE id = :id"), {"id": item_id}
        ).fetchone()
        before = dict(row._mapping) if row else {}
        conn.execute(text("DELETE FROM user_portfolio WHERE id = :id"), {"id": item_id})
        conn.execute(
            text("""
                INSERT INTO audit_log (table_name, operation, record_id, actor, before_json)
                VALUES ('user_portfolio', 'DELETE', :id, 'dashboard', :before)
            """),
            {"id": item_id, "before": json.dumps(before, default=str)},
        )
```

---

## Telemetry Table Summary

| Table | Purpose | Priority | New? |
|-------|---------|---------|------|
| `workflow_runs` | One row per workflow execution; status, cost, snapshot age | P0 | ✅ New |
| `cost_logs` | Per-node token/cost (add `run_id`, `thinking_tokens`) | P0 | Extend |
| `workflow_events` | Structured event log: fallbacks, failures, deliveries | P0 | ✅ New |
| `llm_traces` | Full prompt/response/finish_reason per LLM call | P1 | ✅ New |
| `audit_log` | Before/after for every portfolio and brief mutation | P1 | ✅ New |

**Total new schema additions**: 3 new tables + 2 column additions to `cost_logs`.

---

## Structured Logging Format (P2)

Replace plain-text loguru output with JSON-structured lines for machine parsing.

```python
# investment_workflow.py — replace logger.add()
logger.add(
    sys.stdout,
    format=lambda record: json.dumps({
        "ts":      record["time"].isoformat(),
        "level":   record["level"].name,
        "run_id":  record["extra"].get("run_id", ""),
        "node":    record["extra"].get("node", ""),
        "message": record["message"],
    }) + "\n",
    level="INFO",
)
```

With context binding:
```python
# In each node:
with logger.contextualize(run_id=run_id, node="data_collector"):
    logger.info("提取關鍵市場數值")
    ...
    logger.success(f"完成 data_ok={raw_market_data.get('data_ok', '?')}")
```

This enables `grep '{"level":"ERROR"'` or `jq 'select(.run_id=="abc")' daily.log` without a log aggregation system.
