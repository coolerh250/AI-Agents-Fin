# Production-Grade LangGraph Architecture Recommendation
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Executive Summary

The current system works reliably for a single-user, single-timezone, cron-driven workflow at ~$2.54/month. It is not production-grade because it has no checkpoint resilience, unbounded LLM costs, no auth, no retry, and no observability beyond stdout logs. This document defines a concrete path to production-grade status, structured in three tiers: **Immediate** (P0, <2 hours each), **Short-term** (P1-P2, <1 day each), and **Structural** (P3-P5, architecture changes).

---

## Tier 1: Immediate — Zero-Tolerance Issues

These three changes are the minimum required for a system that handles real financial data.

### T1-A: Cap Opus Thinking Budget

**File**: `market_analyst_agents.py:122`
**Risk fixed**: Token explosion, $0.60/run worst case
**Effort**: 5 minutes

```python
# BEFORE (current — dangerous):
def _llm_opus() -> ChatAnthropic:
    return ChatAnthropic(
        model=_MODEL_OPUS,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},  # invalid — remove this
    )

# AFTER (production):
def _llm_opus() -> ChatAnthropic:
    return ChatAnthropic(
        model=_MODEL_OPUS,
        max_tokens=2048,                        # actual output is 600-1000 tokens
        thinking={"type": "enabled", "budget_tokens": 5000},  # bounded, predictable
    )
```

**Effect**: Chief strategist cost becomes deterministic at ~$0.027/run (vs current ~$0.053 expected, $0.127 observed). Monthly cost drops from ~$2.54 to ~$1.60.

---

### T1-B: Add Node-Level Error Boundaries

**File**: `market_analyst_agents.py` — every LLM node
**Risk fixed**: Fan-in failure silently drops entire daily brief
**Effort**: 30 minutes

Every LLM node currently raises unhandled exceptions on API failure. The correct pattern is: catch the exception, write a degraded-but-valid state value, and let the graph continue with reduced quality rather than aborting.

```python
# Pattern to apply to ALL LLM nodes:
def chip_analyst_node(state: WorkflowState) -> dict:
    logger.info("[ChipAnalyst] 開始籌碼分析")
    try:
        raw = state.get("raw_market_data") or {}
        # ... existing logic ...
        response = _llm(_MODEL_SONNET).invoke([...])
        _record_usage("chip_analyst", _MODEL_SONNET, response, latency_ms)
        return {"chip_report": _extract_text(response)}
    except Exception as exc:
        logger.error(f"[ChipAnalyst] API 失敗，使用降級輸出: {exc}")
        return {"chip_report": json.dumps({
            "sentiment": "unknown", "foreign_net": 0, "trust_net": 0,
            "dealer_net": 0, "divergence_signal": False,
            "reasoning": f"資料取得失敗: {exc}"
        })}
```

Apply the same pattern to: `tech_analyst_node`, `chief_strategist_node`, `portfolio_manager_node`, `format_agent_node`.

**Effect**: If one parallel branch fails (Claude 529), the other branch's result is preserved and chief_strategist can proceed with partial (degraded) input. The brief is still generated and pushed.

---

### T1-C: Add Streamlit Authentication

**File**: `dashboard.py` — entry point
**Risk fixed**: Portfolio holdings (financial data) exposed to all LAN users
**Effort**: 20 minutes

```python
# Install: uv add streamlit-authenticator
import streamlit_authenticator as stauth

# dashboard.py — add at the top of the file, before any st.* calls:
credentials = {
    "usernames": {
        os.getenv("DASH_USER", "admin"): {
            "name":     os.getenv("DASH_NAME", "Admin"),
            "password": stauth.Hasher([os.getenv("DASH_PASSWORD", "change_me")]).generate()[0],
        }
    }
}
authenticator = stauth.Authenticate(credentials, "ai_agent_studio", "auth", cookie_expiry_days=7)
name, authentication_status, _ = authenticator.login("Login", "main")
if not authentication_status:
    st.stop()
```

Add to `.env`:
```
DASH_USER=admin
DASH_NAME=管理員
DASH_PASSWORD=your_secure_password_here
```

