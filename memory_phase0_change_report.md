# Memory Phase 0 — Change Report
**AI Agent Studio | 2026-05-15**

---

## Summary

Phase 0 completes all 5 foundation fixes from `hybrid_memory_architecture_roadmap.md`. Items 0-A, 0-B, and 0-D were already shipped in prior phases. Items 0-C and 0-E are new in this delivery, plus the `session_episodes` table that seeds Phase 1 episodic memory.

---

## Phase 0 Checklist

| ID | Fix | Status | Delivered In |
|----|-----|--------|-------------|
| 0-A | Singleton DB engine (`@lru_cache`) | ✅ Done | P0 security fixes |
| 0-B | Snapshot freshness check (abort >12h, warn >6h) | ✅ Done | Observability Phase 6 |
| 0-C | `price_stale` flag on yfinance failure | ✅ Done | **This delivery** |
| 0-D | `UNIQUE KEY uq_trade_date` on daily_briefs | ✅ Done | Observability Phase 6 |
| 0-E | LangGraph checkpointer (resume on failure) | ✅ Done | **This delivery** |

---

## 0-C: Price Stale Flag

### Location
`market_analyst_agents.py` — `portfolio_manager_node`

### Problem
From `tool_risk_matrix.md`:
> "Silent price fallback in portfolio — stale prices lead to wrong hold/sell decisions"

`calculate_pnl()` calls yfinance per holding. When yfinance fails for a ticker, it silently returns `current_price = entry_price` (cost basis as current price). The LLM advisor then calculates 0% P&L and may recommend "hold" when the actual position is down 20%.

### Implementation
```python
stale = [h["stock_id"] for h in enriched
         if h.get("current_price") is None
         or h.get("current_price") == h.get("entry_price")]
if stale:
    emit_event(run_id, "fallback_activated", "portfolio_manager",
               {"reason": "price_stale", "stocks": stale}, severity="warn")
```

Adds `fallback_activated` event to `workflow_events` — visible in dashboard Events tab, queryable in A-008 weekly report.

---

## 0-E: LangGraph Checkpointer

### Location
`investment_workflow.py` — `build_graph()`

### Problem
From `state_management.md`:
> "If workflow crashes after `chief_strategist_node` completes, entire 40-second Opus computation is lost and must be re-run."

At observed cost of ~$0.022/Opus call, a crash-and-rerun wastes ~$0.022 plus 20s. More importantly, it prevents the daily brief from being delivered when a downstream failure (DB connection, LINE API) would otherwise be recoverable.

### Implementation
```python
try:
    from langgraph.checkpoint.sqlite import SqliteSaver
    checkpointer = SqliteSaver.from_conn_string("./checkpoints.db")
    return graph.compile(checkpointer=checkpointer)
except Exception as exc:
    logger.warning(f"[build_graph] Checkpointer unavailable — running without checkpointing")
    return graph.compile()
```

Invocation uses `run_id` as `thread_id`:
```python
_graph_config = {"configurable": {"thread_id": run_id}}
result = graph.invoke(initial_state, config=_graph_config)
```

### How to resume after failure
If the workflow crashes mid-run (e.g., after chief_strategist but before save_to_db):
```bash
# Re-run with the SAME run_id (read from workflow_runs table or logs)
# LangGraph will resume from the last saved checkpoint
uv run python investment_workflow.py --resume <run_id>
```

Note: `--resume` CLI flag is not yet implemented. For now, manual resume requires calling `graph.invoke()` with the same `run_id` as `thread_id`. The checkpoint data in `checkpoints.db` handles the rest.

### Storage
`checkpoints.db` (SQLite) in the project directory. One checkpoint file per project. Grows ~1–5 KB per completed run. No cleanup needed at current scale.

---

## session_episodes Table

### Purpose
Structured episodic memory for chief_strategist. Enables future vector embedding and semantic retrieval (Phase 5).

