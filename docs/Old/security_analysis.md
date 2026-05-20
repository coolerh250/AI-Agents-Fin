# Security Analysis
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Severity Classification

| Level | Meaning |
|-------|---------|
| 🔴 HIGH | Immediate risk; data exposure, injection, or credential leak possible |
| 🟡 MEDIUM | Exploitable under specific conditions or with partial attacker access |
| 🟢 LOW | Best-practice deviation; low immediate impact |

---

## 1. Credential Handling

### 1.1 Anthropic API Key

| Property | Status |
|----------|--------|
| Storage | `.env` file (gitignored ✅) |
| Template | `.env.template` committed to repo ✅ |
| Runtime access | `os.getenv("ANTHROPIC_API_KEY")` in every agent node |
| Validation | `investment_workflow.py` and `backtest_agent.py` abort if key absent |
| Exposure risk | Key is passed as a constructor argument to `ChatAnthropic(api_key=...)` on every LLM call — appears in memory and potentially in any debug dump of the object |

**Severity: 🟢 LOW** — Properly gitignored; standard pattern.

---

### 1.2 TiDB Credentials

| Property | Status |
|----------|--------|
| Storage | `.env` file (gitignored ✅) |
| Default password | **Empty string** in `.env.template` |
| Connection string | `mysql+pymysql://{user}:{password}@{host}:{port}/{db}` — built in `_engine()` at call time |
| Exposure | Connection URL contains plaintext password; visible in stack traces on connection error |
| SSL | No `ssl_ca` or `ssl_verify_cert` in connection URL — TiDB connection is **unencrypted** |

**Severity: 🟡 MEDIUM** — Empty default password acceptable for dev; production needs SSL and non-empty password.

---

### 1.3 LINE / Telegram Tokens

| Property | Status |
|----------|--------|
| Storage | `.env` file (gitignored ✅) |
| Usage | `os.getenv()` in `messenger_tools.py` |
| Graceful skip | If not set, functions return `{"status": "skipped"}` ✅ |
| Risk | LINE `Channel Access Token` is a long-lived bearer token — rotation policy undefined |

**Severity: 🟢 LOW**

---

## 2. SSL / TLS Risks

### 2.1 TWSE SSL Verification Disabled

**Location:** `twse_fetcher.py:24`
```python
with httpx.Client(timeout=10.0, headers=_HEADERS, verify=False) as client:
```

**Reason documented:** TWSE certificate lacks Subject Key Identifier extension (legacy TW gov CA).

**Risk:** Man-in-the-middle attack could substitute fabricated TAIEX data, leading to incorrect backtest entries in TiDB and potentially wrong trading signals.

**Severity: 🟡 MEDIUM** — Internal network deployment reduces exposure; TWSE data is public so data integrity (not confidentiality) is the concern.

**Remediation options:**
1. Install the GRCA (Government Root CA) certificate on the server: `update-ca-certificates`
2. Pass the CA bundle explicitly: `verify="/etc/ssl/certs/GRCA.pem"`

---

### 2.2 httpx InsecureRequestWarning Not Suppressed

With `verify=False`, `httpx` emits a `InsecureRequestWarning` to stderr. This warning currently appears in logs, which is the desired behavior (visibility).

---

## 3. External API Access Risk

### 3.1 TAIFEX HTML Scraping

**Location:** `finance_mcp_server.py:get_tw_future_chips`

- Uses a `POST` request to `https://www.taifex.com.tw/cht/3/futContractsDate`
- Parses HTML with BeautifulSoup using CSS class selectors (`table_f`, `12bk`, `serial-3`)
- **No authentication required** (public data)

**Risk:** TAIFEX page structure changes silently break the scraper. A malformed response could cause `_parse_num()` to raise or return 0, sending incorrect chip data downstream.

**Error handling:** Wrapped in `_retry()` with 1 retry; returns `{"error": True, "message": ...}` on failure. Downstream `data_collector_node` falls back to raw snapshot data.

**Severity: 🟡 MEDIUM** — Fragile external dependency, but failure is handled gracefully.

---

### 3.2 Yahoo Finance / Anue API Rate Limiting

- No rate limiting, throttling, or backoff strategy on yfinance or Anue API calls
- If Yahoo Finance blocks the IP (403/429), `get_us_market_summary` fails all 4 symbols
- `get_financial_news` makes 1 request per run with 1 retry; no exponential backoff

