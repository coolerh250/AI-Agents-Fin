# Privilege Boundary Analysis
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Overview

This document maps the privilege each system component actually holds against the principle of **least privilege** — what it minimally needs. Violations are flagged with the gap and a recommended fix.

---

## Privilege Matrix

### Component Definitions

| Component | What it is | Run context |
|-----------|-----------|-------------|
| `investment_workflow.py` | Main cron-triggered workflow | `itadmin` user, cron |
| `backtest_agent.py` | Post-run accuracy evaluator | `itadmin` user, manual / cron |
| `agent_orchestrator.py` | System health maintenance agent | `itadmin` user, manual |
| `dashboard.py` | Streamlit web dashboard | `itadmin` user, persistent process |
| `finance_mcp_server.py` | Finance data MCP child process | Same env as `test_collection.py` |
| `system_inspector.py` | System stats MCP child process | Same env as `agent_orchestrator.py` |
| `test_collection.py` | Data collection script | `itadmin` user, cron |

---

## 1. Database Privilege Boundaries

### Current State

All components share a single TiDB credential (`root`):

```python
# database_tools.py:16-23 — used by ALL scripts
def _engine() -> Engine:
    user = os.getenv("TIDB_USER", "root")       # root by default
    password = os.getenv("TIDB_PASSWORD", "")   # empty by default
    url = f"mysql+pymysql://{user}:{password}@..."
    return create_engine(url, pool_pre_ping=True)
```

The `root` account has `GRANT ALL PRIVILEGES ON *.*` — including `DROP TABLE`, `TRUNCATE`, `CREATE DATABASE`, and `GRANT OPTION`.

### Least-Privilege Analysis

| Component | Tables It Actually Reads | Tables It Actually Writes | Minimum Required Privilege |
|-----------|-------------------------|--------------------------|---------------------------|
| `investment_workflow.py` | `user_portfolio` | `cost_logs`, `daily_briefs`, `user_portfolio` (seed) | SELECT on `user_portfolio`; INSERT on `cost_logs`, `daily_briefs` |
| `backtest_agent.py` | `daily_briefs` | `market_actuals`, `cost_logs` (should) | SELECT on `daily_briefs`; INSERT/UPDATE on `market_actuals` |
| `agent_orchestrator.py` | None | `cost_logs` (should) | INSERT on `cost_logs` |
| `dashboard.py` | All tables | `market_actuals`, `user_portfolio` | SELECT ALL; INSERT/UPDATE/DELETE on `market_actuals`, `user_portfolio` |
| `finance_mcp_server.py` | None | `daily_briefs` (via `save_brief_to_db` orphan) | INSERT on `daily_briefs` (when used) |
| `test_collection.py` | None | None | No DB access needed |

### Privilege Gap Summary

| Gap | Current | Required | Violation |
|-----|---------|----------|-----------|
| Any component can `DROP TABLE` | `root` has DDL | No DDL needed | 🔴 CRITICAL |
| `test_collection.py` has full DB access | `root` in env | No DB access | 🟠 HIGH |
| `agent_orchestrator.py` has write access to all tables | `root` in env | System stats only | 🟠 HIGH |
| Single credential controls notification + DB + LLM | Shared `.env` | Scope-separated secrets | 🟠 HIGH |

### Recommended Fix

