# Sandbox Recommendation
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Design Principles

1. **Defense in depth**: multiple independent layers; compromise of one layer does not cascade
2. **Fail-closed for financial data**: if a sandbox check fails, abort rather than proceed with potentially compromised data
3. **Zero new infrastructure for P0**: all P0 recommendations use OS-level controls already present on Ubuntu without installing additional services
4. **Proportional overhead**: sandboxing overhead should not meaningfully delay the 08:20 CST workflow

---

## Layer 1: Input Sanitization Sandbox

### 1.1 Snapshot Integrity Check (P0 — 30 min)

`market_snapshot.json` is the trust foundation of the entire workflow. Add a **HMAC signature** on write and verify on read.

```python
# new file: snapshot_integrity.py
import hashlib, hmac, json, os
from pathlib import Path

_HMAC_KEY = os.getenv("SNAPSHOT_HMAC_KEY", "").encode()  # add to .env

def sign_snapshot(data: dict) -> dict:
    """Attach an HMAC-SHA256 signature to the snapshot dict."""
    if not _HMAC_KEY:
        return data  # skip signing if key not configured (dev mode)
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
    sig = hmac.new(_HMAC_KEY, payload, hashlib.sha256).hexdigest()
    return {**data, "_hmac": sig}

def verify_snapshot(data: dict) -> bool:
    """Return False if signature is missing or invalid."""
    if not _HMAC_KEY:
        return True  # dev mode: skip verification
    sig = data.pop("_hmac", None)
    if not sig:
        return False
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
    expected = hmac.new(_HMAC_KEY, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)
```

```python
# test_collection.py — sign before writing
from snapshot_integrity import sign_snapshot
snapshot = sign_snapshot(snapshot)
SNAPSHOT_FILE.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

# investment_workflow.py — verify before reading
from snapshot_integrity import verify_snapshot
raw = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
if not verify_snapshot(raw):
    logger.error("Snapshot HMAC verification FAILED — possible tampering, aborting")
    sys.exit(1)
snapshot = raw
```

```bash
# .env — add one line
SNAPSHOT_HMAC_KEY=<32-byte random hex>  # openssl rand -hex 32
```

**Effect:** Any modification to `market_snapshot.json` after signing invalidates the HMAC. Attacker cannot substitute poisoned data without access to the key.

---

### 1.2 News Headline Sanitization (P0 — 45 min)

Strip any string that could be interpreted as an LLM instruction before it enters the snapshot.

```python
# finance_mcp_server.py — sanitize news titles
import re

_INJECTION_PATTERNS = [
    r"(?i)(ignore|forget|disregard)\s+(all\s+)?(previous|prior|above|earlier)",
    r"(?i)(system|assistant|human)\s*:?\s*prompt",
    r"(?i)output\s*[:：]\s*\{",
    r"(?i)(新的|重新|改變)\s*(指示|命令|輸出格式)",
    r"(?i)(new|override|forget|ignore)\s+instructions?",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS))

def _sanitize_title(title: str) -> str:
    """Replace suspected injection patterns with a placeholder."""
    if _INJECTION_RE.search(title):
        return "[CONTENT_FILTERED]"
    # Truncate extremely long titles (legitimate titles are < 200 chars)
    return title[:200]

# In get_financial_news():
news.append({
    "title": _sanitize_title(item.get("title", "")),
    ...
})
```

**Limitation:** Pattern-based filtering is a heuristic — it will not catch all creative injection attempts. The more robust solution (Layer 2) is output schema validation so injected text cannot influence structured outputs.

---

### 1.3 LLM Output Type Enforcement (P1 — 2 hrs)

For nodes that must produce structured JSON (`data_collector`, `chip_analyst`, `tech_analyst`), enforce strict schema validation before allowing output to enter the next node.