**Severity: 🟢 LOW** — Low request volume (1/day); unlikely to be rate-limited.

---

## 4. MCP Security Model

### 4.1 stdio Transport

Both MCP servers use **stdio transport** — they are spawned as child processes via `subprocess` (through the MCP Python library's `stdio_client`). This means:

- No network port exposed
- No authentication required (process-level isolation)
- Server inherits environment variables from parent process (including any secrets in env)

**Severity: 🟢 LOW** — stdio is the safer transport option.

---

### 4.2 sys.path Manipulation

**Location:** `finance_mcp_server.py:14`
```python
sys.path.insert(0, str(Path(__file__).parent.parent))
```

This inserts the project root into `sys.path` to import `database_tools` and `messenger_tools`. If the MCP server were ever exposed over network transport (SSE/HTTP), this path manipulation could become relevant. In stdio mode, it's benign.

**Severity: 🟢 LOW**

---

### 4.3 MCP Tool Input Validation

**Location:** `finance_mcp_server.py:save_brief_to_db`
```python
def save_brief_to_db(trade_date: str, brief_text: str, ...):
    d = _date.fromisoformat(trade_date)  # raises ValueError on bad input
    row_id = save_brief(d, brief_text, ...)
```

The `brief_text` parameter is passed directly to a parameterized SQL query (`INSERT INTO daily_briefs ... VALUES (:brief, ...)`) via SQLAlchemy. **SQL injection is NOT possible** due to parameterized queries.

However, `brief_text` is not length-constrained before insertion — a very large input could consume excessive DB storage or cause the INSERT to fail silently.

**Severity: 🟢 LOW**

---

## 5. Streamlit Dashboard

### 5.1 No Authentication

**Location:** `dashboard.py`

The Streamlit dashboard has no authentication layer. Anyone who can reach port 8501 on the server can:
- View portfolio holdings and P&L (financial privacy concern)
- View API cost data
- Add or delete portfolio holdings via the management tab
- Write arbitrary data to `market_actuals` via the manual entry form

**Severity: 🔴 HIGH** — Portfolio holdings are sensitive financial data. The server is on a local network (`10.0.1.20`) which mitigates public exposure, but any user on the LAN can access the dashboard.

**Remediation:** Add `streamlit-authenticator` or place Nginx + basic auth in front of port 8501.

---

### 5.2 Manual `market_actuals` Write

**Location:** `dashboard.py` Tab 1 — manual entry form

The `save_actual()` call accepts arbitrary date and numeric values from the Streamlit form. There is no:
- Date range validation (could write to past/future dates)
- Confirmation step before write
- Audit trail for manual vs automated entries (only the `notes` field distinguishes them)

**Severity: 🟢 LOW** — Data integrity risk, not security risk per se.

---

## 6. Process Execution

### 6.1 No Shell Injection Risk

No code in the project uses `subprocess.run(shell=True)` or `os.system()` with user-controlled input. All subprocess calls go through the MCP SDK's `StdioServerParameters`, which passes arguments as a list (not a shell string).

**Severity: 🟢 LOW** — Clean.

---

### 6.2 daily_run.sh Heredoc Execution

**Location:** `daily_run.sh` (original version, now superseded on remote)

The old Step 3 used a bash heredoc (`<< 'PYEOF'`) to execute inline Python. This is safe as the heredoc content is static (no variable interpolation into the Python code). The updated remote version no longer uses this pattern.

**Severity: 🟢 LOW**

---

## 7. Secret Scanning Summary

| Secret Type | In `.env.template` | Gitignored | Risk |
|-------------|-------------------|------------|------|
| `ANTHROPIC_API_KEY` | Placeholder only ✅ | ✅ | Low |
| `TIDB_PASSWORD` | Empty default ✅ | ✅ | Medium (empty default) |
| `LINE_CHANNEL_ACCESS_TOKEN` | Comment only ✅ | ✅ | Low |
| `TELEGRAM_BOT_TOKEN` | Comment only ✅ | ✅ | Low |
| `market_snapshot.json` | N/A (data file) | ✅ | None |
| `collection_journal.jsonl` | N/A (log file) | ✅ | None |
