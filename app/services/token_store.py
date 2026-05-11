"""Persist QMe OAuth token to a local JSON file."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TypedDict

log = logging.getLogger(__name__)


class TokenData(TypedDict, total=False):
    access_token: str
    token_type: str
    expires_at: float       # unix timestamp
    refresh_token: str


def load(path: str) -> TokenData | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data  # type: ignore[return-value]
    except Exception:
        return None


def save(path: str, data: TokenData) -> None:
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.info("QMe token saved to %s", path)


def delete(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


def is_valid(data: TokenData | None) -> bool:
    if not data or not data.get("access_token"):
        return False
    exp = data.get("expires_at", 0)
    if exp and time.time() >= exp - 60:   # 60s buffer
        return False
    return True
