# Memory Analysis
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## 1. Memory Architecture Overview

The system uses **three distinct memory layers**:

| Layer | Implementation | Scope | Persistence |
|-------|---------------|-------|-------------|
| **Workflow State** | `WorkflowState` TypedDict | Single workflow run | Transient (in-memory) |
| **Relational Storage** | TiDB `agent_memory` database | Cross-run, cross-agent | Permanent |
| **File Cache** | `market_snapshot.json` | Single day | Overwritten each run |

There is **no vector database**, **no embedding store**, and **no semantic memory** layer. All retrieval is exact-match SQL.

---

## 2. TiDB Schema

### Database: `agent_memory`

#### Table: `daily_briefs`

```sql
CREATE TABLE daily_briefs (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    trade_date       DATE           NOT NULL,
    brief_text       TEXT           NOT NULL,   -- full ChiefStrategist prose output
    predicted_gap_pct DECIMAL(5,2),             -- extracted from tech_analyst JSON
    gap_direction    VARCHAR(10),               -- 'up' | 'flat' | 'down'
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Written by:** `save_to_db_node` → `database_tools.save_brief()`
**Read by:** `backtest_agent.load_brief_node`, `dashboard.get_recent_accuracy()`, `finance_mcp_server.backtest_report_resource`
**Index:** None beyond PK — no index on `trade_date` (⚠ sequential scan on large tables)
**Issue:** No UNIQUE constraint on `trade_date` — re-running workflow on same day creates duplicate rows. `get_brief()` uses `ORDER BY id DESC LIMIT 1` as workaround.

---

#### Table: `market_actuals`

```sql
CREATE TABLE market_actuals (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    trade_date       DATE           NOT NULL UNIQUE,   -- enforced deduplication
    open_price       DECIMAL(10,2)  NOT NULL,
    close_price      DECIMAL(10,2)  NOT NULL,
    actual_gap_pct   DECIMAL(5,2)   NOT NULL,
    notes            TEXT,                             -- e.g. "source=TWSE", "manual"
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Written by:** `backtest_agent.fetch_actual_node` (TWSE), `dashboard` manual form
**Read by:** `dashboard.get_recent_accuracy()` (LEFT JOIN with `daily_briefs`)
**Upsert:** `ON DUPLICATE KEY UPDATE` — safe for re-runs
**Issue:** `open_price` is always set equal to `prev_close` because TWSE API does not expose intraday open. The gap proxy (`actual_gap_pct`) is close-to-close change, not true open gap.

---

#### Table: `cost_logs`

```sql
CREATE TABLE cost_logs (
    id                 BIGINT AUTO_INCREMENT PRIMARY KEY,
    agent_name         VARCHAR(50)    NOT NULL,
    model_name         VARCHAR(100)   NOT NULL,
    input_tokens       INT            NOT NULL DEFAULT 0,
    output_tokens      INT            NOT NULL DEFAULT 0,
    estimated_cost_usd DECIMAL(10,6)  NOT NULL DEFAULT 0.000000,
    latency_ms         INT,
    logged_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_agent (agent_name),
    INDEX idx_logged_at (logged_at)
);
```

**Written by:** `_record_usage()` in `market_analyst_agents.py` — called after every LLM invocation
**Read by:** `dashboard.get_cost_summary()`, `dashboard.get_cost_trend()`, `investment_workflow._print_cost_report()`
**Missing agents:** `maintenance_agent` (agent_orchestrator.py) does **not** call `_record_usage`; `backtest_evaluator` (backtest_agent.py) does **not** call `_record_usage`
**Growth:** Unbounded — no TTL, no archival policy. Grows by 6 rows per workflow run.

---

#### Table: `user_portfolio`

```sql
CREATE TABLE user_portfolio (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    stock_id         VARCHAR(20)   NOT NULL,
    entry_price      DECIMAL(10,2) NOT NULL,
    quantity         INT           NOT NULL,           -- in shares (股), not lots (張)
    stop_loss_level  DECIMAL(5,2)  NOT NULL DEFAULT 5.00,
    strategy_type    VARCHAR(20)   NOT NULL DEFAULT '波段',
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_stock_entry (stock_id, entry_price)  -- prevents duplicate seeding
);
```

**Written by:** `database_tools.add_portfolio_item()`, `seed_test_portfolio()`, `update_portfolio_item()`
**Read by:** `portfolio_manager_node` (via `portfolio_tools.get_user_portfolio()`), `dashboard`
**Issue:** UNIQUE KEY is on `(stock_id, entry_price)` — the same stock can appear multiple times with different entry prices. This is intentional for DCA (dollar-cost averaging) but makes the portfolio_manager prompt complex for batched recommendations.

---

## 3. Context Passing Strategy

### Within a Single Run

```
market_snapshot.json
    ↓ (file read in main())
WorkflowState.snapshot  (dict, full raw data)
    ↓ data_collector_node (LLM compression)
WorkflowState.raw_market_data  (compact dict, ~200 bytes)
    ↓ fed separately to:
WorkflowState.chip_report  (str, ~500 tokens JSON)
WorkflowState.tech_report  (str, ~500 tokens JSON)
    ↓ both fed together to chief_strategist
WorkflowState.final_brief  (str, ~600 tokens prose)
    ↓ fed to portfolio_manager along with live DB data
WorkflowState.portfolio_advice  (str, ~200 tokens per holding)
    ↓ both fed to format_agent
WorkflowState.final_report  (str, LINE-formatted ≤2000 chars)
    ↓ persisted + sent
```

### Across Runs (Cross-Day Memory)

Only `final_brief` and gap direction/magnitude survive in TiDB. No prior briefs, no accumulated market context, and no learning from past predictions feed into new runs. Each day starts from zero context.

---

## 4. Embedding / Semantic Memory

**Status: NOT IMPLEMENTED**

No embedding model is called anywhere. All memory retrieval is:
- Exact date lookup (SQL `WHERE trade_date = :d`)
- Recency retrieval (`ORDER BY id DESC LIMIT :n`)

There is no capability to answer "what happened last time TSMC ADR rose >4%?" or "what is our average accuracy in down markets?"

---

## 5. Summarization Strategy

**Status: NOT IMPLEMENTED**

The `chief_strategist` receives raw chip + tech reports without any accumulated historical context. There is no summarization of past briefs passed into the prompt. The agent has no access to its own prior reasoning.

The `backtest_evaluator` explicitly receives the full prior brief text in its evaluation prompt but does not summarize or distill learnings back into any persistent store.

---

## 6. Memory Gaps and Risks

| Gap | Impact | Severity |
|-----|--------|---------|
| No UNIQUE on `daily_briefs.trade_date` | Duplicate rows on workflow re-run; `get_brief()` workaround masks duplicates | Medium |
| `chip_report`, `tech_report`, `portfolio_advice`, `final_report` not persisted | Cannot audit intermediate reasoning; no replay | Medium |
| `accuracy_report` not persisted | Cannot compute accuracy KPIs from DB; dashboard KPI requires re-running backtest | Medium |
| No cross-day context in LLM prompts | Chief strategist has no market trend awareness | High |
| `cost_logs` unbounded growth | Performance degradation on `SUM/AVG` queries after ~1 year | Low |
| `market_snapshot.json` single-file | Concurrent or re-run clobbers in-flight data | Medium |
| No connection pool sharing | Each `_engine()` call creates a new Engine object — potential for connection exhaustion | Medium |
