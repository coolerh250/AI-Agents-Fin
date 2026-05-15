# Memory Inventory
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Memory Taxonomy Overview

The system uses **five distinct memory layers** across three persistence boundaries.

```
┌─────────────────────────────────────────────────────────────────┐
│  LAYER 0 — Prompt Memory (source code, static)                  │
│  System prompts embedded as Python string constants             │
├─────────────────────────────────────────────────────────────────┤
│  LAYER 1 — In-Process State (LangGraph TypedDict, ephemeral)    │
│  WorkflowState · BacktestState · AgentState                     │
├─────────────────────────────────────────────────────────────────┤
│  LAYER 2 — File System (local disk, semi-persistent)            │
│  market_snapshot.json · collection_journal.jsonl · brief files  │
├─────────────────────────────────────────────────────────────────┤
│  LAYER 3 — Relational DB (TiDB, persistent)                     │
│  daily_briefs · market_actuals · cost_logs · user_portfolio     │
├─────────────────────────────────────────────────────────────────┤
│  LAYER 4 — Application Cache (Streamlit, process-local TTL)     │
│  @st.cache_data(ttl=300) · @st.cache_data(ttl=3600)             │
└─────────────────────────────────────────────────────────────────┘

ABSENT:
  ✗ Vector database (no Chroma, Pinecone, Weaviate, Qdrant)
  ✗ Embeddings (no sentence-transformers, no OpenAI embeddings)
  ✗ Semantic / similarity search
  ✗ LangChain ConversationBufferMemory / VectorStoreRetrieverMemory
  ✗ LangGraph Checkpointer (no workflow state resumption)
```

---

## Layer 0: Prompt Memory (Semantic Knowledge)

**Location**: `market_analyst_agents.py:43–109`, `backtest_agent.py:31–49`, `agent_orchestrator.py:30–37`
**Lifetime**: Permanent (hard-coded in source)
**Mutability**: Read-only at runtime

These string constants encode all domain knowledge the agents possess. They are the closest analog to "semantic memory" in the system — but unlike a true semantic store, they cannot be retrieved selectively, updated at runtime, or queried by relevance.

| Constant | Agent | Purpose | Encoded Knowledge |
|----------|-------|---------|-------------------|
| `_COLLECTOR_SYSTEM` | data_collector | Schema extraction | Field names and data types to extract from raw snapshot |
| `_CHIP_SYSTEM` | chip_analyst | Sentiment rules | OI thresholds (< -30K → 極度偏空); divergence signal logic |
| `_TECH_SYSTEM` | tech_analyst | Gap prediction | Weighted average formula (DJIA 20%, NDX 25%, SOX 30%, TSM 25%); gap thresholds |
| `_CHIEF_SYSTEM` | chief_strategist | Output format | Four-section brief structure (盤勢/策略/防守/風險) |
| `_PORTFOLIO_SYSTEM` | portfolio_manager | Decision rules | Stop-loss trigger logic; hold/sell/buy conditions |
| `_FORMAT_SYSTEM` | format_agent | Message format | LINE message constraints (2000 chars max; emoji conventions) |
| `_EVAL_SYSTEM` | evaluate (backtest) | Scoring rubric | Accuracy report format; 0–100 scoring scale |
| `SYSTEM_PROMPT` | think (orchestrator) | Maintenance rules | Disk/CPU thresholds; STATUS label conventions |

**Key gap**: These rules are static. When the market regime changes (e.g., post-FOMC SOX weighting becomes less predictive), there is no mechanism to update them except manual code edits and redeployment.

---

## Layer 1: In-Process State (Short-Term Memory)

### 1.1 WorkflowState — Investment Workflow

**Location**: `market_analyst_agents.py:30–38`
**Scope**: Single `graph.invoke()` call in `investment_workflow.py`
**Lifetime**: Seconds to minutes (one workflow run, ~40–90 seconds)
**Persistence**: None — discarded when Python process exits

