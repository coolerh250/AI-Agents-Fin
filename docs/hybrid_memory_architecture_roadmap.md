# Hybrid Memory Architecture Roadmap
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Current State: Amnesiac Agent

Every day, the chief_strategist processes a fresh market snapshot and generates analysis **with zero memory of previous sessions**. The $0.048 Opus computation that runs each morning is completely stateless — it cannot recall that:

- "SOX has been >1% for 5 consecutive sessions → previous predictions were 100% accurate in this regime"
- "Yesterday we predicted +1.2% gap but actual was -0.3% → TSM ADR was misleading that day"
- "In Q1, chip_analyst's divergence signal preceded 3 of 4 major reversals"

This is not a scaling problem — it is a quality problem. The agent operates at the level of a human analyst who reads only today's newspaper and never looks at yesterday's.

The hybrid memory architecture roadmap proposes a transition from **stateless LLM calls** to **context-aware retrieval-augmented generation** with minimal infrastructure additions.

---

## Target Architecture: Five-Tier Memory Stack

```
┌──────────────────────────────────────────────────────────────────┐
│  TIER 0 — Prompt Memory (existing, enhance)                      │
│  System prompts → versioned, parameterizable, A/B testable       │
├──────────────────────────────────────────────────────────────────┤
│  TIER 1 — In-Process State (existing, harden)                    │
│  LangGraph TypedDict + Checkpointer for fault recovery           │
├──────────────────────────────────────────────────────────────────┤
│  TIER 2 — Relational Memory (existing, fix)                      │
│  TiDB: daily_briefs, market_actuals, cost_logs, user_portfolio   │
│  Fix: singleton engine, UNIQUE trade_date, snapshot staleness    │
├──────────────────────────────────────────────────────────────────┤
│  TIER 3 — Vector Memory (NEW)                                    │
│  Embeddings of daily_briefs → semantic search for similar past   │
│  sessions; TiDB Vector or Chroma for local storage               │
├──────────────────────────────────────────────────────────────────┤
│  TIER 4 — Episodic Memory (NEW)                                  │
│  Structured session records with market regime tags;             │
│  Retrieved by condition similarity to inject into LLM context    │
└──────────────────────────────────────────────────────────────────┘
```

---

## Phase 0: Foundation Fixes (Week 1, ~3 hours total)

These are prerequisite repairs that unblock Tier 2 and Tier 3 work. None add new features — they fix correctness bugs in the existing memory system.

### 0-A: Singleton Engine (30 min)

**File**: `database_tools.py:16`

```python
from functools import lru_cache

@lru_cache(maxsize=1)
def _engine() -> Engine:
    host     = os.getenv("TIDB_HOST", "127.0.0.1")
    port     = os.getenv("TIDB_PORT", "4000")
    user     = os.getenv("TIDB_USER", "root")
    password = os.getenv("TIDB_PASSWORD", "")
    db       = os.getenv("TIDB_DB", "agent_memory")
    url      = f"mysql+pymysql://{user}:{password}@{host}:{port}/{db}?charset=utf8mb4"
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 10, "read_timeout": 30},
    )
```

**Impact**: Eliminates connection pool proliferation. All DB calls share one pool. Removes 25–50ms cold-start overhead per call.

---

### 0-B: Snapshot Freshness Guard (15 min)

**File**: `investment_workflow.py`, after line 134

```python
from datetime import datetime, timezone, timedelta

snapshot = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
snap_ts  = datetime.fromisoformat(snapshot["timestamp"])
snap_age = datetime.now(timezone.utc) - snap_ts.replace(tzinfo=timezone.utc) if snap_ts.tzinfo is None else datetime.now(timezone.utc) - snap_ts
if snap_age > timedelta(hours=12):
    logger.error(f"Snapshot age {snap_age} exceeds 12h — run test_collection.py first")
    sys.exit(1)
logger.info(f"Snapshot loaded: {snapshot['timestamp']} (age: {snap_age})")
```

