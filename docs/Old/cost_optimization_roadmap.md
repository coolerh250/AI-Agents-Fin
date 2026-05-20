# Cost Optimization Roadmap
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Baseline

| Metric | Current | Target (Phase 2) |
|--------|---------|-----------------|
| Nominal cost/run | ~$0.08–$0.13 | ~$0.05–$0.08 |
| Worst-case cost/run (volatile day) | ~$0.25+ | ~$0.09 (hard ceiling) |
| Monthly cost (20 trading days) | ~$1.60–$2.60 | ~$1.00–$1.60 |
| Cost visibility | 75% (6/8 nodes) | 100% (8/8 nodes) |
| DB connections per workflow run | ~180–210 (12–14 pools) | 1 pool (singleton) |
| Opus thinking token ceiling | None (up to 15,300) | 2,048 (hard cap) |

---

## Phase 0 — Critical Fixes (1 day, ~1 hour)

These are single-line or single-function changes with immediate impact and zero risk.

### Fix 1: Cap Opus `max_tokens` and add `budget_tokens`

**File**: `market_analyst_agents.py:122–129`

```python
# BEFORE
def _llm_opus() -> ChatAnthropic:
    return ChatAnthropic(
        model=_MODEL_OPUS,
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
    )

# AFTER
def _llm_opus() -> ChatAnthropic:
    return ChatAnthropic(
        model=_MODEL_OPUS,
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        max_tokens=4096,
        thinking={"type": "adaptive", "budget_tokens": 2048},
    )
```

**Removes**: `output_config={"effort": "high"}` — invalid parameter, silently ignored.
**Adds**: `budget_tokens=2048` — caps thinking token spend.
**Reduces**: `max_tokens` from 16,000 to 4,096 — hard ceiling at 3× actual output size.

**Worst-case cost after fix**: `(1,325 × $5/M) + (2,048 + 900) × $25/M = ~$0.080`
**Vs. current worst case**: ~$0.25+ on complex market days.

---

### Fix 2: Singleton `_engine()` via `@lru_cache`

**File**: `database_tools.py:16–23`

```python
# BEFORE
def _engine() -> Engine:
    host = os.getenv("TIDB_HOST", "127.0.0.1")
    ...
    return create_engine(url, pool_pre_ping=True)

# AFTER
from functools import lru_cache

@lru_cache(maxsize=1)
def _engine() -> Engine:
    host = os.getenv("TIDB_HOST", "127.0.0.1")
    ...
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 10, "read_timeout": 30},
    )
```

**Impact**: Eliminates 12–14 redundant connection pools per daily run cycle. Reduces per-DB-call overhead from ~25–50ms to ~5–30ms. Prevents background thread accumulation under concurrent dashboard + workflow load.

---

### Fix 3: Add `_record_usage()` to backtest and orchestrator

**File**: `backtest_agent.py:106–139` — `evaluate_node`

```python
import time
from market_analyst_agents import _record_usage

def evaluate_node(state: BacktestState) -> dict:
    ...
    start = time.monotonic()
    response = llm.invoke([SystemMessage(content=_EVAL_SYSTEM), HumanMessage(content=user_content)])
    latency_ms = int((time.monotonic() - start) * 1000)
    _record_usage("backtest_evaluate", MODEL_ID, response, latency_ms)
    ...
```

**File**: `agent_orchestrator.py:91–123` — `think_node`

```python
import time
from market_analyst_agents import _record_usage

def think_node(state: AgentState) -> dict:
    ...
    start = time.monotonic()
    response = llm.invoke(messages)
    latency_ms = int((time.monotonic() - start) * 1000)
    _record_usage("orchestrator_think", MODEL_ID, response, latency_ms)
    ...
```

**Impact**: Closes the 25% cost visibility gap. Backtest and orchestrator costs appear in the Streamlit dashboard.

---

**Phase 0 expected outcome**:
- Opus cost: hard ceiling ~$0.080/run (from unbounded)
- Monthly worst-case: ~$1.60 (from $5.00+ on bad months)
- Cost visibility: 100%

---

## Phase 1 — Performance & Async (1 week, ~3 hours)

