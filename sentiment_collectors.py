"""sentiment_collectors.py — Phase 3 §2 raw sentiment collectors.

Three free sources, each writes one (trade_date, stock_id, source) row to
stock_sentiment_daily:
  - cnyes : count 4-digit stock-code mentions in news titles (≥1 = noticed)
  - twse  : daily top-N by trade volume from MI_INDEX20 (raw_count = volume)
  - ptt   : Day 3 — PTT 股板 post + push counts

The composite scorer (Day 4) reads from this table.

CLI:
  uv run python sentiment_collectors.py --source cnyes --days 7
  uv run python sentiment_collectors.py --source twse  --top-n 30
  uv run python sentiment_collectors.py --source all
"""
import argparse
import json
import re
import sys
import time
import urllib.parse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv
from loguru import logger
from sqlalchemy import text

load_dotenv()


_HEADERS = {
    "User-Agent": "Mozilla/5.0 AI-Agent-Studio/1.0 (sentiment_collector)",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

# PTT WAFs reject our default lab User-Agent (Connection-Reset-by-Peer); send a
# real desktop Chrome string when scraping PTT specifically. PTT also requires
# the over18 age-gate cookie.
_PTT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-TW,zh;q=0.9",
}

_CNYES_URL       = "https://api.cnyes.com/media/api/v1/newslist/category/tw_stock"
_TWSE_MI_INDEX20 = "https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX20"
_PTT_BASE        = "https://www.ptt.cc"
_PTT_STOCK_INDEX = "https://www.ptt.cc/bbs/Stock/index.html"

# Taiwan listed-stock IDs are 4 digits (ETFs are 5 — those are intentionally
# skipped at this phase since the technical-signal set assumes equity-style
# OHLCV behaviour). Use a non-digit lookbehind/lookahead instead of \b so
# titles like "9805電力" still match — Python 3 \b treats CJK characters as
# word chars, so \b\d{4}\b silently fails next to a Chinese context.
# `_EXCLUDE_YEARS` drops headline noise like "2026 年展望".
_STOCK_ID_RE   = re.compile(r"(?<!\d)([1-9]\d{3})(?!\d)")
_EXCLUDE_YEARS = {str(y) for y in range(2018, 2036)}


# ── Persistence ───────────────────────────────────────────────────────────────

def _persist(rows: list[tuple]) -> int:
    """UPSERT rows. row = (trade_date, stock_id, source, raw_count, sample_meta)."""
    if not rows:
        return 0
    from database_tools import _engine
    with _engine().begin() as conn:
        for trade_date, stock_id, source, raw_count, sample_meta in rows:
            conn.execute(
                text("""
                    INSERT INTO stock_sentiment_daily
                        (trade_date, stock_id, source, raw_count, sample_meta)
                    VALUES (:td, :sid, :src, :rc, :sm)
                    ON DUPLICATE KEY UPDATE
                        raw_count   = VALUES(raw_count),
                        sample_meta = VALUES(sample_meta),
                        created_at  = CURRENT_TIMESTAMP
                """),
                {"td": trade_date, "sid": stock_id, "src": source,
                 "rc": int(raw_count),
                 "sm": json.dumps(sample_meta, ensure_ascii=False) if sample_meta else None},
            )
    return len(rows)


# ── CNYES news mentions ──────────────────────────────────────────────────────

def _extract_stock_codes(title: str) -> list[str]:
    """De-duplicated 4-digit Taiwan stock-code candidates in title, year-filtered."""
    return list(dict.fromkeys(c for c in _STOCK_ID_RE.findall(title)
                              if c not in _EXCLUDE_YEARS))


def _fetch_cnyes(days: int) -> list[dict]:
    """Paginate CNYES API until all items on a page are older than the cutoff.
    The API silently caps `limit` at 30 per page, so we walk `page=1,2,3,…`
    until either the page returns items strictly before cutoff (early stop)
    or we hit the safety cap. Empirically ~5 pages covers a full trading week."""
    cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    out: list[dict] = []
    with httpx.Client(timeout=15.0,
                      headers={**_HEADERS, "Accept": "application/json"}) as client:
        for page in range(1, 21):  # cap 20 pages = ~600 items, plenty for 7d
            resp = client.get(_CNYES_URL, params={"limit": 30, "page": page})
            resp.raise_for_status()
            items = resp.json()["items"]["data"]
            if not items:
                break
            out.extend(items)
            # Early stop once the newest item on this page predates cutoff —
            # CNYES returns newest-first within a page.
            newest_ts = max((i.get("publishAt") or 0) for i in items)
            if newest_ts < cutoff_ts:
                break
    return out


