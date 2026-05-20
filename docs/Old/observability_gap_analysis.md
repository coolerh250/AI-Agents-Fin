# Observability Gap Analysis
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Executive Summary

The system has **partial observability** — enough to see that it ran, but not enough to know whether it ran correctly. Cost visibility is 75% complete; LLM call tracing is 0%; workflow-level correlation (tying all 8 nodes to a single run) is absent. The most dangerous gap is that four distinct failure modes produce no observable signal beyond a single `logger.warning()` line to stdout.

| Dimension | Current State | Score |
|-----------|--------------|-------|
| Log coverage | loguru to stdout, 8 nodes, plain text | 4/10 |
| Structured logging | None — all plain text | 0/10 |
| Distributed tracing | None | 0/10 |
| Metrics collection | None | 0/10 |
| Token visibility | 75% (6/8 nodes, no thinking breakdown) | 4/10 |
| Workflow correlation | None (no run_id) | 0/10 |
| Audit trail | Partial (cost_logs only, no run linkage) | 2/10 |
| Alerting | None | 0/10 |

---

## 1. Existing Visibility

### 1.1 Logs

**Library**: `loguru` — configured in `investment_workflow.py:32–38`, `backtest_agent.py:20–27`, `agent_orchestrator.py:22–28`

```python
logger.add(sys.stdout, format="{time:HH:mm:ss} | {level:<8} | {message}", level="DEBUG", colorize=False)
```

**Coverage per file**:

| File | Log Level Used | Structured? | To File? |
|------|---------------|-------------|---------|
| `investment_workflow.py` | INFO, SUCCESS, WARNING, ERROR | ❌ plain text | ❌ stdout only |
| `market_analyst_agents.py` | INFO, SUCCESS, WARNING, DEBUG | ❌ plain text | ❌ stdout only |
| `backtest_agent.py` | INFO, SUCCESS, WARNING, ERROR | ❌ plain text | ❌ stdout only |
| `agent_orchestrator.py` | INFO, SUCCESS, WARNING, ERROR | ❌ plain text | ❌ stdout only |
| `portfolio_tools.py` | WARNING only | ❌ plain text | ❌ stdout only |
| `finance_mcp_server.py` | INFO, WARNING | ❌ plain text | stderr only |

**What IS logged** (representative examples):
```
08:23:14 | INFO     | [DataCollector] 提取關鍵市場數值
08:23:15 | SUCCESS  | [DataCollector] 完成 data_ok=True
08:23:15 | DEBUG    | [data_collector] tokens=1260+148 cost=$0.001880 latency=847ms
08:23:17 | SUCCESS  | [ChipAnalyst] 完成：{"sentiment": "偏空"...
08:23:42 | SUCCESS  | [ChiefStrategist] 建議書撰寫完成
```

**What is NOT in logs**:
- No run ID or correlation ID
- No node input values (prompt content, token size before call)
- No raw LLM response text
- No thinking token count
- No fallback activation reason (structured)
- No workflow-level start/end with total duration
- No per-node state diff (what changed in WorkflowState)

### 1.2 Token Visibility

**Mechanism**: `_record_usage()` in `market_analyst_agents.py:148–158` → `log_cost()` → INSERT into `cost_logs` (TiDB)

```python
# cost_logs schema (database_tools.py:96–111)
CREATE TABLE cost_logs (
    id                 BIGINT AUTO_INCREMENT PRIMARY KEY,
    agent_name         VARCHAR(50)    NOT NULL,
    model_name         VARCHAR(100)   NOT NULL,
    input_tokens       INT            NOT NULL DEFAULT 0,
    output_tokens      INT            NOT NULL DEFAULT 0,
    estimated_cost_usd DECIMAL(10,6)  NOT NULL DEFAULT 0.000000,
    latency_ms         INT,
    logged_at          TIMESTAMP      DEFAULT CURRENT_TIMESTAMP
)
```

**What IS tracked**:
- input_tokens, output_tokens per node invocation
- estimated_cost_usd (computed locally via `_calc_cost()`)
- latency_ms (monotonic clock delta)
- agent_name, model_name, logged_at timestamp

