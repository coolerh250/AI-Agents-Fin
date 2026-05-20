"""
mcp_servers/market_data_server.py
Market Data MCP Server — Taiwan Stock Futures Analysis Team
Tools: get_tw_future_chips | get_us_indices | get_tsm_adr | get_us_market_summary | get_financial_news | get_tw_night_futures
Env: public APIs only — no credentials required
"""
import asyncio
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import yfinance as yf
from bs4 import BeautifulSoup
from loguru import logger
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).parent.parent))

_INJECTION_RE = re.compile(
    r"(?i)("
    r"ignore\s+(all\s+)?previous"
    r"|(system|assistant)\s*:\s*(prompt|message)"
    r"|override|jailbreak"
    r"|新的指示|改變系統提示|忽略之前"
    r")"
)

_TAIFEX_URL       = "https://www.taifex.com.tw/cht/3/futContractsDate"
_TAIFEX_NIGHT_URL = "https://www.taifex.com.tw/cht/5/afterHoursPrice"
_ANUE_URL   = "https://api.cnyes.com/media/api/v1/newslist/category/tw_stock?limit=20&page=1"
_YAHOO_V8   = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=2d"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

INDEX_SYMBOLS = {"^DJI": "djia", "^NDX": "ndx", "^SOX": "sox"}
TSM_SYMBOL    = "TSM"

logger.remove()
logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}", level="DEBUG")

mcp = FastMCP("market-data")
logger.info("Market Data MCP Server initializing")


def _sanitize_title(title: str) -> str:
    if _INJECTION_RE.search(title):
        logger.warning(f"[news] filtered suspected injection: {title[:60]}")
        return "[filtered]"
    return title[:200]


async def _aretry(fn: Callable, retries: int = 1, delay: float = 2.0) -> Any:
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                logger.warning(f"Retry {attempt + 1}/{retries} after: {exc}")
                await asyncio.sleep(delay)
    raise last_exc


def _parse_num(text: str) -> int:
    return int(text.strip().replace(",", "").replace("+", "").replace("\xa0", ""))


def _fetch_symbol(sym: str) -> dict:
    """Fetch single symbol via yfinance with Yahoo v8 fallback."""
    try:
        ticker = yf.Ticker(sym)
        fi = ticker.fast_info
        price = fi.last_price
        prev  = fi.previous_close
        if price is None or prev is None:
            raise ValueError("fast_info returned None")
        chg_pct = round((price - prev) / prev * 100, 2)
        return {"price": round(price, 2), "chg_pct": chg_pct, "source": "yfinance"}
    except Exception as yf_exc:
        logger.warning(f"  yfinance fallback for {sym}: {yf_exc}")
        url = _YAHOO_V8.format(sym=sym)
        with httpx.Client(timeout=10.0, headers=_HEADERS) as client:
            r = client.get(url)
            r.raise_for_status()
        meta = r.json()["chart"]["result"][0]["meta"]
        price = meta["regularMarketPrice"]
        prev  = meta["chartPreviousClose"]
        chg_pct = round((price - prev) / prev * 100, 2) if price and prev else None
        return {"price": round(price, 2) if price else None, "chg_pct": chg_pct, "source": "yahoo_v8"}


def _fetch_us_indices_raw() -> dict:
    result, stale = {}, []
    for sym, key in INDEX_SYMBOLS.items():
        try:
            d = _fetch_symbol(sym)
            result[key] = d
        except Exception as exc:
            logger.error(f"  {sym} failed: {exc}")
            result[key] = None
            stale.append(sym)
    return {"data": result, "stale_symbols": stale}


def _fetch_tsm_raw() -> dict:
    return _fetch_symbol(TSM_SYMBOL)


