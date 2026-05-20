"""optimizer_scoring.py — Phase 2

Score a (agent_name, version) pair over a recent time window using
shadow_runs as the data source. Two consumers:

  - optimizer_agent: compare proposed shadow version vs current active
  - optimizer_revert_check: detect post-promotion regression vs baseline

Scoring formula (weights frozen by user decision B + design tradeoff):
    score = 0.55 * accuracy   + 0.25 * stability + 0.20 * cost_term

Per-agent accuracy:
  tech_analyst       : directional accuracy of gap_direction vs market_actuals
  portfolio_manager  : day's strategy_lessons.direction_correct used as proxy
                       (Phase 2.1 TODO: 60% per-stock 1wk PnL + 40% lesson)

Note on iter term: the Phase 2 design specified a 0.10 iter_term, but
shadow_runs has no iter column. The 0.10 weight is rolled into accuracy
(+0.05) and stability (+0.05). Add back when shadow_runs gains iter columns.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from statistics import mean
from typing import Optional

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()
logger = logging.getLogger(__name__)


# ── Tunables ──────────────────────────────────────────────────────────────────

WEIGHTS = {"accuracy": 0.55, "stability": 0.25, "cost": 0.20}
COST_REFERENCE_USD = 0.05      # avg_cost above this → cost_term = 0
MIN_SAMPLES_FOR_SCORE = 5      # below this → score = None
DEFAULT_WINDOW_DAYS = 14
_GAP_FLAT_TOLERANCE_PCT = 0.1  # |actual_gap_pct| ≤ 0.1 counts as "flat"


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class _Sample:
    trade_date: date
    text: str
    cost_usd: float


# ── Public API ────────────────────────────────────────────────────────────────

def score_version(
    agent_name: str,
    version: int,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict:
    """Compute weighted score for (agent_name, version) over the last
    `window_days` days. Returns:

        {
          "score":         float | None,    # None if sample_count < MIN_SAMPLES
          "sample_count":  int,
          "accuracy":      float,           # [0,1]
          "stability":     float,           # parsability fraction
          "avg_cost_usd":  float,
          "cost_term":     float,
          "weights":       dict,
          "window_days":   int,
          "agent_name":    str,
          "version":       int,
        }
    """
    samples = _collect_samples(agent_name, version, window_days)
    out = {
        "score": None,
        "sample_count": len(samples),
        "accuracy": 0.0,
        "stability": 0.0,
        "avg_cost_usd": 0.0,
        "cost_term": 0.0,
        "weights": dict(WEIGHTS),
        "window_days": window_days,
        "agent_name": agent_name,
        "version": version,
    }
    if not samples:
        return out

    if agent_name == "tech_analyst":
        actuals = _load_actuals_map(window_days + 1)
        accuracy, parsed_n = _tech_accuracy(samples, actuals)
    elif agent_name == "portfolio_manager":
        lessons = _load_lessons_map(window_days + 1)
        accuracy, parsed_n = _portfolio_accuracy(samples, lessons)
    else:
        accuracy, parsed_n = 0.5, len(samples)  # neutral for unknown agents

    stability = parsed_n / len(samples) if samples else 0.0
    costed = [s.cost_usd for s in samples if s.cost_usd > 0]
    avg_cost = mean(costed) if costed else 0.0
    cost_term = max(0.0, 1.0 - avg_cost / COST_REFERENCE_USD) if avg_cost else 1.0

    out.update(
        accuracy=round(accuracy, 3),
        stability=round(stability, 3),
        avg_cost_usd=round(avg_cost, 6),
        cost_term=round(cost_term, 3),
    )

    if len(samples) < MIN_SAMPLES_FOR_SCORE:
        return out  # score stays None

    score = (
        WEIGHTS["accuracy"] * accuracy
        + WEIGHTS["stability"] * stability
        + WEIGHTS["cost"] * cost_term
    )
    out["score"] = round(score, 3)
    return out


def score_delta(
    agent_name: str,
    version_a: int,
    version_b: int,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict:
    """Returns {'a': score_version(version_a), 'b': score_version(version_b),
    'delta_score': b.score - a.score (None if either is None)}."""
    a = score_version(agent_name, version_a, window_days=window_days)
    b = score_version(agent_name, version_b, window_days=window_days)
    delta = (b["score"] - a["score"]) if (a["score"] is not None and b["score"] is not None) else None
    return {"a": a, "b": b, "delta_score": None if delta is None else round(delta, 3)}


# ── Sample collection ─────────────────────────────────────────────────────────

def _collect_samples(agent_name: str, version: int, window_days: int) -> list[_Sample]:
    """Pull all shadow_runs rows in window where the version appears as either
    primary_version or shadow_version, returning one sample per row with the
    relevant output and cost."""
    from database_tools import _engine

    try:
        with _engine().connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT primary_version, shadow_version,
                           primary_output,  shadow_output,
                           primary_cost_usd, shadow_cost_usd,
                           DATE(created_at) AS d
                    FROM shadow_runs
                    WHERE created_at >= NOW() - INTERVAL :days DAY
                      AND agent_name = :a
                      AND (primary_version = :v OR shadow_version = :v)
                """),
                {"a": agent_name, "v": version, "days": window_days},
            ).fetchall()
    except Exception as exc:
        logger.warning(f"[optimizer.scoring] _collect_samples failed: {exc}")
        return []

    samples: list[_Sample] = []
    for r in rows:
        pv, sv, p_out, s_out, p_cost, s_cost, d = r
        if int(pv) == version:
            text_ = p_out or ""
            cost = float(p_cost or 0.0)
        elif int(sv) == version:
            text_ = s_out or ""
            cost = float(s_cost or 0.0)
        else:
            continue
        if not text_:
            continue
        samples.append(_Sample(trade_date=d, text=text_, cost_usd=cost))
    return samples