**What is NOT tracked**:
- `thinking_tokens` — Opus extended thinking tokens are folded into `output_tokens`; no separate column exists
- `run_id` — no foreign key to a workflow execution; all 6 rows from one run are indistinguishable from 6 rows across 6 separate runs
- `finish_reason` — stop_sequence vs max_tokens vs error; not captured
- `prompt_hash` — no way to detect identical prompts being resent
- backtest `evaluate_node` — direct `ChatAnthropic`, no `_record_usage()` call
- orchestrator `think_node` — direct `ChatAnthropic`, no `_record_usage()` call

**Coverage**: 6 / 8 LLM invocations tracked = **75%**

### 1.3 Workflow Visibility

The Streamlit dashboard (`dashboard.py`) provides post-hoc visibility:

| Tab | What it shows | Gap |
|-----|--------------|-----|
| 預測準確度 | 30-day prediction accuracy, KPIs, direction distribution | No failure rate, no run-skipped indicator |
| API 成本分析 | 30-day per-node cost aggregates, daily cost trend line | No per-run cost, no thinking token breakdown, no backtest/orchestrator rows |
| 個人持倉管理 | Live P&L, stop-loss flags, historical charts | No staleness indicator for price data |

`_print_cost_report()` (`investment_workflow.py:68–113`) prints a formatted table to stdout after each run — this is the only real-time cost summary but it writes to **stdout only** and is not persisted.

---

## 2. Missing Visibility

### 2.1 Agent Trace

**Gap**: No mechanism exists to capture the full input→output record of each agent node.

| Missing Signal | Impact | Risk |
|---------------|--------|------|
| Node input content (prompt text) | Cannot replay or debug incorrect output | 🔴 HIGH |
| Node output content (raw LLM response) | Cannot detect hallucination post-hoc | 🔴 HIGH |
| Node fallback activation | `data_collector` failure silently injects 5× tokens with only a WARNING | 🟡 HIGH |
| Node state delta | Cannot see what each node added to WorkflowState | 🟡 MEDIUM |
| Node execution order (with timestamps) | Cannot reconstruct timeline for a given run | 🟡 MEDIUM |

**Code location of gap**:
```python
# market_analyst_agents.py:163–187 — data_collector_node
# Only log line: logger.warning("[DataCollector] JSON 解析失敗，使用空 dict")
# Missing: structured trace of {node, input_hash, output_hash, fallback_triggered, reason}
```

### 2.2 LLM Trace

**Gap**: No LLM call-level tracing exists beyond the `_record_usage()` cost row.

| Missing Signal | Impact | Location |
|---------------|--------|---------|
| Request prompt text (system + human) | Cannot audit for prompt injection | All nodes |
| Response raw text | Cannot detect malformed or unexpected outputs | All nodes |
| `finish_reason` | Cannot detect max_tokens truncation | All nodes |
| `thinking_tokens` | Cannot see Opus reasoning depth | `chief_strategist_node` |
| Model version confirmation | Cannot detect silent model routing changes | All nodes |
| API response time (server-side) | `latency_ms` includes Python overhead; cannot isolate network vs. compute | All nodes |
| LLM trace in backtest | Zero visibility into `evaluate_node` LLM call | `backtest_agent.py:106–139` |
| LLM trace in orchestrator | Zero visibility into `think_node` LLM call | `agent_orchestrator.py:91–123` |

### 2.3 Tool Trace

**Gap**: No MCP tool call logging, no external HTTP call logging, no DB operation logging.

| Tool / Function | Missing Signals |
|----------------|----------------|
| `get_tw_future_chips` (MCP) | Call duration, HTTP status, bytes received, parse success |
| `get_us_market_summary` (MCP) | yfinance request count, symbols fetched, stale data flag |
| `calculate_pnl()` (`portfolio_tools.py`) | Per-symbol fetch latency, yfinance success/fail rate, price age |
| `send_line()` / `send_telegram()` | HTTP status code, delivery confirmation — results NOT stored in DB |
| `save_brief()` (`database_tools.py`) | No write audit log — who triggered it, from which script |
| `save_actual()` (`dashboard.py`) | Manual entry not attributed to any user or session |
| DB queries (`get_cost_summary`, etc.) | No query duration, no row count returned |

**Critical gap** — notification delivery is silently discarded:
```python
# market_analyst_agents.py:328–336 — send_notification_node
results = send_brief(report)
for channel, res in results.items():
    if status == "ok":
        logger.success(...)  # logged to stdout, NEVER stored in DB
    else:
        logger.warning(...)  # logged to stdout, NEVER stored in DB
# Return value: {} — workflow state records nothing about delivery outcome
```

