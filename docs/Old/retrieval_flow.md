# Retrieval Flow
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Retrieval Architecture Summary

The system uses **four distinct retrieval mechanisms**, none of which involve semantic similarity or vector search.

| Mechanism | Used Where | Strategy | Latency |
|-----------|-----------|---------|---------|
| **File read** | `investment_workflow.py` | Full file load, no filtering | <10ms (local SSD) |
| **Exact-match SQL** | All `database_tools.py` queries | `WHERE trade_date = :d` | 20–80ms (TiDB Cloud) |
| **Time-window aggregate** | `get_cost_summary`, `get_cost_trend` | `WHERE logged_at >= NOW() - INTERVAL :days DAY` | 50–200ms |
| **Application cache** | `dashboard.py` | TTL-keyed by input hash | 0ms (cache hit); full latency (miss) |

**What is absent**: No fuzzy search, no keyword search, no full-text search, no embedding-based retrieval, no RAG pipeline.

---

## Retrieval Flow Diagrams

### Flow 1: Investment Workflow (daily run)

```
08:00 — test_collection.py
  └─ MCP calls (asyncio.gather) → 3 tools concurrently
       ├─ get_tw_future_chips     → TAIFEX POST scrape
       ├─ get_us_market_summary   → yfinance + Yahoo v8
       └─ get_financial_news      → Anue API
  └─ Write: market_snapshot.json (overwrites previous)
  └─ Append: collection_journal.jsonl

08:20 — investment_workflow.py
  └─ READ: market_snapshot.json → WorkflowState["snapshot"]
       (No staleness check — proceeds even if file is days old)
  │
  └─ data_collector_node
       Input: state["snapshot"]["tools"] (full MCP output)
       Retrieval: None — uses input directly
       Output: raw_market_data (8-field compact dict)
  │
  ├─ chip_analyst_node
  │    Input: raw_market_data["foreign/trust/dealer_oi_net"]
  │    Retrieval: None
  │    Output: chip_report (JSON string)
  │
  └─ tech_analyst_node
       Input: raw_market_data["djia/ndx/sox/tsm_adr_chg_pct"]
       Retrieval: None
       Output: tech_report (JSON string)
  │
  └─ chief_strategist_node
       Input: chip_report + tech_report
       Retrieval: None (no historical context loaded)
       Output: final_brief (free-text)
  │
  └─ portfolio_manager_node
       Input: final_brief + live portfolio data
       Retrieval:
         ① database_tools.get_portfolio()
            SQL: SELECT * FROM user_portfolio ORDER BY created_at
            No filtering — returns ALL holdings
         ② portfolio_tools.calculate_pnl(holdings)
            External: yfinance Ticker.history(period="1d") — one per holding
       Output: portfolio_advice (free-text)
  │
  └─ format_agent_node
       Input: final_brief + portfolio_advice
       Retrieval: None
       Output: final_report (LINE-formatted text)
  │
  └─ save_to_db_node
       Write: daily_briefs (INSERT)
  │
  └─ send_notification_node
       Write: LINE API + Telegram API push
```

---

### Flow 2: Backtest Agent

```
09:00 (or manual) — backtest_agent.py
  │
  └─ load_brief_node
       Retrieval:
         SQL: SELECT * FROM daily_briefs
              WHERE trade_date = :d
              ORDER BY id DESC LIMIT 1
         Strategy: EXACT DATE MATCH, latest-first
         Returns: dict with brief_text, predicted_gap_pct, gap_direction
         On miss: returns None → evaluate_node short-circuits
  │
  └─ fetch_actual_node
       Retrieval:
         External: twse_fetcher.get_taiex_actuals(trade_date)
         HTTP POST to TWSE with verify=False
         Strategy: Exact date match with lookback ≤5 days
       Write: market_actuals (UPSERT: ON DUPLICATE KEY UPDATE)
  │
  └─ evaluate_node
       Input: brief_record + actual_data
       Retrieval: None (no historical patterns loaded)
       Output: accuracy_report (free-text, stdout only — never persisted)
```

---

### Flow 3: Dashboard Retrieval

```
Dashboard page load / tab switch
  │
  ├─ Tab 1: Prediction Accuracy
  │    SQL: get_recent_accuracy(30)
  │         SELECT b.trade_date, b.gap_direction, b.predicted_gap_pct,
  │                a.actual_gap_pct, a.open_price, a.close_price
  │         FROM daily_briefs b
  │         LEFT JOIN market_actuals a ON b.trade_date = a.trade_date
  │         ORDER BY b.trade_date DESC LIMIT 30
  │    Retrieval type: TIME-WINDOW, LEFT JOIN
  │
  ├─ Tab 2: Cost Analytics
  │    SQL: get_cost_summary(30) — GROUP BY agent_name, model_name
  │         get_cost_trend(30)   — GROUP BY DATE(logged_at)
  │    Retrieval type: AGGREGATION over time window
  │
  └─ Tab 3: Portfolio Management
       SQL: get_portfolio() — SELECT * FROM user_portfolio
       Retrieval type: FULL TABLE SCAN (no filter)
       Cache:
         @st.cache_data(ttl=300) wraps calculate_pnl(holdings_json)
         Cache key: serialized holdings list (JSON string)
         Cache miss: yfinance call per holding (synchronous, sequential)
         Cache hit: returns previously enriched list (up to 5 min stale)
       Also:
         @st.cache_data(ttl=3600) wraps get_stock_history(stock_id, period)
         Cache key: (stock_id, period) tuple
         Cache miss: yfinance Ticker.history() call
```

---

## Detailed Query Analysis

### Q1: `get_brief(trade_date: date)`

```sql
SELECT id, trade_date, brief_text, predicted_gap_pct, gap_direction, created_at
FROM daily_briefs
WHERE trade_date = :d
ORDER BY id DESC LIMIT 1
```

