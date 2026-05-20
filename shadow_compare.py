"""
shadow_compare.py
Divergence scoring + shadow_runs persistence for the Phase 1 shadow
rollout. The node wrapper in market_analyst_agents.py runs the
primary path first, then runs the shadow agent, then calls:

    div = compute_divergence(primary_text, shadow_text, output_kind, ...)
    save_shadow_run(run_id, agent_name, primary_version, shadow_version,
                    primary_output, shadow_output, divergence=div,
                    shadow_meta=shadow_loop_result)

Divergence kinds:
  - 'json': both outputs parsed as JSON (with markdown fence tolerance).
    Compare each top-level key; critical_fields get 2x weight; numeric
    fields use a configurable relative tolerance. Score = 1 - matched/total.
  - 'text': difflib.SequenceMatcher ratio (1 - ratio) blended with a
    character n-gram Jaccard score. Score ∈ [0.0, 1.0].

Score conventions: 0.0 = identical, 1.0 = totally different.
"""
import difflib
import json
from typing import Optional

from loguru import logger
from sqlalchemy import text


# ── Public API ────────────────────────────────────────────────────────────────

def compute_divergence(
    primary: str,
    shadow: str,
    output_kind: str = "text",
    *,
    critical_fields: Optional[list[str]] = None,
    numeric_tolerance_pct: float = 10.0,
) -> dict:
    """
    Returns {'score': float (0-1), 'kind': str, 'detail': dict}.
    Never raises — on internal failure returns {'score': 1.0, 'kind': 'compare_error', ...}.
    """
    try:
        if primary == shadow:
            return {"score": 0.0, "kind": output_kind, "detail": {"identical": True}}

        if output_kind == "json":
            return _diverge_json(primary, shadow,
                                 critical_fields=critical_fields or [],
                                 num_tol_pct=numeric_tolerance_pct)
        return _diverge_text(primary, shadow)
    except Exception as exc:
        logger.warning(f"[shadow_compare] compute_divergence failed: {exc}")
        return {"score": 1.0, "kind": "compare_error", "detail": {"error": str(exc)[:200]}}


def save_shadow_run(
    *,
    run_id: str,
    agent_name: str,
    primary_version: int,
    shadow_version: int,
    primary_output: str,
    shadow_output: str,
    divergence: dict,
    shadow_meta: Optional[dict] = None,
    primary_latency_ms: Optional[int] = None,
    shadow_latency_ms: Optional[int] = None,
    primary_cost_usd: Optional[float] = None,
    shadow_cost_usd: Optional[float] = None,
    shadow_error: Optional[str] = None,
) -> Optional[int]:
    """Insert a row into shadow_runs. Returns id, or None on failure (fail-silent)."""
    try:
        from database_tools import _engine
        meta = shadow_meta or {}
        # If shadow_meta provided, prefer its measurements
        if shadow_latency_ms is None:
            shadow_latency_ms = _coerce_int(meta.get("latency_ms"))
        if shadow_cost_usd is None:
            shadow_cost_usd = _coerce_float(meta.get("cost_usd"))

        detail_json = json.dumps(divergence.get("detail") or {},
                                 ensure_ascii=False, default=str)

        with _engine().begin() as conn:
            result = conn.execute(
                text("""
                    INSERT INTO shadow_runs
                        (run_id, agent_name,
                         primary_version, shadow_version,
                         primary_output, shadow_output,
                         primary_latency_ms, shadow_latency_ms,
                         primary_cost_usd, shadow_cost_usd,
                         divergence_score, divergence_kind, divergence_detail,
                         shadow_error)
                    VALUES (:run, :agent,
                            :pv, :sv,
                            :po, :so,
                            :pl, :sl,
                            :pc, :sc,
                            :ds, :dk, :dd,
                            :se)
                """),
                {"run": run_id, "agent": agent_name,
                 "pv": primary_version, "sv": shadow_version,
                 "po": (primary_output or "")[:65000],
                 "so": (shadow_output or "")[:65000],
                 "pl": primary_latency_ms, "sl": shadow_latency_ms,
                 "pc": primary_cost_usd, "sc": shadow_cost_usd,
                 "ds": float(divergence.get("score", 1.0)),
                 "dk": str(divergence.get("kind", "unknown"))[:30],
                 "dd": detail_json,
                 "se": (shadow_error or None)},
            )
            return int(result.lastrowid) if result.lastrowid else None
    except Exception as exc:
        logger.warning(f"[shadow_compare] save_shadow_run failed: {exc}")
        return None