### 2.4 Memory Trace

**Gap**: All reads from TiDB are unobserved.

| Operation | Missing Trace |
|-----------|--------------|
| `get_brief()` in backtest | Was a row found? How old was it? Was gap_direction NULL? |
| `get_portfolio()` in portfolio_manager | How many holdings loaded? Any with missing fields? |
| `get_recent_accuracy()` in dashboard | How many rows had actual_gap_pct=NULL (incomplete backtest)? |
| Snapshot file load | Age of `market_snapshot.json` not validated or logged |
| data_collector fallback | Raw snapshot size (bytes) injected into Sonnet not measured |

**Snapshot staleness** — no freshness check exists:
```python
# investment_workflow.py:134
snapshot = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
logger.info(f"Snapshot loaded: {snapshot['timestamp']}")
# Missing: age check, staleness flag, abort if > 12 hours old
```

### 2.5 Cost Trace

**Gap**: No run-level cost aggregation and no cross-run comparison.

```
Current cost_logs schema:
  id | agent_name | model_name | input_tokens | output_tokens | estimated_cost_usd | latency_ms | logged_at

Missing:
  run_id          — correlate all nodes for one workflow execution
  thinking_tokens — separate from output_tokens for Opus
  run_type        — "investment" vs "backtest" vs "orchestrator"
  session_cost    — pre-computed total per run_id
  cost_delta_pct  — % change vs previous run (for anomaly detection)
```

---

## 3. Monitoring Risk

### 3.1 Invisible Failure

Four failure modes produce no external signal — they log to stdout but return a "success" state to LangGraph:

**Risk A: save_to_db silently fails**
```python
# market_analyst_agents.py:366–368
except Exception as exc:
    logger.error(f"[SaveToDB] 寫入失敗: {exc}")
    return {"db_row_id": None}  # ← LangGraph sees this as success
```
Impact: Daily brief is never persisted. Backtest has no data to evaluate. The workflow completes normally with `result["db_row_id"] == None` and no alert fires.

**Risk B: Cost tracking silently disabled**
```python
# market_analyst_agents.py:157–158
except Exception as exc:
    logger.warning(f"[{agent_name}] cost logging failed: {exc}")
    # No re-raise — workflow continues, cost_logs table is never written
```
Impact: If TiDB connection fails mid-run, all cost tracking for that run is silently dropped. The dashboard shows no data for that run with no indication of why.

**Risk C: send_notification delivers nothing**
```python
# market_analyst_agents.py:319–337
# If LINE and Telegram both return {"status": "error"}, the node returns {}
# LangGraph advances to END — workflow is marked complete
# The user never receives their daily brief
```
Impact: Silent daily delivery failure. User discovers missed notification by checking their phone, not from any system alert.

**Risk D: data_collector fallback is invisible in metrics**
```python
# market_analyst_agents.py:182–184
except Exception:
    logger.warning("[DataCollector] JSON 解析失敗，使用空 dict")
    raw_market_data = {}
# Then chip_analyst and tech_analyst silently inject 5× token load
```
Impact: The cost for that run spikes ~$0.004–$0.013 with no alert. The downstream analysis may be degraded. The only indicator is a single WARNING line in stdout.

### 3.2 Silent Hallucination

LLM output validation is absent at every node:

| Node | Output Format Expected | Validation Exists? | Hallucination Risk |
|------|----------------------|-------------------|-------------------|
| `data_collector` | JSON with 9 specific fields | ❌ try/except JSON parse only | Falls back to raw snapshot |
| `chip_analyst` | JSON `{"sentiment", "foreign_net", ...}` | ❌ None — raw string passed as `chip_report` | Opus receives malformed JSON as input |
| `tech_analyst` | JSON `{"gap_direction", "estimated_gap_pct", ...}` | ❌ None — raw string | gap_direction parsing in save_to_db can silently fail |
| `chief_strategist` | 4-section Chinese brief | ❌ None | Format violations not detected; LINE push sends corrupt message |
| `portfolio_manager` | Per-holding advice per format | ❌ None | Wrong advice format formatted by Haiku as-is |
| `format_agent` | LINE message ≤2000 chars | ❌ None — no length check | Messages > 2000 chars silently truncated by LINE API |

