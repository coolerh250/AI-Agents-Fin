# Context Engineering Analysis
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Overview

Context engineering is how the system assembles, compresses, passes, and prunes information into LLM prompts at each node. This document analyzes all six dimensions: assembly flow, summarization, pruning, stale memory handling, duplicate handling, and context budget per node.

---

## Context Assembly Flow

### Master Diagram — Investment Workflow

```
market_snapshot.json (3–8 KB raw JSON)
    │
    ▼
data_collector_node
    System: _COLLECTOR_SYSTEM (229 chars)
    User:   json.dumps(snapshot["tools"], ...) ← FULL tool outputs
    ─────────────────────────────────────────────────────────
    INPUT TOKENS:  ~1200–2500 (snapshot has 3 tool outputs)
    OUTPUT TOKENS: ~100–150 (8-field compact JSON)
    COMPRESSION:   ~10–20× reduction
    ─────────────────────────────────────────────────────────
    Extracts: foreign_oi_net, trust_oi_net, dealer_oi_net,
              djia_chg_pct, ndx_chg_pct, sox_chg_pct,
              tsm_adr_chg_pct, data_ok, missing_fields
    └─► raw_market_data (dict, ~200 bytes)
         │                        │
         ▼                        ▼
chip_analyst_node          tech_analyst_node   (PARALLEL)
System: _CHIP_SYSTEM       System: _TECH_SYSTEM
  (564 chars)                (674 chars)
User: chip_data (3 fields) User: us_data (4 fields)
─────────────────────────  ─────────────────────────
INPUT:  ~300–400 tokens    INPUT:  ~300–400 tokens
OUTPUT: ~150–250 tokens    OUTPUT: ~200–300 tokens
─────────────────────────  ─────────────────────────
chip_report (str)          tech_report (str)
JSON: sentiment,           JSON: gap_direction,
      foreign_net,               estimated_gap_pct,
      trust_net,                 key_driver,
      dealer_net,                tsm_signal,
      divergence_signal,         reasoning
      reasoning
         │                        │
         └──────────┬─────────────┘
                    ▼
         chief_strategist_node
         System: _CHIEF_SYSTEM (497 chars)
         User:   chip_report + "\n\n" + tech_report
         ─────────────────────────────────────────────
         INPUT:  ~800–1200 tokens
         OUTPUT: ~600–900 tokens (4-section free-text brief)
         ─────────────────────────────────────────────
         final_brief (str) ~1500–2000 chars
                    │
                    ▼
         portfolio_manager_node
         System: _PORTFOLIO_SYSTEM (425 chars)
         User:   final_brief + pnl_lines (N holdings)
         ─────────────────────────────────────────────
         INPUT:  ~600–1000 tokens + ~80 tokens/holding
         OUTPUT: ~300–600 tokens
         ─────────────────────────────────────────────
         portfolio_advice (str) ~400–800 chars
                    │
                    ▼
         format_agent_node
         System: _FORMAT_SYSTEM (360 chars)
         User:   final_brief + portfolio_advice (if non-empty)
         ─────────────────────────────────────────────
         INPUT:  ~1000–1700 tokens
         OUTPUT: ~500–800 tokens (LINE format, max 2000 chars enforced via prompt)
         ─────────────────────────────────────────────
         final_report (str) → LINE push
```

---

## Summarization Flow

### Summarization Point 1: data_collector_node

This is the **only deliberate summarization** in the system. Its role is to compress raw MCP output before routing to specialized analysts.

**Input signal** (from `market_snapshot.json`):
- `get_tw_future_chips.data`: Full TAIFEX HTML scrape result — includes all three institutional investors' data, potentially with multiple date rows
- `get_us_market_summary.data`: Markets dict with all index data (DJIA, NDX, SOX, TSM ADR) plus metadata
- `get_financial_news.data`: Array of news titles + timestamps from Anue

**Compression mechanism**: The system prompt `_COLLECTOR_SYSTEM` instructs Haiku to extract exactly 8 numeric fields. This is prompt-engineering-based summarization — not a deterministic transform.

**Risks of prompt-based summarization**:
1. Haiku may hallucinate values if the snapshot structure is unexpected (e.g., TAIFEX scraper fails and returns partial data)
2. The system prompt does not instruct Haiku what to do when `data_ok=false` — it may silently output zeros
3. JSON parse failure (`json.loads(raw_text)`) results in `raw_market_data = {}`, which causes downstream nodes to fall back to the full snapshot path:
   ```python
   # chip_analyst_node fallback:
   if not chip_data:
       chip_data = state["snapshot"]["tools"]["get_tw_future_chips"]["data"]
   ```
   This fallback injects the full (unsummarized, potentially large) data directly into the Sonnet prompt.

