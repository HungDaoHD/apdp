"""QMe OAuth — server-side login + OTP → capture code → exchange for token."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config import settings
from services.mcp_client import get_storage, cleanup_expired_sessions, invalidate_sessions_for_email, _get_fernet

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/qme", tags=["qme-auth"])

_QME = "https://retail.qand.me"
_TOKEN_URL = f"{settings.QME_MCP_BASE_URL}/oauth/token"

_SESSION_TTL = 300  # 5 minutes

_COOKIE_NAME = "sf_session"
_COOKIE_MAX_AGE = 7 * 86400   # 7 days


def _check_rate_limit(ip: str) -> None:
    from services.rate_limiter import check as _rl_check
    _rl_check(ip, settings.DATA_DIR, limit=5, window=60, prefix="rate_login")


# ── Disk-based login sessions (shared across all Gunicorn workers) ─────────────

def _login_dir() -> Path:
    return Path(settings.DATA_DIR)


def _login_file(login_key: str) -> Path:
    return _login_dir() / f".login_{login_key}.json"


def _save_login_session(login_key: str, data: dict) -> None:
    f = _login_file(login_key)
    f.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data).encode()
    fernet = _get_fernet()
    if fernet:
        payload = fernet.encrypt(payload)
    tmp = f.with_suffix(".tmp")
    tmp.write_bytes(payload)
    try:
        tmp.chmod(0o600)
    except Exception:
        pass
    tmp.replace(f)


def _get_login_session(login_key: str) -> dict | None:
    try:
        raw = _login_file(login_key).read_bytes()
        fernet = _get_fernet()
        if fernet:
            try:
                raw = fernet.decrypt(raw)
            except Exception:
                pass  # unencrypted legacy file
        return json.loads(raw)
    except Exception:
        return None


def _delete_login_session(login_key: str) -> None:
    try:
        _login_file(login_key).unlink(missing_ok=True)
    except Exception:
        pass


def _cleanup_sessions() -> None:
    """Delete expired login session files."""
    data_dir = _login_dir()
    if not data_dir.exists():
        return
    now = time.time()
    for f in data_dir.glob(".login_*.json"):
        try:
            data = json.loads(f.read_text())
            if now - data.get("created_at", 0) > _SESSION_TTL:
                f.unlink(missing_ok=True)
        except Exception:
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/status")
async def qme_status(request: Request):
    session_id = request.cookies.get(_COOKIE_NAME, "")
    if not session_id:
        return {"connected": False, "email": None}
    storage = get_storage(session_id)
    return {"connected": storage.is_connected(), "email": storage.email}


# ── Step 1 — email + password ─────────────────────────────────────────────────

class LoginBody(BaseModel):
    email: str
    password: str

_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"


@router.post("/login")
async def qme_login(body: LoginBody, request: Request):
    ip = request.client.host if request.client else "unknown"
    _check_rate_limit(ip)
    cleanup_expired_sessions()
    _cleanup_sessions()
    state = secrets.token_urlsafe(32)
    code_verifier, code_challenge = _pkce()

    auth_url = (
        f"{settings.QME_MCP_BASE_URL}/oauth/authorize?"
        + urlencode({
            "response_type":         "code",
            "client_id":             settings.QME_CLIENT_ID,
            "redirect_uri":          _REDIRECT_URI,
            "state":                 state,
            "code_challenge":        code_challenge,
            "code_challenge_method": "S256",
            "scope":                 "mcp mcp:read",
            "resource":              settings.QME_MCP_BASE_URL,
        })
    )
    log.info("auth_url: %s (params redacted)", auth_url.split("?")[0])

    pw = hashlib.sha256(body.password.encode()).hexdigest()

    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        # Hit authorize → QMe sets OAuth context in session, redirects to login2
        r0 = await client.get(auth_url)
        log.info("authorize → %s  url=%s", r0.status_code, r0.url)

        if r0.status_code >= 400:
            log.warning("QMe authorize failed (%s): %s", r0.status_code, r0.text[:300])
            raise HTTPException(502, "QMe authorization service unavailable — please try again")

        csrf = _extract_csrf(r0.text)
        if not csrf:
            raise HTTPException(502, f"No CSRF token found on QMe page (url: {r0.url})")

        r1 = await client.post(
            f"{_QME}/Admin/DoLogin2",
            data={
                "email":         body.email,
                "password":      pw,
                "_token":        csrf,
                "login2":        "1",
                "countrycode":   "",
                "isShareClient": "1",
            },
            headers={
                "X-CSRF-TOKEN":     csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Referer":          f"{_QME}/login2",
            },
        )

        try:
            data = r1.json()
        except Exception:
            raise HTTPException(502, f"Unexpected response from DoLogin2: {r1.text[:300]}")

        log.info("DoLogin2 → %s", data)

        if data.get("result") != 1:
            raise HTTPException(401, data.get("msg") or data.get("message") or "Invalid credentials")

        # Extract ref from DoLogin2 redirect URL (parse_qs URL-decodes it)
        login_redirect = data.get("redirect", "")
        ref = data.get("ref", "")
        if not ref and "ref=" in login_redirect:
            ref = parse_qs(urlparse(login_redirect).query).get("ref", [""])[0]
        log.info("ref: %s", ref[:20] if ref else "(empty)")

        # Visit loginverify NOW (same session) so QMe arms the OTP session
        csrf_verify = ""
        if ref:
            r_lv = await client.get(
                f"{_QME}/loginverify",
                params={"ref": ref},
            )
            log.info("loginverify GET → %s  url=%s", r_lv.status_code, r_lv.url)
            # Extract CSRF token from the OTP form — browser sends this with DoVerify
            csrf_verify = _extract_csrf(r_lv.text) or _extract_form_token(r_lv.text)
            log.info("csrf_verify: %s", csrf_verify[:10] if csrf_verify else "(none)")

        all_cookies = {c.name: c.value for c in client.cookies.jar}
        log.info("cookies after loginverify: %s", list(all_cookies.keys()))

    login_key = secrets.token_urlsafe(24)
    _save_login_session(login_key, {
        "email":         body.email,
        "cookies":       all_cookies,
        "state":         state,
        "code_verifier": code_verifier,
        "redirect_uri":  _REDIRECT_URI,
        "csrf_verify":   csrf_verify,
        "created_at":    time.time(),
    })

    # H-3: return login_key as httpOnly cookie (not in body) to prevent JS access
    resp = JSONResponse({"ok": True, "ref": ref})
    resp.set_cookie(
        "sf_login_key",
        login_key,
        httponly=True,
        samesite="strict",
        max_age=_SESSION_TTL,
        secure=settings.SECURE_COOKIES,
    )
    return resp


# ── Step 2 — OTP ──────────────────────────────────────────────────────────────

class VerifyBody(BaseModel):
    email: str
    ref: str
    otp: str

@router.post("/verify")
async def qme_verify(body: VerifyBody, request: Request):
    # H-3: login_key is stored in httpOnly cookie, not sent in body
    login_key = request.cookies.get("sf_login_key", "")
    if not login_key:
        raise HTTPException(400, "No pending login session — please login again")
    session = _get_login_session(login_key)
    if not session:
        raise HTTPException(400, "No pending login session — please login again")
    if time.time() - session.get("created_at", 0) > _SESSION_TTL:
        _delete_login_session(login_key)
        raise HTTPException(400, "Login session expired — please login again")

    cookies = session["cookies"]  # dict from login step (already visited loginverify)
    verify_url = f"{_QME}/loginverify?ref={body.ref}"

    # DoVerify + follow authorize — keep same client so cookies stay fresh
    async with httpx.AsyncClient(follow_redirects=False, timeout=30,
                                 cookies=cookies) as client:
        post_data: dict = {"verifyCode": body.otp, "ref": body.ref}
        if session.get("csrf_verify"):
            post_data["_token"] = session["csrf_verify"]

        xsrf = cookies.get("XSRF-TOKEN", "")
        r = await client.post(
            f"{_QME}/Admin/DoVerify",
            data=post_data,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-XSRF-TOKEN":     xsrf,
                "Referer":          verify_url,
            },
        )

        log.info("DoVerify → status=%s  location=%s",
                 r.status_code, r.headers.get("location", "—"))

        try:
            data = r.json()
        except Exception:
            data = {}

        log.info("DoVerify body → %s", data)

        if not data or data.get("result") != 1:
            raise HTTPException(401, (data or {}).get("msg") or (data or {}).get("message") or "Incorrect OTP")

        # DoVerify redirects back to authorize — follow it with the now-authenticated session
        next_url = r.headers.get("location") or data.get("redirect", "")
        log.info("DoVerify next_url: %s", next_url)

        code, redirect_uri = "", session.get("redirect_uri", "")

        if next_url:
            code, found_uri = _extract_code([next_url])
            if found_uri:
                redirect_uri = found_uri

            if not code:
                r2 = await client.get(next_url)
                location2 = r2.headers.get("location", "")
                log.info("authorize follow → %s  location=%s", r2.status_code, location2)
                code, found_uri = _extract_code([location2])
                if found_uri:
                    redirect_uri = found_uri

    if not code:
        raise HTTPException(
            502,
            "OTP verified but no OAuth code received. "
            f"Last redirect seen: {next_url!r}"
        )

    # OTP correct — consume the login session so it can't be reused
    _delete_login_session(login_key)

    # Invalidate any existing sessions for this email before issuing a new one
    email = session.get("email") or body.email
    invalidate_sessions_for_email(email)

    # Create a new session ID, exchange code for token, set cookie
    session_id = secrets.token_urlsafe(32)
    token_data = await _exchange_code(code, session["code_verifier"], redirect_uri)
    await _save_token(token_data, session_id)
    get_storage(session_id).email = email

    # Usage log — record successful login
    try:
        from services import usage_log_svc
        usage_log_svc.append(email, "login")
    except Exception:
        pass

    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        _COOKIE_NAME,
        session_id,
        httponly=True,
        samesite="strict",
        max_age=_COOKIE_MAX_AGE,
        secure=settings.SECURE_COOKIES,
    )
    # H-3: clear the login-flow cookie now that auth is complete
    resp.delete_cookie("sf_login_key")
    return resp


# ── Disconnect ────────────────────────────────────────────────────────────────

@router.delete("/disconnect")
async def qme_disconnect(request: Request):
    session_id = request.cookies.get(_COOKIE_NAME, "")
    email: str | None = None
    if session_id:
        try:
            email = get_storage(session_id).email
        except Exception:
            pass
        get_storage(session_id).clear()
    # Usage log — record logout
    try:
        from services import usage_log_svc
        usage_log_svc.append(email, "logout")
    except Exception:
        pass
    resp = JSONResponse({"status": "disconnected"})
    resp.delete_cookie(_COOKIE_NAME)
    return resp


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_code(urls: list[str]) -> tuple[str, str]:
    """Return (code, redirect_uri_base) from the first URL that has a code param."""
    for url in urls:
        if not url:
            continue
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        code = (params.get("code") or [None])[0]
        if code:
            redirect_uri = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            return code, redirect_uri
    return "", ""


async def _exchange_code(code: str, code_verifier: str, redirect_uri: str) -> dict:
    log.info("Exchanging code at %s  redirect_uri=%s", _TOKEN_URL, redirect_uri)
    payload: dict = {
        "grant_type":    "authorization_code",
        "code":          code,
        "client_id":     settings.QME_CLIENT_ID,
        "client_secret": settings.QME_CLIENT_SECRET,
        "code_verifier": code_verifier,
    }
    if redirect_uri:
        payload["redirect_uri"] = redirect_uri

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(_TOKEN_URL, data=payload)

    log.info("Token exchange → %s  %s", r.status_code, r.text[:300])

    if r.status_code != 200:
        log.warning("Token exchange failed (%s): %s", r.status_code, r.text[:300])
        raise HTTPException(502, "Token exchange failed — please try logging in again")
    return r.json()


async def _save_token(token_data: dict, session_id: str) -> None:
    from mcp.shared.auth import OAuthToken
    await get_storage(session_id).set_tokens(OAuthToken(
        access_token=token_data["access_token"],
        token_type=token_data.get("token_type", "Bearer"),
        expires_in=token_data.get("expires_in"),
        refresh_token=token_data.get("refresh_token"),
        scope=token_data.get("scope"),
    ))
    log.info("QMe token saved for session %s…", session_id[:8])


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _cookies_str(d: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in d.items())


def _extract_csrf(html: str) -> str:
    for line in html.splitlines():
        if 'name="csrf-token"' in line and 'content="' in line:
            idx = line.index('content="') + 9
            return line[idx:line.index('"', idx)]
    return ""


def _extract_form_token(html: str) -> str:
    """Extract hidden _token field from a Laravel form."""
    import re
    m = re.search(r'<input[^>]+name=["\']_token["\'][^>]+value=["\']([^"\']+)["\']', html)
    if not m:
        m = re.search(r'<input[^>]+value=["\']([^"\']+)["\'][^>]+name=["\']_token["\']', html)
    return m.group(1) if m else ""
