"""
finance_mcp_server.py
Financial Data Collector — MCP Server for Taiwan Stock Futures Analysis Team
Tools: get_tw_future_chips | get_us_market_summary | get_financial_news | save_brief_to_db | send_brief_to_user
Resources: finance://backtest/report
"""
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Allow importing database_tools from the project root
sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
import yfinance as yf
from bs4 import BeautifulSoup
from loguru import logger
from mcp.server.fastmcp import FastMCP

logger.remove()
logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}", level="DEBUG")

mcp = FastMCP("finance-collector")
logger.info("Finance Collector MCP Server initializing")

_TAIFEX_URL = "https://www.taifex.com.tw/cht/3/futContractsDate"
_ANUE_URL = "https://api.cnyes.com/media/api/v1/newslist/category/tw_stock?limit=20&page=1"
_YAHOO_V8 = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=2d"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

US_SYMBOLS = {
    "^DJI": "DJIA",
    "^NDX": "NASDAQ 100",
    "^SOX": "PHLX SOX",
    "TSM":  "TSMC ADR",
}


def _retry(fn: Callable, retries: int = 1, delay: float = 2.0) -> Any:
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                logger.warning(f"Retry {attempt + 1}/{retries} after: {exc}")
                time.sleep(delay)
    raise last_exc


def _parse_num(text: str) -> int:
    return int(text.strip().replace(",", "").replace("+", "").replace("\xa0", ""))


@mcp.tool()
def get_tw_future_chips() -> dict:
    """Fetch TAIFEX institutional traders futures open interest (三大法人期貨留倉部位)."""
    logger.info("Tool 'get_tw_future_chips' invoked")
    date_str = date.today().strftime("%Y/%m/%d")

    def _fetch(commodity_id: str = "TXF") -> dict:
        payload = {
            "queryType": "1", "goDay": "", "doQuery": "1",
            "dateaddcnt": "", "queryDate": date_str, "commodityId": commodity_id,
        }
        with httpx.Client(timeout=15.0, headers=_HEADERS, follow_redirects=True) as client:
            resp = client.post(_TAIFEX_URL, data=payload)
            resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")
        table = soup.find("table", {"class": lambda c: c and "table_f" in c})
        if not table:
            raise ValueError("TAIFEX table not found — page structure may have changed")

        rows = table.find_all("tr", class_="12bk")
        if not rows:
            raise ValueError(f"No data rows (class='12bk') for date {date_str}")

        identity_map = {
            "自營商": "proprietary_dealers",
            "投信":   "investment_trust",
            "外資":   "foreign_investors",
        }
        data: dict = {}

        for tr in rows:
            id_td = tr.find("td", {"class": lambda c: c and "serial-3" in c})
            if not id_td:
                continue
            id_text = id_td.get_text(strip=True)
            key = next((v for k, v in identity_map.items() if k in id_text), None)
            if not key:
                continue

            # Use align="right" to select only numeric cells, which avoids
            # rowspan shift on the first row where the product-name TD is present.
            num_tds = tr.find_all("td", attrs={"align": "right"})
            if not num_tds:
                num_tds = [td for td in tr.find_all("td")
                           if td != id_td and not td.get("class")]
            if len(num_tds) < 11:
                raise ValueError(f"Expected >=11 numeric cells, got {len(num_tds)} for {id_text}")

            def _cell(idx: int, _tds=num_tds) -> int:
                td = _tds[idx]
                span = td.find("span", class_="blue")
                node = span if span else td.find("div")
                raw = node.get_text(strip=True) if node else td.get_text(strip=True)
                return _parse_num(raw)

            data[key] = {"oi_long": _cell(6), "oi_short": _cell(8), "oi_net": _cell(10)}

        if len(data) < 3:
            raise ValueError(f"Parsed only {len(data)}/3 identity rows")

        logger.success(
            f"[get_tw_future_chips] foreign net={data.get('foreign_investors', {}).get('oi_net')}"
        )
        return {
            "date": date_str,
            "source": "TAIFEX futContractsDate POST",
            "product": "臺股期貨 (TXF)",
            "data": data,
        }

    try:
        return _retry(lambda: _fetch("TXF"))
    except Exception as exc:
        logger.warning(f"TXF filter failed ({exc}), retrying without filter")
        try:
            return _retry(lambda: _fetch(""))
        except Exception as exc2:
            logger.error(f"[get_tw_future_chips] Both attempts failed: {exc2}")
            return {"error": True, "message": str(exc2), "source": "TAIFEX futContractsDate"}