# ── JSON divergence ───────────────────────────────────────────────────────────

_FENCE_PREFIXES = ("```json", "```JSON", "```")


def _strip_fence(s: str) -> str:
    s = (s or "").strip()
    for p in _FENCE_PREFIXES:
        if s.startswith(p):
            rest = s[len(p):].lstrip("\r\n ")
            if rest.endswith("```"):
                rest = rest[:-3]
            return rest.strip()
    return s


def _diverge_json(primary: str, shadow: str,
                  *, critical_fields: list[str], num_tol_pct: float) -> dict:
    try:
        p = json.loads(_strip_fence(primary))
        s = json.loads(_strip_fence(shadow))
    except (json.JSONDecodeError, ValueError) as exc:
        # one of them isn't JSON — fall back to text comparison
        text_score = _diverge_text(primary, shadow)["score"]
        return {"score": min(1.0, text_score + 0.2),
                "kind": "parse_failure",
                "detail": {"reason": str(exc)[:200],
                           "text_fallback_score": text_score}}

    if not isinstance(p, dict) or not isinstance(s, dict):
        text_score = _diverge_text(primary, shadow)["score"]
        return {"score": text_score, "kind": "json_non_object",
                "detail": {"text_fallback_score": text_score}}

    keys = set(p) | set(s)
    if not keys:
        return {"score": 0.0, "kind": "json_field_mismatch",
                "detail": {"matched": 0, "total": 0}}

    matched_weight = 0.0
    total_weight = 0.0
    per_field: dict = {}
    crit_set = set(critical_fields)

    for k in sorted(keys):
        w = 2.0 if k in crit_set else 1.0
        total_weight += w
        if k not in p or k not in s:
            per_field[k] = {"match": False, "reason": "missing"}
            continue
        if _value_matches(p[k], s[k], num_tol_pct):
            matched_weight += w
            per_field[k] = {"match": True}
        else:
            per_field[k] = {"match": False, "primary": p[k], "shadow": s[k]}

    score = 1.0 - (matched_weight / total_weight)
    return {
        "score": round(score, 3),
        "kind": "json_field_mismatch",
        "detail": {
            "matched_weight": matched_weight,
            "total_weight": total_weight,
            "critical_fields": critical_fields,
            "per_field": per_field,
        },
    }


def _value_matches(a, b, num_tol_pct: float) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if a == b:
            return True
        denom = max(abs(a), abs(b), 1e-9)
        return abs(a - b) / denom <= num_tol_pct / 100.0
    if isinstance(a, str) and isinstance(b, str):
        return a.strip() == b.strip()
    # list / dict — fall back to exact equality
    return a == b


# ── Text divergence ───────────────────────────────────────────────────────────

def _diverge_text(primary: str, shadow: str) -> dict:
    a = primary or ""
    b = shadow or ""
    if not a and not b:
        return {"score": 0.0, "kind": "text_diff", "detail": {"empty": True}}

    seq_ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()  # 1.0 = identical
    jaccard = _char_ngram_jaccard(a, b, n=3)
    # blend (equal weight); both already 0–1 (higher = more similar)
    similarity = 0.5 * seq_ratio + 0.5 * jaccard
    score = max(0.0, min(1.0, 1.0 - similarity))
    return {
        "score": round(score, 3),
        "kind": "text_diff",
        "detail": {
            "seq_ratio":      round(seq_ratio, 3),
            "char3_jaccard":  round(jaccard, 3),
            "primary_len":    len(a),
            "shadow_len":     len(b),
        },
    }


def _char_ngram_jaccard(a: str, b: str, n: int = 3) -> float:
    sa = _ngrams(a, n)
    sb = _ngrams(b, n)
    if not sa and not sb:
        return 1.0
    inter = sa & sb
    union = sa | sb
    return len(inter) / len(union) if union else 0.0


def _ngrams(s: str, n: int) -> set[str]:
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i+n] for i in range(len(s) - n + 1)}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _coerce_int(v) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _coerce_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