def collect_cnyes_mentions(days: int = 7) -> dict[date, dict[str, int]]:
    """Crawl CNYES TW-stock news, bucket 4-digit mentions by publish date.
    Persists per (date, stock_id, 'cnyes'). Returns {date: {stock_id: count}}."""
    try:
        items = _fetch_cnyes(days=days)
    except Exception as exc:
        logger.error(f"[cnyes] fetch failed: {exc}")
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    by_date:        dict[date, Counter]   = {}
    titles_by_code: dict[tuple, list[str]] = {}

    for item in items:
        ts = item.get("publishAt")
        if not ts:
            continue
        published_at = datetime.fromtimestamp(ts, tz=timezone.utc)
        if published_at < cutoff:
            continue
        td    = published_at.date()
        title = item.get("title", "")
        for code in _extract_stock_codes(title):
            by_date.setdefault(td, Counter())[code] += 1
            titles_by_code.setdefault((td, code), []).append(title[:80])

    rows = []
    for td, counter in by_date.items():
        for stock_id, count in counter.items():
            example_titles = titles_by_code.get((td, stock_id), [])[:3]
            rows.append((td, stock_id, "cnyes", count, {"titles": example_titles}))
    persisted = _persist(rows)
    logger.success(f"[cnyes] {persisted} rows persisted across {len(by_date)} dates "
                   f"({sum(len(c) for c in by_date.values())} (date,stock) pairs)")
    return {td: dict(c) for td, c in by_date.items()}


# ── TWSE top-volume ───────────────────────────────────────────────────────────

def collect_twse_top_volume(top_n: int = 30) -> dict[str, int]:
    """Fetch TWSE MI_INDEX20 (top-20 by trade volume today). Returns {stock_id: volume}.
    Persists per (today, stock_id, 'twse') with raw_count = volume (lots)."""
    try:
        with httpx.Client(timeout=15.0,
                          headers={**_HEADERS, "Accept": "application/json"}) as client:
            resp = client.get(_TWSE_MI_INDEX20)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.error(f"[twse] fetch failed: {exc}")
        return {}

    today = date.today()
    rows: list[tuple] = []
    counts: dict[str, int] = {}

    # MI_INDEX20 field names have varied between TWSE OpenAPI revisions
    # (English keys "Code"/"TradeVolume" vs zh-TW "證券代號"/"成交股數"),
    # so probe both before giving up on a row.
    for entry in data[:top_n]:
        sid = (entry.get("Code") or entry.get("證券代號")
               or entry.get("stock_id") or entry.get("代號"))
        vol_raw = (entry.get("TradeVolume") or entry.get("成交股數")
                   or entry.get("volume"))
        if not sid or vol_raw in (None, ""):
            continue
        try:
            volume = int(str(vol_raw).replace(",", ""))
        except (TypeError, ValueError):
            continue
        sid_s = str(sid).strip()
        counts[sid_s] = volume
        rows.append((today, sid_s, "twse", volume,
                     {"trade_value": entry.get("TradeValue") or entry.get("成交金額"),
                      "name":        entry.get("Name")       or entry.get("證券名稱")}))
    persisted = _persist(rows)
    logger.success(f"[twse] {persisted} top-volume rows for {today}")
    return counts


# ── PTT 股板 ───────────────────────────────────────────────────────────────────

def _push_value(text: str) -> int:
    """PTT push column text → numeric.
    - "" → 0  (no push recorded)
    - "1"…"99" → that integer
    - "爆" → 100   (PTT caps display at 爆 for any post with 100+ pushes)
    - "X1"…"X9" → -10…-90  (噓 count, negative)"""
    text = (text or "").strip()
    if not text:
        return 0
    if text == "爆":
        return 100
    if text.startswith("X"):
        try:
            return -int(text[1:]) * 10
        except ValueError:
            return -10
    try:
        return int(text)
    except ValueError:
        return 0