@mcp.tool()
def get_us_market_summary() -> dict:
    """Fetch US market closing data: DJIA, NASDAQ 100, PHLX SOX, TSMC ADR."""
    logger.info("Tool 'get_us_market_summary' invoked")
    markets = []

    for sym, name in US_SYMBOLS.items():
        try:
            price = change = change_pct = volume = None
            try:
                ticker = yf.Ticker(sym)
                fi = ticker.fast_info
                price = fi.last_price
                prev = fi.previous_close
                volume = fi.last_volume
                if price is None or prev is None:
                    raise ValueError("fast_info returned None")
                change = round(price - prev, 2)
                change_pct = round((price - prev) / prev * 100, 2)
                logger.debug(f"  yfinance OK: {sym}={price}")
            except Exception as yf_exc:
                logger.warning(f"  yfinance fallback for {sym}: {yf_exc}")
                url = _YAHOO_V8.format(sym=sym)
                with httpx.Client(timeout=10.0, headers=_HEADERS) as client:
                    r = client.get(url)
                    r.raise_for_status()
                meta = r.json()["chart"]["result"][0]["meta"]
                price = meta.get("regularMarketPrice")
                prev = meta.get("chartPreviousClose")
                volume = meta.get("regularMarketVolume")
                if price and prev:
                    change = round(price - prev, 2)
                    change_pct = round((price - prev) / prev * 100, 2)
                logger.debug(f"  Yahoo v8 OK: {sym}={price}")

            markets.append({
                "symbol": sym, "name": name,
                "price": round(price, 2) if price else None,
                "change": change, "change_pct": change_pct, "volume": volume,
            })
        except Exception as exc:
            logger.error(f"  {sym} failed: {exc}")
            markets.append({"symbol": sym, "name": name, "error": True, "message": str(exc)})

    ok = sum(1 for m in markets if not m.get("error"))
    logger.success(f"[get_us_market_summary] {ok}/{len(US_SYMBOLS)} symbols OK")
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "source": "yfinance",
        "markets": markets,
    }


@mcp.tool()
def get_financial_news() -> dict:
    """Scan Anue (鉅亨網) financial news headlines for Taiwan stock market."""
    logger.info("Tool 'get_financial_news' invoked")
    try:
        def _fetch():
            with httpx.Client(
                timeout=10.0, headers={**_HEADERS, "Accept": "application/json"}
            ) as client:
                resp = client.get(_ANUE_URL)
                resp.raise_for_status()
                return resp.json()

        raw = _retry(_fetch)
        items = raw["items"]["data"][:15]
        news = []
        for item in items:
            ts = item.get("publishAt")
            news.append({
                "title":    item.get("title", ""),
                "time":     datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None,
                "category": item.get("categoryName", ""),
                "url":      f"https://news.cnyes.com/news/id/{item.get('newsId', '')}",
            })
        logger.success(f"[get_financial_news] {len(news)} headlines fetched")
        return {
            "source":     "Anue 鉅亨網",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "count":      len(news),
            "news":       news,
        }
    except Exception as exc:
        logger.error(f"[get_financial_news] Failed: {exc}")
        return {"error": True, "message": str(exc), "source": "Anue API"}


@mcp.tool()
def save_brief_to_db(
    trade_date: str,
    brief_text: str,
    predicted_gap_pct: Optional[float] = None,
    gap_direction: Optional[str] = None,
) -> dict:
    """Save a ChiefStrategist brief to TiDB (agent_memory.daily_briefs).
    trade_date must be YYYY-MM-DD format.
    """
    logger.info(f"Tool 'save_brief_to_db' invoked for {trade_date}")
    try:
        from database_tools import save_brief
        from datetime import date as _date
        d = _date.fromisoformat(trade_date)
        row_id = save_brief(d, brief_text, predicted_gap_pct, gap_direction)
        logger.success(f"[save_brief_to_db] Saved row id={row_id} for {trade_date}")
        return {"success": True, "row_id": row_id, "trade_date": trade_date}
    except Exception as exc:
        logger.error(f"[save_brief_to_db] Failed: {exc}")
        return {"success": False, "error": str(exc)}


@mcp.tool()
def send_brief_to_user(brief_text: str) -> dict:
    """Send today's investment brief to Line/Telegram. Returns delivery status per channel."""
    logger.info("Tool 'send_brief_to_user' invoked")
    try:
        from messenger_tools import send_brief
        result = send_brief(brief_text)
        logger.success(f"[send_brief_to_user] {result}")
        return result
    except Exception as exc:
        logger.error(f"[send_brief_to_user] Failed: {exc}")
        return {"error": True, "message": str(exc)}


@mcp.resource("finance://backtest/report")
def backtest_report_resource() -> str:
    """Last 5 trading days prediction accuracy as a Markdown table."""
    try:
        from database_tools import get_recent_accuracy
        rows = get_recent_accuracy(5)
        if not rows:
            return "⚠️ 尚無回測數據（請先執行 backtest_agent.py）"
        lines = [
            "| 日期 | 預測方向 | 預測跳空% | 實際跳空% | 誤差 |",
            "|------|---------|---------|---------|------|",
        ]
        for r in rows:
            pred = r.get("predicted_gap_pct")
            actual = r.get("actual_gap_pct")
            err = abs(round((pred or 0.0) - (actual or 0.0), 2)) if pred is not None else "-"
            lines.append(
                f"| {r['trade_date']} | {r.get('gap_direction') or '-'} | "
                f"{pred if pred is not None else '-'} | {actual if actual is not None else '-'} | {err} |"
            )
        return "\n".join(lines)
    except Exception as exc:
        logger.error(f"[backtest_report_resource] {exc}")
        return f"⚠️ 查詢失敗：{exc}"


if __name__ == "__main__":
    logger.info("Starting Finance Collector MCP Server via stdio transport")
    mcp.run(transport="stdio")