@mcp.tool()
async def get_tw_future_chips() -> dict:
    """Fetch TAIFEX institutional traders futures open interest (三大法人期貨留倉部位).
    Scrapes https://www.taifex.com.tw. Returns {"error": true} on holiday/failure."""
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
            raise ValueError(f"No data rows for date {date_str}")

        identity_map = {"自營商": "proprietary_dealers", "投信": "investment_trust",
                        "外資": "foreign_investors"}
        data: dict = {}
        for tr in rows:
            id_td = tr.find("td", {"class": lambda c: c and "serial-3" in c})
            if not id_td:
                continue
            key = next((v for k, v in identity_map.items() if k in id_td.get_text(strip=True)), None)
            if not key:
                continue
            num_tds = tr.find_all("td", attrs={"align": "right"})
            if not num_tds:
                num_tds = [td for td in tr.find_all("td")
                           if td != id_td and not td.get("class")]
            if len(num_tds) < 11:
                raise ValueError(f"Expected >=11 numeric cells, got {len(num_tds)} for {key}")

            def _cell(idx: int, _tds=num_tds) -> int:
                td = _tds[idx]
                span = td.find("span", class_="blue")
                node = span if span else td.find("div")
                raw = node.get_text(strip=True) if node else td.get_text(strip=True)
                return _parse_num(raw)

            data[key] = {"oi_long": _cell(6), "oi_short": _cell(8), "oi_net": _cell(10)}

        if len(data) < 3:
            raise ValueError(f"Parsed only {len(data)}/3 identity rows")
        logger.success(f"[get_tw_future_chips] foreign net={data.get('foreign_investors', {}).get('oi_net')}")
        return {"date": date_str, "source": "TAIFEX futContractsDate POST",
                "product": "臺股期貨 (TXF)", "data": data}

    try:
        return await _aretry(lambda: _fetch("TXF"))
    except Exception as exc:
        logger.warning(f"TXF filter failed ({exc}), retrying without filter")
        try:
            return await _aretry(lambda: _fetch(""))
        except Exception as exc2:
            logger.error(f"[get_tw_future_chips] Both attempts failed: {exc2}")
            return {"error": True, "message": str(exc2), "source": "TAIFEX futContractsDate"}