def _parse_ptt_page(html: str):
    """Returns (posts: list[dict], prev_page_url: Optional[str]).
    posts items shape: {title, push, date}."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    for ent in soup.select("div.r-ent"):
        a = ent.select_one("div.title a")
        if not a:
            continue  # 已被刪除的文章標題會缺 <a>，跳過
        push_elem = ent.select_one("div.nrec span")
        date_elem = ent.select_one("div.date")
        posts.append({
            "title": a.get_text(strip=True),
            "push":  _push_value(push_elem.get_text(strip=True) if push_elem else ""),
            "date":  date_elem.get_text(strip=True) if date_elem else "",
        })
    prev_url = None
    for btn in soup.select("a.btn.wide"):
        # PTT shows "‹ 上頁" with a fullwidth-or-halfwidth space; match by
        # substring to be safe across rendering quirks.
        if "上頁" in btn.get_text():
            href = btn.get("href")
            if href:
                prev_url = urllib.parse.urljoin(_PTT_BASE, href)
            break
    return posts, prev_url


def _ptt_get_with_retry(client: httpx.Client, url: str,
                        attempts: int = 3) -> httpx.Response:
    """PTT's WAF intermittently TCP-resets new TLS handshakes (verified
    2026-06-03: 1 of 3 fresh attempts gets [Errno 104] Connection reset
    by peer). Retry with 2s/4s backoff before giving up so the cron job
    doesn't drop a whole day's data over a transient reset."""
    last_exc: Optional[Exception] = None
    for n in range(attempts):
        try:
            resp = client.get(url)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code} on {url}")
            return resp
        except (httpx.ConnectError, httpx.RemoteProtocolError, RuntimeError) as exc:
            last_exc = exc
            if n < attempts - 1:
                time.sleep(2.0 * (n + 1))
    assert last_exc is not None
    raise last_exc


def collect_ptt_stock_buzz(pages: int = 3) -> dict[str, int]:
    """Scrape PTT 股板 N back-pages. Returns {stock_id: combined_score} where
    combined = mentions + max(push_total, 0) * 0.2  (push weighted 20%).
    Persists one row per (today, stock_id, 'ptt'). On any HTTP / parse error
    returns {} and emits a warning log so the Day-5 orchestrator can downgrade
    the composite weight (Phase 3 risk register: PTT WAF / HTML drift)."""
    url = _PTT_STOCK_INDEX
    posts_all: list[dict] = []
    try:
        with httpx.Client(timeout=15.0, headers=_PTT_HEADERS,
                          cookies={"over18": "1"},
                          follow_redirects=True) as client:
            for page_n in range(pages):
                resp = _ptt_get_with_retry(client, url)
                posts, prev = _parse_ptt_page(resp.text)
                posts_all.extend(posts)
                if not prev:
                    break
                url = prev
                # 1 req/sec — be a polite scraper. Skip sleep on the final
                # iteration; nothing to wait for.
                if page_n < pages - 1:
                    time.sleep(1.0)
    except Exception as exc:
        logger.warning(f"[ptt] scrape failed (suppressed): {exc}")
        return {}

    agg: dict[str, dict] = {}
    for post in posts_all:
        codes = _extract_stock_codes(post["title"])
        for code in codes:
            slot = agg.setdefault(code, {"mentions": 0, "push_total": 0, "titles": []})
            slot["mentions"]   += 1
            slot["push_total"] += max(post["push"], 0)
            if len(slot["titles"]) < 3:
                slot["titles"].append(post["title"][:80])

    today = date.today()
    rows: list[tuple] = []
    counts: dict[str, int] = {}
    for code, slot in agg.items():
        combined = int(round(slot["mentions"] + slot["push_total"] * 0.2))
        counts[code] = combined
        rows.append((today, code, "ptt", combined,
                     {"mentions":   slot["mentions"],
                      "push_total": slot["push_total"],
                      "titles":     slot["titles"]}))
    persisted = _persist(rows)
    logger.success(f"[ptt] {persisted} stocks from {len(posts_all)} posts across "
                   f"{pages} pages")
    return counts


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 3 §2 sentiment collectors")
    parser.add_argument("--source", choices=["cnyes", "twse", "ptt", "all"],
                        default="all", help="Source to collect")
    parser.add_argument("--days",  type=int, default=7,
                        help="CNYES lookback window (default 7)")
    parser.add_argument("--top-n", type=int, default=30,
                        help="TWSE top-N by volume (default 30)")
    parser.add_argument("--pages", type=int, default=3,
                        help="PTT back-pages to scrape (default 3)")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stdout, format="{time:HH:mm:ss} | {level:<8} | {message}",
               level="INFO", colorize=False)

    if args.source in ("cnyes", "all"):
        collect_cnyes_mentions(days=args.days)
    if args.source in ("twse", "all"):
        collect_twse_top_volume(top_n=args.top_n)
    if args.source in ("ptt", "all"):
        collect_ptt_stock_buzz(pages=args.pages)
    return 0


if __name__ == "__main__":
    sys.exit(main())