```python
# market_analyst_agents.py — add validators

_COLLECTOR_SCHEMA = {
    "foreign_oi_net": int, "trust_oi_net": int, "dealer_oi_net": int,
    "djia_chg_pct": float, "ndx_chg_pct": float,
    "sox_chg_pct": float, "tsm_adr_chg_pct": float,
    "data_ok": bool,
}
_CHIP_SCHEMA = {
    "sentiment": str, "foreign_net": int, "trust_net": int,
    "dealer_net": int, "divergence_signal": bool, "reasoning": str,
}
_TECH_SCHEMA = {
    "gap_direction": str, "estimated_gap_pct": float,
    "key_driver": str, "tsm_signal": str, "reasoning": str,
}

_TECH_DIRECTION_ALLOWED = {"up", "flat", "down"}

def _enforce_schema(data: dict, schema: dict, agent: str) -> dict:
    """Return only schema-defined fields with correct types. Log violations."""
    result = {}
    for field, typ in schema.items():
        val = data.get(field)
        if val is None:
            logger.warning(f"[{agent}] missing required field: {field}")
            continue
        try:
            result[field] = typ(val)  # coerce to expected type
        except (TypeError, ValueError):
            logger.warning(f"[{agent}] field {field!r} cannot coerce {val!r} to {typ.__name__}")
    return result

# In chip_analyst_node — before returning:
try:
    parsed = json.loads(result)
    parsed = _enforce_schema(parsed, _CHIP_SCHEMA, "chip_analyst")
    result = json.dumps(parsed, ensure_ascii=False)  # re-serialize clean dict
except json.JSONDecodeError:
    result = json.dumps({})  # abort with empty dict; triggers fallback

# In tech_analyst_node — validate gap_direction
if parsed.get("gap_direction") not in _TECH_DIRECTION_ALLOWED:
    logger.warning(f"[tech_analyst] invalid gap_direction: {parsed.get('gap_direction')}")
    parsed["gap_direction"] = None  # force null → DB NULL → detected by monitoring
```

**Effect:** Even if Sonnet produces a response with injected instructions mixed in, the `_enforce_schema` function discards all non-schema fields. The output forwarded to Opus is a clean, type-validated dict — not free-form text.

---

## Layer 2: LLM Trust Degradation Sandbox

### 2.1 Node Trust Labels (P1 — conceptual, 1 hr to implement)

Treat LangGraph state fields as carrying a trust level. Fields derived from external sources or earlier LLM outputs should be labeled and handled with progressively less trust.

```python
# market_analyst_agents.py — extend WorkflowState
class WorkflowState(TypedDict):
    snapshot:              dict           # EXTERNAL — trust: NONE
    raw_market_data:       dict           # LLM-derived (Haiku) — trust: LOW
    chip_report:           str            # LLM-derived (Sonnet) — trust: LOW
    tech_report:           str            # LLM-derived (Sonnet) — trust: LOW
    final_brief:           str            # LLM-derived (Opus) — trust: MEDIUM
    final_report:          str            # LLM-derived (Haiku) — trust: MEDIUM
    portfolio_advice:      str            # LLM-derived (Sonnet) — trust: MEDIUM
    db_row_id:             Optional[int]  # system-generated — trust: HIGH
```

**Enforcement rules:**
- `chip_report` and `tech_report` (trust: LOW) are validated by schema before entering Opus context
- `final_brief` (trust: MEDIUM) is length-capped before entering `format_agent`
- `final_report` (trust: MEDIUM) is Markdown-stripped before delivery to Telegram

---

### 2.2 Telegram Markdown Stripping (P0 — 15 min)

The most direct injection path to the user is via Telegram with `parse_mode="Markdown"`. Disable it for LLM-generated content:

```python
# messenger_tools.py — remove parse_mode from send_telegram
resp = client.post(url, json={
    "chat_id": chat_id,
    "text": message,
    # "parse_mode": "Markdown",  ← REMOVED — prevents clickable link injection
})
```

If Markdown formatting is desired for operator-generated alerts only, use it selectively and only for strings that are templates, not LLM outputs.