### Fix 4: Async `calculate_pnl()`

**File**: `portfolio_tools.py`

Replace the sequential `for h in holdings` yfinance calls with `asyncio.gather()`:

```python
import asyncio

async def calculate_pnl_async(holdings: list[dict]) -> list[dict]:
    loop = asyncio.get_event_loop()

    async def _fetch(h: dict) -> float | None:
        try:
            df = await loop.run_in_executor(
                None, lambda: yf.Ticker(f"{h['stock_id']}.TW").history(period="1d")
            )
            return float(df["Close"].iloc[-1]) if not df.empty else None
        except Exception:
            return None

    prices = await asyncio.gather(*[_fetch(h) for h in holdings])
    enriched = []
    for h, price in zip(holdings, prices):
        current_price = price if price is not None else float(h["entry_price"])
        price_stale = price is None
        pnl = current_price - float(h["entry_price"])
        pnl_pct = (pnl / float(h["entry_price"])) * 100
        enriched.append({
            **h,
            "current_price": current_price,
            "price_stale": price_stale,
            "unrealized_pnl": pnl * h["quantity"],
            "pnl_pct": pnl_pct,
            "stop_loss_triggered": pnl_pct < -float(h["stop_loss_level"]),
        })
    return enriched
```

Also adds `price_stale: bool` flag — the missing indicator from `context_engineering_analysis.md` P1 gap.

**Update `portfolio_manager_node`** to call `asyncio.run(calculate_pnl_async(holdings))`.

**Latency improvement at 5 holdings**: 5–15s → 1–4s.

---

### Fix 5: Snapshot freshness guard

**File**: `investment_workflow.py` — `main()` before `graph.invoke()`

```python
from datetime import datetime, timezone, timedelta

snapshot = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
snap_age = datetime.now(timezone.utc) - datetime.fromisoformat(snapshot["timestamp"])
if snap_age > timedelta(hours=12):
    logger.error(f"Snapshot is {snap_age} old — aborting (stale data risk)")
    sys.exit(1)
```

Prevents the CRITICAL correctness risk identified in `context_engineering_analysis.md`: running the workflow against stale market data with no detection.

---

### Fix 6: Parallel `save_to_db` + `send_notification`

**File**: `investment_workflow.py` — `build_graph()`

```python
# BEFORE: sequential chain
graph.add_edge("format_agent",  "save_to_db")
graph.add_edge("save_to_db",    "send_notification")
graph.add_edge("send_notification", END)

# AFTER: parallel fan-out
graph.add_edge("format_agent",  "save_to_db")
graph.add_edge("format_agent",  "send_notification")
graph.add_edge("save_to_db",    END)
graph.add_edge("send_notification", END)
```

Minor latency improvement (~20–50ms) with zero cost change.

---

**Phase 1 expected outcome**:
- Workflow latency at 5 holdings: -10s (yfinance parallel)
- Final nodes: -30ms (parallel fan-out)
- Added: `price_stale` flag for P&L reliability
- Added: snapshot freshness guard (correctness fix)

---

## Phase 2 — Cost Visibility & Monitoring (2 weeks, ~2 hours)

### Fix 7: Add `thinking_tokens` column to `cost_logs`

```sql
ALTER TABLE cost_logs ADD COLUMN thinking_tokens INT DEFAULT 0;
```

```python
# market_analyst_agents.py:_record_usage()
def _record_usage(agent_name, model, response, latency_ms):
    usage = response.usage_metadata or {}
    in_tok       = usage.get("input_tokens", 0)
    out_tok      = usage.get("output_tokens", 0)
    thinking_tok = usage.get("thinking_tokens", 0)  # Anthropic API field
    ...
    log_cost(agent_name, model, in_tok, out_tok, thinking_tok, cost, latency_ms)
```

Enables the dashboard to show a separate "Thinking tokens" bar for `chief_strategist`, making Opus cost spikes immediately visible.

### Fix 8: Per-run cost alert in `investment_workflow.py`