**Index used**: If there is an index on `trade_date` (not shown in `ensure_cost_logs_table` DDL — not guaranteed). The `CREATE TABLE` in `database_tools.py` does not define an explicit index on `trade_date` for `daily_briefs`.

**Risk**: Full table scan if no index on `trade_date`. At 250 rows/year, the table is small — performance is acceptable today but degrades without index.

**Correctness issue**: `ORDER BY id DESC LIMIT 1` silently handles duplicate rows (same `trade_date` inserted twice). The duplicate accumulates but is hidden.

---

### Q2: `get_recent_accuracy(days: int = 5)`

```sql
SELECT b.trade_date, b.gap_direction, b.predicted_gap_pct,
       a.actual_gap_pct, a.open_price, a.close_price
FROM daily_briefs b
LEFT JOIN market_actuals a ON b.trade_date = a.trade_date
ORDER BY b.trade_date DESC
LIMIT :days
```

**Index used**: Requires index on `daily_briefs.trade_date` (for ORDER BY) and `market_actuals.trade_date` (for JOIN). Neither is explicitly defined in the code.

**LEFT JOIN behavior**: Returns `NULL` for `actual_gap_pct` when no backtest has been run for a date — this is correct and handled in `_calc_accuracy_kpi()`.

---

### Q3: `get_cost_summary(days: int = 30)`

```sql
SELECT agent_name, model_name,
       SUM(input_tokens), SUM(output_tokens),
       SUM(estimated_cost_usd), AVG(latency_ms), COUNT(*)
FROM cost_logs
WHERE logged_at >= NOW() - INTERVAL :days DAY
GROUP BY agent_name, model_name
ORDER BY total_cost_usd DESC
```

**Index used**: `idx_logged_at` on `logged_at` (defined in `ensure_cost_logs_table`). This makes the time window filter efficient.

**Growth concern**: As `cost_logs` grows (6 rows/run × 20 days/month × years), the aggregation window covers more rows. At 3 years: ~4,320 rows. Still fast with the index.

---

### Q4: `get_portfolio()` → `calculate_pnl()`

```python
def get_portfolio() -> list[dict]:
    SELECT * FROM user_portfolio ORDER BY created_at

def calculate_pnl(holdings: list[dict]) -> list[dict]:
    for h in holdings:
        df = yf.Ticker(f"{h['stock_id']}.TW").history(period="1d")
```

**Pattern**: Full table load → serial external API calls.

**Latency**: Each `yf.Ticker.history()` call takes ~1–3 seconds (blocking HTTP). With N holdings:
- 1 holding: ~1–3s
- 5 holdings: ~5–15s (sequential, no parallelism)
- 20 holdings: ~20–60s

**No intermediate caching in the workflow**: When `portfolio_manager_node` calls `calculate_pnl()`, there is no TTL cache at the function level. The Streamlit `@st.cache_data(ttl=300)` wraps `_fetch_pnl()` but only applies to the dashboard — the workflow always fetches fresh prices.

---

## Retrieval Ranking Logic

There is **no relevance ranking** anywhere in the system. All retrieval returns either:

1. **Exact match**: The specific row for a given `trade_date` (or `None`)
2. **Recency-ordered set**: The most recent N rows by date
3. **Full set**: All rows in a table (portfolio, cost aggregate)
4. **Aggregated window**: Time-windowed GROUP BY (cost analytics)

**Implication**: When `chief_strategist_node` generates today's brief, it has no access to:
- Similar market conditions from historical sessions
- The accuracy scores of previous predictions
- Trending patterns (e.g., "SOX has been predictively strong for 5 consecutive sessions")
- Previous briefs that were subsequently validated as correct

All reasoning is stateless from the LLM's perspective — each run starts from scratch with only the current snapshot.

---

## Embedding Usage

**None.** Zero embeddings are generated or consumed anywhere in the system.

The system does not use:
- `langchain.embeddings` / `langchain_anthropic` embedding models
- `sentence-transformers`
- External embedding APIs (OpenAI, Cohere, etc.)
- Any vector index (FAISS, Chroma, Qdrant, Pinecone, Weaviate, TiDB Vector)

---

## Similarity Search

**None.** The system cannot answer questions like:
- "Find historical sessions similar to today's market conditions"
- "Which past predictions were correct under similar SOX/TSM ADR patterns?"
- "What was the outcome when foreign OI net was in the range [-20K, -15K]?"

All these questions require embedding + vector similarity search. They are currently unanswerable by the system's retrieval layer.

---

## Retrieval Coverage by Agent

| Agent | Memory Read | Retrieval Strategy | Reads History? |
|-------|-------------|-------------------|---------------|
| data_collector | market_snapshot.json | File read | ❌ Today only |
| chip_analyst | WorkflowState (in-memory) | State pass-through | ❌ Today only |
| tech_analyst | WorkflowState (in-memory) | State pass-through | ❌ Today only |
| chief_strategist | WorkflowState (in-memory) | State pass-through | ❌ No history |
| portfolio_manager | TiDB user_portfolio + yfinance | Full table + serial HTTP | ❌ Live only |
| format_agent | WorkflowState (in-memory) | State pass-through | ❌ No history |
| backtest evaluate | TiDB daily_briefs + TWSE | Exact date SQL | ✅ One past day only |
| dashboard accuracy | TiDB daily_briefs + market_actuals | LEFT JOIN, N rows | ✅ Last 30 days |
| orchestrator think | MCP system_inspector | MCP stdio call | ❌ No history |

**The critical gap**: No agent reads historical patterns to inform its current analysis. The chief_strategist, despite being the most expensive LLM ($0.048/run), operates with zero contextual memory of past sessions.
