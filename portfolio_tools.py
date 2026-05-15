"""
portfolio_tools.py
Personal portfolio helpers: load holdings from TiDB, enrich with live prices via yfinance.
"""
from typing import Optional

import yfinance as yf
from loguru import logger

from database_tools import get_portfolio


def get_user_portfolio(user_id: Optional[str] = None) -> list[dict]:
    """Return holdings for a LINE user. user_id=None returns legacy NULL-user records."""
    return get_portfolio(user_id=user_id)


def calculate_pnl(holdings: list[dict]) -> list[dict]:
    """
    Enrich each holding with live P&L data.
    Falls back to entry_price when yfinance returns no data.
    """
    enriched = []
    for h in holdings:
        entry_price = float(h["entry_price"])
        quantity    = int(h["quantity"])
        stop_loss   = float(h["stop_loss_level"])
        current_price = entry_price

        try:
            df = yf.Ticker(f"{h['stock_id']}.TW").history(period="1d")
            if not df.empty:
                current_price = float(df["Close"].iloc[-1])
            else:
                logger.warning(f"[PortfolioTools] {h['stock_id']}.TW: empty DataFrame, using entry_price")
        except Exception as exc:
            logger.warning(f"[PortfolioTools] {h['stock_id']}.TW: yfinance error ({exc}), using entry_price")

        enriched.append({
            **h,
            "current_price":       current_price,
            "unrealized_pnl":      (current_price - entry_price) * quantity,
            "pnl_pct":             (current_price - entry_price) / entry_price * 100,
            "stop_loss_triggered": current_price < entry_price * (1 - stop_loss / 100),
        })

    return enriched
