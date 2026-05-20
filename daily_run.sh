#!/bin/bash
# daily_run.sh — Taiwan Stock Futures Analysis Team daily automation
# Crontab: 0 8 * * 1-5 /home/itadmin/ai_agent_studio/daily_run.sh >> /home/itadmin/ai_agent_studio/logs/daily_run.log 2>&1
set -euo pipefail

LOG_DIR="/home/itadmin/ai_agent_studio/logs"
mkdir -p "$LOG_DIR"

source /home/itadmin/.local/bin/env
cd /home/itadmin/ai_agent_studio

ts() { date '+%Y-%m-%d %H:%M:%S'; }

# ── Active failure / success notification (A-3) ──────────────────────────────
# Send a LINE message immediately when any pipefail step fails, instead of
# relying on next-morning alert_runner to notice "yesterday didn't run".
notify_line() {
  MSG="$1" uv run python -c '
import os
from messenger_tools import send_line
send_line(os.environ["MSG"])
' >/dev/null 2>&1 || true
}

on_error() {
  local CODE=$?
  local LINE=$1
  local CMD=$2
  notify_line "🚨 daily_run failed (exit ${CODE}) at line ${LINE}: ${CMD}"
}
trap 'on_error $LINENO "$BASH_COMMAND"' ERR

echo "[$(ts)] ===== 台股期貨分析團隊 daily run ====="

echo "[$(ts)] Step 1: 市場資料採集"
uv run test_collection.py

echo "[$(ts)] Step 1.5: 前一交易日回測（自我評估，回填 market_actuals / session_episodes / strategy_lessons）"
# 須在 Step 2 之前執行：此時最新的建議書仍是「前一交易日」，TWSE 當日收盤資料已公布。
# 回測產出的 actuals 與 lessons 會即時供 Step 2 的 chief_strategist 注入。
uv run python backtest_agent.py || echo "⚠️  backtest_agent 失敗（略過）"

echo "[$(ts)] Step 2: 分析團隊執行 + 建議書存入 DB"
uv run investment_workflow.py

echo "[$(ts)] Step 2.5: 發送個人化持股分析給非擁有者使用者"
uv run python - <<'PYEOF'
import os
import json
from datetime import date
from database_tools import get_all_portfolio_users, get_brief
from market_analyst_agents import generate_portfolio_analysis_for_user
from messenger_tools import send_line_to_user

owner_id = os.getenv("LINE_USER_ID") or None
all_users = get_all_portfolio_users()
non_owners = [u for u in all_users if u != owner_id]

if not non_owners:
    print("無其他持倉使用者，Step 2.5 略過")
else:
    brief = get_brief(date.today())
    brief_context = (brief.get("line_report") or brief.get("brief_text") or "") if brief else ""
    for uid in non_owners:
        analysis = generate_portfolio_analysis_for_user(uid, brief_context)
        if analysis:
            result = send_line_to_user(uid, analysis)
            print(f"  → {uid[:15]}... 推播結果: {json.dumps(result, ensure_ascii=False)}")
        else:
            print(f"  → {uid[:15]}... 無持倉資料，略過")
PYEOF

echo "[$(ts)] Step 3: 傳送今日建議書（fallback，workflow 已推播則略過）"
uv run python - <<'PYEOF'
from database_tools import _engine, get_brief
from messenger_tools import send_brief
from sqlalchemy import text as sql_text
from datetime import date
import json

engine = _engine()
with engine.connect() as conn:
    already_sent = conn.execute(sql_text(
        "SELECT COUNT(*) FROM tool_audit_log"
        " WHERE tool_id = 'notification.push_investment_brief'"
        " AND status = 'ok' AND DATE(created_at) = :d"
    ), {"d": str(date.today())}).scalar() or 0

if already_sent:
    print("今日已由 workflow 推播，Step 3 略過")
else:
    r = get_brief(date.today())
    if r:
        content = r.get("line_report") or r["brief_text"]
        result = send_brief(content)
        print(f"推播結果: {json.dumps(result, ensure_ascii=False)}")
    else:
        print("⚠️  今日建議書不存在，略過推播")
PYEOF

echo "[$(ts)] Step 4: Agent 品質評估"
uv run python evaluation_runner.py || echo "⚠️  evaluation_runner 失敗（略過）"

echo "[$(ts)] Step 5: 清除過期策略教訓"
uv run python - <<'PYEOF'
from database_tools import cleanup_expired_lessons
n = cleanup_expired_lessons()
if n:
    print(f"已歸檔 {n} 筆過期 strategy_lessons")
PYEOF

echo "[$(ts)] ===== daily run 完成 ====="
notify_line "✅ daily_run completed in ${SECONDS}s"