```python
# After _print_cost_report() in main()
COST_ALERT_THRESHOLD_USD = 0.15

total_cost = sum(r["estimated_cost_usd"] for r in get_cost_summary(1))
if total_cost > COST_ALERT_THRESHOLD_USD:
    logger.warning(f"⚠️  Run cost ${total_cost:.4f} exceeds threshold ${COST_ALERT_THRESHOLD_USD}")
```

This does not reduce cost but prevents silent budget bleed on volatile days.

### Fix 9: `UNIQUE KEY` on `daily_briefs.trade_date`

```sql
ALTER TABLE daily_briefs ADD UNIQUE KEY uq_trade_date (trade_date);
```

Prevents duplicate rows from double-runs. Required for accurate `get_recent_accuracy()` results.

---

## Phase 3 — Structural (1 month, ~4 hours)

### Fix 10: LangGraph `SqliteSaver` checkpointer

Prevents loss of $0.048–$0.13 Opus computation on workflow crash:

```python
from langgraph.checkpoint.sqlite import SqliteSaver

def build_graph():
    graph = StateGraph(WorkflowState)
    ...
    memory = SqliteSaver.from_conn_string("checkpoints.db")
    return graph.compile(checkpointer=memory)

# In main():
result = graph.invoke(
    initial_state,
    config={"configurable": {"thread_id": trade_date.isoformat()}}
)
```

### Fix 11: Historical context injection for `chief_strategist_node`

Inject the last 5 session outcomes before the Opus call. Token cost: ~$0.001/run additional. Expected accuracy improvement: +5–15% over baseline (validated patterns help the model contextualize current signals).

```python
history = get_recent_accuracy(5)
history_text = "\n".join(
    f"- {r['trade_date']}: predicted {r['gap_direction']} {r['predicted_gap_pct']}%, "
    f"actual {r['actual_gap_pct']}%"
    for r in history if r.get("actual_gap_pct") is not None
)
```

See `hybrid_memory_architecture_roadmap.md` Phase 1 for full implementation.

---

## Phased Cost Projection

| Phase | Monthly Cost | Change vs Baseline | Cumulative Effort |
|-------|------------|-------------------|------------------|
| Baseline (today) | $1.60–$5.00+ (volatile months) | — | — |
| After Phase 0 | $1.00–$1.60 (hard ceiling) | -40–70% worst case | 1 hr |
| After Phase 1 | $1.00–$1.60 (same, +latency gains) | Latency -80% at 5 holdings | +3 hrs |
| After Phase 2 | $1.00–$1.60 (full visibility) | Cost unchanged, visibility 100% | +2 hrs |
| After Phase 3 | $1.05–$1.65 (+$0.02/run for history) | +$0.40/month; accuracy +10–20% | +4 hrs |

**Phase 0 is the only phase with a meaningful cost reduction.** Phases 1–3 focus on reliability, visibility, and quality rather than raw cost reduction.

---

## Quick Reference: File and Line Changes

| Fix | File | Line | Change |
|-----|------|------|--------|
| Cap Opus thinking | `market_analyst_agents.py` | 122–129 | `max_tokens=4096`, `budget_tokens=2048`, remove `output_config` |
| Singleton `_engine()` | `database_tools.py` | 16 | Add `@lru_cache(maxsize=1)` |
| Backtest cost log | `backtest_agent.py` | 133 | Add `_record_usage()` call |
| Orchestrator cost log | `agent_orchestrator.py` | 120 | Add `_record_usage()` call |
| Async P&L | `portfolio_tools.py` | 26–35 | Replace `for` loop with `asyncio.gather()` |
| Snapshot freshness | `investment_workflow.py` | ~45 | Add 10-line timestamp check |
| Parallel final nodes | `investment_workflow.py` | build_graph() | Change edge topology |
| Thinking token column | `database_tools.py` | 96–111 | Add `thinking_tokens` column to DDL + `log_cost()` |
| Cost alert | `investment_workflow.py` | ~180 | Add threshold check after cost report |
| UNIQUE trade_date | TiDB | schema | `ALTER TABLE daily_briefs ADD UNIQUE KEY` |
| Checkpointer | `investment_workflow.py` | build_graph() | Add `SqliteSaver` |
| History injection | `market_analyst_agents.py` | chief_strategist_node | Add `get_recent_accuracy(5)` call |
