# Chief Strategist Context Update Report
**AI Agent Studio | 2026-05-16**

---

## Summary

Added Adaptive Flywheel Phase 1 lesson injection to `chief_strategist_node`. The prompt now receives up to 3 regime-matched past lessons (≤600 chars) in addition to the existing accuracy history block. Zero breaking changes; the injection is fully fail-silent.

---

## Before

```
user_content =
  籌碼面報告: {chip_report}
  技術面報告: {tech_report}

  [optionally] 【近期預測準確率 N% (K/N筆)】
  ...accuracy history lines...
```

## After

```
user_content =
  籌碼面報告: {chip_report}
  技術面報告: {tech_report}

  [optionally] 【近期預測準確率 N% (K/N筆)】
  ...accuracy history lines...

  [optionally] 【歷史策略教訓（SOX strong / 外資 bearish）】
  ● [2026-05-10] direction ✗: 外資淨賣 -18k 但 SOX 強勁...
  ● [2026-04-28] overconfidence ✗: 評分 82 分卻跳空翻空...
```

---

## Code Changes

### `market_analyst_agents.py`

**New constant** (line ~24):
```python
_CTX_LIMIT_CHIEF_LESSONS_CHARS = 600
```

**New injection block** in `chief_strategist_node`, after the existing accuracy-history block and before `_llm_opus().invoke(...)`:
```python
try:
    from lesson_retriever import get_lesson_context
    lessons = get_lesson_context(
        state.get("raw_market_data") or {},
        limit=3,
        max_chars=_CTX_LIMIT_CHIEF_LESSONS_CHARS,
    )
    if lessons:
        user_content += f"\n\n{lessons}"
except Exception as exc:
    logger.debug(f"[ChiefStrategist] strategy lessons 載入失敗（略過）: {exc}")
```

---

## Failure Modes (All Silent)

| Scenario | Behaviour |
|----------|-----------|
| `strategy_lessons` table does not exist yet | `get_relevant_lessons()` catches exception → returns `[]` → `get_lesson_context()` returns `""` → no injection |
| No lessons in DB (first run) | Returns `""` → no injection |
| `raw_market_data` missing from state | `state.get("raw_market_data") or {}` → empty dict → regime = neutral/neutral → query runs, returns 0 matches or low-relevance lessons |
| DB connection failure | `get_relevant_lessons()` catches, logs warning, returns `[]` |
| Import error for lesson_retriever | Outer `except Exception` catches, logs debug, skips |

---

## Context Budget

| Item | Chars | Tokens (approx) |
|------|-------|----------------|
| Accuracy history (existing, capped) | ≤800 | ~200 |
| Strategy lessons (new, capped) | ≤600 | ~150 |
| **Net new** | **≤600** | **+150** |

Opus 200K context window. +150 tokens per run adds ~$0.000225 at current pricing — negligible.

---

## Lesson Format Injected

```
【歷史策略教訓（SOX {regime_sox} / 外資 {regime_foreign_oi}）】
● [YYYY-MM-DD] {error_label} {✓/✗}: {lesson_text truncated at 200 chars}
● [YYYY-MM-DD] ...
```

Error labels: `direction`, `overconfidence`, `magnitude`, `missing_data`, `stale_snapshot`, `correct`.

---

## Expected Behaviour Over Time

| Period | Expected Effect |
|--------|----------------|
| Days 1–14 | ≤14 lessons in DB; regime filtering may return 0–2 matches; minor context enrichment |
| Days 15–30 | Enough history for regime clustering; chief_strategist begins seeing patterns |
| Days 30–90 | Full lesson pool (~90 active); consistent regime-matched context injection |
| Day 91+ | Oldest lessons expire; rolling 90-day window maintained automatically |
