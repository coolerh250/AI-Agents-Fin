"""
strategy_profile.py
Strategy-as-data substrate for the multi-agent transformation.

Each pipeline agent (data_collector, chip_analyst, tech_analyst,
chief_strategist, portfolio_manager, format_agent) reads its prompt /
params / tool whitelist from the `agent_strategy_profiles` table at
runtime, rather than from hard-coded module constants. This makes the
agents tunable by a future Optimizer Agent (Phase 2) without code
changes.

Loader contract:
  - load_active_profile(name)  → currently-running version, or None
  - load_shadow_profile(name)  → shadow version (if any), or None
  - load_profile_or_fallback(name, fallback_*) → never returns None;
        falls back to the in-code constants and emits a
        'fallback_activated' event when the DB is unavailable.

Initial seeding:
  - seed_initial_profiles() walks the existing hard-coded prompts in
    market_analyst_agents.py and writes them as v1 (is_active=1) for
    each agent. Idempotent — skips agents that already have v1.
"""
import json
from dataclasses import dataclass
from typing import Optional

from loguru import logger
from sqlalchemy import text


@dataclass(frozen=True)
class StrategyProfile:
    agent_name:     str
    version:        int
    system_prompt:  str
    params:         dict          # {weights, thresholds, max_iter, token_budget, ...}
    tool_whitelist: list[str]
    model_name:     str
    max_tokens:     int
    is_active:      bool
    is_shadow:      bool


def _row_to_profile(row) -> StrategyProfile:
    return StrategyProfile(
        agent_name=row[0],
        version=int(row[1]),
        system_prompt=row[2],
        params=_safe_json_parse(row[3], {}),
        tool_whitelist=_safe_json_parse(row[4], []),
        model_name=row[5],
        max_tokens=int(row[6]),
        is_active=bool(row[7]),
        is_shadow=bool(row[8]),
    )


