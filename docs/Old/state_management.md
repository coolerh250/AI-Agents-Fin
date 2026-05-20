# State Management Analysis
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## 1. State Schema Inventory

### 1.1 WorkflowState (investment_workflow)

**File:** `market_analyst_agents.py:30`

```python
class WorkflowState(TypedDict):
    snapshot:         dict           # full market_snapshot.json, ~3 KB raw JSON
    raw_market_data:  dict           # compressed output from data_collector, ~200 bytes
    chip_report:      str            # JSON string from chip_analyst, ~300 tokens
    tech_report:      str            # JSON string from tech_analyst, ~300 tokens
    final_brief:      str            # prose from chief_strategist, ~600 tokens
    final_report:     str            # LINE-formatted text from format_agent, ≤2000 chars
    db_row_id:        Optional[int]  # TiDB row ID from save_to_db_node
    portfolio_advice: str            # per-holding advice from portfolio_manager, ~200 tok/holding
```

**Key properties:**
- All 8 fields are present from the initial state construction in `main()`
- No field is ever deleted or explicitly reset
- All nodes return partial dicts — LangGraph merges them into the shared state

### 1.2 BacktestState

**File:** `backtest_agent.py:52`

```python
class BacktestState(TypedDict):
    trade_date:      str            # YYYY-MM-DD
    brief_record:    Optional[dict] # row from daily_briefs table
    actual_data:     Optional[dict] # TWSE response + derived fields
    accuracy_report: str            # LLM evaluation output
```

### 1.3 AgentState (maintenance)

**File:** `agent_orchestrator.py:50`

```python
class AgentState(TypedDict):
    system_stats:    dict  # psutil data from MCP
    final_analysis:  str   # Claude prose output
    status:          str   # READY | WARNING | CRITICAL | UNKNOWN
```

---

## 2. State Layer Classification

| Layer | What it Is | Where it Lives | Scope | Survives Failure? |
|-------|-----------|----------------|-------|-------------------|
| **Workflow State** | `TypedDict` in-process | Python heap | Single run, single process | ❌ No |
| **Relational Storage** | TiDB `agent_memory` DB | Remote database | Cross-run, cross-agent | ✅ Yes |
| **File Cache** | `market_snapshot.json` | Local disk | Single day | ✅ Until overwritten |
| **LangGraph Checkpoint** | `MemorySaver` or `SqliteSaver` | In-memory / SQLite | Run-level resume | ❌ NOT IMPLEMENTED |

---

## 3. Shared State Analysis

### 3.1 State Visibility Per Node

| Node | Reads | Writes | State After Node |
|------|-------|--------|-----------------|
| `data_collector` | `snapshot` | `raw_market_data` | snapshot ✅, raw_market_data ✅ |
| `chip_analyst` | `raw_market_data`, `snapshot` | `chip_report` | + chip_report ✅ |
| `tech_analyst` | `raw_market_data`, `snapshot` | `tech_report` | + tech_report ✅ |
| `chief_strategist` | `chip_report`, `tech_report` | `final_brief` | + final_brief ✅ |
| `portfolio_manager` | `final_brief` | `portfolio_advice` | + portfolio_advice ✅ |
| `format_agent` | `final_brief`, `portfolio_advice` | `final_report` | + final_report ✅ |
| `save_to_db` | `final_brief`, `tech_report`, `snapshot` | `db_row_id` | + db_row_id ✅ |
| `send_notification` | `final_report` | `{}` (no-op) | unchanged |

### 3.2 Stale State Problem

`snapshot` (full raw JSON, ~3 KB) is carried in the WorkflowState throughout all 8 nodes. Only `data_collector` actually needs it. After `data_collector` writes `raw_market_data`, the `snapshot` field becomes dead weight.

**Impact:**
- Minor memory overhead (~3 KB × 8 node state copies)
- `save_to_db_node` uses `snapshot["timestamp"][:10]` as trade_date — this is the ONLY reason snapshot must survive past data_collector. A cleaner design would extract `trade_date` into its own state field during data_collector.

### 3.3 Parallel Branch Write Safety

During the fan-out phase, `chip_analyst` and `tech_analyst` run concurrently in separate threads. Both write to the shared state dict via their return values (`{"chip_report": ...}` and `{"tech_report": ...}`). 

LangGraph merges these returns using an internal state reducer. Since the two branches write **disjoint keys**, there is no write conflict. This is safe.

**Risk edge case**: If both branches attempted to write the same key (e.g., both returned `{"raw_market_data": ...}`), LangGraph would use the last writer's value, with no error. This does not occur in the current implementation but is an implicit assumption that must be maintained in future development.

---