```sql
-- Create minimal-privilege DB users
CREATE USER 'workflow_writer'@'127.0.0.1' IDENTIFIED BY '<strong_password>';
GRANT SELECT ON agent_memory.user_portfolio TO 'workflow_writer'@'127.0.0.1';
GRANT INSERT ON agent_memory.cost_logs TO 'workflow_writer'@'127.0.0.1';
GRANT INSERT ON agent_memory.daily_briefs TO 'workflow_writer'@'127.0.0.1';

CREATE USER 'backtest_rw'@'127.0.0.1' IDENTIFIED BY '<strong_password>';
GRANT SELECT ON agent_memory.daily_briefs TO 'backtest_rw'@'127.0.0.1';
GRANT INSERT, UPDATE ON agent_memory.market_actuals TO 'backtest_rw'@'127.0.0.1';
GRANT INSERT ON agent_memory.cost_logs TO 'backtest_rw'@'127.0.0.1';

CREATE USER 'dashboard_user'@'127.0.0.1' IDENTIFIED BY '<strong_password>';
GRANT SELECT ON agent_memory.* TO 'dashboard_user'@'127.0.0.1';
GRANT INSERT, UPDATE ON agent_memory.market_actuals TO 'dashboard_user'@'127.0.0.1';
GRANT INSERT, UPDATE, DELETE ON agent_memory.user_portfolio TO 'dashboard_user'@'127.0.0.1';
-- NO DDL, NO DROP
```

Use separate env var sets per script:
```
TIDB_USER_WORKFLOW=workflow_writer
TIDB_PASS_WORKFLOW=<password>
TIDB_USER_DASHBOARD=dashboard_user
TIDB_PASS_DASHBOARD=<password>
```

---

## 2. API Key Privilege Boundaries

### Current State

Single `ANTHROPIC_API_KEY` is loaded by all scripts:

```python
# All agent scripts
ChatAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"), ...)
```

This key has:
- Full access to all Anthropic models (Haiku, Sonnet, Opus)
- No per-script spend limit
- No model restriction (could call Opus when Haiku is expected)

### Least-Privilege Analysis

| Component | Models it should use | Models it should NOT access |
|-----------|---------------------|----------------------------|
| `data_collector` | Haiku only | Sonnet, Opus |
| `chip_analyst`, `tech_analyst` | Sonnet only | Opus |
| `chief_strategist` | Opus | — |
| `backtest_agent` | Haiku only | Sonnet, Opus |
| `agent_orchestrator` | Haiku only | Sonnet, Opus |
| `format_agent` | Haiku only | Sonnet, Opus |
| `portfolio_manager` | Sonnet only | Opus |

### Gap: No Model-Scoped Keys

Anthropic does not currently offer per-model API keys. However:

1. **Spend limits** can be set in the Anthropic console (monthly hard cap) — not currently configured
2. **`max_tokens`** is the only per-call governor; missing on `_llm_opus()` (`max_tokens=16000` — excessively high)
3. A compromised component can call Opus (`claude-opus-4-7`) instead of Haiku, incurring 5× the cost

### Recommended Fix

```python
# market_analyst_agents.py — add budget_tokens cap
def _llm_opus() -> ChatAnthropic:
    return ChatAnthropic(
        model=_MODEL_OPUS,
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        max_tokens=4096,           # was 16000 — reduce blast radius
        thinking={"type": "enabled", "budget_tokens": 2048},  # cap thinking
    )
```

Set a monthly hard cap in the Anthropic console (recommended: $10/month for single-user system at $2.54/month expected).

---

## 3. LLM Agent Privilege Boundaries

### LangGraph Node Trust Levels

Each LangGraph node should be treated as a **trust-level boundary**: a node receives inputs and produces outputs; the next node must not blindly trust that output contains no injection.

| Node | Input Source | Output Destination | Input Trust | Output Risk |
|------|-------------|-------------------|-------------|-------------|
| `data_collector` | `market_snapshot.json` (external data) | `raw_market_data` dict | UNTRUSTED | Medium — may contain injected strings |
| `chip_analyst` | `raw_market_data` (from Haiku output) | `chip_report` string | LOW | High — free-form text sent to Opus |
| `tech_analyst` | `raw_market_data` (from Haiku output) | `tech_report` string | LOW | High — free-form text sent to Opus |
| `chief_strategist` | `chip_report` + `tech_report` (from Sonnet) | `final_brief` string | MEDIUM | High — 1:1 to push notification |
| `portfolio_manager` | `final_brief` (from Opus) + live prices | `portfolio_advice` string | MEDIUM | High — financial advice to user |
| `format_agent` | `final_brief` + `portfolio_advice` | `final_report` string | MEDIUM | CRITICAL — sent directly to LINE/Telegram |
| `save_to_db` | LangGraph state | TiDB `daily_briefs` | MEDIUM | High — stored and re-read by backtest |
| `send_notification` | `final_report` (from Haiku format_agent) | LINE/Telegram API | LOW | CRITICAL — external delivery |