---

## Tier 2: Short-Term — Resilience and Observability

### T2-A: LangGraph Checkpointing

**File**: `investment_workflow.py:build_graph()`
**Risk fixed**: All upstream computation lost on any node failure
**Effort**: 15 minutes

```python
# investment_workflow.py

from langgraph.checkpoint.sqlite import SqliteSaver

def build_graph(checkpointer=None):
    graph = StateGraph(WorkflowState)
    # ... all add_node and add_edge calls unchanged ...
    return graph.compile(checkpointer=checkpointer)


def main():
    # ...
    checkpointer = SqliteSaver.from_conn_string("workflow_checkpoints.db")
    graph = build_graph(checkpointer=checkpointer)

    # Use a stable thread_id so re-runs resume from the last checkpoint
    from datetime import date
    thread_id = f"daily_{date.today().isoformat()}"

    result = graph.invoke(
        initial_state,
        config={"configurable": {"thread_id": thread_id}},
    )
```

**Effect**: Each completed node's output is persisted to SQLite. If `chief_strategist` fails at 30 seconds in, a re-run resumes from `chief_strategist` directly — `data_collector`, `chip_analyst`, `tech_analyst` are not re-executed. The $0.009 spent on those nodes is not wasted.

Add `workflow_checkpoints.db` to `.gitignore`.

---

### T2-B: Per-LLM Retry with Jitter

**File**: `market_analyst_agents.py` — `_llm()` and `_llm_opus()` factories
**Risk fixed**: Single transient API error fails entire workflow; retry storm on simultaneous parallel retries
**Effort**: 15 minutes

```python
import anthropic
from langchain_core.utils.utils import build_extra_kwargs_from_env

def _llm(model: str, max_tokens: int = 1024) -> ChatAnthropic:
    return ChatAnthropic(
        model=model,
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        max_tokens=max_tokens,
    ).with_retry(
        stop_after_attempt=3,
        wait_exponential_jitter=True,       # CRITICAL: prevents retry storm on parallel branches
        retry_if_exception_type=(
            anthropic.RateLimitError,
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
        ),
    )
```

**Why `wait_exponential_jitter=True` is critical**: When `chip_analyst` and `tech_analyst` both receive a 529 simultaneously, jitter adds a random delay (e.g., 2.3s vs 4.7s) to each branch's retry, preventing them from hammering the API in synchronized waves.

---

### T2-C: Add `trade_date` to WorkflowState

**File**: `market_analyst_agents.py:30`, `data_collector_node`, `save_to_db_node`
**Risk fixed**: Fragile `snapshot["timestamp"][:10]` coupling; future schema changes break silently
**Effort**: 15 minutes

```python
class WorkflowState(TypedDict):
    snapshot:         dict
    trade_date:       str            # ← ADD: populated by data_collector
    raw_market_data:  dict
    chip_report:      str
    tech_report:      str
    final_brief:      str
    final_report:     str
    db_row_id:        Optional[int]
    portfolio_advice: str

def data_collector_node(state: WorkflowState) -> dict:
    # ... existing logic ...
    trade_date = state["snapshot"]["timestamp"][:10]  # extract here, once
    return {"raw_market_data": raw_market_data, "trade_date": trade_date}

def save_to_db_node(state: WorkflowState) -> dict:
    trade_date = date.fromisoformat(state["trade_date"])  # use state field, not snapshot
    # ... rest unchanged ...
```

---

### T2-D: Persist Backtest Accuracy Report

**File**: `backtest_agent.py` — add `save_accuracy_node`
**Risk fixed**: Accuracy history not queryable; dashboard computes KPIs from raw data instead of Claude's nuanced evaluation
**Effort**: 30 minutes