@mcp.tool()
async def get_us_indices() -> dict:
    """Fetch DJIA (^DJI), NASDAQ 100 (^NDX), PHLX SOX (^SOX) previous-day closes.
    Source: yfinance primary, Yahoo v8 fallback per symbol.
    Returns: {"djia_chg_pct": float, "ndx_chg_pct": float, "sox_chg_pct": float,
              "djia_close": float, "ndx_close": float, "sox_close": float,
              "stale_symbols": list, "as_of": str}"""
    logger.info("Tool 'get_us_indices' invoked")
    try:
        raw = await _aretry(_fetch_us_indices_raw)
        d = raw["data"]
        return {
            "djia_chg_pct": d["djia"]["chg_pct"] if d["djia"] else None,
            "ndx_chg_pct":  d["ndx"]["chg_pct"]  if d["ndx"]  else None,
            "sox_chg_pct":  d["sox"]["chg_pct"]  if d["sox"]  else None,
            "djia_close":   d["djia"]["price"]   if d["djia"] else None,
            "ndx_close":    d["ndx"]["price"]    if d["ndx"]  else None,
            "sox_close":    d["sox"]["price"]    if d["sox"]  else None,
            "stale_symbols": raw["stale_symbols"],
            "as_of": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        logger.error(f"[get_us_indices] Failed: {exc}")
        return {"error": True, "message": str(exc)}


@mcp.tool()
async def get_tsm_adr() -> dict:
    """Fetch TSMC ADR (TSM) previous-day close and change %.
    Separated from US indices to allow tech_analyst to weight TSM as a distinct signal.
    Returns: {"tsm_close": float, "tsm_chg_pct": float, "source": str, "as_of": str}"""
    logger.info("Tool 'get_tsm_adr' invoked")
    try:
        raw = await _aretry(_fetch_tsm_raw)
        return {
            "tsm_close":   raw["price"],
            "tsm_chg_pct": raw["chg_pct"],
            "source":      raw["source"],
            "as_of":       datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        logger.error(f"[get_tsm_adr] Failed: {exc}")
        return {"error": True, "message": str(exc)}


@mcp.tool()
async def get_us_market_summary() -> dict:
    """Combined: fetch DJIA, NDX, SOX, TSMC ADR as a single markets list.
    Calls get_us_indices + get_tsm_adr internally."""
    logger.info("Tool 'get_us_market_summary' (shim) invoked")
    try:
        idx_raw = await _aretry(_fetch_us_indices_raw)
        tsm_raw = await _aretry(_fetch_tsm_raw)
        d = idx_raw["data"]

        def _market(sym: str, name: str, data: dict) -> dict:
            if data is None:
                return {"symbol": sym, "name": name, "error": True}
            price = data["price"]
            prev  = price / (1 + data["chg_pct"] / 100) if price and data["chg_pct"] is not None else None
            change = round(price - prev, 2) if price and prev else None
            return {"symbol": sym, "name": name, "price": price,
                    "change": change, "change_pct": data["chg_pct"], "volume": None}

        markets = [
            _market("^DJI", "DJIA",        d["djia"]),
            _market("^NDX", "NASDAQ 100",  d["ndx"]),
            _market("^SOX", "PHLX SOX",    d["sox"]),
            {"symbol": "TSM", "name": "TSMC ADR",
             "price": tsm_raw["price"], "change_pct": tsm_raw["chg_pct"], "volume": None},
        ]
        ok = sum(1 for m in markets if not m.get("error"))
        logger.success(f"[get_us_market_summary] {ok}/4 symbols OK")
        return {"as_of": datetime.now(timezone.utc).isoformat(),
                "source": "yfinance", "markets": markets}
    except Exception as exc:
        logger.error(f"[get_us_market_summary] Failed: {exc}")
        return {"error": True, "message": str(exc)}


@mcp.tool()
async def get_financial_news(max_items: int = 15) -> dict:
    """Fetch Taiwan stock news from Anue (鉅亨網). Titles sanitized against injection.
    Source: https://api.cnyes.com/media/api/v1/newslist/category/tw_stock
    Returns: {"news": [{"title", "time", "url"}], "count": int, "fetched_at": str}"""
    logger.info("Tool 'get_financial_news' invoked")
    try:
        def _fetch():
            with httpx.Client(
                timeout=10.0, headers={**_HEADERS, "Accept": "application/json"}
            ) as client:
                resp = client.get(_ANUE_URL)
                resp.raise_for_status()
                return resp.json()

        raw   = await _aretry(_fetch)
        items = raw["items"]["data"][:max_items]
        news  = []
        for item in items:
            ts = item.get("publishAt")
            news.append({
                "title":    _sanitize_title(item.get("title", "")),
                "time":     datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None,
                "category": item.get("categoryName", ""),
                "url":      f"https://news.cnyes.com/news/id/{item.get('newsId', '')}",
            })
        logger.success(f"[get_financial_news] {len(news)} headlines fetched")
        return {"source": "Anue 鉅亨網",
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "count": len(news), "news": news}
    except Exception as exc:
        logger.error(f"[get_financial_news] Failed: {exc}")
        return {"error": True, "message": str(exc), "source": "Anue API"}


@mcp.tool()
async def get_tw_night_futures() -> dict:
    """Fetch TAIFEX 台指期 (TXF) after-hours session close price and change.
    Source: https://www.taifex.com.tw/cht/5/afterHoursPrice
    Returns: {"night_close": int, "night_chg": float, "night_chg_pct": float,
              "volume": int, "product": str, "as_of": str}
    or {"error": true, "message": str} on holiday / pre-session / parse failure."""
    logger.info("Tool 'get_tw_night_futures' invoked")

    def _fetch() -> dict:
        with httpx.Client(timeout=15.0, headers=_HEADERS, follow_redirects=True) as client:
            resp = client.get(_TAIFEX_NIGHT_URL)
            resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")
        # TAIFEX afterHoursPrice table columns (typical order):
        # 商品 | 月份 | 最後成交價 | 漲跌 | 漲跌幅% | 成交量 | …
        for table in soup.find_all("table"):
            for tr in table.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) < 5:
                    continue
                first = tds[0].get_text(strip=True)
                if "臺股期貨" not in first and "TX" not in first:
                    continue

                def _td(idx: int, flt: bool = False):
                    txt = tds[idx].get_text(strip=True).replace(",", "").replace("+", "")
                    if not txt or txt in ("-", "--", "—"):
                        return None
                    return float(txt) if flt else int(float(txt))

                night_close = _td(2)
                night_chg   = _td(3, flt=True)
                chg_pct_raw = tds[4].get_text(strip=True).replace("+", "").replace("%", "").replace(",", "")
                night_chg_pct = float(chg_pct_raw) if chg_pct_raw not in ("", "-", "--", "—") else None
                volume      = _td(5)

                if night_close is None:
                    raise ValueError("night_close is None — market may be closed or page structure changed")

                logger.success(f"[get_tw_night_futures] close={night_close} chg_pct={night_chg_pct}%")
                return {
                    "product":       "臺股期貨 (TXF)",
                    "night_close":   night_close,
                    "night_chg":     night_chg,
                    "night_chg_pct": night_chg_pct,
                    "volume":        volume,
                    "as_of":         datetime.now(timezone.utc).isoformat(),
                    "source":        "TAIFEX afterHoursPrice",
                }
        raise ValueError("臺股期貨 row not found — holiday, pre-session, or page structure changed")

    try:
        return await _aretry(_fetch)
    except Exception as exc:
        logger.warning(f"[get_tw_night_futures] Failed: {exc}")
        return {"error": True, "message": str(exc), "source": "TAIFEX afterHoursPrice"}


if __name__ == "__main__":
    logger.info("Starting Market Data MCP Server via stdio transport")
    mcp.run(transport="stdio")
