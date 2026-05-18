"""
twse_fetcher.py
Fetch TAIEX (發行量加權股價指數) daily data from TWSE public JSON API.
TWSE /MI_INDEX?type=IND returns close + daily change; open price is not published,
so close-to-close change is used as the gap-direction proxy.
"""
import ssl
import httpx
from datetime import date, timedelta

from loguru import logger

_TWSE_MI_INDEX = "https://www.twse.com.tw/exchangeReport/MI_INDEX"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TaiwanStockBot/1.0)"}


def _twse_ssl_context() -> ssl.SSLContext:
    # TWSE certificate chain (legacy TW gov CA) lacks Subject Key Identifier extension.
    # OpenSSL 3.x enforces SKI via verify_flags; resetting to 0 removes that check
    # while keeping CA chain validation and hostname verification intact.
    ctx = ssl.create_default_context()
    ctx.verify_flags = 0
    return ctx


def _fetch_ind(trade_date: date) -> dict:
    date_str = trade_date.strftime("%Y%m%d")
    url = f"{_TWSE_MI_INDEX}?response=json&date={date_str}&type=IND"
    try:
        with httpx.Client(timeout=10.0, headers=_HEADERS, verify=_twse_ssl_context()) as client:
            resp = client.get(url)
            resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning(f"[TWSE] fetch failed for {date_str}: {exc}")
        return {}


def _parse_taiex(mi_data: dict) -> dict | None:
    """
    Walk tables in MI_INDEX IND response and return the 發行量加權股價指數 row as a dict.
    Expected fields: 收盤指數, 漲跌點數, 漲跌百分比(%)
    """
    for table in mi_data.get("tables", []):
        fields = table.get("fields", [])
        for row in table.get("data", []):
            if row and "發行量加權股價指數" in str(row[0]):
                mapped = dict(zip(fields, row))
                try:
                    def _f(key: str) -> float:
                        val = str(mapped.get(key, "0")).replace(",", "").lstrip("+")
                        return float(val) if val not in ("", "--", "---") else 0.0

                    close      = _f("收盤指數")
                    change_pts = _f("漲跌點數")
                    change_pct = _f("漲跌百分比(%)")
                    return {
                        "close":      round(close, 2),
                        "change_pts": round(change_pts, 2),
                        "change_pct": round(change_pct, 2),
                        "prev_close": round(close - change_pts, 2),
                    }
                except (ValueError, TypeError) as exc:
                    logger.warning(f"[TWSE] parse error: {exc} | row={mapped}")
                    return None
    return None


def get_taiex_actuals(trade_date: date, max_lookback: int = 5) -> dict | None:
    """
    Return TAIEX actuals for trade_date (or the most recent trading day within
    max_lookback days if the exact date has no data).

    Returned keys:
        trade_date      — the date data was found for (may differ from input)
        prev_close      — previous close derived from 漲跌點數
        open_price      — same as prev_close (TWSE API does not publish intraday open)
        close_price     — 收盤指數
        actual_gap_pct  — close-to-close change % (proxy for gap direction)
        close_chg_pct   — same as actual_gap_pct
        source          — "TWSE"
    """
    check_date = trade_date
    for _ in range(max_lookback):
        raw = _fetch_ind(check_date)
        if raw.get("stat") == "OK":
            row = _parse_taiex(raw)
            if row:
                logger.info(
                    f"[TWSE] {check_date} — 收盤 {row['close']} 漲跌 {row['change_pct']:+.2f}%"
                )
                return {
                    "trade_date":     str(check_date),
                    "prev_close":     row["prev_close"],
                    "open_price":     row["prev_close"],
                    "close_price":    row["close"],
                    "actual_gap_pct": row["change_pct"],
                    "close_chg_pct":  row["change_pct"],
                    "source":         "TWSE",
                }
        check_date -= timedelta(days=1)

    logger.error(f"[TWSE] No data for {trade_date} within {max_lookback}-day lookback")
    return None
