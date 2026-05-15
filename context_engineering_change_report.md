# Context Engineering — Change Report
**AI Agent Studio | 2026-05-15**

---

## Summary

3 context engineering changes across 2 files. Zero breaking changes to workflow outputs.

---

## 1. SQL History Injection — chief_strategist_node

### Problem
Chief strategist (Opus + Extended Thinking, ~41% of total cost) was **completely amnesiac**: every run started with zero knowledge of past predictions or outcomes. From `context_engineering_analysis.md`:
> "Context windows massively underutilized. Opus `max_tokens=16000` wastes 8× needed."
> "No agent reads historical patterns to inform analysis."

### Change
Added a context injection block at the end of `chief_strategist_node` user content:

```python
# market_analyst_agents.py, chief_strategist_node
try:
    from database_tools import get_recent_accuracy_context
    history = get_recent_accuracy_context(days=14)
    if history:
        history = history[:_CTX_LIMIT_CHIEF_HISTORY_CHARS]
        user_content += f"\n\n{history}"
except Exception as exc:
    logger.debug(f"[ChiefStrategist] 歷史上下文載入失敗（略過）: {exc}")
```

New helper in `database_tools.py`:
```python
def get_recent_accuracy_context(days: int = 14) -> str
```
Queries `daily_briefs JOIN market_actuals` for last 14 days, returns formatted string:
```
【近期預測準確率 70% (7/10筆)】
  2026-05-14 ✓ 預測 up(+0.8%) → 實際 +1.1%
  2026-05-13 ✗ 預測 up(+0.5%) → 實際 -0.3%
  2026-05-12 ✓ 預測 flat(+0.1%) → 實際 +0.2%
  ...
```

### Backward compatibility
- Returns `""` when `market_actuals` has no joined rows (first days of operation)
- Wrapped in try/except — DB failure causes silent skip, never crashes workflow
- Adds ≤800 chars to user content (well within Opus 200K context window)

### Expected effect
- Chief strategist can now identify when recent predictions are consistently wrong in specific regimes
- Enables self-correction: "SOX strong signal predicted up correctly 4/4 times → increase confidence"
- **Estimated accuracy improvement: +5–8% direction accuracy** (from `hybrid_memory_architecture_roadmap.md`)

---

## 2. Context Size Limits

### Problem
From `memory_scalability_report.md`: portfolio block grows ~65 tokens per holding. At 20+ holdings it dominates Sonnet context; at 100+ holdings, cost is significant.

### Change
Two constants added to `market_analyst_agents.py`:

```python
_CTX_LIMIT_CHIEF_HISTORY_CHARS = 800   # injected SQL history cap
_CTX_LIMIT_PORTFOLIO_CHARS     = 3000  # portfolio PnL block cap
```

Portfolio manager node truncates the `pnl_lines` block:
```python
portfolio_block = "\n".join(pnl_lines)[:_CTX_LIMIT_PORTFOLIO_CHARS]
```

At current scale (1–5 holdings): no effect. Prevents context explosion if holdings grow to 20+.

---

## 3. Price Stale Flag — portfolio_manager_node

### Problem
From `tool_risk_matrix.md`:
> "Silent price fallback in portfolio (stale prices = wrong hold/sell decisions)"

When `calculate_pnl()` calls yfinance and a ticker fails, it silently returns `current_price = entry_price`. The advisor then sees 0% P&L and may recommend "hold" when the stock has actually moved significantly.

### Change
After `calculate_pnl()` call, check for stale prices and emit observable event:

```python
stale = [h["stock_id"] for h in enriched
         if h.get("current_price") is None
         or h.get("current_price") == h.get("entry_price")]
if stale:
    logger.warning(f"[PortfolioManager] 現價可能為舊資料：{stale}")
    emit_event(run_id, "fallback_activated", "portfolio_manager",
               {"reason": "price_stale", "stocks": stale}, severity="warn")
```

Also hardened the bare `calculate_pnl()` call — if the entire function raises, falls back to `holdings` (no PnL enrichment) rather than crashing the node.

### What changed
- **Before**: silent wrong data, no visibility
- **After**: `fallback_activated` event in `workflow_events` table, WARNING log line, queryable in dashboard Events tab

---

## 4. Deferred Items

| Item | Why Deferred |
|------|-------------|
| Reduce `final_brief` redundant passing | Requires graph topology change (>1h). `final_brief` flows through `portfolio_manager` → `format_agent` — removing it from state would require passing only necessary fields per node. Low actual cost impact. |
| Remove `snapshot` from state after `data_collector` | Minor memory saving (~3KB), not worth the topology change |
| Streamlit cache freshness on new trading day | Dashboard concern, separate from workflow |

---

## Context Budget After Changes

| Node | Before (tokens) | After (tokens) | Change |
|------|----------------|----------------|--------|
| chief_strategist user content | 800–1200 | 1000–1500 | +≤200 (history) |
| portfolio_manager user content | 710–1110 | 710–1110 (capped at 3000c) | no change at current scale |

Both remain well within model context limits. Chief strategist uses 1–2% of Opus 200K window.

---

## Files Changed

| File | Lines Changed | Description |
|------|--------------|-------------|
| `market_analyst_agents.py` | +25 | Constants, history injection block, price_stale check |
| `database_tools.py` | +45 | `get_recent_accuracy_context()` function |
