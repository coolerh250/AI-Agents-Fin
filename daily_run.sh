#!/bin/bash
# daily_run.sh — Taiwan Stock Futures Analysis Team daily automation
# Crontab: 20 8 * * 1-5 /home/itadmin/ai_agent_studio/daily_run.sh >> /home/itadmin/ai_agent_studio/logs/daily_run.log 2>&1
set -euo pipefail

LOG_DIR="/home/itadmin/ai_agent_studio/logs"
mkdir -p "$LOG_DIR"

source /home/itadmin/.local/bin/env
cd /home/itadmin/ai_agent_studio

ts() { date '+%Y-%m-%d %H:%M:%S'; }

echo "[$(ts)] ===== 台股期貨分析團隊 daily run ====="

echo "[$(ts)] Step 1: 市場資料採集"
uv run test_collection.py

echo "[$(ts)] Step 2: 分析團隊執行 + 建議書存入 DB"
uv run investment_workflow.py

echo "[$(ts)] Step 3: 傳送今日建議書"
uv run python - <<'PYEOF'
from database_tools import get_brief
from messenger_tools import send_brief
from datetime import date
import json

r = get_brief(date.today())
if r:
    result = send_brief(r["brief_text"])
    print(f"推播結果: {json.dumps(result, ensure_ascii=False)}")
else:
    print("⚠️  今日建議書不存在，略過推播")
PYEOF

echo "[$(ts)] ===== daily run 完成 ====="
