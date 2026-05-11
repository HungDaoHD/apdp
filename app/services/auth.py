"""Cookie session (HMAC-SHA256 signed)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from fastapi import Request

COOKIE_NAME     = "apdp_session"
SESSION_MAX_AGE = 86400 * 7   # 7 days


def _sign(payload: dict, secret: str) -> str:
    data = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    sig  = hmac.new(secret.encode(), data.encode(), hashlib.sha256).hexdigest()
    return f"{data}.{sig}"


def _verify(token: str, secret: str) -> dict | None:
    try:
        data, sig = token.rsplit(".", 1)
        expected  = hmac.new(secret.encode(), data.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(data + "=="))
        if time.time() > payload.get("exp", 0):
            return None
        return payload
    except Exception:
        return None


def make_session_token(username: str, secret: str) -> str:
    payload = {"sub": username, "exp": int(time.time()) + SESSION_MAX_AGE}
    return _sign(payload, secret)


def get_session(request: Request, secret: str) -> dict | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return _verify(token, secret)
