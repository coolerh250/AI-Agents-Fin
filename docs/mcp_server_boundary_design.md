# MCP Server Boundary Design
**AI Agent Studio — Taiwan Stock Futures Analysis Team**  
_Plan date: 2026-05-16 | Status: DESIGN — pending implementation_

---

## Architecture Overview

Four purpose-scoped MCP servers replace the current two (`finance_mcp_server`, `system_inspector`).
Each server carries only the credentials it needs. No server has access to both DB credentials
and notification tokens simultaneously.

```
┌──────────────────────────────────────────────────────────────────────┐
│  TOOL GOVERNANCE BOUNDARY — Phase 1                                  │
│                                                                      │
│  ┌─ market_data_server ────────────┐  Env: none (public APIs)       │
│  │  get_tw_future_chips    🌐      │  Consumers: test_collection.py │
│  │  get_us_indices         🌐      │  Called by: StdioServerParams  │
│  │  get_tsm_adr            🌐      │  Phase 2 additions:            │
│  │  get_financial_news     🌐      │    get_taiex_actuals (TWSE)    │
│  └─────────────────────────────────┘    get_portfolio_pnl (yfinance)│
│                                                                      │
│  ┌─ persistence_server ────────────┐  Env: TIDB_* + MCP_WRITE_TOKEN │
│  │  save_brief        ✍️ 🔑        │  Consumers: investment_workflow │
│  │  save_actual       ✍️ 🔑        │             backtest_agent      │
│  │  get_brief         📖           │  (Phase 1: save_brief only)    │
│  │  get_recent_accuracy 📖         │  Write: MCP_WRITE_TOKEN required│
│  └─────────────────────────────────┘  Audit: tool_audit_log         │
│                                                                      │
│  ┌─ notification_server ───────────┐  Env: LINE_* + TELEGRAM_*      │
│  │  push_investment_brief 📣 🔑    │       + MCP_NOTIFY_TOKEN        │
│  │  push_raw              📣 🔑    │  Consumer: send_notification_node│
│  └─────────────────────────────────┘  Auth: MCP_NOTIFY_TOKEN        │
│                                        Dedup: by trade date          │
│                                        Audit: tool_audit_log         │
│                                                                      │
│  ┌─ system_server ─────────────────┐  Env: none                     │
│  │  get_system_stats      📖       │  Consumer: agent_orchestrator   │
│  └─────────────────────────────────┘  (rename from system_inspector) │
└──────────────────────────────────────────────────────────────────────┘

Legend: 🌐 external HTTP  📖 read-only  ✍️ write  📣 notify  🔑 auth required
```

---

## Server 1: `mcp_servers/market_data_server.py`

**Replaces**: `mcp_servers/finance_mcp_server.py` (read-only tools)  
**Purpose**: All external market data fetching — public APIs, no side effects  
**Credentials**: None (all public APIs, no auth required)  
**Consumers**: `test_collection.py`

### Tools

#### `get_tw_future_chips() → dict`

Moved from `finance_mcp_server.py` with no logic changes in Phase 1.

```python
@mcp.tool()
async def get_tw_future_chips() -> dict:
    """Fetch TAIFEX institutional traders futures open interest (三大法人).
    Scrapes https://www.taifex.com.tw. Returns {"error": true} on holiday/failure.
    Source: POST form scrape; brittle on site redesign (see tool_risk_matrix §Risk 9)."""
```

Retry logic: replace blocking `time.sleep()` with `await asyncio.sleep()` in `_aretry()`.

#### `get_us_indices() → dict`

Split from `get_us_market_summary`. Contains DJIA, NDX, SOX only.

```python
@mcp.tool()
async def get_us_indices() -> dict:
    """Fetch DJIA (^DJI), NASDAQ 100 (^NDX), PHLX SOX (^SOX) previous-day closes.
    Source: yfinance primary, Yahoo v8 fallback per symbol.
    Returns: {"djia_chg_pct": float, "ndx_chg_pct": float, "sox_chg_pct": float,
              "djia_close": float, "ndx_close": float, "sox_close": float,
              "stale_symbols": list[str]}"""
```

#### `get_tsm_adr() → dict`

Split from `get_us_market_summary`. TSM ADR only.

```python
@mcp.tool()
async def get_tsm_adr() -> dict:
    """Fetch TSMC ADR (TSM) previous-day close and change %.
    Separated from US indices to allow tech_analyst to weight TSM as a distinct signal.
    Returns: {"tsm_close": float, "tsm_chg_pct": float, "source": str}"""
```