def _safe_json_parse(value, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


# ── Loaders ────────────────────────────────────────────────────────────────────

_PROFILE_COLS = ("agent_name, version, system_prompt, params_json, tool_whitelist, "
                 "model_name, max_tokens, is_active, is_shadow")


def load_active_profile(agent_name: str) -> Optional[StrategyProfile]:
    """Return the is_active=1 row for the agent, or None."""
    try:
        from database_tools import _engine
        with _engine().connect() as conn:
            row = conn.execute(
                text(f"SELECT {_PROFILE_COLS} FROM agent_strategy_profiles "
                     f"WHERE agent_name = :a AND is_active = 1 LIMIT 1"),
                {"a": agent_name},
            ).fetchone()
        return _row_to_profile(row) if row else None
    except Exception as exc:
        logger.warning(f"[strategy_profile] load_active_profile({agent_name}) failed: {exc}")
        return None


def load_shadow_profile(agent_name: str) -> Optional[StrategyProfile]:
    """Return the is_shadow=1 row for the agent, or None."""
    try:
        from database_tools import _engine
        with _engine().connect() as conn:
            row = conn.execute(
                text(f"SELECT {_PROFILE_COLS} FROM agent_strategy_profiles "
                     f"WHERE agent_name = :a AND is_shadow = 1 LIMIT 1"),
                {"a": agent_name},
            ).fetchone()
        return _row_to_profile(row) if row else None
    except Exception as exc:
        logger.warning(f"[strategy_profile] load_shadow_profile({agent_name}) failed: {exc}")
        return None


def load_profile_or_fallback(
    agent_name: str,
    fallback_prompt: str,
    fallback_params: dict,
    fallback_tools: list[str],
    fallback_model: str,
    fallback_max_tokens: int,
    run_id: Optional[str] = None,
) -> StrategyProfile:
    """
    Never returns None. Tries DB first; on miss or error, returns a
    StrategyProfile constructed from the fallback args (version=0).
    Emits 'fallback_activated' workflow event so we know when an agent
    is running off code defaults instead of DB.
    """
    profile = load_active_profile(agent_name)
    if profile is not None:
        return profile

    try:
        from telemetry import emit_event
        emit_event(run_id, "fallback_activated", agent_name,
                   {"reason": "strategy_profile_db_miss"}, severity="warn")
    except Exception:
        pass

    return StrategyProfile(
        agent_name=agent_name,
        version=0,
        system_prompt=fallback_prompt,
        params=dict(fallback_params or {}),
        tool_whitelist=list(fallback_tools or []),
        model_name=fallback_model,
        max_tokens=fallback_max_tokens,
        is_active=False,
        is_shadow=False,
    )


# ── Profile mutation (used by seeder + future promote CLI) ────────────────────

def _profile_exists(agent_name: str, version: int) -> bool:
    try:
        from database_tools import _engine
        with _engine().connect() as conn:
            row = conn.execute(
                text("SELECT 1 FROM agent_strategy_profiles "
                     "WHERE agent_name = :a AND version = :v LIMIT 1"),
                {"a": agent_name, "v": version},
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _insert_profile(
    *,
    agent_name: str,
    version: int,
    is_active: int,
    is_shadow: int,
    system_prompt: str,
    params: dict,
    tool_whitelist: list[str],
    model_name: str,
    max_tokens: int,
    notes: Optional[str] = None,
    created_by: str = "human",
    parent_version: Optional[int] = None,
) -> None:
    try:
        from database_tools import _engine
        with _engine().begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO agent_strategy_profiles
                        (agent_name, version, is_active, is_shadow,
                         system_prompt, params_json, tool_whitelist,
                         model_name, max_tokens, notes, created_by,
                         parent_version, activated_at)
                    VALUES (:a, :v, :ac, :sh, :sp, :pj, :tw,
                            :mn, :mt, :nt, :cb, :pv,
                            CASE WHEN :ac = 1 THEN NOW() ELSE NULL END)
                """),
                {"a": agent_name, "v": version, "ac": is_active, "sh": is_shadow,
                 "sp": system_prompt,
                 "pj": json.dumps(params or {}, ensure_ascii=False),
                 "tw": json.dumps(tool_whitelist or [], ensure_ascii=False),
                 "mn": model_name, "mt": max_tokens,
                 "nt": notes, "cb": created_by, "pv": parent_version},
            )
    except Exception as exc:
        logger.warning(f"[strategy_profile] _insert_profile({agent_name} v{version}) failed: {exc}")


# ── Initial seed ──────────────────────────────────────────────────────────────

_TECH_V2_SHADOW_PROMPT = """你是台股技術面專家，根據前一日美股表現與台指期夜盤數據預測今日台股開盤跳空方向與力道。

【可選工具】
- `get_us_market_summary`：重新抓 DJIA / NASDAQ-100 / SOX / TSMC ADR 最新收盤
- `get_tw_night_futures`：重新抓台指期夜盤 (TXF) 收盤與漲跌幅

【何時主動呼叫工具】
- 若使用者提供的 `night_futures_chg_pct` 為 null / 缺失 → 呼叫 `get_tw_night_futures` 確認是否真的尚未開盤；若仍 null 才以美股權重模式分析。
- 若使用者提供的 `sox_chg_pct` 異常（> +5% 或 < -5%）→ 呼叫 `get_us_market_summary` 二次確認。
- 否則直接使用提供的數據；不要為了使用工具而呼叫工具。

【權重規則】
若有夜盤資料（night_futures_chg_pct 非 null）：
- 台指期夜盤 change_pct（40%）← 最直接的台股開盤前信號
- PHLX SOX change_pct（20%）
- NASDAQ 100 change_pct（18%）
- TSMC ADR change_pct（14%）
- DJIA change_pct（8%）

若無夜盤資料：
- PHLX SOX change_pct（30%）
- NASDAQ 100 change_pct（25%）
- TSMC ADR change_pct（25%）
- DJIA change_pct（20%）

跳空判斷基準（加權平均）：
- > +1.5% → 強力跳空高開（預估 +1%~+2%）
- +0.5%~+1.5% → 溫和高開（預估 +0.3%~+1%）
- -0.5%~+0.5% → 平開（預估 ±0.3%）
- < -1.5% → 跳空低開

【最終輸出】（嚴格 JSON，不加 markdown code block）
{"gap_direction": "up"|"flat"|"down", "estimated_gap_pct": float, "key_driver": str, "tsm_signal": str, "night_futures_used": bool, "reasoning": str}
"""


def seed_pilot_shadow_profiles() -> int:
    """Seed v2 (is_shadow=1) for Phase 1 pilot agents (currently tech_analyst).
    Idempotent — skips agents that already have v2."""
    try:
        from market_analyst_agents import _MODEL_SONNET
    except Exception as exc:
        logger.error(f"[strategy_profile] cannot import model constants: {exc}")
        return 0

    pilots = [
        # (agent_name, version, prompt, model, max_tokens, params, tool_whitelist)
        ("tech_analyst", 2, _TECH_V2_SHADOW_PROMPT, _MODEL_SONNET, 1024,
         {"max_iter": 3, "token_budget": 4000},
         ["get_us_market_summary", "get_tw_night_futures"]),
    ]
    inserted = 0
    for name, version, prompt, model, mt, params, tools in pilots:
        if _profile_exists(name, version=version):
            continue
        _insert_profile(
            agent_name=name, version=version,
            is_active=0, is_shadow=1,
            system_prompt=prompt, params=params, tool_whitelist=tools,
            model_name=model, max_tokens=mt,
            notes=f"Phase 1 pilot shadow v{version} — Day 6/8 seed",
            created_by="seed_pilot_shadow", parent_version=1,
        )
        inserted += 1
        logger.info(f"[strategy_profile] seeded {name} v{version} (shadow)")
    return inserted


def seed_initial_profiles() -> int:
    """
    Seed v1 (is_active=1) for each pipeline agent using current hard-coded
    prompts from market_analyst_agents. Idempotent — agents already at v1
    are skipped. Returns the count of newly inserted rows.
    """
    try:
        from market_analyst_agents import (
            _COLLECTOR_SYSTEM, _CHIP_SYSTEM, _TECH_SYSTEM, _CHIEF_SYSTEM,
            _PORTFOLIO_SYSTEM, _FORMAT_SYSTEM,
            _MODEL_HAIKU, _MODEL_SONNET, _MODEL_OPUS,
        )
    except Exception as exc:
        logger.error(f"[strategy_profile] seed: cannot import prompts: {exc}")
        return 0

    seeds = [
        # (agent_name, prompt, model, max_tokens, params)
        ("data_collector",    _COLLECTOR_SYSTEM, _MODEL_HAIKU,  1024, {}),
        ("chip_analyst",      _CHIP_SYSTEM,      _MODEL_SONNET, 1024, {}),
        ("tech_analyst",      _TECH_SYSTEM,      _MODEL_SONNET, 1024, {}),
        ("chief_strategist",  _CHIEF_SYSTEM,     _MODEL_OPUS,   4096,
            {"thinking": "adaptive"}),
        ("portfolio_manager", _PORTFOLIO_SYSTEM, _MODEL_SONNET, 1024, {}),
        ("format_agent",      _FORMAT_SYSTEM,    _MODEL_HAIKU,  2048, {}),
    ]
    inserted = 0
    for agent_name, prompt, model, max_tokens, params in seeds:
        if _profile_exists(agent_name, version=1):
            continue
        _insert_profile(
            agent_name=agent_name, version=1,
            is_active=1, is_shadow=0,
            system_prompt=prompt,
            params=params,
            tool_whitelist=[],
            model_name=model,
            max_tokens=max_tokens,
            notes="Phase 1 seed v1 — captured from in-code constants",
            created_by="seed_v1",
        )
        inserted += 1
        logger.info(f"[strategy_profile] seeded {agent_name} v1 (active)")
    return inserted
