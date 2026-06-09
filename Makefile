# AI Agent Studio — deployment Makefile
# Single-host Ubuntu/Debian install orchestrator.
# Run `make help` to list all targets.
#
# Quick start on a fresh host:
#   git clone <repo> ai_agent_studio && cd ai_agent_studio
#   make install
#
# Step-by-step (debug / re-run a single phase):
#   make install-deps   # uv + Docker
#   make env            # interactive .env collection
#   make db-up          # docker compose up TiDB + wait
#   make db-init seed snapshot
#   make services cron
#   make verify

SHELL := /bin/bash
# -e + pipefail catch real failures; -u disabled because /etc/bash.bashrc on
# Ubuntu references $PS1 and emits noisy "unbound variable" lines in non-
# interactive shells. -u doesn't add safety for this Makefile's targets
# (recipes use $(make-vars) and $$shell-locals that are always set).
.SHELLFLAGS := -e -o pipefail -c
.ONESHELL:
.DEFAULT_GOAL := help

REPO_ROOT  := $(shell pwd)
RENDER_DIR := $(REPO_ROOT)/deploy/rendered
UV_BIN     := $(shell command -v uv 2>/dev/null || echo $$HOME/.local/bin/uv)
RUN_USER   := $(shell id -un)

# Color codes for friendlier output
C_CYAN   := \033[36m
C_GREEN  := \033[32m
C_YELLOW := \033[33m
C_RED    := \033[31m
C_OFF    := \033[0m

.PHONY: help install install-deps env db-up db-init seed snapshot services cron verify status clean

help: ## List all targets
	@printf "$(C_CYAN)AI Agent Studio — Makefile targets$(C_OFF)\n\n"
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  $(C_GREEN)%-14s$(C_OFF) %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@printf "\nQuick start: $(C_YELLOW)make install$(C_OFF)\n"

install: install-deps env db-up db-init seed snapshot services cron verify ## One-shot fresh install
	@printf "\n$(C_GREEN)[OK] full install complete$(C_OFF)\n"
	@printf "Dashboard: http://127.0.0.1:8501  Webhook: http://127.0.0.1:8502/health\n"

install-deps: ## Verify (and install if missing) uv + Docker
	@printf "$(C_CYAN)[deps]$(C_OFF) checking uv\n"
	@if ! command -v uv >/dev/null 2>&1; then \
	  printf "$(C_YELLOW)[deps]$(C_OFF) installing uv...\n"; \
	  curl -LsSf https://astral.sh/uv/install.sh | sh; \
	  export PATH="$$HOME/.local/bin:$$PATH"; \
	else printf "  uv ✓ ($$(uv --version))\n"; fi
	@printf "$(C_CYAN)[deps]$(C_OFF) checking docker\n"
	@if ! command -v docker >/dev/null 2>&1; then \
	  printf "$(C_YELLOW)[deps]$(C_OFF) installing docker...\n"; \
	  curl -fsSL https://get.docker.com | sh; \
	  sudo usermod -aG docker $(RUN_USER); \
	  printf "$(C_YELLOW)[deps]$(C_OFF) NOTE: log out / log back in for docker group to take effect\n"; \
	else printf "  docker ✓ ($$(docker --version))\n"; fi
	@printf "$(C_CYAN)[deps]$(C_OFF) uv sync (project dependencies)\n"
	@$(UV_BIN) sync

env: ## Interactive .env collection (skips if .env already exists)
	@if [ -f .env ]; then \
	  printf "$(C_GREEN)[env]$(C_OFF) .env already present — keeping existing values\n"; \
	else \
	  $(UV_BIN) run python deploy/prompt_secrets.py; \
	fi

db-up: ## docker compose up TiDB + wait for readiness
	@printf "$(C_CYAN)[db-up]$(C_OFF) docker compose up TiDB\n"
	@docker compose -f docker/docker-compose.yml up -d
	@printf "$(C_CYAN)[db-up]$(C_OFF) waiting for TiDB to accept connections\n"
	@$(UV_BIN) run python deploy/bootstrap.py wait-db --timeout 60

db-init: ## Run all 19 ensure_*_table() — idempotent CREATE TABLE IF NOT EXISTS
	@$(UV_BIN) run python deploy/bootstrap.py db-init

seed: ## Seed v1 strategy profiles (all 6 agents + sentiment_curator)
	@$(UV_BIN) run python deploy/bootstrap.py seed

snapshot: ## Produce market_snapshot.json (test_collection.py)
	@$(UV_BIN) run python deploy/bootstrap.py snapshot

services: ## Render + install systemd units, enable --now
	@printf "$(C_CYAN)[services]$(C_OFF) rendering systemd unit files\n"
	@REPO_ROOT="$(REPO_ROOT)" RUN_USER="$(RUN_USER)" UV_BIN="$(UV_BIN)" \
	  $(UV_BIN) run python deploy/render_templates.py systemd
	@printf "$(C_CYAN)[services]$(C_OFF) installing to /etc/systemd/system (sudo)\n"
	@sudo install -m 644 $(RENDER_DIR)/ai-agent-dashboard.service /etc/systemd/system/
	@sudo install -m 644 $(RENDER_DIR)/ai-agent-webhook.service   /etc/systemd/system/
	@sudo systemctl daemon-reload
	@sudo systemctl enable --now ai-agent-dashboard ai-agent-webhook
	@printf "$(C_GREEN)[services]$(C_OFF) dashboard + webhook enabled and started\n"

cron: ## Render crontab template + install user crontab
	@printf "$(C_CYAN)[cron]$(C_OFF) rendering crontab\n"
	@REPO_ROOT="$(REPO_ROOT)" UV_BIN="$(UV_BIN)" \
	  $(UV_BIN) run python deploy/render_templates.py crontab
	@printf "$(C_CYAN)[cron]$(C_OFF) installing user crontab (replaces existing)\n"
	@crontab $(RENDER_DIR)/crontab
	@printf "$(C_GREEN)[cron]$(C_OFF) installed; $$(crontab -l | grep -cE '^[^#]') active entries\n"

verify: ## Health-check dashboard / webhook / DB / cron / profiles
	@printf "$(C_CYAN)[verify]$(C_OFF) systemd services\n"
	@systemctl is-active ai-agent-dashboard >/dev/null && printf "  ai-agent-dashboard ✓\n" || printf "  $(C_RED)ai-agent-dashboard FAIL$(C_OFF)\n"
	@systemctl is-active ai-agent-webhook   >/dev/null && printf "  ai-agent-webhook   ✓\n" || printf "  $(C_RED)ai-agent-webhook   FAIL$(C_OFF)\n"
	@printf "$(C_CYAN)[verify]$(C_OFF) HTTP endpoints\n"
	@curl -fsS http://127.0.0.1:8501/_stcore/health >/dev/null && printf "  dashboard /_stcore/health ✓\n" || printf "  $(C_RED)dashboard FAIL$(C_OFF)\n"
	@curl -fsS http://127.0.0.1:8502/health         >/dev/null && printf "  webhook   /health         ✓\n" || printf "  $(C_RED)webhook FAIL$(C_OFF)\n"
	@printf "$(C_CYAN)[verify]$(C_OFF) TiDB connectivity\n"
	@$(UV_BIN) run python -c "from database_tools import _engine; from sqlalchemy import text; print('  TiDB ✓ profiles=' + str(_engine().connect().execute(text('SELECT COUNT(*) FROM agent_strategy_profiles WHERE is_active=1')).scalar()))"
	@printf "$(C_CYAN)[verify]$(C_OFF) cron entries\n"
	@N=$$(crontab -l 2>/dev/null | grep -cE '^[^#[:space:]]'); printf "  active cron lines: $$N\n"; [ "$$N" -ge 9 ] || printf "  $(C_YELLOW)WARN expected >= 9$(C_OFF)\n"
	@printf "$(C_CYAN)[verify]$(C_OFF) market_snapshot.json\n"
	@[ -f market_snapshot.json ] && printf "  $$(ls -lh market_snapshot.json | awk '{print $$5, $$6, $$7, $$8}')\n" || printf "  $(C_RED)missing$(C_OFF)\n"

status: ## Show current systemctl + cron + DB status
	@printf "$(C_CYAN)[status]$(C_OFF) services\n"
	@systemctl status ai-agent-dashboard --no-pager 2>/dev/null | head -3 || true
	@systemctl status ai-agent-webhook   --no-pager 2>/dev/null | head -3 || true
	@printf "$(C_CYAN)[status]$(C_OFF) docker compose\n"
	@docker compose -f docker/docker-compose.yml ps
	@printf "$(C_CYAN)[status]$(C_OFF) cron\n"
	@crontab -l 2>/dev/null | grep -cE '^[^#[:space:]]' | xargs -I{} printf "  active entries: {}\n"

clean: ## Stop services + uninstall (DANGEROUS, confirms first)
	@printf "$(C_RED)[clean]$(C_OFF) will stop services, remove systemd units, and clear crontab.\n"
	@read -p "Proceed? (yes/N) " ans; [ "$$ans" = "yes" ] || (echo "abort"; exit 1)
	@sudo systemctl disable --now ai-agent-dashboard ai-agent-webhook 2>/dev/null || true
	@sudo rm -f /etc/systemd/system/ai-agent-dashboard.service /etc/systemd/system/ai-agent-webhook.service
	@sudo systemctl daemon-reload
	@crontab -r 2>/dev/null || true
	@docker compose -f docker/docker-compose.yml down
	@printf "$(C_GREEN)[clean]$(C_OFF) services + cron + TiDB stopped. .env and DB volume preserved.\n"