**Rationale for split**: `get_us_market_summary` combined US indices (macro) and TSM ADR
(Taiwan-specific lead). The `chip_analyst_node` needs them as separate signals.
See `mcp_migration_plan.md §Category D1`.

#### `get_financial_news(max_items: int = 15) → dict`

Moved from `finance_mcp_server.py`. **Must include** `_sanitize_title()` filtering.

```python
@mcp.tool()
async def get_financial_news(max_items: int = 15) -> dict:
    """Fetch Taiwan stock news from Anue (鉅亨網). Titles sanitized against injection.
    Source: https://api.cnyes.com/media/api/v1/newslist/category/tw_stock
    Returns: {"items": [{"title": str, "time": str, "url": str}], "fetched_at": str}
    Risk: Anue is unofficial; titles flow into LLM prompts (sanitized at this boundary)."""
```

Title sanitization is the **primary security improvement** for this tool. Any title matching
`_INJECTION_KEYWORDS` is replaced with `"[filtered]"`. See `mcp_governance_phase1_plan.md §1A-3`.

### Phase 2 additions (not Phase 1)

```python
# Phase 2 — promoted from direct calls:
async def get_taiex_actuals(trade_date: str, max_lookback: int = 5) -> dict: ...
async def get_portfolio_pnl(cache_ttl_seconds: int = 300) -> dict: ...
```

### Credential isolation

```
market_data_server receives: PATH, HOME (runtime only)
market_data_server does NOT receive: TIDB_*, LINE_*, TELEGRAM_*, ANTHROPIC_API_KEY, MCP_*_TOKEN
```

Note: `get_portfolio_pnl` (Phase 2) will need `TIDB_*` credentials. The env builder in
`utils/mcp_env.py` will add them conditionally based on whether the tool is called.
For Phase 1, `market_data_env()` passes no credentials.

---

## Server 2: `mcp_servers/persistence_server.py`

**New file** (replaces direct `database_tools` imports for write operations)  
**Purpose**: TiDB reads and writes with auth enforcement on all mutations  
**Credentials**: `TIDB_*` + `MCP_WRITE_TOKEN`  
**Consumers**: `investment_workflow.py` (Phase 1: `save_brief` only), `backtest_agent.py` (Phase 2)

### Auth guard pattern

All write tools use the same guard at the top of their body:

```python
WRITE_TOKEN = os.getenv("MCP_WRITE_TOKEN", "")

def _check_write_auth(key: str) -> bool:
    return bool(WRITE_TOKEN) and key == WRITE_TOKEN
```

### Tools

#### `save_brief(trade_date, brief_text, predicted_gap_pct, gap_direction, _api_key) → dict`

Promoted from the orphaned `save_brief_to_db` in `finance_mcp_server`. Backed by
`database_tools.save_brief()` (which already uses ON DUPLICATE KEY UPDATE).

```python
@mcp.tool()
def save_brief(
    trade_date: str,
    brief_text: str,
    predicted_gap_pct: Optional[float],
    gap_direction: Optional[str],
    _api_key: str = "",
) -> dict:
    """Save daily investment brief to TiDB daily_briefs. Upserts on trade_date.
    Requires MCP_WRITE_TOKEN in _api_key.
    Returns: {"success": bool, "row_id": int, "error": str|None}"""
    if not _check_write_auth(_api_key):
        _log_unauthorized("persistence", "save_brief")
        return {"success": False, "row_id": 0, "error": "unauthorized"}
    with audit_tool("persistence", "save_brief"):
        row_id = _db_save_brief(trade_date, brief_text, predicted_gap_pct, gap_direction)
        return {"success": True, "row_id": row_id, "error": None}
```

#### `save_actual(trade_date, open_price, close_price, actual_gap_pct, notes, _api_key) → dict`

Phase 1 stub (declared but consumer wiring is Phase 2 — `backtest_agent.py`).

```python
@mcp.tool()
def save_actual(
    trade_date: str, open_price: float, close_price: float,
    actual_gap_pct: float, notes: str = "", _api_key: str = "",
) -> dict:
    """Upsert TAIEX actuals to market_actuals. Requires MCP_WRITE_TOKEN."""
```

#### `get_brief(trade_date: str) → dict`

Read-only. No auth required.

```python
@mcp.tool()
def get_brief(trade_date: str) -> dict:
    """Fetch most recent daily brief for trade_date (YYYY-MM-DD).
    Returns the full brief row or {"found": false} if no record."""
```