### Schema
```sql
CREATE TABLE IF NOT EXISTS session_episodes (
    id                  BIGINT        AUTO_INCREMENT PRIMARY KEY,
    run_id              VARCHAR(36)   NOT NULL,
    trade_date          DATE          NOT NULL,
    brief_id            BIGINT        NULL,
    predicted_direction VARCHAR(10)   NULL,   -- up/flat/down
    predicted_gap_pct   DECIMAL(6,3)  NULL,
    actual_direction    VARCHAR(10)   NULL,   -- backfilled by backtest_agent
    actual_gap_pct      DECIMAL(6,3)  NULL,   -- backfilled
    direction_correct   TINYINT       NULL,   -- backfilled
    foreign_oi_net      INT           NULL,
    trust_oi_net        INT           NULL,
    dealer_oi_net       INT           NULL,
    djia_chg_pct        DECIMAL(6,3)  NULL,
    ndx_chg_pct         DECIMAL(6,3)  NULL,
    sox_chg_pct         DECIMAL(6,3)  NULL,
    tsm_adr_chg_pct     DECIMAL(6,3)  NULL,
    divergence_signal   TINYINT       NULL,   -- from chip_analyst JSON
    regime_sox          VARCHAR(10)   NULL,   -- strong/neutral/weak
    regime_foreign_oi   VARCHAR(10)   NULL,   -- bearish/neutral/bullish
    workflow_cost_usd   DECIMAL(10,6) NULL,
    created_at          TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_se_trade_date (trade_date),
    INDEX idx_se_run (run_id)
)
```

### Data flow
1. `save_to_db_node` calls `log_session_episode()` after `save_brief()` succeeds
2. Numeric inputs extracted from `state["raw_market_data"]` (populated by data_collector)
3. `divergence_signal` parsed from `state["chip_report"]` JSON
4. Regime tags derived at insert time from sox_chg_pct and foreign_oi_net thresholds
5. `actual_*` columns remain NULL until backtest_agent backfills them (Phase 5 item)

### Idempotency
`ON DUPLICATE KEY UPDATE` on `trade_date` — safe to re-run on the same day (updates prediction fields, preserves actual_* backfill).

---

## Phase 1 Episodic Memory (Next — Not Implemented)

Phase 0 lays the foundation. Phase 1 adds:
1. Backtest agent backfills `actual_direction`, `actual_gap_pct`, `direction_correct` into `session_episodes`
2. `get_recent_sessions_context(days=10)` reads regime-tagged rows for injection
3. Chief strategist receives structured past-session context: "Last 3 times SOX was strong + foreign_oi bearish → 2/3 times gap was flat despite tech signal"

**Expected accuracy improvement: +5–8%** (from `hybrid_memory_architecture_roadmap.md`)

---

## Phase 2 Vector Memory (Future)

Embed each `session_episodes` row → store in TiDB Vector extension or local Chroma → cosine similarity retrieval of 5 most similar past sessions → inject as "historical precedents" block in chief_strategist prompt.

**Expected accuracy improvement: +10–15%**

---

## Memory Architecture — Current State

| Layer | Before | After Phase 0 |
|-------|--------|--------------|
| 0 — Prompt | 8 static constants | + SQL history in chief prompt |
| 1 — In-process | 8-field TypedDict | + SqliteSaver checkpoints |
| 2 — File system | market_snapshot.json | + checkpoints.db |
| 3 — TiDB relational | 4 tables | + session_episodes, tool_audit_log |
| 4 — Streamlit cache | TTL-keyed | unchanged |
| 5 — Vector | absent | roadmap defined |

---

## Files Changed

| File | Lines Added | Description |
|------|-------------|-------------|
| `database_tools.py` | +140 | `ensure_tool_audit_log_table`, `log_tool_call`, `validate_tool_permission`, `get_recent_accuracy_context`, `ensure_session_episodes_table`, `log_session_episode` |
| `market_analyst_agents.py` | +38 | Price stale flag + event, session episode logging in save_to_db_node |
| `investment_workflow.py` | +12 | SqliteSaver checkpointer, thread_id in graph config |