### Key Violations

**Violation 1: Haiku output treated as trusted by Sonnet**

```python
# chip_analyst_node — chip_data may contain injected strings from Haiku output
chip_data = {k: raw[k] for k in ("foreign_oi_net", "trust_oi_net", "dealer_oi_net") if k in raw}
if not chip_data:
    chip_data = state["snapshot"]["tools"]["get_tw_future_chips"]["data"]  # fallback to raw
user_content = f"三大法人台指期留倉數據：\n{json.dumps(chip_data, ...)}"
```

The dict `chip_data` comes from Haiku's JSON output with no field-level type validation. If `foreign_oi_net` is a string instead of an integer, it could contain injection text.

**Violation 2: Sonnet outputs treated as trusted by Opus**

```python
# chief_strategist_node
user_content = (
    f"籌碼面報告：\n{state['chip_report']}\n\n"
    f"技術面報告：\n{state['tech_report']}"
)
```

`chip_report` and `tech_report` are raw text strings from Sonnet. No schema validation, no content policy check, no maximum length check before passing to Opus.

**Violation 3: Opus output treated as trusted by Haiku**

```python
# format_agent_node
user_content = f"原始建議書：\n{state['final_brief']}"
if portfolio_section:
    user_content += f"\n\n使用者持股診斷：\n{portfolio_section}"
```

Opus output (`final_brief`) is passed to Haiku's formatting prompt with instruction to "directly output the formatted message." If Opus output contains Telegram-formatted injection, Haiku will format it faithfully.

### Recommended Fix: Output Schema Validation at Trust Boundaries

```python
# market_analyst_agents.py — add before chip_analyst passes to chief_strategist
_CHIP_SCHEMA = {"sentiment": str, "foreign_net": int, "trust_net": int,
                "dealer_net": int, "divergence_signal": bool, "reasoning": str}

def _validate_chip_output(raw: str) -> tuple[dict, bool]:
    try:
        parsed = json.loads(raw)
        for field, expected_type in _CHIP_SCHEMA.items():
            if field not in parsed:
                return {}, False
            if not isinstance(parsed[field], expected_type):
                return {}, False
        # Strip any unexpected fields before forwarding
        return {k: parsed[k] for k in _CHIP_SCHEMA}, True
    except json.JSONDecodeError:
        return {}, False
```

For free-text outputs (chief_strategist, format_agent), enforce a maximum character length before passing to the next node:

```python
def _cap_text(text: str, max_chars: int = 8000) -> str:
    return text[:max_chars] if len(text) > max_chars else text
```

---

## 4. Network Privilege Boundaries

### Current Outbound Connections

| Destination | Protocol | Auth | Egress Control |
|------------|----------|------|----------------|
| `api.anthropic.com` | HTTPS | Bearer token | None (OS-level) |
| `www.taifex.com.tw` | HTTPS | None (public) | None |
| `api.cnyes.com` | HTTPS | None (public) | None |
| `query1.finance.yahoo.com` | HTTPS | None (public) | None |
| `api.line.me` | HTTPS | Bearer token | None |
| `api.telegram.org` | HTTPS | Bot token | None |

### Current Inbound

| Port | Service | Auth | Listener |
|------|---------|------|----------|
| 22 | SSH | Key-based (per deployment_guide.md) | 0.0.0.0 or hardened |
| 4000 | TiDB | root/empty password | 0.0.0.0 or 127.0.0.1 — **UNKNOWN** |
| 8501 | Streamlit | NONE | 0.0.0.0 — **CRITICAL** |

