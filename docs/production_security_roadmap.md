# Production Security Roadmap
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Executive Summary

The system has **3 CRITICAL** and **5 HIGH** security issues that require remediation before stable production operation. All P0 items can be resolved in under 3 hours and require no new infrastructure.

| Phase | Focus | Effort | Risk Eliminated |
|-------|-------|--------|-----------------|
| P0 — Immediate (today) | Authentication, secrets baseline, Markdown injection, snapshot integrity | ~2.5 hrs | CRITICAL × 3, HIGH × 2 |
| P1 — Short-term (this week) | LLM output validation, privilege reduction, audit logging | ~5 hrs | HIGH × 3, MEDIUM × 4 |
| P2 — Medium-term (this month) | Process isolation, DB RBAC, MCP env scoping, monitoring | ~6 hrs | MEDIUM × 3, LOW × 2 |
| P3 — Long-term (next sprint) | Secrets manager, dependency audit, full audit trail | ~8 hrs | Compliance, supply chain |

---

## Phase 0 — Immediate Fixes (Day 1, ~2.5 hrs)

### Fix 0-A: Streamlit Authentication (30 min)

**Risk closed:** E-01 (Unauthenticated portfolio write), I-01 (Portfolio data exposed to LAN)

**Option A — SSH Tunnel (recommended, 10 min):**

```bash
# On server: bind to localhost only
# Edit process start command or create a wrapper:
# streamlit run dashboard.py --server.address 127.0.0.1 --server.port 8501
```

Add to `daily_run.sh` or systemd unit. Existing LAN access stops immediately. Users access via:
```bash
ssh -L 8501:127.0.0.1:8501 ai-agents-server
```

**Option B — streamlit-authenticator (2 hrs):**

```bash
uv add streamlit-authenticator pyyaml bcrypt
```

```python
# dashboard.py — insert before all st.* calls
import streamlit_authenticator as stauth, yaml
from yaml.loader import SafeLoader

with open(".auth_config.yaml") as f:
    _cfg = yaml.load(f, Loader=SafeLoader)

_auth = stauth.Authenticate(
    _cfg["credentials"], _cfg["cookie"]["name"],
    _cfg["cookie"]["key"], _cfg["cookie"]["expiry_days"],
)
_, auth_status, _ = _auth.login()
if not auth_status:
    st.stop()
```

Create `.auth_config.yaml` (add to `.gitignore`):
```yaml
credentials:
  usernames:
    admin:
      name: Admin
      password: $2b$12$PLACEHOLDER  # replace with: python -c "import bcrypt; print(bcrypt.hashpw(b'your_pass', bcrypt.gensalt()).decode())"
cookie:
  expiry_days: 1
  key: REPLACE_WITH_32_CHAR_RANDOM_STRING
  name: ai_agent_auth
```

---

### Fix 0-B: Telegram Markdown Injection (15 min)

**Risk closed:** T-05 Markdown injection via LLM output, phishing link risk

**File:** `messenger_tools.py:67`

```python
# Before:
resp = client.post(url, json={
    "chat_id": chat_id,
    "text": message,
    "parse_mode": "Markdown",
})

# After:
resp = client.post(url, json={
    "chat_id": chat_id,
    "text": message,
    # parse_mode removed — plain text only for LLM-generated content
})
```

If Markdown formatting is needed for system alerts (not LLM content), use a separate `send_telegram_alert()` function that accepts pre-validated template strings only.

---

### Fix 0-C: Output Length Guards (15 min)

**Risk closed:** Oversized LLM output causing downstream API errors or injection amplification

**File:** `messenger_tools.py`

```python
_LINE_MAX = 4000
_TG_MAX   = 4096

def send_line(message: str) -> dict:
    message = message[:_LINE_MAX]
    ...

def send_telegram(message: str) -> dict:
    message = message[:_TG_MAX]
    ...
```

**File:** `market_analyst_agents.py` — add cap before passing to next node:

```python
def _cap_text(text: str, max_chars: int = 8000) -> str:
    return text[:max_chars]

# chief_strategist_node — before returning:
result = _cap_text(_extract_text(response))
return {"final_brief": result}
```

---

### Fix 0-D: Snapshot Integrity (30 min)

**Risk closed:** S-03 (Snapshot spoofing), T-02 (Memory poisoning via file)

```bash
# Generate HMAC key (run once on server)
python3 -c "import secrets; print(secrets.token_hex(32))"
# Add to .env:
# SNAPSHOT_HMAC_KEY=<output_above>
```

**New file:** `snapshot_integrity.py`