```python
# database_tools.py — add function:
def save_accuracy_report(trade_date: date, score: int, report_text: str) -> None:
    with _engine().begin() as conn:
        conn.execute(text("""
            INSERT INTO accuracy_logs (trade_date, score, report_text)
            VALUES (:d, :score, :report)
            ON DUPLICATE KEY UPDATE score=:score, report_text=:report
        """), {"d": trade_date, "score": score, "report": report_text})

# backtest_agent.py — add node before END:
def save_accuracy_node(state: BacktestState) -> dict:
    report = state["accuracy_report"]
    score_match = re.search(r"綜合評分[：:]\s*(\d+)", report)
    score = int(score_match.group(1)) if score_match else -1
    try:
        from database_tools import save_accuracy_report
        save_accuracy_report(date.fromisoformat(state["trade_date"]), score, report)
        logger.success(f"[SaveAccuracy] 評估報告已儲存 score={score}")
    except Exception as exc:
        logger.warning(f"[SaveAccuracy] 儲存失敗（繼續）: {exc}")
    return {}

# Graph:
# evaluate → save_accuracy → END
```

---

### T2-E: Commit `uv.lock` and Fix Stale `daily_run.sh`

**Effort**: 10 minutes

```bash
# On remote server: generate lockfile
cd /home/itadmin/ai_agent_studio && uv lock

# Copy to local repo:
scp ai-agents-server:/home/itadmin/ai_agent_studio/uv.lock .

# Update local daily_run.sh to match remote (3-step, no duplicate push)
# Then commit both:
git add uv.lock daily_run.sh
git commit -m "Add uv.lock for reproducible builds; sync daily_run.sh to remote version"
git push
```

---

## Tier 3: Structural — Architecture Changes

These require more planning but yield the highest long-term value.

### T3-A: Add Conditional Routing for Data Quality

Currently, if `data_collector` returns `{}` (JSON parse failure), all downstream nodes silently use raw snapshot data or return degraded output. The graph has no way to indicate "today's data quality is LOW" to users.

**Proposed change**: Add a `quality_router` conditional edge after `data_collector`:

```python
def quality_router(state: WorkflowState) -> str:
    raw = state.get("raw_market_data", {})
    if not raw or not raw.get("data_ok"):
        return "low_quality_path"     # skip LLM analysis, send warning push
    return "normal_path"

graph.add_conditional_edges(
    "data_collector",
    quality_router,
    {"normal_path": "chip_analyst", "low_quality_path": "send_notification"},
)
```

**Effect**: If market data is unavailable, the system pushes a "資料取得失敗" notification instead of generating a low-quality brief. Users receive a useful signal instead of a potentially misleading analysis.

---

### T3-B: Add Human-in-the-Loop Interrupt Before Notification

For a trading signal system, sending an incorrect recommendation to a user's LINE has real financial consequences. Consider adding an interrupt point before `send_notification` for manual review during high-volatility periods:

```python
# Compile with interrupt:
graph.compile(
    checkpointer=SqliteSaver.from_conn_string("workflow_checkpoints.db"),
    interrupt_before=["send_notification"],   # pause here, await human approval
)

# Workflow:
# 1. graph.invoke() runs through format_agent → save_to_db → PAUSE
# 2. Human reviews brief in Streamlit or CLI
# 3. graph.invoke(None, config={"configurable": {"thread_id": ...}}) resumes → send_notification
```

This pattern is especially valuable during:
- Taiwan election days
- Fed rate decision days
- Major TSMC earnings announcements

---

### T3-C: Shared SQLAlchemy Engine Singleton

**File**: `database_tools.py`
**Risk fixed**: New SQLAlchemy connection pool created on every DB call (6+ times per workflow run)

```python
# database_tools.py — replace _engine() per-call pattern:
from functools import lru_cache

@lru_cache(maxsize=1)
def _get_engine():
    url = "mysql+pymysql://{user}:{password}@{host}:{port}/{db}".format(
        user=os.getenv("TIDB_USER", "root"),
        password=os.getenv("TIDB_PASSWORD", ""),
        host=os.getenv("TIDB_HOST", "127.0.0.1"),
        port=os.getenv("TIDB_PORT", "4000"),
        db=os.getenv("TIDB_DB", "agent_memory"),
    )
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=2)
```

`lru_cache(maxsize=1)` ensures only one engine is ever created per process. All DB functions share the same connection pool, preventing connection exhaustion when dashboard and workflow run concurrently.

---

