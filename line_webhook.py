"""
line_webhook.py
LINE Messaging API webhook server — Phase 6: portfolio management via LINE messages.

Supported commands (send as LINE text messages):
  查詢持股 / 持股          → show current holdings with live P&L
  新增 2330 1000股 成本850  → add holding (qty + entry price)
  新增 2330 1000 850        → add holding (qty + entry price, shorthand)
  刪除 2330                → delete holding by stock_id
  更新 2330 成本900         → update entry_price for a stock
  說明 / help              → show command reference

Run: uvicorn line_webhook:app --host 0.0.0.0 --port 8502
"""
import base64
import hashlib
import hmac
import json
import os
import re

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from loguru import logger

from database_tools import (
    add_portfolio_item,
    create_login_token,
    delete_portfolio_by_stock,
    get_portfolio,
    update_portfolio_entry_price,
)
from portfolio_tools import calculate_pnl

load_dotenv()

app = FastAPI(title="LINE Portfolio Webhook")

_LINE_REPLY_API = "https://api.line.me/v2/bot/message/reply"
_LINE_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
_WEBHOOK_SECRET = os.getenv("LINE_WEBHOOK_SECRET", "").encode()

# ── Command patterns ───────────────────────────────────────────────────────────

_RE_ADD = re.compile(
    r"新增\s*(\d{4})\s*[,，]?\s*(\d+)\s*股?\s*[,，]?\s*成本\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_RE_ADD_SHORT = re.compile(
    r"新增\s*(\d{4})\s+(\d+)\s+(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_RE_DELETE = re.compile(r"刪除\s*(\d{4})", re.IGNORECASE)
_RE_UPDATE = re.compile(
    r"更新\s*(\d{4})\s*[,，]?\s*成本\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


# ── Signature verification ─────────────────────────────────────────────────────

def _verify_signature(body: bytes, sig_header: str) -> bool:
    if not _WEBHOOK_SECRET:
        logger.warning("LINE_WEBHOOK_SECRET not set — skipping signature verification")
        return True
    expected = base64.b64encode(
        hmac.new(_WEBHOOK_SECRET, body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(sig_header, expected)


# ── Reply helper ───────────────────────────────────────────────────────────────

def _reply(reply_token: str, text: str) -> None:
    if not _LINE_TOKEN:
        logger.warning("LINE_CHANNEL_ACCESS_TOKEN not set — cannot reply")
        return
    try:
        with httpx.Client(timeout=10.0) as client:
            client.post(
                _LINE_REPLY_API,
                headers={
                    "Authorization": f"Bearer {_LINE_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={
                    "replyToken": reply_token,
                    "messages": [{"type": "text", "text": text[:4000]}],
                },
            )
    except Exception as exc:
        logger.error(f"[webhook] reply failed: {exc}")


# ── Command handlers ───────────────────────────────────────────────────────────

def _handle_query(user_id: str) -> str:
    holdings = get_portfolio(user_id=user_id, _caller="line_webhook")
    if not holdings:
        return "目前沒有持倉記錄。\n輸入「說明」查看新增指令。"
    enriched = calculate_pnl(holdings, _caller="line_webhook")
    lines = ["📊 持倉損益報告\n"]
    for h in enriched:
        pnl_sign = "+" if h["unrealized_pnl"] >= 0 else ""
        lines.append(
            f"【{h['stock_id']}】"
            f" 成本 {h['entry_price']} → 現價 {h['current_price']:.0f}\n"
            f"  持股 {h['quantity']} 股 | 損益 {pnl_sign}{h['unrealized_pnl']:.0f} 元"
            f" ({pnl_sign}{h['pnl_pct']:.1f}%)\n"
            f"  策略: {h['strategy_type']} | 止損: {'⚠️ 觸發' if h['stop_loss_triggered'] else '未觸發'}"
        )
    return "\n".join(lines)


def _handle_add(user_id: str, stock_id: str, qty: int, entry: float) -> str:
    ok = add_portfolio_item(
        stock_id=stock_id,
        entry_price=entry,
        quantity=qty,
        user_id=user_id,
        _caller="line_webhook",
    )
    if ok:
        return f"✅ 已新增 {stock_id}，{qty} 股，成本 {entry} 元"
    return f"⚠️ {stock_id} 已存在持倉，請先刪除再新增，或用「更新」修改成本。"


def _handle_delete(user_id: str, stock_id: str) -> str:
    deleted = delete_portfolio_by_stock(stock_id=stock_id, user_id=user_id)
    if deleted:
        return f"🗑 已刪除 {stock_id} 持倉"
    return f"⚠️ 找不到 {stock_id} 持倉記錄"


def _handle_update(user_id: str, stock_id: str, entry: float) -> str:
    updated = update_portfolio_entry_price(
        stock_id=stock_id, entry_price=entry, user_id=user_id
    )
    if updated:
        return f"✅ {stock_id} 成本已更新為 {entry} 元"
    return f"⚠️ 找不到 {stock_id} 持倉，請先新增"


_HELP_TEXT = """\
📋 持倉管理指令

查詢持股         — 顯示持倉損益
新增 2330 1000股 成本850  — 新增持倉
新增 2330 1000 850        — 簡短格式
刪除 2330        — 刪除持倉
更新 2330 成本900 — 修改成本價
登入代碼         — 取得 Dashboard 登入代碼

每個帳號的資料互相隔離。"""


def _dispatch(user_id: str, text: str) -> str:
    text = text.strip()
    if text in ("登入代碼", "login", "Login", "登入"):
        token = create_login_token(user_id)
        return (
            f"🔑 您的 Dashboard 登入代碼：\n\n"
            f"  {token}\n\n"
            f"5 分鐘內有效，請立即在 Dashboard 輸入。\n"
            f"⚠️ 請勿將代碼告知他人"
        )
    if text in ("查詢持股", "持股", "查詢", "portfolio"):
        return _handle_query(user_id)
    if text in ("說明", "help", "?", "？"):
        return _HELP_TEXT
    m = _RE_ADD.search(text) or _RE_ADD_SHORT.search(text)
    if m:
        return _handle_add(user_id, m.group(1), int(m.group(2)), float(m.group(3)))
    m = _RE_DELETE.search(text)
    if m:
        return _handle_delete(user_id, m.group(1))
    m = _RE_UPDATE.search(text)
    if m:
        return _handle_update(user_id, m.group(1), float(m.group(2)))
    return "❓ 不認識的指令。輸入「說明」查看可用指令。"


# ── Webhook endpoint ───────────────────────────────────────────────────────────

@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    sig = request.headers.get("X-Line-Signature", "")
    if not _verify_signature(body, sig):
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    for event in payload.get("events", []):
        if event.get("type") != "message":
            continue
        msg = event.get("message", {})
        if msg.get("type") != "text":
            continue
        user_id: str = event["source"]["userId"]
        text: str = msg["text"]
        reply_token: str = event["replyToken"]
        logger.info(f"[webhook] user={user_id[:8]}... msg={text[:40]}")
        reply_text = _dispatch(user_id, text)
        _reply(reply_token, reply_text)

    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok"}
