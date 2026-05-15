"""
snapshot_integrity.py
HMAC-SHA256 sign/verify for market_snapshot.json.
Set SNAPSHOT_HMAC_KEY env var to enable. Omit to run without signing (backward-compatible).
"""
import hashlib
import hmac as _hmac
import json
import os

from loguru import logger

_KEY = os.getenv("SNAPSHOT_HMAC_KEY", "").encode()


def sign_snapshot(data: dict) -> dict:
    """Return data with _hmac field appended. No-op if SNAPSHOT_HMAC_KEY is not set."""
    if not _KEY:
        return data
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
    sig = _hmac.new(_KEY, canonical, hashlib.sha256).hexdigest()
    return {**data, "_hmac": sig}


def verify_snapshot(data: dict) -> bool:
    """
    Verify snapshot signature. Returns True if:
    - SNAPSHOT_HMAC_KEY is not configured (skip)
    - Snapshot has no _hmac field (transition period — allow unsigned)
    - Signature matches

    Logs a warning if signature is present but does not match.
    Does not abort in any case (transition period).
    """
    if not _KEY:
        return True
    sig = data.get("_hmac")
    if sig is None:
        logger.warning("Snapshot has no HMAC signature — running unverified")
        return True
    incoming = {k: v for k, v in data.items() if k != "_hmac"}
    canonical = json.dumps(incoming, sort_keys=True, ensure_ascii=False).encode()
    expected = _hmac.new(_KEY, canonical, hashlib.sha256).hexdigest()
    if not _hmac.compare_digest(sig, expected):
        logger.warning("Snapshot HMAC mismatch — possible tampering or stale key")
    return True