```python
class WorkflowState(TypedDict):
    snapshot:         dict        # Raw MCP output (all tool responses)
    raw_market_data:  dict        # Compressed by data_collector (8 fields)
    chip_report:      str         # JSON string from chip_analyst (~300 chars)
    tech_report:      str         # JSON string from tech_analyst (~300 chars)
    final_brief:      str         # Free-text from chief_strategist (~2000 chars)
    final_report:     str         # LINE-formatted from format_agent (~2000 chars)
    db_row_id:        Optional[int]  # Set after save_to_db_node
    portfolio_advice: str         # Sonnet output from portfolio_manager (~500 chars)
```

**State size estimate per run**:
| Field | Typical Size | Notes |
|-------|-------------|-------|
| `snapshot` | 3–8 KB | 3 MCP tool outputs as nested JSON |
| `raw_market_data` | ~200 bytes | 8 numeric fields only |
| `chip_report` | ~300 bytes | JSON string: 6 fields |
| `tech_report` | ~400 bytes | JSON string: 5 fields + reasoning |
| `final_brief` | ~1.5–2 KB | Full text brief |
| `final_report` | ~1.5–2 KB | LINE-formatted brief |
| `portfolio_advice` | ~400–800 bytes | Per-holding advice text |
| **Total** | **~7–14 KB** | |

**Missing capability**: No checkpointer configured. If the workflow crashes after `chief_strategist_node` (e.g., DB connection failure), the entire 40-second Opus computation is lost and cannot be resumed.

---

### 1.2 BacktestState — Backtest Agent

**Location**: `backtest_agent.py:52–57`
**Scope**: Single `graph.invoke()` in `backtest_agent.py`
**Lifetime**: ~5–15 seconds (one backtest evaluation)

```python
class BacktestState(TypedDict):
    trade_date:      str            # Input: YYYY-MM-DD
    brief_record:    Optional[dict] # Loaded from TiDB daily_briefs
    actual_data:     Optional[dict] # Loaded from TWSE via twse_fetcher
    accuracy_report: str            # Haiku evaluation output
```

---

### 1.3 AgentState — Maintenance Orchestrator

**Location**: `agent_orchestrator.py:50–54`
**Scope**: Single `graph.invoke()` in `agent_orchestrator.py`
**Lifetime**: ~5–10 seconds

```python
class AgentState(TypedDict):
    system_stats:    dict  # From MCP system_inspector
    final_analysis:  str   # Haiku analysis text
    status:          str   # READY | WARNING | CRITICAL
```

---

## Layer 2: File System Memory (Transient/Episodic)

### 2.1 market_snapshot.json

**Location**: `/home/itadmin/ai_agent_studio/market_snapshot.json`
**Writer**: `test_collection.py` — overwrites on every run
**Reader**: `investment_workflow.py` — reads once at startup
**Lifetime**: Persistent on disk but logically valid for one trading day
**Schema**:
```json
{
  "timestamp": "2026-05-14T22:30:00+00:00",
  "overall_latency_s": 4.231,
  "success_rate": 1.0,
  "tools": {
    "get_tw_future_chips": { "latency_s": 2.1, "success": true, "data": {...} },
    "get_us_market_summary": { "latency_s": 1.8, "success": true, "data": {...} },
    "get_financial_news": { "latency_s": 1.5, "success": true, "data": {...} }
  }
}
```

**Critical gap**: No timestamp freshness validation in `investment_workflow.py`. If `test_collection.py` fails or is not run, the workflow silently uses the previous day's (or last week's) data without error. A stale snapshot produces a confident but misleading investment brief.

---

### 2.2 collection_journal.jsonl

**Location**: `/home/itadmin/ai_agent_studio/collection_journal.jsonl`
**Writer**: `test_collection.py` — appends one line per run
**Reader**: Nobody — never read by any code
**Lifetime**: Grows indefinitely (no pruning)
**Schema** (per line):
```json
{
  "run_at": "2026-05-14T22:30:00+00:00",
  "overall_latency_s": 4.231,
  "success_rate": 1.0,
  "per_tool": {
    "get_tw_future_chips":   { "latency_s": 2.1, "success": true },
    "get_us_market_summary": { "latency_s": 1.8, "success": true },
    "get_financial_news":    { "latency_s": 1.5, "success": true }
  }
}
```