**Impact**: Prevents silently running analysis on stale data. Most impactful correctness fix.

---

### 0-C: price_stale Flag (20 min)

**File**: `portfolio_tools.py:calculate_pnl()`

```python
enriched.append({
    **h,
    "current_price":       current_price,
    "price_stale":         current_price == entry_price,  # ← ADD THIS
    "unrealized_pnl":      ...,
    ...
})
```

**File**: `market_analyst_agents.py:portfolio_manager_node()`

```python
stale = [h["stock_id"] for h in enriched if h.get("price_stale")]
if stale:
    user_content = f"⚠️ 以下持股現價資料失效（yfinance 無法取得），以成本價代替：{stale}\n\n" + user_content
```

**Impact**: LLM can now reason correctly about price data quality instead of treating stale prices as valid.

---

### 0-D: UNIQUE trade_date on daily_briefs (10 min)

```sql
ALTER TABLE daily_briefs
ADD UNIQUE KEY uq_trade_date (trade_date);
```

Update `save_brief()` to use INSERT ... ON DUPLICATE KEY UPDATE instead of plain INSERT:

```python
conn.execute(text("""
    INSERT INTO daily_briefs (trade_date, brief_text, predicted_gap_pct, gap_direction)
    VALUES (:d, :brief, :gap_pct, :direction)
    ON DUPLICATE KEY UPDATE
        brief_text       = VALUES(brief_text),
        predicted_gap_pct = VALUES(predicted_gap_pct),
        gap_direction    = VALUES(gap_direction)
"""), ...)
```

**Impact**: Running the workflow twice on the same day is now idempotent. No ghost rows in accuracy analytics.

---

### 0-E: LangGraph Checkpointer (30 min)

**File**: `investment_workflow.py`

```python
from langgraph.checkpoint.sqlite import SqliteSaver

def build_graph():
    memory = SqliteSaver.from_conn_string("checkpoints.db")
    graph = StateGraph(WorkflowState)
    # ... add nodes and edges ...
    return graph.compile(checkpointer=memory)

def main():
    ...
    graph = build_graph()
    trade_date_str = snapshot["timestamp"][:10]
    config = {"configurable": {"thread_id": trade_date_str}}
    result = graph.invoke(initial_state, config=config)
```

**Impact**: If the workflow crashes after chief_strategist completes, re-running on the same day resumes from `save_to_db_node` — the $0.048 Opus computation is not lost.

---

## Phase 1: Structured Episodic Memory (Month 1, ~6 hours)

### Goal

Move from the current write-only episodic store (`collection_journal.jsonl`, `brief_*.txt`) to a **structured, queryable episodic memory** that captures the full session context in TiDB.

### New Table: session_episodes

```sql
CREATE TABLE session_episodes (
    id                BIGINT AUTO_INCREMENT PRIMARY KEY,
    trade_date        DATE NOT NULL,
    UNIQUE KEY uq_episode_date (trade_date),

    -- Market inputs (numeric, queryable without embedding)
    foreign_oi_net    INT,
    trust_oi_net      INT,
    dealer_oi_net     INT,
    djia_chg_pct      FLOAT,
    ndx_chg_pct       FLOAT,
    sox_chg_pct       FLOAT,
    tsm_adr_chg_pct   FLOAT,

    -- Prediction outputs
    predicted_direction VARCHAR(10),     -- up / flat / down
    predicted_gap_pct   FLOAT,

    -- Validation (filled by backtest_agent)
    actual_direction    VARCHAR(10),
    actual_gap_pct      FLOAT,
    direction_correct   TINYINT(1),      -- 1 = correct, 0 = wrong, NULL = not yet evaluated
    gap_error_abs       FLOAT,           -- |predicted - actual|

    -- Market regime tags (derived, for retrieval filtering)
    regime_sox          VARCHAR(20),     -- 'strong_up' / 'mild_up' / 'flat' / 'down'
    regime_foreign_oi   VARCHAR(20),     -- 'extreme_short' / 'short' / 'neutral' / 'long'
    divergence_signal   TINYINT(1),

    -- Full text references
    brief_id            BIGINT,          -- FK to daily_briefs.id
    collection_latency  FLOAT,           -- Overall MCP collection latency
    workflow_cost_usd   FLOAT,           -- Sum of cost_logs for this trade_date

    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_trade_date (trade_date),
    INDEX idx_direction_correct (direction_correct),
    INDEX idx_regime (regime_sox, regime_foreign_oi)
);
```

