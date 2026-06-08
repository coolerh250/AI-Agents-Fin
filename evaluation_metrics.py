"""
evaluation_metrics.py
Rule-based per-agent scoring — no LLM calls, no DB access, no exceptions raised.
Every function returns an EvalResult dict.
"""
import json
import re
from typing import Optional

# EvalResult keys
# quality_score     : float 0-100
# schema_valid      : bool
# missing_fields    : list[str]
# hallucination_flags: list[str]
# extra_metrics     : dict


def _strip_fence(text: str) -> str:
    """Remove markdown code fences, same logic as save_to_db_node."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return text.strip()


def _extract_tech_json(raw: str) -> Optional[dict]:
    """Extract the gap_direction JSON object from tech_analyst output.

    Mirrors market_analyst_agents._extract_tech_json — duplicated here so this
    module stays free of workflow imports. Handles three formats:
      1. pure JSON (v1 single-shot)
      2. ```json\n{...}\n``` markdown-wrapped
      3. preamble + JSON (Phase 1.5 ReAct loop's final_text emits a free-form
         summary / markdown table BEFORE the JSON object)

    Returns None when no JSON containing "gap_direction" can be located."""
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        s = s[s.index("\n") + 1:] if "\n" in s else s
    try:
        return json.loads(s.strip())
    except json.JSONDecodeError:
        pass
    needle = '"gap_direction"'
    needle_idx = s.find(needle)
    if needle_idx < 0:
        return None
    depth, start = 0, -1
    for i in range(needle_idx - 1, -1, -1):
        c = s[i]
        if c == '}':
            depth += 1
        elif c == '{':
            if depth == 0:
                start = i
                break
            depth -= 1
    if start < 0:
        return None
    depth = 0
    for j in range(start, len(s)):
        c = s[j]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:j + 1])
                except json.JSONDecodeError:
                    return None
    return None


def eval_data_collector(raw_dict: dict) -> dict:
    """
    Evaluate data_collector completeness via session_episodes-reconstructed dict.
    7 numeric keys each worth 10 pts; data_ok flag +10; all-present bonus +20.
    """
    required = [
        "foreign_oi_net", "trust_oi_net", "dealer_oi_net",
        "djia_chg_pct", "ndx_chg_pct", "sox_chg_pct", "tsm_adr_chg_pct",
    ]
    missing = []
    score = 0.0

    for key in required:
        val = raw_dict.get(key)
        if val is not None and isinstance(val, (int, float)):
            score += 10.0
        else:
            missing.append(key)

    if raw_dict.get("data_ok") is not None:
        score += 10.0

    if not missing:
        score += 20.0  # all-present bonus

    schema_valid = len(missing) == 0

    return {
        "quality_score": round(min(score, 100.0), 1),
        "schema_valid": schema_valid,
        "missing_fields": missing,
        "hallucination_flags": [],
        "extra_metrics": {
            "present_count": len(required) - len(missing),
            "required_count": len(required),
        },
    }


def eval_chip_analyst(text: str) -> dict:
    """
    Evaluate chip_analyst output.
    Expects JSON with divergence_signal key; checks Chinese section keywords.
    """
    missing_fields = []
    hallucination_flags = []
    json_parseable = False
    divergence_signal = None
    score = 0.0

    # Length check (+20)
    if 100 <= len(text) <= 4000:
        score += 20.0
    elif len(text) < 100:
        missing_fields.append("output_too_short")
    else:
        missing_fields.append("output_too_long")

    # Chinese keyword check (+10)
    for kw in ("外資", "投信", "自營商"):
        if kw in text:
            score += 10.0 / 3  # ~3.3 each
    score = round(score, 1)

    # JSON parse (+40 for parseable, +30 for divergence_signal)
    try:
        parsed = json.loads(_strip_fence(text))
        json_parseable = True
        score += 40.0

        if "divergence_signal" in parsed:
            score += 30.0
            divergence_signal = parsed.get("divergence_signal")
        else:
            missing_fields.append("divergence_signal")

        # Hallucination: divergence_signal=True but foreign OI is positive (same direction)
        foreign_net = parsed.get("foreign_oi_net") or parsed.get("foreign_net")
        if divergence_signal is True and isinstance(foreign_net, (int, float)) and foreign_net > 0:
            hallucination_flags.append("divergence_conflict")

    except (json.JSONDecodeError, ValueError):
        missing_fields.append("json_parse_failed")

    schema_valid = json_parseable and "divergence_signal" not in missing_fields

    return {
        "quality_score": round(min(score, 100.0), 1),
        "schema_valid": schema_valid,
        "missing_fields": missing_fields,
        "hallucination_flags": hallucination_flags,
        "extra_metrics": {
            "json_parseable": json_parseable,
            "divergence_signal": divergence_signal,
            "text_length": len(text),
        },
    }


def eval_tech_analyst(text: str) -> dict:
    """
    Evaluate tech_analyst output — MUST be valid JSON with gap_direction + estimated_gap_pct.
    save_to_db_node depends on this for gap_direction stored in daily_briefs.
    """
    missing_fields = []
    hallucination_flags = []
    json_parseable = False
    gap_direction = None
    estimated_gap_pct = None
    score = 0.0

    parsed = _extract_tech_json(text)
    if parsed is None:
        missing_fields.append("json_parse_failed")
        hallucination_flags.append("json_parse_failed")
    else:
        json_parseable = True
        score += 30.0

        # gap_direction check (+30)
        gap_direction = parsed.get("gap_direction")
        if gap_direction in ("up", "flat", "down"):
            score += 30.0
        else:
            missing_fields.append("gap_direction")

        # estimated_gap_pct numeric check (+20)
        estimated_gap_pct = parsed.get("estimated_gap_pct")
        if isinstance(estimated_gap_pct, (int, float)):
            score += 20.0
            # Range check (+20)
            if abs(estimated_gap_pct) <= 5.0:
                score += 20.0
            else:
                hallucination_flags.append("gap_pct_out_of_range")
        else:
            missing_fields.append("estimated_gap_pct")

        # Direction / sign consistency
        if (gap_direction == "up" and isinstance(estimated_gap_pct, (int, float)) and estimated_gap_pct < 0):
            hallucination_flags.append("direction_pct_mismatch")
        elif (gap_direction == "down" and isinstance(estimated_gap_pct, (int, float)) and estimated_gap_pct > 0):
            hallucination_flags.append("direction_pct_mismatch")

    schema_valid = (
        json_parseable
        and "gap_direction" not in missing_fields
        and not hallucination_flags
    )

    return {
        "quality_score": round(min(score, 100.0), 1),
        "schema_valid": schema_valid,
        "missing_fields": missing_fields,
        "hallucination_flags": hallucination_flags,
        "extra_metrics": {
            "gap_direction": gap_direction,
            "estimated_gap_pct": estimated_gap_pct,
        },
    }


def eval_chief_strategist(text: str) -> dict:
    """
    Evaluate chief_strategist final_brief — 4 required Chinese section markers.
    """
    required_sections = ["盤勢定調", "操作策略", "關鍵防守點", "風險提示"]
    missing_fields = []
    hallucination_flags = []
    score = 0.0

    for section in required_sections:
        if section in text:
            score += 25.0
        else:
            missing_fields.append(section)

    if len(text) < 200:
        hallucination_flags.append("brief_too_short")
    elif len(text) > 3000:
        hallucination_flags.append("brief_too_long")

    schema_valid = len(missing_fields) == 0

    return {
        "quality_score": round(min(score, 100.0), 1),
        "schema_valid": schema_valid,
        "missing_fields": missing_fields,
        "hallucination_flags": hallucination_flags,
        "extra_metrics": {
            "text_length": len(text),
            "section_count": len(required_sections) - len(missing_fields),
        },
    }


def eval_format_agent(text: str) -> dict:
    """
    Evaluate format_agent final_report — 4 emoji section markers + LINE length limit.
    """
    required_emojis = ["📊", "⚔️", "🛡️", "⚠️"]
    missing_fields = []
    hallucination_flags = []
    score = 0.0

    for emoji in required_emojis:
        if emoji in text:
            score += 20.0
        else:
            missing_fields.append(f"missing_section_{emoji}")

    if len(text) <= 2000:
        score += 20.0
    else:
        hallucination_flags.append("exceeds_line_limit")

    schema_valid = len(missing_fields) == 0 and not hallucination_flags

    return {
        "quality_score": round(min(score, 100.0), 1),
        "schema_valid": schema_valid,
        "missing_fields": missing_fields,
        "hallucination_flags": hallucination_flags,
        "extra_metrics": {
            "char_length": len(text),
            "has_portfolio_section": "💼" in text,
        },
    }


def compute_brief_quality_score(agent_scores: dict) -> float:
    """
    Weighted composite of per-agent quality scores (0–100).
    Missing agents contribute 0 with full weight — no silent skipping.
    """
    weights = {
        "data_collector":    0.10,
        "chip_analyst":      0.20,
        "tech_analyst":      0.25,
        "chief_strategist":  0.35,
        "format_agent":      0.10,
    }
    total = sum(
        float(agent_scores.get(agent, 0.0) or 0.0) * w
        for agent, w in weights.items()
    )
    return round(total, 1)