**Note**: This is the system's only operational history log for the MCP data collection step, but it is never queried. It is write-only from the system's perspective.

---

### 2.3 investment_brief_{ts}.txt

**Location**: `/home/itadmin/ai_agent_studio/investment_brief_YYYYMMDD_HHMM.txt`
**Writer**: `investment_workflow.py:158` — writes one file per run
**Reader**: Nobody — never read by any code
**Lifetime**: Accumulates on disk indefinitely
**Content**: Full `final_brief` text (Opus output, ~2000 chars)

**Note**: These files are redundant with `daily_briefs.brief_text` in TiDB. They represent a secondary backup with no reader.

---

## Layer 3: TiDB Relational Memory (Long-Term)

**Database**: `agent_memory` on TiDB Cloud
**Access**: Via `database_tools.py` using SQLAlchemy / PyMySQL
**Connection**: New pool created per function call (no singleton — see scalability report)

### 3.1 daily_briefs

**Purpose**: Long-term record of AI-generated investment briefs and predictions
**Writer**: `save_to_db_node` (investment_workflow) via `save_brief()`; `save_brief_to_db` MCP tool (orphaned)
**Reader**: `get_brief()` in backtest_agent; `get_recent_accuracy()` in dashboard + backtest_agent

| Column | Type | Notes |
|--------|------|-------|
| `id` | BIGINT AUTO_INCREMENT | Primary key |
| `trade_date` | DATE | Trading day (from snapshot timestamp) |
| `brief_text` | TEXT (implied) | Full Opus brief (~2000 chars) |
| `predicted_gap_pct` | FLOAT | Extracted from tech_report JSON |
| `gap_direction` | VARCHAR | `up` / `flat` / `down` |
| `created_at` | TIMESTAMP | INSERT time |

**No deduplication guard**: If workflow runs twice on the same `trade_date`, two rows are inserted. `get_brief()` returns `ORDER BY id DESC LIMIT 1` — so duplicates are hidden but accumulate.

---

### 3.2 market_actuals

**Purpose**: Ground-truth market data for backtest evaluation
**Writer**: `fetch_actual_node` (backtest_agent) via `save_actual()`; `dashboard.py` manual form via `save_actual()`
**Reader**: `get_actual()` in backtest_agent; `get_recent_accuracy()` for JOIN with `daily_briefs`

| Column | Type | Notes |
|--------|------|-------|
| `trade_date` | DATE | Unique key (`ON DUPLICATE KEY UPDATE`) |
| `open_price` | FLOAT | TAIEX open index |
| `close_price` | FLOAT | TAIEX close index |
| `actual_gap_pct` | FLOAT | Actual gap vs previous close |
| `notes` | VARCHAR | `source=TWSE` or `manual` |

**Deduplication**: `ON DUPLICATE KEY UPDATE` — safe, latest write wins.

---

### 3.3 cost_logs

**Purpose**: Cost and performance telemetry for each LLM invocation
**Writer**: `_record_usage()` in `market_analyst_agents.py` — called from each LLM node
**Reader**: `get_cost_summary()` and `get_cost_trend()` for dashboard; `_print_cost_report()` in workflow

| Column | Type | Notes |
|--------|------|-------|
| `id` | BIGINT AUTO_INCREMENT | Primary key |
| `agent_name` | VARCHAR(50) | Node name (`chip_analyst`, `tech_analyst`, etc.) |
| `model_name` | VARCHAR(100) | Full model ID |
| `input_tokens` | INT | |
| `output_tokens` | INT | |
| `estimated_cost_usd` | DECIMAL(10,6) | Calculated locally, not from Anthropic |
| `latency_ms` | INT | Wall-clock ms per LLM call |
| `logged_at` | TIMESTAMP | INSERT time |

**Gap**: `evaluate_node` in backtest_agent and `think_node` in agent_orchestrator instantiate `ChatAnthropic` directly — they do NOT call `_record_usage()`. These LLM costs are NOT tracked.