### Privilege Violations

1. **TiDB port binding:** If TiDB Docker container binds port 4000 to `0.0.0.0`, any host on the LAN can connect as `root` with empty password. Verify with `docker ps` and `ss -tlnp | grep 4000` on the server.

2. **Streamlit binding:** `streamlit run dashboard.py` defaults to `0.0.0.0:8501` — accessible from any LAN host, no authentication.

3. **No egress allowlist:** The Python process can make arbitrary outbound HTTPS connections. A compromised LLM output or dependency cannot exploit this directly (no code execution path), but it is not enforced.

### Recommended Fixes

```bash
# TiDB: ensure Docker only binds to localhost
# In docker-compose.yml or docker run command:
# ports: - "127.0.0.1:4000:4000"

# Streamlit: bind to localhost only and add auth
streamlit run dashboard.py --server.address 127.0.0.1
# Then access via SSH tunnel: ssh -L 8501:127.0.0.1:8501 ai-agents-server
```

---

## 5. MCP Server Privilege Boundaries

### Current State

Both MCP servers inherit the **full environment** of their parent process:

```python
# test_collection.py:47-49
server_params = StdioServerParameters(
    command="uv",
    args=["run", "mcp_servers/finance_mcp_server.py"],
    env=None,  # ← inherits full parent environment including all secrets
)
```

`finance_mcp_server.py` needs:
- HTTP access to TAIFEX, Yahoo Finance, Anue (✅ legitimate)
- TiDB credentials (`save_brief_to_db` orphan tool — but currently unused)
- LINE/Telegram tokens (`send_brief_to_user` orphan tool — but currently unused)

`system_inspector.py` needs:
- `psutil` access to CPU/memory/disk (✅ legitimate)
- **Nothing else** — but it inherits `ANTHROPIC_API_KEY`, `TIDB_PASSWORD`, `LINE_CHANNEL_ACCESS_TOKEN`, `TELEGRAM_BOT_TOKEN`

### Privilege Violation: MCP Servers Receive All Secrets

`system_inspector.py` has no legitimate need for any secret. Yet it receives all `.env` variables because `env=None` inherits the parent environment.

### Recommended Fix

```python
# agent_orchestrator.py — pass minimal environment to MCP server
import os

SYSTEM_INSPECTOR_ENV = {
    "PATH": os.environ.get("PATH", ""),
    # No API keys, no TiDB credentials, no tokens
}

server_params = StdioServerParameters(
    command="uv",
    args=["run", "mcp_servers/system_inspector.py"],
    env=SYSTEM_INSPECTOR_ENV,  # minimal env, not None
)
```

For `finance_mcp_server.py`, scope the env to only what the active tools need:

```python
FINANCE_MCP_ENV = {
    "PATH": os.environ.get("PATH", ""),
    # Only include DB/messenger creds if the orphan tools are being used
    # If orphan tools remain unused: no credentials needed
}
```

---

## Privilege Boundary Summary

| Boundary | Current Violation | Severity | Fix Effort |
|----------|-----------------|----------|------------|
| TiDB: all components use root | DDL privilege available to all | 🔴 CRITICAL | 2 hrs |
| Streamlit: no auth, write access | Any LAN user can mutate data | 🔴 CRITICAL | 2 hrs |
| LLM chain: no trust degradation between nodes | Sonnet/Haiku output trusted by Opus | 🟠 HIGH | 4 hrs |
| MCP servers inherit all secrets | system_inspector has API keys it doesn't need | 🟠 HIGH | 30 min |
| TiDB port possibly LAN-exposed | root@empty available to LAN | 🟠 HIGH | 15 min |
| Anthropic API: no model scope or spend cap | Any component can call Opus | 🟡 MEDIUM | 1 hr |
| No per-component DB credentials | One compromised component = full DB access | 🟡 MEDIUM | 2 hrs |
