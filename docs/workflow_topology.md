# Workflow Topology Analysis
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## 1. Graph Registry

Three independent LangGraph `StateGraph` instances exist. They share no state, no checkpointer, and no inter-graph communication channel.

| Graph | File | Entry | Nodes | Compile |
|-------|------|-------|-------|---------|
| `investment_workflow` | `investment_workflow.py` | `main()` — CLI / cron | 8 | `graph.compile()` |
| `backtest_agent` | `backtest_agent.py` | `main()` — CLI / cron | 3 | `graph.compile()` |
| `maintenance_agent` | `agent_orchestrator.py` | `main()` — manual CLI | 2 | `graph.compile()` |

---

## 2. Graph 1 — investment_workflow (Primary Production Graph)

### 2.1 Topology Classification

**Type: Hybrid DAG — Fan-out / Barrier-join / Linear tail**

This is the most complex graph. It is a Directed Acyclic Graph (no back-edges, no cycles). It combines:
- One fan-out point (data_collector → two parallel branches)
- One implicit barrier join ([chip_analyst, tech_analyst] → chief_strategist)
- Six strictly linear tail nodes

### 2.2 Full Edge Table

| From | To | Edge Type | Notes |
|------|----|-----------|-------|
| `START` | `data_collector` | Sequential | Entry point |
| `data_collector` | `chip_analyst` | Fan-out branch A | Runs immediately after data_collector |
| `data_collector` | `tech_analyst` | Fan-out branch B | Runs immediately after data_collector |
| `chip_analyst` | `chief_strategist` | Barrier join A | chief_strategist waits for BOTH |
| `tech_analyst` | `chief_strategist` | Barrier join B | chief_strategist waits for BOTH |
| `chief_strategist` | `portfolio_manager` | Sequential | — |
| `portfolio_manager` | `format_agent` | Sequential | — |
| `format_agent` | `save_to_db` | Sequential | — |
| `save_to_db` | `send_notification` | Sequential | — |
| `send_notification` | `END` | Terminal | — |

### 2.3 Topology Diagram

```
START
  │
  ▼
data_collector ──────────────────────────────────── ①
  │                                                  │
  ├──► chip_analyst ──────────────────────────────── ②
  │                                                  │
  └──► tech_analyst ──────────────────────────────── ②
            │                    │
            └────────┬───────────┘
                     ▼
              chief_strategist ◄── BARRIER JOIN (waits for both ②)
                     │
                     ▼
              portfolio_manager
                     │
                     ▼
               format_agent
                     │
                     ▼
                save_to_db
                     │
                     ▼
             send_notification
                     │
                     ▼
                    END
```

### 2.4 Parallelism Behavior

LangGraph executes fan-out branches in **separate Python threads** when using `graph.invoke()` (synchronous compile). This means:

- `chip_analyst` and `tech_analyst` WILL run concurrently via `threading.Thread`
- Both read from a shared (immutable per-call) copy of `WorkflowState`
- Both write disjoint keys (`chip_report` vs `tech_report`) — no write conflict
- `chief_strategist` is scheduled only after BOTH branches have returned

**Theoretical time savings**: ~2–3 seconds per run (concurrent Sonnet calls vs sequential)

**Caveat**: Python's GIL does not block I/O threads. Since both nodes are I/O-bound (Claude API HTTP calls), true concurrency is achieved at the OS level via `urllib3` socket blocking. This has not been verified with instrumentation.

---

## 3. Graph 2 — backtest_agent (Post-market Evaluation)

### 3.1 Topology Classification

**Type: Pure Linear DAG (pipeline)**

No fan-out, no fan-in, no conditional routing.

### 3.2 Edge Table

| From | To | Notes |
|------|----|-------|
| `START` | `load_brief` | Reads from TiDB |
| `load_brief` | `fetch_actual` | Fetches from TWSE + writes to TiDB |
| `fetch_actual` | `evaluate` | Claude Haiku LLM call |
| `evaluate` | `END` | Prints to stdout only |

### 3.3 Topology Diagram

```
START → load_brief → fetch_actual → evaluate → END
           │               │              │
        TiDB read      TWSE HTTP      Haiku LLM
                      + TiDB write    (stdout only)
```

### 3.4 Key Structural Issue

`evaluate_node` outputs `accuracy_report` to stdout. There is no edge to a persistence node. The evaluation result is **never written to TiDB**. Any future "accuracy trend" feature requires a new `save_accuracy_node` to be added.

---

## 4. Graph 3 — maintenance_agent (System Health Check)

### 4.1 Topology Classification

**Type: Pure Linear DAG (2-node pipeline)**

### 4.2 Edge Table

| From | To | Notes |
|------|----|-------|
| `act` | `think` | MCP stdio call → Claude Haiku |
| `think` | `END` | Status extraction from LLM prose |

### 4.3 Topology Diagram

```
act_node ─────────────────────────────────── think_node
  │                                                │
asyncio.run(_fetch_mcp_stats())           ChatAnthropic(Haiku)
    │                                       status = READY / WARNING / CRITICAL
subprocess: system_inspector.py
```

### 4.4 Key Structural Issue

`act_node` contains `asyncio.run()` inside a synchronous node. This works when called from a plain synchronous context (CLI). However, if called from:
- An existing event loop (Jupyter, FastAPI endpoint, async test suite)
- A LangGraph async invocation (`await graph.ainvoke()`)

It will raise `RuntimeError: This event loop is already running`. This is a **latent async-context incompatibility**.

---

## 5. What Is NOT Present

### 5.1 DAG Structures Not Used

| Pattern | Status | Impact |
|---------|--------|--------|
| **Cyclic graph** | ❌ None | Cannot re-run a node on failure; no retry loops |
| **Recursive loop** | ❌ None | Cannot implement self-correction / reflection cycles |
| **Conditional routing** | ❌ None | Cannot branch on data quality, LLM confidence, or error state |
| **Interrupt flow** | ❌ None | Cannot pause for human review of chief_strategist output |
| **Approval flow** | ❌ None | Portfolio buy/sell recommendations execute without confirmation |
| **Sub-graphs** | ❌ None | Each workflow is a flat graph; no composable sub-graph reuse |
| **Map-reduce** | ❌ None | Portfolio holdings processed in one batch prompt, not per-holding |

### 5.2 Missing Safety Structures

| Structure | What It Would Enable |
|-----------|---------------------|
| `add_conditional_edges` with error router | Graceful degradation when an API call fails |
| `interrupt_before=["portfolio_manager"]` | Human-in-the-loop approval before trade recommendations are pushed |
| `interrupt_before=["send_notification"]` | Final review gate before LINE/Telegram push |
| Back-edge to retry node | Re-run only the failed node (not entire graph) |
| `checkpointer=MemorySaver()` | Resume from last checkpoint on failure |

---

## 6. Cross-Graph Dependency Map

The three graphs are invoked independently but share infrastructure:

```
investment_workflow ─────writes──► TiDB: daily_briefs
                    ─────writes──► TiDB: cost_logs

backtest_agent ──────reads───► TiDB: daily_briefs
               ─────writes──► TiDB: market_actuals
               ─────writes──► TiDB: cost_logs (MISSING — TD-7)

maintenance_agent ─────reads──► psutil (via MCP subprocess)
                  ─────writes──► nothing (no persistence)
```

**No graph ever reads another graph's output in real time.** The backtest_agent reads `daily_briefs` written by the investment_workflow from a previous run. They are decoupled by cron schedule, not by LangGraph inter-graph communication.