#### `get_recent_accuracy(days: int = 7) → list`

Read-only. No auth required.

```python
@mcp.tool()
def get_recent_accuracy(days: int = 7) -> list:
    """Return last N trade days joining daily_briefs + market_actuals.
    Each entry includes trade_date, predicted_gap_pct, actual_gap_pct, direction_correct."""
```

### DB implementation

The persistence_server uses `database_tools` functions directly (import at module level,
not lazy). The `_engine()` singleton should be `@lru_cache(maxsize=1)` to avoid the
per-call pool exhaustion described in `tool_risk_matrix.md §Risk 8`. This is the Phase 1
opportunity to fix it — applying `@lru_cache` inside the server rather than modifying
the shared `database_tools.py` module.

### Credential isolation

```
persistence_server receives: TIDB_HOST, TIDB_PORT, TIDB_USER, TIDB_PASSWORD, TIDB_DB,
                              MCP_WRITE_TOKEN
persistence_server does NOT receive: LINE_*, TELEGRAM_*, ANTHROPIC_API_KEY
```

---

## Server 3: `mcp_servers/notification_server.py`

**New file** (replaces direct `messenger_tools` imports in `send_notification_node`)  
**Purpose**: LINE and Telegram push with dedup, auth, and audit  
**Credentials**: `LINE_*`, `TELEGRAM_*`, `MCP_NOTIFY_TOKEN`  
**Consumers**: `investment_workflow.py` (`send_notification_node`)

### Tools

#### `push_investment_brief(brief_text, _api_key) → dict`

The primary production tool. Wraps `messenger_tools.format_brief()` + `send_brief()`.

```python
@mcp.tool()
def push_investment_brief(brief_text: str, _api_key: str = "") -> dict:
    """Format and push investment brief to all configured channels.
    Applies format_brief() transformation. Deduplicates — skips if already sent today.
    Message truncated to 4000 chars before LINE API call (LINE max: 5000 chars).
    Requires MCP_NOTIFY_TOKEN in _api_key.

    Returns: {
        "line": {"status": "ok"|"skipped"|"error", "detail": str},
        "telegram": {"status": "ok"|"skipped"|"error", "detail": str},
        "dedup_skipped": bool
    }"""
    if not _check_notify_auth(_api_key):
        _log_unauthorized("notification", "push_investment_brief")
        return {"line": {"status": "error"}, "telegram": {"status": "error"},
                "error": "unauthorized", "dedup_skipped": False}

    if _already_sent_today():
        logger.warning("[Notification] Already sent today — skipping")
        return {"line":     {"status": "skipped", "reason": "dedup"},
                "telegram": {"status": "skipped", "reason": "dedup"},
                "dedup_skipped": True}

    message = format_brief(brief_text)
    if len(message) > 4000:
        message = message[:4000]

    with audit_tool("notification", "push_investment_brief"):
        line_result = send_line(message)
        tg_result   = send_telegram(message)

    return {"line": line_result, "telegram": tg_result, "dedup_skipped": False}
```

#### `push_raw(channel, message, _api_key) → dict`

For manual alerts and testing. Not called by any cron workflow.

```python
@mcp.tool()
def push_raw(channel: str, message: str, _api_key: str = "") -> dict:
    """Push raw text to a single channel (line|telegram). For manual use only.
    message is sent as-is (no format_brief transformation). Max 4000 chars.
    Requires MCP_NOTIFY_TOKEN."""
```

### Deduplication

`_already_sent_today()` checks `tool_audit_log` for today's successful `push_investment_brief`
call. No separate `notification_log` table needed in Phase 1:

```python
def _already_sent_today() -> bool:
    from datetime import date
    with _minimal_conn() as conn:
        count = conn.execute(text("""
            SELECT COUNT(*) FROM tool_audit_log
            WHERE tool_name = 'push_investment_brief'
              AND status    = 'ok'
              AND DATE(called_at) = :d
        """), {"d": date.today()}).scalar()
    return (count or 0) > 0
```

This requires the notification_server to have read access to `tool_audit_log`.
Since `tool_audit_log` is in the same TiDB DB, the notification_server needs
`TIDB_*` credentials too — but only for dedup checking, not for brief storage.

**Revised credential list for notification_server**:

```
LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
MCP_NOTIFY_TOKEN
TIDB_HOST, TIDB_PORT, TIDB_USER, TIDB_PASSWORD, TIDB_DB  ← read-only for dedup
```

