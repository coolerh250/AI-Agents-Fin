"""deploy/render_templates.py — substitute ${VARS} in deploy/templates/*.tmpl.

Uses `string.Template` (stdlib, no jinja2 dependency). Render-and-write only —
file installation (sudo cp, crontab) is handled by the Makefile so this stays
side-effect-free / testable.

Substitution vars (all auto-derived from env + sys.executable):
  ${REPO_ROOT}     — absolute path to this repo's root
  ${RUN_USER}      — the user the systemd services should run as
  ${UV_BIN}        — absolute path to the `uv` binary
  ${UV_BIN_DIR}    — directory containing the uv binary (for cron's PATH)

Usage:
    uv run python deploy/render_templates.py systemd  > /tmp/dashboard.service
    uv run python deploy/render_templates.py crontab  > /tmp/crontab.rendered
    uv run python deploy/render_templates.py all      # writes to deploy/rendered/
"""
from __future__ import annotations

import argparse
import getpass
import os
import shutil
import sys
from pathlib import Path
from string import Template

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TMPL_DIR = _REPO_ROOT / "deploy" / "templates"
_OUT_DIR = _REPO_ROOT / "deploy" / "rendered"


def _resolve_vars() -> dict[str, str]:
    """Derive substitution vars from env + system state. Env wins for overrides."""
    uv_bin = os.environ.get("UV_BIN") or shutil.which("uv") or (
        Path.home() / ".local" / "bin" / "uv"
    ).as_posix()
    uv_bin_path = Path(uv_bin)
    return {
        "REPO_ROOT":  os.environ.get("REPO_ROOT", str(_REPO_ROOT)),
        "RUN_USER":   os.environ.get("RUN_USER", getpass.getuser()),
        "UV_BIN":     str(uv_bin),
        "UV_BIN_DIR": str(uv_bin_path.parent),
    }


def _render(tmpl_name: str, vars_: dict[str, str]) -> str:
    src = (_TMPL_DIR / tmpl_name).read_text(encoding="utf-8")
    return Template(src).substitute(vars_)


def render_systemd(out_dir: Path, vars_: dict[str, str]) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name in ("ai-agent-dashboard.service.tmpl", "ai-agent-webhook.service.tmpl"):
        body = _render(name, vars_)
        out = out_dir / name.replace(".tmpl", "")
        out.write_text(body, encoding="utf-8")
        written.append(out)
    return written


def render_crontab(out_dir: Path, vars_: dict[str, str]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    body = _render("crontab.tmpl", vars_)
    out = out_dir / "crontab"
    out.write_text(body, encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Render deploy templates")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for c in ("systemd", "crontab", "all"):
        sub.add_parser(c)
    args = parser.parse_args()

    vars_ = _resolve_vars()
    # Print resolution for transparency
    print("[render] substitutions:", file=sys.stderr)
    for k, v in vars_.items():
        print(f"  ${{{k}}} = {v}", file=sys.stderr)

    if args.cmd == "systemd":
        for p in render_systemd(_OUT_DIR, vars_):
            print(f"[render] wrote {p}", file=sys.stderr)
    elif args.cmd == "crontab":
        p = render_crontab(_OUT_DIR, vars_)
        print(f"[render] wrote {p}", file=sys.stderr)
    elif args.cmd == "all":
        for p in render_systemd(_OUT_DIR, vars_):
            print(f"[render] wrote {p}", file=sys.stderr)
        p = render_crontab(_OUT_DIR, vars_)
        print(f"[render] wrote {p}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