**No summarization elsewhere**: Nodes pass their full text output forward. `chief_strategist_node` receives the complete `chip_report` AND `tech_report` without any compression.

---

## Context Budget Per Node

| Node | Model | System Tokens | User Tokens | Total Input | Max Output | Budget Utilization |
|------|-------|--------------|------------|------------|-----------|-------------------|
| data_collector | Haiku | ~57 | ~1200–2500 | ~1260–2560 | 1024 | ~23–48% of 4K window |
| chip_analyst | Sonnet | ~142 | ~300–400 | ~440–540 | 1024 | ~11–13% of 4K window |
| tech_analyst | Sonnet | ~169 | ~300–400 | ~470–570 | 1024 | ~12–14% of 4K window |
| chief_strategist | Opus | ~125 | ~800–1200 | ~925–1325 | 16000* | ~2–3% of 200K window |
| portfolio_manager | Sonnet | ~107 | ~600–1000 | ~710–1110 | 1024 | ~18–27% of 4K window |
| format_agent | Haiku | ~90 | ~1000–1700 | ~1090–1790 | 2048 | ~21–34% of 4K window |

_* `max_tokens=16000` for Opus is 8× the actual output size (~2000 tokens). This means each chief_strategist call reserves 16,000 token slots in Anthropic's response budget, incurring maximum cost even when the output is short._

**Key findings**:
- Context windows are **massively underutilized** — no node approaches context limits today
- The Opus `max_tokens=16000` reservation inflates cost without proportional value
- As portfolio grows (more holdings), `portfolio_manager_node` input grows linearly: each holding adds ~80 tokens to the user message

---

## Context Pass-Through Analysis

Every node passes its **full text output** to downstream nodes. There is no intermediate compression between `chip_analyst → chief_strategist` or `chief_strategist → format_agent`.

```
Snapshot (3–8 KB)
    → data_collector → raw_market_data (200B)  ← COMPRESSED ✅
        → chip_report (300–500 chars)           ← DIRECT PASS ✅ (small)
        → tech_report (300–500 chars)           ← DIRECT PASS ✅ (small)
            → final_brief (1500–2000 chars)     ← DIRECT PASS ✅ (acceptable)
                → portfolio_advice (400–800 chars) ← DIRECT PASS ✅ (small)
                    → final_report (1500–2000 chars) ← DIRECT PASS ✅

State size grows: 200B → 1KB → 2KB → 3.5KB → 4.5KB → 6.5KB
```

The context engineering is sound at current scale. The only risk is the `snapshot` field remaining in `WorkflowState` for the entire run — it is only used by `data_collector_node` but stays allocated in memory through to `save_to_db_node`.

---

## Stale Memory Handling

### Problem 1: market_snapshot.json staleness (CRITICAL)

**File**: `investment_workflow.py:134–139`

```python
snapshot = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
logger.info(f"Snapshot loaded: {snapshot['timestamp']}")

for tool_name, tool_data in snapshot["tools"].items():
    if not tool_data.get("success"):
        logger.warning(f"Tool {tool_name} reported failure in snapshot — proceeding anyway")
```

The workflow:
1. Logs the snapshot timestamp (for human visibility)
2. Warns on failed tools but **does not abort**
3. Has **no maximum age check** — a 3-day-old snapshot is accepted silently

**Attack scenario**: If `test_collection.py` is not run on a trading day (network failure, server restart), the workflow runs against last week's data and sends a confident but misleading LINE notification.

**Current detection**: None. The loguru `logger.info(f"Snapshot loaded: {snapshot['timestamp']}")` line gives the timestamp, but only if a human reads the terminal output.

**Fix needed**:
```python
from datetime import datetime, timezone, timedelta
snap_age = datetime.now(timezone.utc) - datetime.fromisoformat(snapshot["timestamp"])
if snap_age > timedelta(hours=12):
    logger.error(f"Snapshot is {snap_age} old — aborting to prevent stale analysis")
    sys.exit(1)
```

---

### Problem 2: portfolio P&L staleness (MEDIUM)

**File**: `portfolio_tools.py:26–35`

When `yfinance` returns an empty DataFrame or raises an exception:
```python
current_price = entry_price   # Default: cost basis = zero P&L
try:
    df = yf.Ticker(f"{h['stock_id']}.TW").history(period="1d")
    if not df.empty:
        current_price = float(df["Close"].iloc[-1])
    else:
        logger.warning(...)  # warning only, no state flag
except Exception as exc:
    logger.warning(...)      # warning only, no state flag
```

The portfolio_manager_node receives `損益 0%` for any holding where yfinance failed. There is no `price_stale: bool` flag in the enriched dict. The LLM cannot distinguish "zero P&L" from "price fetch failure."