### Populating session_episodes

In `investment_workflow.py`, after `save_to_db_node`, add a `save_episode_node`:

```python
def save_episode_node(state: WorkflowState) -> dict:
    from database_tools import save_episode
    raw = state.get("raw_market_data", {})

    # Derive regime tags
    sox = raw.get("sox_chg_pct", 0)
    regime_sox = "strong_up" if sox > 1.5 else "mild_up" if sox > 0.5 else "down" if sox < -0.5 else "flat"

    foreign_oi = raw.get("foreign_oi_net", 0)
    regime_fi  = "extreme_short" if foreign_oi < -30000 else "short" if foreign_oi < -10000 else "long" if foreign_oi > 0 else "neutral"

    save_episode(
        trade_date=date.fromisoformat(state["snapshot"]["timestamp"][:10]),
        market_inputs=raw,
        regime_sox=regime_sox,
        regime_fi=regime_fi,
        predicted_direction=...,  # from tech_report JSON
        predicted_gap_pct=...,
        brief_id=state.get("db_row_id"),
    )
```

### Structured Retrieval (available immediately after Phase 1)

```python
def get_similar_regime_episodes(
    regime_sox: str,
    regime_fi: str,
    limit: int = 5
) -> list[dict]:
    sql = """
        SELECT e.*, b.brief_text
        FROM session_episodes e
        LEFT JOIN daily_briefs b ON e.brief_id = b.id
        WHERE e.regime_sox = :sox
          AND e.regime_foreign_oi = :fi
          AND e.direction_correct IS NOT NULL
        ORDER BY e.trade_date DESC
        LIMIT :n
    """
    ...
```

This enables injecting historical precedents into the chief_strategist prompt:
```python
# Before invoking chief_strategist_node:
similar = get_similar_regime_episodes(regime_sox, regime_fi, limit=3)
if similar:
    historical_context = "\n".join([
        f"[{s['trade_date']}] SOX={s['sox_chg_pct']:+.1f}%, 外資OI={s['foreign_oi_net']:,} → "
        f"預測:{s['predicted_direction']} 實際:{s['actual_direction']} "
        f"({'✓' if s['direction_correct'] else '✗'}, 誤差:{s['gap_error_abs']:.2f}%)"
        for s in similar
    ])
    user_content = f"歷史相似盤勢（近3次同regime）：\n{historical_context}\n\n" + user_content
```

**Expected quality improvement**: Chief strategist can now reference "last 3 times SOX was strong and foreign OI was short, we overestimated gap. Adjust accordingly."

---

## Phase 2: Vector Memory (Month 2–3, ~12 hours)

### Goal

Enable semantic retrieval of past sessions by market condition similarity, beyond the rule-based regime tags introduced in Phase 1.

### Architecture Choice

**Option A: TiDB Vector extension**
- Pros: No new infrastructure; one DB for everything; TiDB Cloud has vector search support
- Cons: TiDB vector search is newer, less battle-tested than Chroma; requires TiDB >= 7.4

**Option B: Local Chroma**
- Pros: Mature, Python-native, runs locally; no cloud dependency
- Cons: Second storage system; needs sync with TiDB

**Recommended**: TiDB Vector for this use case (single-user, low volume, already on TiDB).

### Embedding Model

Use Claude's built-in text embeddings or a local model:

