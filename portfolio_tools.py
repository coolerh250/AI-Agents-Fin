"""
portfolio_tools.py
Personal portfolio helpers: load holdings from TiDB, enrich with live prices,
technical indicators (RSI14, MA5/MA20), and TWSE institution flows.
"""
from typing import Optional

import httpx
import pandas as pd
import yfinance as yf
from loguru import logger

from database_tools import get_portfolio

_TWSE_T86_URL = (
    "https://www.twse.com.tw/fund/T86?response=json&selectType=ALLBUT0999"
)


def get_user_portfolio(user_id: Optional[str] = None,
                       _caller: Optional[str] = None) -> list[dict]:
    """Return holdings for a LINE user. user_id=None returns legacy NULL-user records."""
    from database_tools import validate_tool_permission
    validate_tool_permission("get_user_portfolio", _caller)
    return get_portfolio(user_id=user_id, _caller=_caller)


def _calc_rsi(closes: pd.Series, period: int = 14) -> float:
    delta = closes.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs    = gain / loss
    return round(float(100 - 100 / (1 + rs.iloc[-1])), 1)


def _fetch_institution_flows(stock_ids: list[str]) -> dict[str, dict]:
    """
    Fetch today's TWSE T86 institution net buy/sell for the given stock IDs.
    Returns {stock_id: {foreign_net, trust_net, dealer_net, institution_net}}.
    Returns {} on non-trading days or any error.
    """
    if not stock_ids:
        return {}
    try:
        resp = httpx.get(
            _TWSE_T86_URL,
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("stat") != "OK":
            logger.debug(f"[PortfolioTools] TWSE T86: {body.get('stat', 'no stat')}")
            return {}
        fields = body["fields"]
        idx = {f: i for i, f in enumerate(fields)}
        fi = idx.get(
            "外陸資買賣超股數(不含外資自營商)",
            idx.get("外資買賣超股數", 4),
        )
        ti = idx.get("投信買賣超股數", 7)
        di = idx.get("自營商買賣超股數", 14)
        ni = idx.get("三大法人買賣超股數", 15)

        def _parse(v: str) -> int:
            return int(v.replace(",", "").replace("+", "") or "0")

        result: dict[str, dict] = {}
        for row in body["data"]:
            sid = row[0].strip()
            if sid in stock_ids:
                result[sid] = {
                    "foreign_net":     _parse(row[fi]),
                    "trust_net":       _parse(row[ti]),
                    "dealer_net":      _parse(row[di]),
                    "institution_net": _parse(row[ni]),
                }
        return result
    except Exception as exc:
        logger.warning(f"[PortfolioTools] TWSE T86 取得失敗: {exc}")
        return {}


def calculate_pnl(holdings: list[dict], _caller: Optional[str] = None) -> list[dict]:
    """
    Enrich each holding with live P&L, technical indicators, and institution flows.
    Falls back gracefully when any data source is unavailable.
    """
    from database_tools import validate_tool_permission
    validate_tool_permission("calculate_pnl", _caller)
    stock_ids  = [h["stock_id"] for h in holdings]
    inst_flows = _fetch_institution_flows(stock_ids)
    enriched   = []

    for h in holdings:
        entry_price   = float(h["entry_price"])
        quantity      = int(h["quantity"])
        stop_loss     = float(h["stop_loss_level"])
        current_price = entry_price
        rsi_14 = ma5 = ma20 = price_vs_ma20 = None

        try:
            df = yf.Ticker(f"{h['stock_id']}.TW").history(period="60d")
            if not df.empty:
                closes        = df["Close"]
                current_price = float(closes.iloc[-1])
                if len(closes) >= 20:
                    ma5           = round(float(closes.tail(5).mean()), 2)
                    ma20          = round(float(closes.tail(20).mean()), 2)
                    price_vs_ma20 = "上方" if current_price >= ma20 else "下方"
                if len(closes) >= 15:
                    rsi_14 = _calc_rsi(closes)
            else:
                logger.warning(
                    f"[PortfolioTools] {h['stock_id']}.TW: empty DataFrame, using entry_price"
                )
        except Exception as exc:
            logger.warning(
                f"[PortfolioTools] {h['stock_id']}.TW: yfinance error ({exc}), using entry_price"
            )

        flows = inst_flows.get(h["stock_id"], {})
        enriched.append({
            **h,
            "current_price":       current_price,
            "unrealized_pnl":      (current_price - entry_price) * quantity,
            "pnl_pct":             (current_price - entry_price) / entry_price * 100,
            "stop_loss_triggered": current_price < entry_price * (1 - stop_loss / 100),
            "rsi_14":        rsi_14,
            "ma5":           ma5,
            "ma20":          ma20,
            "price_vs_ma20": price_vs_ma20,
            "foreign_net":     flows.get("foreign_net"),
            "trust_net":       flows.get("trust_net"),
            "dealer_net":      flows.get("dealer_net"),
            "institution_net": flows.get("institution_net"),
        })

    return enriched