**Downstream consequence**: If 2330 has fallen 12% and the stop-loss threshold is 5%, but yfinance returns empty data that day, the user receives `止損觸發: 否` and `建議續抱` instead of a sell signal.

---

### Problem 3: Streamlit cache staleness (LOW)

`@st.cache_data(ttl=300)` wraps `_fetch_pnl()`. The cache key is the **serialized holdings JSON string**. If only portfolio metadata changes (e.g., stop-loss level updated), the cache key changes and the cache invalidates correctly. This is sound.

However, the historical price cache `@st.cache_data(ttl=3600)` for `get_stock_history()` does not invalidate when the market closes on a new trading day — it serves up-to-one-hour-stale prices.

---

## Duplicate Memory Handling

### Duplicate daily_briefs rows

**Problem**: If `investment_workflow.py` is run twice on the same trading day, two rows are inserted into `daily_briefs` with the same `trade_date`.

**Current handling**: `get_brief()` uses `ORDER BY id DESC LIMIT 1` — always returns the most recent. This hides duplicates but they accumulate.

**Affected queries**:
- `get_recent_accuracy(days)` uses `SELECT ... FROM daily_briefs b LEFT JOIN market_actuals a` without `DISTINCT trade_date` → duplicate dates inflate the count
- Dashboard accuracy charts may show the same date twice

**Fix**: Add `UNIQUE KEY uq_trade_date (trade_date)` to `daily_briefs`, or add an upsert pattern similar to `market_actuals`.

---

### Duplicate market_actuals rows

**Handling**: `ON DUPLICATE KEY UPDATE` — last write wins. Safe and correct.

---

### Duplicate user_portfolio rows

**Handling**: `UNIQUE KEY uq_stock_entry (stock_id, entry_price)` + `INSERT IGNORE` / exception catch. Safe.

---

## Memory Pruning

### Current state: NO automatic pruning anywhere

| Store | Prune Policy | Issue |
|-------|-------------|-------|
| `market_snapshot.json` | Overwrite (not prune) | Only one copy kept; history lost |
| `collection_journal.jsonl` | Append-only | Grows indefinitely, never read |
| `investment_brief_*.txt` | Accumulate | One file per run, never cleaned |
| `daily_briefs` | None | Grows indefinitely |
| `market_actuals` | None | Grows indefinitely |
| `cost_logs` | None | Grows indefinitely |
| `user_portfolio` | Manual delete via dashboard | No age-based pruning |

**Expected growth rate**:
- `collection_journal.jsonl`: ~300 bytes/entry × 20 entries/month = ~6KB/month → ~72KB/year
- `investment_brief_*.txt`: ~2KB × 20 files/month × 12 = ~480KB/year
- `daily_briefs` TiDB: ~6KB × 250 rows/year → ~1.5MB/year → negligible
- `cost_logs` TiDB: ~100 bytes × 120 rows/month → ~144KB/year → negligible

At current scale, lack of pruning is not a capacity risk. However, the `investment_brief_*.txt` file accumulation could confuse operators (hundreds of files with similar names).

**Recommended minimal pruning**:
```python
# In investment_workflow.py — cleanup brief files older than 30 days
from datetime import datetime, timedelta
cutoff = datetime.now() - timedelta(days=30)
for f in Path(".").glob("investment_brief_*.txt"):
    if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
        f.unlink()
```

---

## Context Engineering Gaps — Priority Matrix

| Gap | Impact | Effort | Priority |
|-----|--------|--------|---------|
| No snapshot freshness validation | 🔴 HIGH — stale data produces misleading briefs | Low (10 lines) | P0 |
| `price_stale` flag missing in `calculate_pnl()` | 🟡 MEDIUM — silent P&L errors affect hold/sell decisions | Low (5 lines) | P1 |
| Opus `max_tokens=16000` wastes token budget | 🟠 MEDIUM — 8× cost ceiling for 2K-token output | Low (1 line) | P1 |
| No context for chief_strategist (historical patterns) | 🟡 MEDIUM — improves prediction quality | High (vector retrieval) | P3 |
| `daily_briefs` lacks UNIQUE trade_date | 🟠 LOW — duplicate rows in backtest | Low (1 ALTER TABLE) | P2 |
| `collection_journal.jsonl` never read | 🟢 LOW — write-only, wasted I/O | Medium (add consumer) | P3 |
| No pruning on `investment_brief_*.txt` | 🟢 LOW — disk hygiene only | Low (5 lines) | P3 |
| data_collector JSON parse failure fallback | 🟠 MEDIUM — fallback sends large raw data to Sonnet | Low (explicit error handling) | P2 |
