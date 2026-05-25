"""Disk-backed IP rate limiter — shared across all Gunicorn workers."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from fastapi import HTTPException


def check(ip: str, data_dir: str, *,
          limit: int = 5, window: int = 60, prefix: str = "rate") -> None:
    """Raise HTTP 429 if *ip* exceeds *limit* requests within *window* seconds."""
    now = time.time()
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16]
    rate_file = Path(data_dir) / f".{prefix}_{ip_hash}.json"
    try:
        attempts = [t for t in json.loads(rate_file.read_text()).get("a", [])
                    if now - t < window]
    except Exception:
        attempts = []
    if len(attempts) >= limit:
        raise HTTPException(429, f"Too many requests — please wait {window}s")
    attempts.append(now)
    try:
        rate_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = rate_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"a": attempts}))
        tmp.replace(rate_file)
    except Exception:
        pass
