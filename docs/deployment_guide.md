# Enterprise AI Agent Studio — 部署指南

目標環境：Ubuntu Server 26.04，AMD Ryzen 7 / 30 GB RAM / 232 GB NVMe SSD

---

## Phase 0：遠端連線準備（本機執行）

### SSH Key 建立與佈署

```powershell
# 產生 SSH Key
ssh-keygen -t ed25519 -f "$HOME\.ssh\ai_agents_server" -C "ai-agents-server"

# 將公鑰複製到遠端（首次需要密碼）
$key = Get-Content "$HOME\.ssh\ai_agents_server.pub"
ssh itadmin@10.0.1.20 "mkdir -p ~/.ssh && echo '$key' >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys"
```

### 本機 SSH Config（`~/.ssh/config`）

```
Host ai-agents-server
    HostName 10.0.1.20
    User itadmin
    IdentityFile ~/.ssh/ai_agents_server
    ServerAliveInterval 60
    ServerAliveCountMax 3
```

---

## Phase 1：遠端環境初始化

```bash
# 系統更新與基礎工具
echo 'p@ssw0rd' | sudo -S apt-get update -y
echo 'p@ssw0rd' | sudo -S apt-get install -y \
    build-essential python3-dev libssl-dev libffi-dev \
    curl wget git vim htop net-tools

# 安全加固（SSH）
echo 'p@ssw0rd' | sudo -S tee /etc/ssh/sshd_config.d/hardening.conf <<EOF
PermitRootLogin no
X11Forwarding no
EOF
echo 'p@ssw0rd' | sudo -S systemctl restart sshd
```

---

## Phase 2：Docker 安裝

```bash
# 安裝 Docker Engine
curl -fsSL https://get.docker.com | sh
echo 'p@ssw0rd' | sudo -S usermod -aG docker itadmin

# 建立隔離網路
echo 'p@ssw0rd' | sudo -S docker network create \
    --driver bridge \
    --subnet 172.20.0.0/16 \
    --opt com.docker.network.bridge.name=br-agent \
    agent_sandbox
```

Docker 版本確認：Engine 29.4.3 / Compose v5.1.3

---

## Phase 3：Python 環境與專案初始化

```bash
# 安裝 uv
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env

# 建立專案目錄
mkdir -p /home/itadmin/ai_agent_studio/mcp_servers
cd /home/itadmin/ai_agent_studio

# 初始化 uv 專案（Python 3.13+）
uv init --python 3.13
uv venv --python 3.13

# 安裝所有依賴
uv add beautifulsoup4 httpx langchain-anthropic langgraph \
       loguru lxml mcp pandas psutil pydantic \
       python-dotenv yfinance
```

---

## Phase 4：佈署應用程式檔案

從本機工作目錄（VS Code\AI Agents）scp 至遠端：

```powershell
$remote = "ai-agents-server:/home/itadmin/ai_agent_studio"
$local  = "c:\Users\stpadmin\Documents\VS Code\AI Agents"

# MCP Servers
scp "$local\mcp_servers\system_inspector.py"  "${remote}/mcp_servers/"
scp "$local\mcp_servers\finance_mcp_server.py" "${remote}/mcp_servers/"

# 測試腳本
scp "$local\test_mcp_client.py"  "$remote/"
scp "$local\test_collection.py"  "$remote/"

# Agent 編排
scp "$local\agent_orchestrator.py" "$remote/"
```

設定 `.env`（遠端）：

```bash
cat > /home/itadmin/ai_agent_studio/.env <<EOF
ANTHROPIC_API_KEY=your_key_here
LOG_LEVEL=INFO
EOF
```

---

## Phase 5：驗證

```bash
cd /home/itadmin/ai_agent_studio
source $HOME/.local/bin/env

# 依賴確認
uv run python -c "from bs4 import BeautifulSoup; import pandas, yfinance; print('deps OK')"

# MCP 通訊層測試（system_inspector）
uv run test_mcp_client.py

# 財金數據採集測試（三工具並發）
uv run test_collection.py
# 預期：Success rate 100% (3/3)，latency < 5s

# Agent 編排測試（LangGraph + Claude Haiku）
uv run agent_orchestrator.py
# 預期：STATUS: READY
```

---

## 專案結構

```
ai_agent_studio/
├── .env                        # API Key（不進版控）
├── .env.template               # Key 範本
├── .gitignore
├── .python-version             # 3.14
├── pyproject.toml              # uv 依賴宣告
├── main.py                     # 入口點（佔位）
├── agent_orchestrator.py       # Phase 4：LangGraph 編排
├── test_mcp_client.py          # Phase 3：MCP 通訊測試
├── test_collection.py          # Phase 5：財金數據並發採集測試
├── market_snapshot.json        # 採集輸出（runtime 產生）
├── collection_journal.jsonl    # 採集日誌（runtime 追加）
└── mcp_servers/
    ├── system_inspector.py     # Phase 3：系統感知 MCP Server
    └── finance_mcp_server.py   # Phase 5：財金數據 MCP Server
```

---

## 已知問題與解法

| 問題 | 解法 |
|------|------|
| TAIFEX 自營商欄位偏移（rowspan） | `tr.find_all("td", attrs={"align": "right"})` 取數值欄 |
| sudo 非互動式需要密碼 | `echo 'password' \| sudo -S command` |
| Python 3.14 在 pyproject.toml 需 `>=3.13` | 避免寫死 3.14，uv 會找已安裝版本 |
| 大型 Python 檔案無法透過 heredoc 部署 | 本機寫 Temp 檔 → scp 佈署 |

---

## 硬體規格（ai-agents-server）

| 項目 | 規格 |
|------|------|
| IP | 10.0.1.20 |
| OS | Ubuntu Server 26.04 |
| CPU | AMD Ryzen 7 4800U — 8核 / 16執行緒 |
| RAM | 30 GB |
| 磁碟 | NVMe SSD 238.5 GB（LVM 擴充後 232 GB 可用）|
| Docker 網路 | agent_sandbox 172.20.0.0/16（bridge: br-agent）|