---

### 2.3 Output Length Cap Before Push (P0 — 15 min)

```python
# messenger_tools.py — add before API call
MAX_LINE_CHARS = 4000    # LINE text message limit
MAX_TG_CHARS   = 4096    # Telegram message limit

def send_line(message: str) -> dict:
    message = message[:MAX_LINE_CHARS]
    ...

def send_telegram(message: str) -> dict:
    message = message[:MAX_TG_CHARS]
    ...
```

Prevents context-window overload and ensures oversized LLM outputs (from token count manipulation attacks) cannot force API errors that might expose error details.

---

## Layer 3: Execution Sandbox (Process and OS)

### 3.1 Dedicated System User (P0 — 30 min)

Currently all scripts run as `itadmin`, the same user that manages the server. Create a dedicated low-privilege user:

```bash
# On Ubuntu server
sudo useradd --system --no-create-home --shell /usr/sbin/nologin ai_agent
sudo mkdir -p /opt/ai_agent_studio
sudo chown ai_agent:ai_agent /opt/ai_agent_studio
sudo chmod 750 /opt/ai_agent_studio

# Move project files
sudo mv /home/itadmin/ai_agent_studio /opt/ai_agent_studio/
sudo chown -R ai_agent:ai_agent /opt/ai_agent_studio/
```

Update cron to run as `ai_agent`:
```bash
sudo crontab -u ai_agent -e
# 20 8 * * 1-5 cd /opt/ai_agent_studio && uv run python investment_workflow.py
```

**Effect:** If a workflow script is compromised, the attacker gets `ai_agent` shell access — not `itadmin`. The `ai_agent` user has no sudo rights, no shell login, and access only to the project directory.

---

### 3.2 Filesystem Permissions (P0 — 15 min)

```bash
# .env should be readable only by the runner
chmod 600 /opt/ai_agent_studio/.env

# Output files: restrict to owner
umask 0027  # set in /etc/cron.d/ai_agents or in daily_run.sh

# market_snapshot.json: only the collector should write it
# Workflow scripts only need read
chmod 644 /opt/ai_agent_studio/market_snapshot.json  # after signing
```

---

### 3.3 MCP Server Environment Isolation (P0 — 30 min)

As detailed in `privilege_boundary_analysis.md`, pass minimal environments to MCP child processes:

```python
# agent_orchestrator.py
MINIMAL_ENV = {"PATH": os.environ["PATH"]}
StdioServerParameters(command="uv", args=["run", "mcp_servers/system_inspector.py"], env=MINIMAL_ENV)

# test_collection.py — finance_mcp_server only needs HTTP, no DB/messenger secrets
FINANCE_ENV = {"PATH": os.environ["PATH"]}
# If save_brief_to_db is intentionally deployed: add TIDB_* vars
# If send_brief_to_user is intentionally deployed: add LINE_*/TELEGRAM_* vars
# Currently both are orphans — pass nothing
StdioServerParameters(command="uv", args=["run", "mcp_servers/finance_mcp_server.py"], env=FINANCE_ENV)
```

---

### 3.4 systemd Service with Security Directives (P1 — 1 hr)

Replace the cron approach with a systemd service for the investment workflow. This enables OS-level sandboxing:

```ini
# /etc/systemd/system/ai-investment-workflow.service
[Unit]
Description=AI Investment Workflow
After=network.target

[Service]
Type=oneshot
User=ai_agent
WorkingDirectory=/opt/ai_agent_studio
ExecStart=/home/ai_agent/.local/bin/uv run python investment_workflow.py
EnvironmentFile=/opt/ai_agent_studio/.env

# Filesystem sandboxing
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/opt/ai_agent_studio
PrivateTmp=yes

# Network restrictions
RestrictAddressFamilies=AF_INET AF_INET6
# Do NOT use PrivateNetwork — workflow needs outbound HTTPS

# Capability restrictions
CapabilityBoundingSet=
NoNewPrivileges=yes

# Prevent privilege escalation
SecureBits=noroot

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/ai-investment-workflow.timer
[Unit]
Description=Run AI Investment Workflow daily on trading days

[Timer]
OnCalendar=Mon-Fri 08:20 Asia/Taipei
Persistent=true

[Install]
WantedBy=timers.target
```