```python
# Option A: Voyage AI embeddings (Anthropic-affiliated, fast, cheap)
from anthropic import Anthropic
client = Anthropic()

def embed_session(text: str) -> list[float]:
    response = client.beta.embeddings.create(
        model="voyage-3",
        input=text,
        input_type="document"
    )
    return response.data[0].embedding

# Option B: Local sentence-transformers (no API cost, no network dependency)
from sentence_transformers import SentenceTransformer
model = SentenceTransformer("paraphrase-multilingual-mpnet-base-v2")

def embed_session(text: str) -> list[float]:
    return model.encode(text).tolist()
```

### What to Embed

**Document**: A compact representation of each session episode combining market inputs, regime, and prediction:

```python
def _session_to_embed_text(episode: dict) -> str:
    return (
        f"交易日 {episode['trade_date']}: "
        f"外資OI {episode['foreign_oi_net']:+,d}口 ({episode['regime_foreign_oi']}), "
        f"台積電ADR {episode['tsm_adr_chg_pct']:+.2f}%, "
        f"SOX {episode['sox_chg_pct']:+.2f}% ({episode['regime_sox']}), "
        f"那斯達克 {episode['ndx_chg_pct']:+.2f}%. "
        f"預測:{episode['predicted_direction']} ({episode['predicted_gap_pct']:+.2f}%). "
        f"實際:{episode['actual_direction']} ({episode['actual_gap_pct']:+.2f}%). "
        f"準確:{episode['direction_correct']}."
    )
```

### Embedding Table (TiDB Vector)

```sql
-- Requires TiDB 7.4+ with vector extension
CREATE TABLE session_embeddings (
    id          BIGINT PRIMARY KEY,                  -- FK to session_episodes.id
    trade_date  DATE NOT NULL,
    embedding   VECTOR(1024) NOT NULL,               -- dimension depends on model
    VECTOR INDEX idx_embedding (embedding) USING HNSW
);
```

### Semantic Retrieval Example

```python
def get_k_similar_sessions(
    current_embed: list[float],
    k: int = 5,
    min_date: str = "2020-01-01"
) -> list[dict]:
    """Retrieve K most similar past sessions by cosine similarity."""
    sql = """
        SELECT e.id, e.trade_date, e.direction_correct, e.gap_error_abs,
               e.predicted_direction, e.actual_direction,
               VEC_COSINE_DISTANCE(se.embedding, :query_vec) AS distance
        FROM session_embeddings se
        JOIN session_episodes e ON se.id = e.id
        WHERE e.direction_correct IS NOT NULL
          AND e.trade_date >= :min_date
        ORDER BY distance
        LIMIT :k
    """
    ...
```

### Integration with chief_strategist_node

```python
def chief_strategist_node(state: WorkflowState) -> dict:
    raw = state.get("raw_market_data", {})

    # Build today's context embedding
    today_text = _build_today_context(raw)
    today_embed = embed_session(today_text)

    # Retrieve 5 most similar past sessions
    similar = get_k_similar_sessions(today_embed, k=5)
    historical_section = _format_historical_context(similar)

    user_content = (
        f"【歷史相似盤勢（向量檢索 Top-5）】\n{historical_section}\n\n"
        f"籌碼面報告：\n{state['chip_report']}\n\n"
        f"技術面報告：\n{state['tech_report']}"
    )
    # ... rest of node unchanged
```

**Expected quality improvement**: Chief strategist can reference the 5 most similar sessions from history, including their outcomes. If today's market exactly resembles a day that resulted in a false positive for "strong up", the strategist can hedge the prediction.

---

## Phase 3: Adaptive Prompt Memory (Month 4–6, ~8 hours)

### Goal

The system prompts (`_CHIP_SYSTEM`, `_TECH_SYSTEM`, `_CHIEF_SYSTEM`) currently encode static rules. This phase makes them **adaptable** based on backtest performance feedback.

### Approach: Prompt Versioning + Performance Tracking

**New table: prompt_versions**
```sql
CREATE TABLE prompt_versions (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    prompt_name  VARCHAR(50) NOT NULL,    -- '_CHIP_SYSTEM', '_TECH_SYSTEM', etc.
    version      INT NOT NULL,
    prompt_text  TEXT NOT NULL,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    deprecated_at TIMESTAMP,
    UNIQUE KEY uq_prompt_version (prompt_name, version)
);
```

