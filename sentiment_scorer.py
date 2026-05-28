"""Composite sentiment scoring for Phase 3 §2.

Reads from stock_sentiment_daily (Day 1 schema, three sources written by
Day 2-3 collectors) and aggregates a per-stock score in [0, 1] over a
rolling lookback window. Top-N by composite is the candidate pool for
the §2 weekly job (Day 5).

Composite weights (Phase 3 baseline; learned weights are a Phase 4 task):
  CNYES news mentions  : 0.40
  TWSE top-volume rank : 0.30
  PTT 股板 buzz       : 0.30
"""
from __future__ import annotations

from typing import Optional

from loguru import logger
from sqlalchemy import text


_WEIGHTS_BASELINE = {"cnyes": 0.40, "twse": 0.30, "ptt": 0.30}
# Fallback when PTT pool empty (scraper failure / weekend): re-allocate
# PTT's 0.3 weight to the remaining two sources, keeping their ratio (4:3).
_WEIGHTS_NO_PTT   = {"cnyes": 0.55, "twse": 0.45, "ptt": 0.0}


def _minmax(values: dict[str, float]) -> dict[str, float]:
    """Min-max normalize values to [0, 1]. All-equal pool → all 0."""
    if not values:
        return {}
    lo = min(values.values())
    hi = max(values.values())
    if hi <= lo:
        return {k: 0.0 for k in values}
    span = hi - lo
    return {k: (v - lo) / span for k, v in values.items()}


def _load_source_totals(source: str, lookback_days: int) -> dict[str, int]:
    """SUM(raw_count) per stock_id for one source over the last N days."""
    from database_tools import _engine
    with _engine().connect() as conn:
        rows = conn.execute(
            text("""
                SELECT stock_id, SUM(raw_count) AS total
                FROM stock_sentiment_daily
                WHERE source = :s
                  AND trade_date >= CURDATE() - INTERVAL :d DAY
                GROUP BY stock_id
            """),
            {"s": source, "d": lookback_days},
        ).fetchall()
    return {r[0]: int(r[1] or 0) for r in rows}


def compute_composite_score(lookback_days: int = 7) -> dict[str, float]:
    """Rolling N-day aggregate of the three sources → per-stock composite.

    Returns {stock_id: composite_score in [0, 1]}, keyed only by 4-digit
    equity codes (5-digit ETFs and ill-formed IDs are dropped here so
    downstream technical_signals can assume equity-style OHLCV).

    Auto-downgrades PTT to zero weight when its pool is empty (Phase 3
    risk register: PTT WAF / HTML drift fallback)."""
    raw = {src: _load_source_totals(src, lookback_days) for src in _WEIGHTS_BASELINE}
    norms = {src: _minmax(raw[src]) for src in _WEIGHTS_BASELINE}

    universe = set().union(*[d.keys() for d in raw.values()])
    universe = {sid for sid in universe if len(sid) == 4 and sid.isdigit()}
    if not universe:
        logger.warning("[scorer] empty universe — no §2 sentiment data yet")
        return {}

    weights = _WEIGHTS_BASELINE
    if not raw["ptt"]:
        logger.info("[scorer] PTT pool empty → fallback weights cnyes=0.55 twse=0.45")
        weights = _WEIGHTS_NO_PTT

    return {
        sid: sum(weights[src] * norms[src].get(sid, 0.0) for src in _WEIGHTS_BASELINE)
        for sid in universe
    }


def compute_top_n_composite(n: int = 10, lookback_days: int = 7
                            ) -> list[tuple[str, float]]:
    """Sorted top-N (stock_id, composite_score) pairs, descending."""
    composite = compute_composite_score(lookback_days)
    return sorted(composite.items(), key=lambda kv: -kv[1])[:n]


# ── Debug CLI ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Inspect §2 composite scoring")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--n",    type=int, default=10)
    args = parser.parse_args()

    picks = compute_top_n_composite(n=args.n, lookback_days=args.days)
    for rank, (sid, score) in enumerate(picks, 1):
        print(f"  #{rank:>2}  {sid}  composite={score:.4f}")