---

### 3.4 user_portfolio

**Purpose**: User's stock holdings with cost basis and stop-loss settings
**Writer**: `add_portfolio_item()`, `update_portfolio_item()`, `delete_portfolio_item()` (dashboard + seed)
**Reader**: `get_portfolio()` via `portfolio_tools.get_user_portfolio()` in portfolio_manager_node and dashboard

| Column | Type | Notes |
|--------|------|-------|
| `id` | BIGINT AUTO_INCREMENT | Primary key |
| `stock_id` | VARCHAR(20) | e.g., `2330` |
| `entry_price` | DECIMAL(10,2) | Cost basis |
| `quantity` | INT | Number of shares |
| `stop_loss_level` | DECIMAL(5,2) | Percentage (e.g., `5.00` = 5%) |
| `strategy_type` | VARCHAR(20) | `波段` / `長抱` / `存股` / `當沖` |
| `created_at` | TIMESTAMP | |
| `UNIQUE KEY` | `(stock_id, entry_price)` | Prevents exact duplicates |

---

## Layer 4: Application Cache (Streamlit)

**Location**: `dashboard.py:29–48`
**Scope**: Single `streamlit run` process
**Implementation**: `@st.cache_data` decorator with TTL

| Cache Function | TTL | Data Cached | Invalidation |
|---------------|-----|-------------|-------------|
| `_fetch_pnl()` | 300s (5 min) | Enriched P&L from yfinance | `st.cache_data.clear()` on form submit |
| `get_stock_history()` | 3600s (1 hr) | Historical OHLC from yfinance | Never explicitly cleared |

**Limitation**: Cache is process-local (in-memory). A Streamlit restart clears all cached data. Multiple browser tabs share the same cache (Streamlit server-side). No cross-process sharing with the investment workflow.

---

## Memory Type Classification Summary

| Memory Type | Present? | Implementation | Location |
|-------------|---------|---------------|---------|
| **Short-term** | ✅ | LangGraph TypedDict state | In-process, per-run |
| **Long-term declarative** | ✅ | TiDB relational tables | 4 tables in `agent_memory` |
| **Episodic** | ⚠️ Partial | `collection_journal.jsonl` (unread); `brief_*.txt` files (unread); `cost_logs` (partially queried) | Disk + TiDB |
| **Semantic** | ⚠️ Hard-coded | System prompts as string constants | `market_analyst_agents.py` source |
| **Procedural** | ✅ (implicit) | LangGraph graph topology; retry logic | Graph edges in workflow files |
| **Vector** | ❌ Absent | None | — |
| **Working memory cache** | ⚠️ Limited | `@st.cache_data` | Streamlit process only |

---

## Memory Lifecycle Matrix

| Store | Created By | Read By | Updated By | Deleted By | Expires |
|-------|-----------|---------|-----------|-----------|---------|
| System prompts | Developer | LLM node invoke | Code deploy | Code deploy | Never |
| WorkflowState | `graph.invoke()` | Each node | Each node | Process exit | Never (ephemeral) |
| `market_snapshot.json` | `test_collection.py` | `investment_workflow.py` | `test_collection.py` (overwrite) | Manual | Never (auto) |
| `collection_journal.jsonl` | `test_collection.py` | Nobody | Append-only | Manual | Never |
| `brief_*.txt` | `investment_workflow.py` | Nobody | Never | Manual | Never |
| `daily_briefs` | `save_to_db_node` | `backtest_agent`, dashboard | Never | Never | Never |
| `market_actuals` | `backtest_agent`, dashboard | `backtest_agent`, dashboard | ON DUPLICATE KEY | Never | Never |
| `cost_logs` | `_record_usage()` | Dashboard, cost report | Never | Never | Never |
| `user_portfolio` | Dashboard, seed | `portfolio_manager_node`, dashboard | Dashboard edit | Dashboard delete | Never |
| Streamlit cache | First page load | Dashboard render | TTL expiry | `cache_data.clear()` | 300s / 3600s |