**New table: prompt_performance**
```sql
CREATE TABLE prompt_performance (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    prompt_version_id BIGINT NOT NULL,
    trade_date      DATE NOT NULL,
    direction_correct TINYINT(1),
    gap_error_abs   FLOAT,
    cost_usd        FLOAT,
    INDEX idx_prompt_version (prompt_version_id)
);
```

### Adaptive Loop

```
Every 20 trading days (monthly):
1. Query: accuracy rate for current prompt version
2. If accuracy < 55% for 3 consecutive weeks:
   a. Run backtest_agent on last 20 days with detailed error analysis
   b. Send analysis to Claude Opus with prompt engineering task:
      "Current chip analyst prompt achieves 52% accuracy.
       Here are the last 20 prediction errors: [...]
       Propose a revised _CHIP_SYSTEM prompt."
   c. Store proposed version in prompt_versions table
   d. A/B test new prompt for 5 days
   e. If improvement, promote; else revert
```

This is the most complex phase — it requires careful guardrails to prevent prompt drift. Recommended as a future research item rather than near-term production work.

---

## Roadmap Timeline

| Phase | Name | Effort | Value | When |
|-------|------|--------|-------|------|
| **0-A** | Singleton engine | 30 min | Connection stability | Week 1 |
| **0-B** | Snapshot freshness | 15 min | Correctness fix | Week 1 |
| **0-C** | price_stale flag | 20 min | P&L accuracy | Week 1 |
| **0-D** | UNIQUE trade_date | 10 min | Data integrity | Week 1 |
| **0-E** | LangGraph checkpointer | 30 min | Fault recovery | Week 1 |
| **1** | session_episodes table | 4 hr | Regime-based retrieval | Month 1 |
| **1+** | Historical context injection | 2 hr | +5–10% accuracy | Month 1 |
| **2** | Embeddings + vector search | 8 hr | Semantic retrieval | Month 2–3 |
| **2+** | RAG in chief_strategist | 4 hr | +10–20% accuracy | Month 3 |
| **3** | Adaptive prompt versioning | 8 hr | Self-improving system | Month 4–6 |

---

## Expected Quality Improvements

| Memory Tier Added | Mechanism | Expected Accuracy Improvement |
|------------------|----------|------------------------------|
| Phase 0 (fixes only) | Correctness, no stale data | Baseline accuracy now reliable |
| Phase 1 (regime tags) | Rule-based historical context | +5–8% direction accuracy |
| Phase 2 (vector retrieval) | Semantic similarity to past sessions | +10–15% direction accuracy |
| Phase 3 (adaptive prompts) | Feedback-driven prompt tuning | +3–7% direction accuracy |

**Cumulative**: From unreliable baseline → potentially +20–30% improvement in prediction accuracy by consistently feeding the strategist agent relevant historical context it currently never has access to.

---

## Architecture Comparison

| Dimension | Current | Phase 1 | Phase 2 | Phase 3 |
|-----------|---------|---------|---------|---------|
| Memory types | Short-term + relational | + Episodic | + Vector | + Adaptive |
| Retrieval | None | Exact-match SQL | Cosine similarity | Feedback loop |
| Chief strategist context | Today only | Last 3 similar sessions | Top-5 similar sessions | Tuned for regime |
| Infrastructure added | None | 1 new table | Vector index | 2 new tables |
| Monthly cost impact | $0 | +$0.10 (DB storage) | +$0.50–$2 (embeddings) | +$1–5 (A/B testing) |
| Development risk | Low | Low | Medium | High |

---

## Guiding Principle

> The current system is not memory-limited — it is memory-blind. Adding vector retrieval without first fixing the Phase 0 foundations (stale snapshots, connection pooling, duplicate rows) will build expensive infrastructure on a broken base. Fix the foundation in Week 1, then add intelligence incrementally.
