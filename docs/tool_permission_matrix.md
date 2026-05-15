# Tool Permission Matrix
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Permission Levels (Proposed)

| Level | Symbol | Definition | Examples |
|-------|--------|-----------|---------|
| **READ** | 📖 | Read-only access to external state; no side effects | `get_system_stats`, `get_brief()` |
| **WRITE** | ✍️ | Modifies internal state (database rows) | `save_brief()`, `log_cost()` |
| **NOTIFY** | 📣 | Sends messages to external users | `send_line()`, `send_telegram()` |
| **EXTERNAL_READ** | 🌐 | Fetches data from external HTTP APIs | `get_tw_future_chips`, `get_taiex_actuals` |
| **EXECUTE** | ⚙️ | Runs subprocesses or spawns external processes | `asyncio.run(_fetch_mcp_stats())` |

---

## Full Permission Matrix

### Column definitions:
- **Cron (08:00)**: `test_collection.py` — runs as cron, no human in loop
- **Cron (08:20)**: `investment_workflow.py` — main production run
- **Cron (09:00)**: `backtest_agent.py` — post-market evaluation
- **Dashboard**: `dashboard.py` — Streamlit, LAN-accessible, unauthenticated
- **Maintenance**: `agent_orchestrator.py` — manual CLI run
- **MCP Client**: Any future MCP-capable client (Claude Desktop, API agent)

| Tool / Function | Permission | Cron 08:00 | Cron 08:20 | Cron 09:00 | Dashboard | Maintenance | MCP Client | Auth Today | Auth Needed |
|----------------|-----------|:----------:|:----------:|:----------:|:---------:|:-----------:|:----------:|-----------|------------|
| **MCP Tools** | | | | | | | | | |
| `get_system_stats` | 📖 EXTERNAL_READ | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ | None | None (local) |
| `get_tw_future_chips` | 🌐 EXTERNAL_READ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ | None | Rate limit |
| `get_us_market_summary` | 🌐 EXTERNAL_READ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ | None | Rate limit |
| `get_financial_news` | 🌐 EXTERNAL_READ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ | None | Content filter |
| `save_brief_to_db` ⚠️ | ✍️ WRITE | ❌ | ❌ | ❌ | ❌ | ❌ | ⚠️ ANY | None | **API key required** |
| `send_brief_to_user` ⚠️ | 📣 NOTIFY | ❌ | ❌ | ❌ | ❌ | ❌ | ⚠️ ANY | None | **API key required** |
| **Database Functions** | | | | | | | | | |
| `get_brief()` | 📖 READ | ❌ | ❌ | ✅ | ✅ | ❌ | ❌ | None | OK |
| `get_actual()` | 📖 READ | ❌ | ❌ | ✅ | ✅ | ❌ | ❌ | None | OK |
| `get_cost_summary()` | 📖 READ | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ | None | OK |
| `get_cost_trend()` | 📖 READ | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ | None | OK |
| `get_portfolio()` | 📖 READ | ❌ | ✅ | ❌ | ✅ | ❌ | ❌ | None | OK |
| `get_recent_accuracy()` | 📖 READ | ❌ | ❌ | ✅ | ✅ | ❌ | ❌ | None | OK |
| `save_brief()` | ✍️ WRITE | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | None | Workflow-only |
| `save_actual()` | ✍️ WRITE | ❌ | ❌ | ✅ | ✅ (manual) | ❌ | ❌ | None | Workflow-only |
| `log_cost()` | ✍️ WRITE | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | None | Workflow-only |
| `add_portfolio_item()` | ✍️ WRITE | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ | None | **Auth required** |
| `update_portfolio_item()` | ✍️ WRITE | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ | None | **Auth required** |
| `delete_portfolio_item()` | ✍️ WRITE | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ | None | **Auth required** |
| **Portfolio & Notification** | | | | | | | | | |
| `calculate_pnl()` | 🌐 EXTERNAL_READ | ❌ | ✅ | ❌ | ✅ | ❌ | ❌ | None | Cache+fallback flag |
| `send_line()` | 📣 NOTIFY | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | Bearer token | Rate limit, dedup |
| `send_telegram()` | 📣 NOTIFY | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | Bearer token | Rate limit, dedup |
| `send_brief()` | 📣 NOTIFY | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | Bearer token | Rate limit, dedup |
| **Market Data** | | | | | | | | | |
| `get_taiex_actuals()` | 🌐 EXTERNAL_READ | ❌ | ❌ | ✅ | ❌ | ❌ | ❌ | None (verify=False) | Fix TLS |
| **LLM Invocations** | | | | | | | | | |
| `_llm()` (Haiku/Sonnet) | ⚙️ EXECUTE | ❌ | ✅ | ✅ | ❌ | ✅ | ❌ | API key | Budget cap |
| `_llm_opus()` | ⚙️ EXECUTE | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | API key | **budget_tokens cap** |