# ── Ground-truth loaders ──────────────────────────────────────────────────────

def _load_actuals_map(days: int) -> dict[date, float]:
    """{trade_date: actual_gap_pct} for the last `days` days."""
    from database_tools import _engine

    try:
        with _engine().connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT trade_date, actual_gap_pct
                    FROM market_actuals
                    WHERE trade_date >= CURDATE() - INTERVAL :d DAY
                      AND actual_gap_pct IS NOT NULL
                """),
                {"d": days},
            ).fetchall()
        return {r[0]: float(r[1]) for r in rows}
    except Exception as exc:
        logger.warning(f"[optimizer.scoring] _load_actuals_map failed: {exc}")
        return {}


def _load_lessons_map(days: int) -> dict[date, int]:
    """{trade_date: direction_correct (0/1)} for the last `days` days."""
    from database_tools import _engine

    try:
        with _engine().connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT trade_date, direction_correct
                    FROM strategy_lessons
                    WHERE trade_date >= CURDATE() - INTERVAL :d DAY
                      AND is_active = 1
                """),
                {"d": days},
            ).fetchall()
        return {r[0]: int(r[1] or 0) for r in rows}
    except Exception as exc:
        logger.warning(f"[optimizer.scoring] _load_lessons_map failed: {exc}")
        return {}


# ── Per-agent accuracy ────────────────────────────────────────────────────────

def _tech_accuracy(
    samples: list[_Sample], actuals: dict[date, float]
) -> tuple[float, int]:
    """Directional accuracy of predicted gap_direction vs actual_gap_pct sign.
    Returns (accuracy ∈ [0,1], parsed_count)."""
    parsed = 0
    correct = 0
    for s in samples:
        sig = _extract_tech_signal(s.text)
        if sig is None:
            continue
        parsed += 1
        pd_dir = (sig.get("gap_direction") or "").lower()
        if pd_dir not in {"up", "down", "flat"}:
            continue
        actual_pct = actuals.get(s.trade_date)
        if actual_pct is None:
            continue
        if abs(actual_pct) <= _GAP_FLAT_TOLERANCE_PCT:
            actual_dir = "flat"
        elif actual_pct > 0:
            actual_dir = "up"
        else:
            actual_dir = "down"
        if pd_dir == actual_dir:
            correct += 1
    if parsed == 0:
        return 0.0, 0
    return correct / parsed, parsed


def _portfolio_accuracy(
    samples: list[_Sample], lessons: dict[date, int]
) -> tuple[float, int]:
    """Per-day proxy: use strategy_lessons.direction_correct for each sample's
    trade_date. Sample is 'parsed' if at least one 【股票代碼：XXXX】 block found.
    TODO Phase 2.1: replace with 60% per-stock 1wk PnL + 40% lesson."""
    parsed = 0
    correct = 0
    for s in samples:
        actions = _extract_portfolio_actions(s.text)
        if not actions:
            continue
        parsed += 1
        dc = lessons.get(s.trade_date)
        if dc is None:
            continue
        correct += int(dc)
    if parsed == 0:
        return 0.0, 0
    return correct / parsed, parsed


# ── Output parsers ────────────────────────────────────────────────────────────

_TECH_JSON_DIR_RE = re.compile(
    r'"gap_direction"\s*:\s*"(up|down|flat)"', re.IGNORECASE
)
_TECH_JSON_PCT_RE = re.compile(
    r'"estimated_gap_pct"\s*:\s*(-?\d+(?:\.\d+)?)'
)


def _extract_tech_signal(text_: str) -> Optional[dict]:
    """Try JSON parse first; fall back to regex over raw text. Returns
    {'gap_direction': str, 'estimated_gap_pct': float|None} or None."""
    if not text_:
        return None
    candidate = text_.strip()

    # 1) Pure JSON object
    if candidate.startswith("{"):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict) and "gap_direction" in obj:
                return {
                    "gap_direction": str(obj.get("gap_direction", "")).lower(),
                    "estimated_gap_pct": _safe_float(obj.get("estimated_gap_pct")),
                }
        except (json.JSONDecodeError, ValueError):
            pass

    # 2) Embedded JSON snippet anywhere in text
    m_dir = _TECH_JSON_DIR_RE.search(candidate)
    if m_dir:
        pct = None
        m_pct = _TECH_JSON_PCT_RE.search(candidate)
        if m_pct:
            pct = _safe_float(m_pct.group(1))
        return {"gap_direction": m_dir.group(1).lower(), "estimated_gap_pct": pct}

    return None


_PORTFOLIO_STOCK_RE = re.compile(r"【股票代碼[：:]\s*(\d{4,6})")
_PORTFOLIO_ACTION_TOKENS = ("買", "賣", "減", "加碼", "續抱", "持有", "停損")


def _extract_portfolio_actions(text_: str) -> list[str]:
    """Return list of stock codes found in the report (used as parsability
    signal). Empty list = unparsable."""
    if not text_:
        return []
    if not any(tok in text_ for tok in _PORTFOLIO_ACTION_TOKENS):
        return []
    return _PORTFOLIO_STOCK_RE.findall(text_)


def _safe_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── CLI for ad-hoc inspection ─────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Score one (agent, version) pair.")
    p.add_argument("agent")
    p.add_argument("version", type=int)
    p.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS)
    args = p.parse_args()

    result = score_version(args.agent, args.version, window_days=args.days)
    print(json.dumps(result, default=str, ensure_ascii=False, indent=2))
