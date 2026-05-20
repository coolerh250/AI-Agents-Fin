# Agent Inventory
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Summary Table

| # | Agent | File | Model | Has LLM | Memory Type |
|---|-------|------|-------|---------|-------------|
| 1 | data_collector | market_analyst_agents.py | Haiku 4.5 | ✅ | Stateless |
| 2 | chip_analyst | market_analyst_agents.py | Sonnet 4.6 | ✅ | Stateless |
| 3 | tech_analyst | market_analyst_agents.py | Sonnet 4.6 | ✅ | Stateless |
| 4 | chief_strategist | market_analyst_agents.py | Opus 4.7 + Thinking | ✅ | Stateless |
| 5 | portfolio_manager | market_analyst_agents.py | Sonnet 4.6 | ✅ | Live DB read |
| 6 | format_agent | market_analyst_agents.py | Haiku 4.5 | ✅ | Stateless |
| 7 | save_to_db | market_analyst_agents.py | — | ❌ | Write-only DB |
| 8 | send_notification | market_analyst_agents.py | — | ❌ | Stateless |
| 9 | backtest_evaluator | backtest_agent.py | Haiku 4.5 | ✅ | Read DB brief |
| 10 | maintenance_agent | agent_orchestrator.py | Haiku 4.5 | ✅ | MCP live data |

---

## 1. data_collector

| Property | Value |
|----------|-------|
| **Role** | Pre-processes the raw market snapshot JSON into a compact, structured dict for downstream agents |
| **Model** | `claude-haiku-4-5-20251001` |
| **System prompt** | Instructs extraction of 8 numeric fields into JSON (foreign/trust/dealer OI nets, US market change %s) |
| **Input** | Full `market_snapshot.json` tools section (~3 KB raw JSON) |
| **Output** | Compact JSON: `{foreign_oi_net, trust_oi_net, dealer_oi_net, djia_chg_pct, ndx_chg_pct, sox_chg_pct, tsm_adr_chg_pct, data_ok, missing_fields}` |
| **Tools used** | None (pure text transformation) |
| **Memory** | None — single-turn, no history |
| **Fallback** | If JSON parse fails → uses empty `{}`, downstream nodes fall back to raw `snapshot["tools"]` |
| **Token sensitivity** | LOW — small, structured output |
| **Cost logged** | ✅ via `_record_usage` |

---

## 2. chip_analyst

| Property | Value |
|----------|-------|
| **Role** | Interprets three-party futures open interest (三大法人籌碼) and outputs a sentiment JSON |
| **Model** | `claude-sonnet-4-6` |
| **System prompt** | Rule-based: foreign OI thresholds map to sentiment labels; divergence detection |
| **Input** | 3 numeric fields from `raw_market_data` (or raw TAIFEX data as fallback) |
| **Output** | JSON: `{sentiment, foreign_net, trust_net, dealer_net, divergence_signal, reasoning}` |
| **Tools used** | None |
| **Memory** | None — single-turn |
| **Token sensitivity** | LOW — compact input and output |
| **Cost logged** | ✅ |
| **Risk** | Output is a JSON string stored in `chip_report`; parsed downstream by `chief_strategist` as plain text (not re-parsed as JSON) |

---

## 3. tech_analyst

| Property | Value |
|----------|-------|
| **Role** | Predicts Taiwan market gap direction from prior-day US market performance |
| **Model** | `claude-sonnet-4-6` |
| **System prompt** | Weighted-average rules: DJIA 20%, NDX 25%, SOX 30%, TSMC ADR 25% |
| **Input** | 4 US market change % values |
| **Output** | JSON: `{gap_direction, estimated_gap_pct, key_driver, tsm_signal, reasoning}` |
| **Tools used** | None |
| **Memory** | None — single-turn |
| **Token sensitivity** | LOW |
| **Cost logged** | ✅ |
| **Critical output** | `gap_direction` and `estimated_gap_pct` are extracted by `save_to_db_node` for `daily_briefs` table |

---

## 4. chief_strategist

| Property | Value |
|----------|-------|
| **Role** | Integrates chip + tech reports into a structured daily investment brief |
| **Model** | `claude-opus-4-7` with Extended Thinking |
| **LLM config** | `max_tokens=16000`, `thinking={"type": "adaptive"}`, `output_config={"effort": "high"}` ⚠ |
| **System prompt** | 4-section structured output: 盤勢定調、操作策略、關鍵防守點、風險提示 |
| **Input** | `chip_report` (JSON string ~300 tokens) + `tech_report` (JSON string ~300 tokens) |
| **Output** | Prose investment brief (~500–1 000 tokens) stored in `final_brief` |
| **Tools used** | None |
| **Memory** | None — single-turn; no access to historical briefs |
| **Token sensitivity** | **HIGHEST** — Opus pricing, extended thinking, `max_tokens=16000` ceiling |
| **Cost logged** | ✅ |
| **Known issues** | 1. `output_config={"effort": "high"}` is not a valid `ChatAnthropic` parameter — silently ignored or may cause a warning. 2. `thinking={"type": "adaptive"}` — adaptive thinking budget may generate very long reasoning chains, dramatically increasing cost beyond estimated $0.048/run |

