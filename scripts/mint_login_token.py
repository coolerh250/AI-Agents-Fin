#!/usr/bin/env python
"""scripts/mint_login_token.py — break-glass dashboard login

Generates a dashboard OTP login token directly, bypassing the LINE
webhook. Use this while the LINE webhook is not enabled — it is the
admin login path for the dashboard.

    uv run python scripts/mint_login_token.py             # owner (LINE_USER_ID)
    uv run python scripts/mint_login_token.py <user_id>   # a specific LINE user

The 8-char code is valid for 5 minutes — enter it on the dashboard
login screen. The token is verified to have persisted before printing,
so a printed code is always a working one.
"""
import argparse
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mint a dashboard OTP login token (break-glass, no webhook).")
    parser.add_argument("user_id", nargs="?", default=None,
                        help="LINE user id; defaults to LINE_USER_ID from .env")
    args = parser.parse_args()

    user_id = args.user_id or os.getenv("LINE_USER_ID", "")
    if not user_id:
        print("ERROR: no user_id given and LINE_USER_ID is not set in .env")
        return 1

    from database_tools import create_login_token, _engine

    token = create_login_token(user_id)

    # create_login_token returns a token string even if the DB write failed,
    # so verify the row actually landed before telling the user it works.
    with _engine().connect() as conn:
        ok = conn.execute(
            text("SELECT 1 FROM login_tokens "
                 "WHERE token = :t AND used = 0 AND expires_at > NOW()"),
            {"t": token},
        ).fetchone()
    if not ok:
        print("ERROR: token generated but not persisted (DB write failed) — check logs")
        return 1

    print()
    print(f"  登入代碼:  {token}")
    print(f"  使用者:    {user_id[:10]}...")
    print(f"  有效期限:  5 分鐘")
    print()
    print("  請在 Dashboard 登入欄輸入上方代碼。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