**Specific hallucination blind spot**:
```python
# market_analyst_agents.py:354–360
try:
    raw = state["tech_report"].strip()
    ...
    tech = json.loads(raw)
    gap_pct = float(tech.get("estimated_gap_pct", 0))
    gap_dir = tech.get("gap_direction")
except Exception:
    pass  # ← gap_pct and gap_dir silently remain None — stored as NULL in daily_briefs
```
If tech_analyst hallucinates a non-JSON response, `gap_direction` is silently stored as NULL. Backtest can never evaluate direction accuracy for that day.

### 3.3 Hidden Token Explosion

Three token explosion vectors exist with no detection:

**Vector 1: Opus thinking runaway**
```
_llm_opus() configuration: max_tokens=16000, thinking={"type": "adaptive"}
No budget_tokens cap. On highly volatile market days, thinking can reach 12,000+ tokens.
Worst-case run cost: ~$0.31 (vs. typical $0.08)
Current detection: NONE — only visible in cost_logs.output_tokens AFTER the fact
```

**Vector 2: data_collector fallback injection**
```
Normal path:    chip_analyst input = ~400 tokens (compact JSON)
Fallback path:  chip_analyst input = ~1,500–2,500 tokens (raw snapshot)
Token spike: +1,100–2,100 input tokens to Sonnet
Current detection: WARNING log only — cost_logs shows higher input_tokens but no alert fires
```

**Vector 3: format_agent output overflow**
```
format_agent max_tokens = 2048
Actual LINE message budget = 2000 chars ≠ 2000 tokens
If chief_strategist produces a longer-than-usual brief, format_agent can approach max_tokens
Current detection: NONE — no check for output_tokens approaching max_tokens
```

### 3.4 Missing Audit Trail

| Action | Who | When | Stored? | Reversible? |
|--------|-----|------|---------|-------------|
| Workflow run (investment) | cron / manual | logged_at in cost_logs | Partial (no run_id) | N/A |
| `save_brief()` write | investment_workflow | logged_at in daily_briefs | Yes | Via SQL DELETE |
| `save_actual()` manual entry | dashboard user | No audit trail | No | Silently overwrites |
| Portfolio `add_portfolio_item()` | dashboard user | created_at in user_portfolio | No actor, no IP | Via SQL DELETE |
| Portfolio `update_portfolio_item()` | dashboard user | NOT stored at all | ❌ No timestamp | Impossible |
| Portfolio `delete_portfolio_item()` | dashboard user | NOT stored at all | ❌ No record | Impossible |
| LINE/Telegram delivery | send_notification_node | NOT stored | ❌ No | N/A |
| cost_logging failure | _record_usage | WARNING to stdout only | ❌ No | N/A |

**Portfolio mutation blind spot** — no audit log exists for any of the three mutation operations. A deleted holding or incorrect stop-loss update leaves no trace:
```python
# dashboard.py:305–313
if st.button("💾 儲存變更"):
    for _, row in edited.iterrows():
        update_portfolio_item(int(row["id"]), ...)
    # No log, no audit row, no before/after snapshot
```

---

## 4. Gap Priority Matrix

| Gap | Severity | Effort to Fix | Priority |
|-----|---------|--------------|---------|
| No `run_id` in `cost_logs` | 🔴 CRITICAL | 30 min | **P0** |
| `save_to_db` failure is invisible | 🔴 CRITICAL | 15 min | **P0** |
| `send_notification` delivery not stored | 🔴 CRITICAL | 20 min | **P0** |
| `thinking_tokens` missing from `cost_logs` | 🔴 HIGH | 20 min | **P0** |
| No snapshot freshness validation | 🔴 HIGH | 20 min | **P0** |
| No per-run cost total alert | 🔴 HIGH | 30 min | **P0** |
| LLM trace (backtest + orchestrator) | 🟡 HIGH | 30 min | **P1** |
| Output schema validation (all nodes) | 🟡 HIGH | 2 hrs | **P1** |
| Notification delivery audit log | 🟡 HIGH | 30 min | **P1** |
| Portfolio mutation audit log | 🟡 MEDIUM | 1 hr | **P1** |
| Structured logging (JSON format) | 🟡 MEDIUM | 1 hr | **P2** |
| data_collector fallback token measurement | 🟡 MEDIUM | 30 min | **P2** |
| LangSmith / LangFuse LLM tracing | 🟢 LOW | 2 hrs | **P3** |
| OpenTelemetry span tracing | 🟢 LOW | 4 hrs | **P3** |