### T3-D: Add UNIQUE Constraint on `daily_briefs.trade_date`

**Risk fixed**: Re-running workflow on same day creates duplicate rows; `get_brief()` workaround masks this

```sql
ALTER TABLE daily_briefs ADD UNIQUE INDEX uq_trade_date (trade_date);
```

Run on TiDB once. Existing duplicate rows (if any) must be deduplicated first:
```sql
DELETE b1 FROM daily_briefs b1
INNER JOIN daily_briefs b2
WHERE b1.id < b2.id AND b1.trade_date = b2.trade_date;
```

Then add the constraint. After this, re-runs on the same day will fail at the INSERT level — wrap in `ON DUPLICATE KEY UPDATE` in `save_brief()`.

---

### T3-E: Async Migration Path (Long-term)

Migrating all workflow nodes to `async def` unlocks:
- True concurrent I/O (no GIL constraint on API calls)
- Support for concurrent user workflows in the future
- Cleaner MCP integration (`await session.call_tool()` without `asyncio.run()` wrapper)

**Migration order** (to minimize risk):
1. `send_notification_node` — no LLM, just HTTP; lowest risk
2. `save_to_db_node` — DB I/O only
3. `format_agent_node` — Haiku LLM, smallest latency impact
4. `data_collector_node` — Haiku LLM
5. `chip_analyst_node` + `tech_analyst_node` — parallel Sonnet; migrate together
6. `portfolio_manager_node` — Sonnet + yfinance
7. `chief_strategist_node` — Opus; most complex, migrate last

Invoke via `await graph.ainvoke(initial_state)` in an async `main()`.

---

## Production Readiness Checklist

| Category | Check | Status | Tier |
|----------|-------|--------|------|
| **Cost** | Opus `budget_tokens` cap | ❌ | T1-A |
| **Security** | Dashboard authentication | ❌ | T1-C |
| **Resilience** | Node error boundaries | ❌ | T1-B |
| **Resilience** | LangGraph checkpointing | ❌ | T2-A |
| **Resilience** | Per-LLM retry + jitter | ❌ | T2-B |
| **Data Quality** | Unique on `daily_briefs.trade_date` | ❌ | T3-D |
| **Observability** | Accuracy logs persisted | ❌ | T2-D |
| **Observability** | `backtest_agent` cost logged | ❌ | T2-D adjacent |
| **Reproducibility** | `uv.lock` committed | ❌ | T2-E |
| **IaC** | Local `daily_run.sh` matches remote | ❌ | T2-E |
| **State** | `trade_date` in WorkflowState | ❌ | T2-C |
| **DB** | Shared engine singleton | ❌ | T3-C |
| **Routing** | Data quality conditional edge | ❌ | T3-A |
| **UX** | Human-in-the-loop interrupt | ❌ (optional) | T3-B |
| **Performance** | Async migration | ❌ (optional) | T3-E |

---

## Recommended Implementation Order

**Week 1 (before next trading day):**
1. T1-A: Cap Opus thinking — 5 min, immediate cost safety
2. T1-B: Node error boundaries — 30 min, no more total failures on 529
3. T2-A: LangGraph checkpointing — 15 min, protect $0.05+ of work per run
4. T2-B: Retry with jitter — 15 min, handle transient API errors gracefully
5. T1-C: Dashboard auth — 20 min, financial data should not be public

**Week 2:**
6. T2-C: `trade_date` in state — 15 min, removes hidden coupling
7. T2-D: Persist accuracy reports — 30 min, enables trend analysis
8. T3-D: UNIQUE on trade_date — 5 min SQL, prevents data corruption
9. T3-C: Shared DB engine — 20 min, fixes connection pool leak
10. T2-E: Commit `uv.lock` + fix `daily_run.sh` — 10 min, IaC hygiene

**Month 2 (when time allows):**
11. T3-A: Data quality conditional routing
12. T3-E: Async migration (phased, start with no-LLM nodes)
13. T3-B: Human-in-the-loop interrupt (on high-volatility days only)

**Total estimated effort for production-grade status**: ~4 hours for Weeks 1–2 items.