---

## 5. portfolio_manager

| Property | Value |
|----------|-------|
| **Role** | Combines market outlook with live portfolio P&L to generate per-holding buy/hold/sell advice |
| **Model** | `claude-sonnet-4-6` |
| **System prompt** | Private asset advisor — generates sell signal if market bearish + stop-loss triggered; hold otherwise |
| **Input** | `final_brief` (full prose, ~500–1 000 tokens) + enriched portfolio data (live prices from yfinance) |
| **Output** | Per-holding advice block: current price, P&L%, recommended action, reason |
| **Tools used** | None (lazy imports `portfolio_tools.get_user_portfolio()`, `calculate_pnl()`) |
| **Memory** | **Live DB read** — queries `user_portfolio` table + yfinance on every run |
| **Token sensitivity** | MEDIUM — input includes full brief prose |
| **Cost logged** | ✅ |
| **Side effect** | Calls `yfinance` during LangGraph execution — network latency directly impacts workflow wall time |

---

## 6. format_agent

| Property | Value |
|----------|-------|
| **Role** | Reformats verbose brief into a LINE-optimised ≤2 000-character mobile message |
| **Model** | `claude-haiku-4-5-20251001` |
| **System prompt** | LINE formatting rules: emoji per section, length cap, conditional 💼 portfolio section |
| **Input** | `final_brief` (full prose) + optional `portfolio_advice` |
| **Output** | `final_report` — LINE-ready text |
| **Tools used** | None |
| **Memory** | None |
| **max_tokens** | 2 048 |
| **Token sensitivity** | MEDIUM — largest input of all Haiku nodes |
| **Cost logged** | ✅ |

---

## 7. save_to_db

| Property | Value |
|----------|-------|
| **Role** | Persists `final_brief` and predicted gap direction to `daily_briefs` TiDB table |
| **Model** | None |
| **Input** | `final_brief`, `tech_report` (for gap extraction), `snapshot["timestamp"]` |
| **Output** | `db_row_id` |
| **Error handling** | Catches all exceptions; returns `db_row_id=None` on failure (non-fatal) |
| **Gap extraction** | Re-parses `tech_report` JSON string inline — duplicates logic from `tech_analyst_node` |

---

## 8. send_notification

| Property | Value |
|----------|-------|
| **Role** | Dispatches `final_report` to all configured push channels (LINE, Telegram) |
| **Model** | None |
| **Input** | `final_report` |
| **Output** | `{}` (no state change) |
| **Behaviour** | Skips gracefully if env vars not set; logs per-channel result |
| **Error handling** | Non-fatal — logs warning, does not raise |

---

## 9. backtest_evaluator

| Property | Value |
|----------|-------|
| **Role** | Compares yesterday's predicted gap vs TWSE actual, produces accuracy score 0–100 |
| **Model** | `claude-haiku-4-5-20251001` |
| **System prompt** | Structured evaluation: 預測回顧、實際走勢、準確度評分 (0–100), 反省與學習 |
| **Input** | Full `brief_text` from `daily_briefs` + TWSE actuals dict |
| **Output** | Prose accuracy report (printed to stdout, **NOT persisted to DB**) |
| **Memory** | Read DB for `brief_record`; write DB for `market_actuals` |
| **Token sensitivity** | MEDIUM — full brief text as input context |
| **Known issue** | Accuracy report is printed to console and log file only — not stored in TiDB. No historical trend queryable from DB. |

---

## 10. maintenance_agent

| Property | Value |
|----------|-------|
| **Role** | Diagnoses Ubuntu server health (CPU/memory/disk) and outputs READY/WARNING/CRITICAL |
| **Model** | `claude-haiku-4-5-20251001` |
| **System prompt** | Ubuntu AI workload maintenance expert — concise actionable recommendations |
| **Input** | Live `psutil` stats via MCP stdio call |
| **Output** | Prose analysis + `status` enum (READY/WARNING/CRITICAL/UNKNOWN) |
| **Tools used** | MCP tool `get_system_stats` (via `system_inspector.py` subprocess) |
| **Memory** | None — single diagnostic snapshot |
| **Invocation** | Manual only (`python agent_orchestrator.py`) — not in cron schedule |
| **Token sensitivity** | LOW |
| **Cost logged** | ❌ — no `_record_usage` call |

---

## Agents NOT Wired Into Any Workflow

| MCP Tool | Registered in | Should be used by | Status |
|----------|--------------|-------------------|--------|
| `save_brief_to_db` | `finance_mcp_server.py` | — | **DEAD CODE** — `save_to_db_node` calls `database_tools.save_brief` directly |
| `send_brief_to_user` | `finance_mcp_server.py` | — | **DEAD CODE** — `send_notification_node` calls `messenger_tools.send_brief` directly |
| `finance://backtest/report` | `finance_mcp_server.py` | Dashboard or external MCP client | **UNUSED RESOURCE** — no client reads it |