```python
import hashlib, hmac, json, os

_KEY = os.getenv("SNAPSHOT_HMAC_KEY", "").encode()

def sign_snapshot(data: dict) -> dict:
    if not _KEY:
        return data
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
    sig = hmac.new(_KEY, payload, hashlib.sha256).hexdigest()
    return {**data, "_hmac": sig}

def verify_snapshot(data: dict) -> bool:
    if not _KEY:
        return True
    sig = data.pop("_hmac", None)
    if not sig:
        return False
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
    expected = hmac.new(_KEY, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)
```

**`test_collection.py`** — add after building snapshot dict:
```python
from snapshot_integrity import sign_snapshot
snapshot = sign_snapshot(snapshot)
SNAPSHOT_FILE.write_text(json.dumps(snapshot, ...), encoding="utf-8")
```

**`investment_workflow.py`** — add after `json.loads`:
```python
from snapshot_integrity import verify_snapshot
if not verify_snapshot(snapshot):
    logger.error("Snapshot integrity check FAILED — aborting")
    sys.exit(1)
```

---

### Fix 0-E: MCP Server Environment Isolation (30 min)

**Risk closed:** Credential exposure to child processes that don't need them

**`agent_orchestrator.py`:**
```python
MINIMAL_ENV = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
server_params = StdioServerParameters(
    command="uv",
    args=["run", "mcp_servers/system_inspector.py"],
    env=MINIMAL_ENV,
)
```

**`test_collection.py`:**
```python
FINANCE_ENV = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
server_params = StdioServerParameters(
    command="uv",
    args=["run", "mcp_servers/finance_mcp_server.py"],
    env=FINANCE_ENV,
)
```

---

### Fix 0-F: News Headline Sanitization (30 min)

**Risk closed:** T-01 (Prompt injection via financial news headlines)

**`finance_mcp_server.py`** — add after imports:

```python
import re

_INJECTION_RE = re.compile(
    r"(?i)("
    r"(ignore|forget|disregard)\s+(all\s+)?(previous|prior|above|earlier)"
    r"|(system|assistant|human)\s*:?\s*(prompt|message)"
    r"|output\s*[:：]\s*[\{\[]"
    r"|(新的|重新|改變)\s*(指示|命令|輸出格式|系統提示)"
    r"|(override|bypass|jailbreak|injection)"
    r")"
)

def _sanitize_news_title(title: str) -> str:
    if _INJECTION_RE.search(title):
        logger.warning(f"[get_financial_news] Filtered suspected injection in title: {title[:50]}")
        return "[FILTERED]"
    return title[:200]  # hard length cap
```

In `get_financial_news()`:
```python
news.append({
    "title": _sanitize_news_title(item.get("title", "")),
    ...
})
```

---

## Phase 1 — Short-Term (This Week, ~5 hrs)

### Fix 1-A: LLM Output Schema Enforcement (2 hrs)

**Risk closed:** T-03 (Context hijacking via multi-hop chain)

Add `_enforce_schema()` and `_validate_json_output()` to `market_analyst_agents.py` (full implementation in `telemetry_design.md` Layer 4 and `sandbox_recommendation.md` Layer 1.3).

Key change: `chip_analyst` and `tech_analyst` return **cleaned dicts** (not raw strings) to `chief_strategist`. The dict is re-serialized from validated fields only — injected text in unexpected fields is discarded.

---

### Fix 1-B: TiDB Non-Empty Password (15 min)

**Risk closed:** Empty password on TiDB root user

```bash
# On server, connect to TiDB and set password
mysql -h 127.0.0.1 -P 4000 -u root
> ALTER USER 'root'@'%' IDENTIFIED BY '<strong_password>';
> FLUSH PRIVILEGES;
```

Update `.env`:
```
TIDB_PASSWORD=<strong_password>
```

---

### Fix 1-C: Verify TiDB Port Binding (15 min)

**Risk closed:** TiDB accessible from LAN with root credentials

```bash
# On server
ss -tlnp | grep 4000
docker ps --format "table {{.Names}}\t{{.Ports}}"
```

If the output shows `0.0.0.0:4000`, the TiDB container is LAN-accessible. Fix:

```yaml
# docker-compose.yml (create if missing)
services:
  tidb:
    ports:
      - "127.0.0.1:4000:4000"  # bind to localhost only
```

---

### Fix 1-D: Audit Logging for DB Mutations (2 hrs)

**Risk closed:** R-01 (Unattributed portfolio deletion), R-02 (Unattributed actuals write)

Implement the `audit_log` table and wrap `delete_portfolio_item` and `update_portfolio_item` as designed in `telemetry_design.md` Layer 5.

```sql
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    table_name  VARCHAR(50)  NOT NULL,
    operation   VARCHAR(10)  NOT NULL,
    record_id   BIGINT       NULL,
    actor       VARCHAR(50)  NOT NULL DEFAULT 'system',
    before_json JSON         NULL,
    after_json  JSON         NULL,
    created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);
```