Note: This is a pragmatic compromise. The fully isolated design (notification server has no DB)
would require a separate dedup mechanism (e.g., file-based lock). For Phase 1, the
tool_audit_log approach is simpler and self-consistent.

### Credential isolation

```
notification_server receives: LINE_*, TELEGRAM_*, MCP_NOTIFY_TOKEN, TIDB_* (read-only dedup)
notification_server does NOT receive: ANTHROPIC_API_KEY, MCP_WRITE_TOKEN
```

---

## Server 4: `mcp_servers/system_server.py`

**Renamed from**: `mcp_servers/system_inspector.py`  
**Purpose**: Local system health monitoring  
**Credentials**: None  
**Consumers**: `agent_orchestrator.py`

No logic changes — rename only for consistency with the server naming scheme.
Update `agent_orchestrator.py` to reference `mcp_servers/system_server.py`.

### Tools (unchanged)

```python
@mcp.tool()
def get_system_stats() -> dict:
    """Return CPU %, memory %, and disk usage as structured JSON.
    Source: psutil (local OS, no external API). Blocks ~1 s for cpu_percent(interval=1)."""
```

---

## Deprecated: `mcp_servers/finance_mcp_server.py`

**Action after Phase 1**: Add deprecation header, keep for one sprint for parallel testing,
then delete once `test_collection.py` is verified against `market_data_server`.

Deprecation header to add immediately after 1A-1:

```python
# DEPRECATED: This server is superseded by market_data_server.py (read-only tools)
# and has had save_brief_to_db + send_brief_to_user removed.
# Scheduled for deletion after test_collection.py migration is verified.
# See docs/mcp_governance_phase1_plan.md §1B for migration plan.
```

---

## Tool Placement Decision Summary

| Tool | Phase 1 Server | Placement Rationale |
|------|---------------|---------------------|
| `get_tw_future_chips` | market_data | Read-only public API; no credentials needed |
| `get_us_indices` | market_data | Read-only; split from `get_us_market_summary` |
| `get_tsm_adr` | market_data | Read-only; separate TSM ADR signal from US indices |
| `get_financial_news` | market_data | Read-only; must be sanitized at this boundary |
| `save_brief` | persistence | DB write; auth-gated; TiDB-only credentials |
| `save_actual` | persistence | DB write (Phase 2 consumer wiring) |
| `get_brief` | persistence | DB read; co-located with write tools |
| `get_recent_accuracy` | persistence | DB read; used by backtest_agent |
| `push_investment_brief` | notification | LINE/Telegram; must be isolated from DB write creds |
| `push_raw` | notification | Manual testing; same credential scope |
| `get_system_stats` | system | psutil only; no credentials |
| `get_taiex_actuals` | market_data | **Phase 2** — currently direct call in backtest_agent |
| `get_portfolio_pnl` | market_data | **Phase 2** — requires async per-holding fetching |

### Direct calls preserved (not MCP-ified in any phase)

| Function | Reason |
|----------|--------|
| `database_tools.log_cost()` | Internal telemetry; 6 calls/run; MCP overhead unjustified |
| `database_tools.get_brief()` | Internal read within same process; no external side effects |
| `database_tools.get_portfolio()` | Internal read; dashboard + workflow use directly |
| `database_tools.log_session_episode()` | Internal telemetry |
| `messenger_tools.format_brief()` | Pure string transformation; no I/O |
| All `lesson_writer` / `lesson_retriever` functions | Internal flywheel helpers |
| All `evaluation_*` functions | Internal evaluation pipeline |
| `_llm()` / `_llm_opus()` factories | LLM invocations are the reasoning layer, not tools |

---

## Consumer Matrix (Phase 1 target state)

| Consumer | market_data | persistence | notification | system |
|----------|:-----------:|:-----------:|:------------:|:------:|
| `test_collection.py` | ✅ (3 tools) | ❌ | ❌ | ❌ |
| `investment_workflow.py` | ❌ | ✅ save_brief | ✅ push_investment_brief | ❌ |
| `backtest_agent.py` | ❌ | ❌ (Phase 2) | ❌ | ❌ |
| `agent_orchestrator.py` | ❌ | ❌ | ❌ | ✅ get_system_stats |
| `dashboard.py` | ❌ | ❌ | ❌ | ❌ |

**Zero orphaned tools**: every tool in every server has ≥1 consumer in this matrix.
`save_actual` and `get_brief`/`get_recent_accuracy` in persistence_server are declared for
Phase 2 consumers but are not orphaned — they have clear named future consumers.