---

## Current vs Required Permission Controls

### Tools with Insufficient Permission Controls Today

#### 1. `save_brief_to_db` and `send_brief_to_user` (MCP)

**Current**: No authentication. Any MCP client that connects to `finance_mcp_server.py` can call these tools.

**Required**:
```python
# Minimum viable auth guard:
@mcp.tool()
def save_brief_to_db(trade_date: str, brief_text: str, ..., _token: str = "") -> dict:
    expected = os.getenv("MCP_WRITE_TOKEN", "")
    if not expected or _token != expected:
        logger.warning(f"[save_brief_to_db] Unauthorized call attempt")
        return {"success": False, "error": "unauthorized"}
    # ... proceed
```

**Environment variable to add**:
```
MCP_WRITE_TOKEN=<random 32-char hex>
```

#### 2. Dashboard DB Writes (Portfolio Management)

**Current**: No authentication. Any LAN user at `:8501` can add/delete/modify holdings.

**Required**: Streamlit authenticator (T1-C from production recommendation). After auth is added, portfolio CRUD forms are only reachable by authenticated users.

Additionally, server-side validation is needed (Streamlit's `min_value`/`max_value` are client hints only):
```python
# Before calling add_portfolio_item():
if not re.match(r'^[0-9A-Z]{4,6}$', new_stock.strip()):
    st.error("股票代碼格式不正確（4–6位英數字）")
    st.stop()
if new_entry < 0.01 or new_entry > 100000:
    st.error("成本價超出有效範圍")
    st.stop()
```

#### 3. Notification Tools (`send_line`, `send_telegram`)

**Current**: Only called by `send_notification_node`, which runs as part of the production workflow. No per-call auth — auth is implicit (tokens must be set for the call to do anything).

**Required addition**: A **deduplication guard** to prevent double-pushing if the workflow is run twice on the same day:
```python
# In send_notification_node or messenger_tools:
def _already_sent_today() -> bool:
    """Check cost_logs or a dedicated sent_log table for today's notification."""
    from database_tools import _engine
    from datetime import date
    with _engine().connect() as conn:
        count = conn.execute(text(
            "SELECT COUNT(*) FROM notification_log WHERE sent_date = :d"
        ), {"d": date.today()}).scalar()
    return count > 0
```

#### 4. LLM Invocations — No Cost Cap

**Current**: `_llm()` (Haiku/Sonnet) has no per-call token budget enforcement. `_llm_opus()` has `max_tokens=16000` (8× needed).

**Required**: Cap at realistic values:
- Haiku: `max_tokens=1024` ✅ (already correct in most nodes)
- Sonnet: `max_tokens=1024` ✅ (correct)
- Opus: `max_tokens=2048` + `thinking={"type": "enabled", "budget_tokens": 5000}` ❌ (not yet applied)

---

## Validation Mechanism Analysis

| Tool | Input Validation | Type | Gap |
|------|-----------------|------|-----|
| `get_tw_future_chips` | None | 🔴 None | `date_str` uses today's date only; no param validation |
| `get_us_market_summary` | None | 🔴 None | Symbol list hardcoded; no validation needed |
| `get_financial_news` | None | 🔴 None | No param; output titles not sanitized |
| `save_brief_to_db` | `date.fromisoformat(trade_date)` raises on bad format | 🟡 Partial | No auth; no max length on brief_text |
| `send_brief_to_user` | None | 🔴 None | No auth; no max length on brief_text |
| `save_brief()` | `date` type enforced by function signature | 🟠 Type only | No max length on brief_text |
| `add_portfolio_item()` | Streamlit UI hints (client-side) | 🔴 None | No server-side format check on stock_id |
| `calculate_pnl()` | None | 🔴 None | stock_id.TW passed directly to yfinance |
| `send_line()` | Empty check: `if not token or not user_id` | 🟠 Partial | No message length check (LINE max 5000 chars) |

---

## Timeout Handling Analysis

| Tool | Has Timeout? | Value | Risk |
|------|-------------|-------|------|
| `get_tw_future_chips` | ✅ | httpx 15.0 s | OK |
| `get_us_market_summary` | ✅ | httpx 10.0 s (v8 fallback) | yfinance has no explicit timeout |
| `get_financial_news` | ✅ | httpx 10.0 s | OK |
| `get_taiex_actuals` | ✅ | httpx 10.0 s | OK |
| `send_line()` | ✅ | httpx 10.0 s | OK |
| `send_telegram()` | ✅ | httpx 10.0 s | OK |
| `calculate_pnl()` via yfinance | ❌ | None | yfinance `.history()` has no configurable timeout |
| `database_tools._engine()` | ❌ | None | SQLAlchemy default: no connect_timeout, no statement_timeout |
| `_llm_opus().invoke()` | ❌ | None (Anthropic default ~600 s) | OK for current use; would block thread |

**Recommended DB timeout addition**:
```python
def _engine() -> Engine:
    url = f"mysql+pymysql://..."
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": 10,      # fail fast on network issue
            "read_timeout": 30,         # prevent hung queries
        }
    )
```

---

## Audit Logging Analysis

| Tool | Audit Trail? | Where? | Gap |
|------|-------------|--------|-----|
| All LLM nodes | ✅ `cost_logs` table | TiDB | Good coverage |
| `send_line()` | ✅ loguru `logger.success/warning` | stdout only | Not persisted |
| `send_telegram()` | ✅ loguru | stdout only | Not persisted |
| `save_brief()` | ❌ No log | — | Should log `trade_date`, `row_id` to `cost_logs` or separate table |
| `save_actual()` | ✅ loguru | stdout only | Not persisted |
| `get_tw_future_chips` | ✅ MCP server loguru | stderr only | Not persisted |
| `add_portfolio_item()` | ❌ No log | — | Portfolio mutations not audited |
| `delete_portfolio_item()` | ❌ No log | — | Portfolio mutations not audited |
| MCP tool invocations | ❌ No invocation log | — | No record of which tools were called, when, by whom |

**Minimum audit table for notifications**:
```sql
CREATE TABLE notification_log (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    sent_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sent_date   DATE NOT NULL,
    channel     VARCHAR(20) NOT NULL,
    status      VARCHAR(10) NOT NULL,
    message_len INT,
    INDEX idx_sent_date (sent_date)
);
```

---

## Permission Control Implementation Priority

| Priority | Control | Tool(s) | Effort | Risk Removed |
|----------|---------|---------|--------|-------------|
| **P0** | Auth guard on orphaned MCP write tools | `save_brief_to_db`, `send_brief_to_user` | 15 min | 🔴 → 🟢 |
| **P0** | Credential isolation (env whitelist) | All MCP subprocesses | 20 min | 🔴 → 🟠 |
| **P0** | Streamlit authentication | Dashboard all DB writes | 20 min | 🟡 → 🟢 |
| **P1** | News title sanitization | `get_financial_news` output | 20 min | 🔴 → 🟠 |
| **P1** | Stock code server-side validation | `add_portfolio_item` | 10 min | 🟠 → 🟢 |
| **P1** | Notification deduplication guard | `send_notification_node` | 30 min | 🟡 → 🟢 |
| **P2** | DB connect/statement timeout | All `database_tools` | 10 min | 🟡 → 🟠 |
| **P2** | Notification audit log table | `send_line`, `send_telegram` | 30 min | Gap → tracked |
| **P2** | LINE message length check | `send_line()` | 5 min | 🟠 → 🟢 |
| **P3** | Portfolio mutation audit log | `add/delete/update_portfolio_item` | 30 min | Gap → tracked |
| **P3** | MCP tool invocation log | All MCP tools | 1 hr | Gap → tracked |