```python
# database_tools.py — wrap delete_portfolio_item
def delete_portfolio_item(item_id: int, actor: str = "system") -> None:
    with _engine().begin() as conn:
        row = conn.execute(text("SELECT * FROM user_portfolio WHERE id = :id"), {"id": item_id}).fetchone()
        before = dict(row._mapping) if row else {}
        conn.execute(text("DELETE FROM user_portfolio WHERE id = :id"), {"id": item_id})
        conn.execute(
            text("INSERT INTO audit_log (table_name, operation, record_id, actor, before_json) VALUES (:t, 'DELETE', :id, :actor, :b)"),
            {"t": "user_portfolio", "id": item_id, "actor": actor, "b": json.dumps(before, default=str)},
        )
```

---

### Fix 1-E: Opus Token Budget Cap (30 min)

**Risk closed:** D-02 (Opus thinking token cost explosion)

```python
# market_analyst_agents.py
def _llm_opus() -> ChatAnthropic:
    return ChatAnthropic(
        model=_MODEL_OPUS,
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        max_tokens=4096,           # reduced from 16000
        thinking={"type": "enabled", "budget_tokens": 2048},  # hard cap on thinking
    )
```

Set monthly spend limit in Anthropic console: Dashboard → Usage limits → Set hard limit to $10.

---

## Phase 2 — Medium-Term (This Month, ~6 hrs)

### Fix 2-A: Dedicated `ai_agent` OS User (1 hr)

```bash
sudo useradd --system --shell /usr/sbin/nologin ai_agent
sudo mkdir -p /opt/ai_agent_studio
sudo cp -r /home/itadmin/ai_agent_studio/* /opt/ai_agent_studio/
sudo chown -R ai_agent:ai_agent /opt/ai_agent_studio/
sudo chmod 600 /opt/ai_agent_studio/.env
sudo crontab -u ai_agent -e
# Add cron entries for ai_agent user
```

### Fix 2-B: Per-Component TiDB Credentials (2 hrs)

Create minimal-privilege MySQL users as specified in `privilege_boundary_analysis.md` Section 1. Each workflow script reads its own `TIDB_USER_*` and `TIDB_PASS_*` env vars.

### Fix 2-C: systemd Service with Security Directives (1 hr)

Replace cron with the systemd service unit from `sandbox_recommendation.md` Layer 3.4. Enables `ProtectSystem=strict`, `PrivateTmp`, and `NoNewPrivileges`.

### Fix 2-D: `.env` Access Restriction (5 min)

```bash
chmod 600 /opt/ai_agent_studio/.env
# Verify no group/other read:
ls -la /opt/ai_agent_studio/.env
# Should show: -rw------- 1 ai_agent ai_agent
```

### Fix 2-E: Investment Brief File Cleanup (30 min)

```python
# investment_workflow.py — after writing brief file
brief_file.write_text(brief, encoding="utf-8")
brief_file.chmod(0o600)  # owner read/write only

# Add cleanup of files older than 30 days
import glob, time
for old_file in Path(".").glob("investment_brief_*.txt"):
    if time.time() - old_file.stat().st_mtime > 30 * 86400:
        old_file.unlink()
```

---

## Phase 3 — Long-Term (~8 hrs)

### Fix 3-A: Secrets Manager Integration (4 hrs)

Replace the `.env` file pattern with a proper secrets manager. For the Ubuntu server, **HashiCorp Vault Community Edition** is the recommended option (free, self-hosted, supports dynamic DB credentials):

```bash
# Install Vault on Ubuntu
wget -O - https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
sudo apt install vault

# Store secrets
vault kv put secret/ai-agent-studio \
  anthropic_api_key="..." \
  tidb_password="..." \
  line_token="..." \
  telegram_token="..."
```

```python
# Replace os.getenv() calls with vault client
import hvac
client = hvac.Client(url="http://127.0.0.1:8200", token=os.getenv("VAULT_TOKEN"))
secrets = client.secrets.kv.read_secret_version(path="ai-agent-studio")["data"]["data"]
ANTHROPIC_KEY = secrets["anthropic_api_key"]
```

Benefits:
- Secrets never written to filesystem in plaintext
- Access audit log built-in (every read is logged)
- Token rotation without code changes
- Lease expiration for time-bound access

### Fix 3-B: Dependency Supply Chain Audit (2 hrs)

```bash
# Install safety scanner
uv add --dev safety pip-audit

# Audit current dependencies
uv run pip-audit
uv run safety scan

# Pin all dependency hashes in uv.lock (uncomment in pyproject.toml or use --frozen)
# uv lock  # generates uv.lock with content hashes
```

