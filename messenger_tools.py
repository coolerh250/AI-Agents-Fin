"""
messenger_tools.py
LINE Messaging API + Telegram push notification helpers.
LINE Notify was discontinued 2025-03-31; this module uses the Messaging API instead.
"""
import os
import re

import httpx
from dotenv import load_dotenv

load_dotenv()

_LINE_PUSH_API = "https://api.line.me/v2/bot/message/push"
_TG_API = "https://api.telegram.org/bot{token}/sendMessage"
_LINE_MAX = 4000
_TG_MAX = 4096


def format_brief(brief_text: str) -> str:
    sections = []
    for title in ("盤勢定調", "操作策略", "關鍵防守點"):
        match = re.search(rf"【{title}】\n(.*?)(?=【|\Z)", brief_text, re.DOTALL)
        if match:
            sections.append(f"【{title}】\n{match.group(1).strip()}")
    return "📊 台股期貨今日建議書\n\n" + "\n\n".join(sections)


def send_line(message: str) -> dict:
    """Send via LINE Messaging API (Push Message).
    Requires LINE_CHANNEL_ACCESS_TOKEN and LINE_USER_ID in .env.
    Setup: https://developers.line.biz/ → Messaging API channel → Channel access token
    """
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    user_id = os.getenv("LINE_USER_ID", "")
    if not token or not user_id:
        return {"status": "skipped", "reason": "LINE_CHANNEL_ACCESS_TOKEN or LINE_USER_ID not set"}
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                _LINE_PUSH_API,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "to": user_id,
                    "messages": [{"type": "text", "text": message[:_LINE_MAX]}],
                },
            )
            resp.raise_for_status()
        return {"status": "ok", "channel": "line", "http_status": resp.status_code}
    except Exception as exc:
        return {"status": "error", "channel": "line", "error": str(exc)}


def send_telegram(message: str) -> dict:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return {"status": "skipped", "reason": "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set"}
    try:
        url = _TG_API.format(token=token)
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json={
                "chat_id": chat_id,
                "text": message[:_TG_MAX],
            })
            resp.raise_for_status()
        return {"status": "ok", "channel": "telegram", "http_status": resp.status_code}
    except Exception as exc:
        return {"status": "error", "channel": "telegram", "error": str(exc)}


def send_brief(brief_text: str) -> dict:
    """Send the investment brief to all configured channels. Text is sent as-is."""
    return {
        "line":     send_line(brief_text[:_LINE_MAX]),
        "telegram": send_telegram(brief_text[:_TG_MAX]),
    }


def send_line_to_user(user_id: str, message: str) -> dict:
    """Push a message to a specific LINE user by user_id (not the default LINE_USER_ID)."""
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    if not token:
        return {"status": "skipped", "reason": "LINE_CHANNEL_ACCESS_TOKEN not set"}
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                _LINE_PUSH_API,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "to": user_id,
                    "messages": [{"type": "text", "text": message[:_LINE_MAX]}],
                },
            )
            resp.raise_for_status()
        return {"status": "ok", "channel": "line", "user_id": user_id,
                "http_status": resp.status_code}
    except Exception as exc:
        return {"status": "error", "channel": "line", "user_id": user_id, "error": str(exc)}