**Effect:** Even if the Python process is compromised, it cannot write outside `/opt/ai_agent_studio`, cannot access home directories, uses a private `/tmp`, and cannot gain new capabilities.

---

## Layer 4: Streamlit Authentication Sandbox

### 4.1 streamlit-authenticator (P0 — 2 hrs)

```python
# dashboard.py — add at top
import streamlit_authenticator as stauth
import yaml
from yaml.loader import SafeLoader

with open("auth_config.yaml") as f:
    config = yaml.load(f, Loader=SafeLoader)

authenticator = stauth.Authenticate(
    config["credentials"],
    config["cookie"]["name"],
    config["cookie"]["key"],
    config["cookie"]["expiry_days"],
)

name, authentication_status, username = authenticator.login()

if authentication_status is False:
    st.error("Username/password is incorrect")
    st.stop()
elif authentication_status is None:
    st.warning("Please enter your username and password")
    st.stop()
# Everything below only executes for authenticated users
```

```yaml
# auth_config.yaml (gitignored — add to .gitignore)
credentials:
  usernames:
    admin:
      name: Admin
      password: $2b$12$<bcrypt_hash>  # generate: stauth.Hasher(['your_password']).generate()
cookie:
  expiry_days: 1
  key: <random_32_char_string>
  name: ai_agent_auth
```

### 4.2 SSH Tunnel as Alternative (P0 — 10 min)

The simplest approach: bind Streamlit to localhost and access via SSH tunnel.

```bash
# Server: start dashboard bound to 127.0.0.1 only
streamlit run dashboard.py --server.address 127.0.0.1 --server.port 8501

# Client: open tunnel before accessing
ssh -L 8501:127.0.0.1:8501 ai-agents-server
# Then browse: http://localhost:8501
```

**Effect:** Port 8501 is no longer accessible from the LAN at all. Only users with SSH access can reach the dashboard. This is the recommended approach for a single-user system — simpler than adding a Python auth library, and enforced at the OS level.

---

## Sandbox Implementation Roadmap

| Layer | Control | Priority | Effort | Coverage |
|-------|---------|----------|--------|----------|
| L1 | Snapshot HMAC integrity check | 🔴 P0 | 30 min | Memory poisoning (S-03, T-02) |
| L1 | News headline injection filter | 🔴 P0 | 45 min | Prompt injection (T-01) |
| L2 | Telegram Markdown stripped | 🔴 P0 | 15 min | Markdown injection |
| L2 | Output length cap before push | 🔴 P0 | 15 min | Oversize output attacks |
| L3 | Streamlit SSH tunnel or auth | 🔴 P0 | 10 min | Unauthenticated write (E-01) |
| L3 | MCP minimal env isolation | 🟠 P1 | 30 min | Credential exposure to child procs |
| L3 | Dedicated `ai_agent` OS user | 🟠 P1 | 30 min | Lateral movement from compromise |
| L3 | `.env` chmod 600 | 🟠 P1 | 5 min | Credential file exposure |
| L1 | LLM output schema enforcement | 🟠 P1 | 2 hrs | Context hijacking (T-03) |
| L3 | systemd service + security directives | 🟡 P2 | 1 hr | Filesystem + process isolation |
| L1 | Node trust labels + field validation | 🟡 P2 | 1 hr | Multi-hop chain attacks |
| L3 | TiDB per-component credentials | 🟡 P2 | 2 hrs | Privilege escalation via DB root |

**Total P0 effort: ~2 hours**  
**Total P1 effort: ~3.5 hours**  
**Total P2 effort: ~4 hours**