Add to CI/CD (or weekly cron):
```bash
uv run pip-audit --format=json --output=audit_report.json
# Alert if any HIGH/CRITICAL CVEs found
```

### Fix 3-C: Content Policy Framework (2 hrs)

Add a content moderation step before push notification delivery. For a single-user financial system, a lightweight rule-based approach is sufficient:

```python
# new file: content_policy.py
import re

# Patterns that should never appear in a legitimate financial brief
_SUSPICIOUS_URLS = re.compile(r"https?://(?!www\.taifex\.com\.tw|api\.anthropic\.com)[a-z0-9.-]+\.[a-z]{2,}/\S*")
_PHONE_NUMBERS = re.compile(r"\b\+?[0-9]{10,15}\b")

def audit_final_report(text: str) -> tuple[bool, list[str]]:
    """Return (is_clean, list_of_violations). Call before send_notification_node."""
    violations = []
    if _SUSPICIOUS_URLS.search(text):
        violations.append("unexpected URL in report")
    if _PHONE_NUMBERS.search(text):
        violations.append("phone number pattern in report")
    if len(text) > 6000:
        violations.append(f"report length {len(text)} exceeds expected maximum")
    return len(violations) == 0, violations
```

```python
# market_analyst_agents.py — send_notification_node
from content_policy import audit_final_report

def send_notification_node(state: WorkflowState) -> dict:
    report = state.get("final_report", "")
    is_clean, violations = audit_final_report(report)
    if not is_clean:
        logger.error(f"[SendNotification] Content policy violations: {violations}")
        logger.error("[SendNotification] Aborting push notification — review brief manually")
        return {}  # abort delivery; A-003 alert will fire
    ...
```

---

## Consolidated Change List

| ID | File | Change | Phase | Effort |
|----|------|--------|-------|--------|
| 0-A | `dashboard.py` | SSH tunnel or streamlit-authenticator | P0 | 30 min |
| 0-B | `messenger_tools.py` | Remove `parse_mode="Markdown"` | P0 | 5 min |
| 0-C | `messenger_tools.py`, `market_analyst_agents.py` | Length caps on outputs | P0 | 15 min |
| 0-D | `test_collection.py`, `investment_workflow.py` | Snapshot HMAC sign + verify | P0 | 30 min |
| 0-E | `agent_orchestrator.py`, `test_collection.py` | Minimal MCP env | P0 | 30 min |
| 0-F | `finance_mcp_server.py` | News headline injection filter | P0 | 30 min |
| 1-A | `market_analyst_agents.py` | LLM output schema enforcement | P1 | 2 hrs |
| 1-B | TiDB (shell) | Set root password | P1 | 15 min |
| 1-C | Docker / server | Verify TiDB port binding | P1 | 15 min |
| 1-D | `database_tools.py`, TiDB | Audit log table + mutation wrappers | P1 | 2 hrs |
| 1-E | `market_analyst_agents.py` | Opus `max_tokens=4096`, `budget_tokens=2048` | P1 | 10 min |
| 2-A | Ubuntu server | Dedicated `ai_agent` user | P2 | 1 hr |
| 2-B | `database_tools.py`, TiDB | Per-component DB credentials | P2 | 2 hrs |
| 2-C | Ubuntu server | systemd service + security directives | P2 | 1 hr |
| 2-D | Server filesystem | `chmod 600 .env` | P2 | 5 min |
| 2-E | `investment_workflow.py` | Brief file cleanup + chmod | P2 | 30 min |
| 3-A | `.env` → Vault | HashiCorp Vault integration | P3 | 4 hrs |
| 3-B | `pyproject.toml` | Dependency CVE scanning | P3 | 2 hrs |
| 3-C | New `content_policy.py` | Pre-delivery content audit | P3 | 2 hrs |

**Total P0:** ~2 hrs | **Total P1:** ~5 hrs | **Total P2:** ~5 hrs | **Total P3:** ~8 hrs

---

## Security Coverage After Each Phase

| Phase | CRITICAL Fixed | HIGH Fixed | Coverage Improvement |
|-------|---------------|------------|---------------------|
| Baseline | 0/3 | 0/5 | — |
| After P0 | 3/3 | 2/5 | Snapshot integrity, unauthenticated dashboard, Markdown injection, MCP env leakage |
| After P1 | 3/3 | 5/5 | Context hijacking, empty DB password, unaudited mutations, Opus cost explosion |
| After P2 | 3/3 | 5/5 | Process isolation, privilege reduction, filesystem hardening |
| After P3 | 3/3 | 5/5 | Supply chain, secrets management, content policy |

**Risk posture:** After P0 + P1 (~7 hours total), all CRITICAL and HIGH risks are mitigated. P2 and P3 address defense-in-depth and compliance-oriented controls.