## 4. Transient vs Persisted State

### 4.1 Transient State (in-memory only, lost on failure)

| Field | Produced by | Lost if node fails |
|-------|------------|-------------------|
| `raw_market_data` | data_collector | Yes — all downstream work must restart |
| `chip_report` | chip_analyst | Yes — if chief_strategist fails, chip_report is gone |
| `tech_report` | tech_analyst | Yes — same as above |
| `final_brief` | chief_strategist | Yes — $0.05+ of compute lost |
| `portfolio_advice` | portfolio_manager | Yes |
| `final_report` | format_agent | Yes |

### 4.2 Persisted State (survives process death)

| Artifact | Written by | Storage | Recovery? |
|----------|-----------|---------|----------|
| `daily_briefs` row | save_to_db_node | TiDB | ✅ Full recovery |
| `market_actuals` row | backtest:fetch_actual | TiDB | ✅ Full recovery |
| `cost_logs` rows | `_record_usage()` in each node | TiDB | ✅ Partial (per node, immediate) |
| `investment_brief_*.txt` | `main()` post-graph | Local file | ✅ Yes |

### 4.3 The Critical Gap: Checkpoint Strategy

**Status: NOT IMPLEMENTED**

LangGraph supports checkpointing via `graph.compile(checkpointer=...)`. Without a checkpointer:

- If `chief_strategist` (15–45 second Opus call) raises a transient error (529 Overloaded, network timeout, connection reset), the **entire graph aborts from the beginning**
- All upstream computation (data_collector + chip_analyst + tech_analyst) is discarded
- No resume-from-checkpoint is possible
- The daily brief is not generated; LINE/Telegram push does not occur

```python
# Current (no checkpoint):
graph.compile()

# Minimum viable checkpoint (in-memory, resumable within same process):
from langgraph.checkpoint.memory import MemorySaver
graph.compile(checkpointer=MemorySaver())

# Production-grade checkpoint (survives process restart):
from langgraph.checkpoint.sqlite import SqliteSaver
graph.compile(checkpointer=SqliteSaver.from_conn_string("checkpoints.db"))
```

With checkpointing enabled, each completed node's output is persisted. On retry, LangGraph replays only from the last completed node.

---

## 5. State Schema Design Issues

### Issue 1: No `trade_date` field in WorkflowState

`save_to_db_node` extracts the trade date from `state["snapshot"]["timestamp"][:10]`. This couples the persistence layer to the raw snapshot structure. If the snapshot schema changes (e.g., timestamp becomes `trade_date` directly), `save_to_db_node` silently reads the wrong date.

**Fix:** Add `trade_date: str` to `WorkflowState`, populated by `data_collector_node`.

### Issue 2: `snapshot` Carried Through Entire Graph

After `data_collector` runs, `snapshot` (raw ~3 KB JSON) is no longer needed by any downstream node — except `save_to_db_node` uses `snapshot["timestamp"]`. This creates an implicit dependency that prevents garbage collection of the snapshot dict.

**Fix:** Extract `trade_date: str` in `data_collector_node`; remove snapshot dependency from `save_to_db_node`.

### Issue 3: No Schema Validation at Runtime

`WorkflowState` is a `TypedDict` — Python's type system does not enforce field presence or type at runtime. A node returning `{"chip_report": None}` (e.g., on JSON parse failure) would corrupt `chief_strategist`'s input without raising an error.

**Fix:** Add a `_validate_state(state: WorkflowState)` assertion helper called at `chief_strategist_node` entry, raising explicitly if `chip_report` or `tech_report` is empty/None.

### Issue 4: Optional Fields Mixed With Required Fields

`db_row_id: Optional[int]` and `portfolio_advice: str` (empty string as default) are initialized in the WorkflowState constructor. This means nodes that check `if state.get("portfolio_advice")` may silently receive an empty string instead of detecting a missing value.

### Issue 5: Accuracy Report Never Enters State OR Storage

In `backtest_agent.py`, `evaluate_node` writes `accuracy_report` to `BacktestState`. This value is read only by `main()` for `print()`. There is no `save_accuracy_node` that persists it to TiDB. Historical accuracy evaluation is not queryable.

---

## 6. State Lifecycle Summary

```
Graph start         Graph running       Graph end
      │                   │                  │
      ▼                   ▼                  ▼
WorkflowState(     [nodes mutate      final state dict
  snapshot=...,     state via          returned to
  raw_market_data   partial dict       caller in
  ={},              merges]            main()
  chip_report="",
  ...               ⚠ NO CHECKPOINT:  written to file
)                   if any node fails  + TiDB (partial)
                    → all in-memory
                      state is lost
```
