"""FastAPI shared dependencies."""
from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import Depends, HTTPException, Request

from services.authz import is_admin, is_allowed, is_workload_admin, is_workload_user

log = logging.getLogger(__name__)

_NOT_AUTHORISED = "This account is not authorised to use SurveyFlow"

_CSRF_HEADER = "x-csrf-token"
_CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


async def require_qme(request: Request) -> str:
    """Dependency: verify QMe is connected for this session.

    If the access_token has expired but a refresh_token is available,
    automatically refreshes the token (transparent to the user).
    Session is valid for up to 7 days from original login.

    Returns session_id so route handlers can pass it to MCP calls.
    """
    from services.mcp_client import InvalidSessionId, get_storage
    session_id = request.cookies.get("sf_session", "")
    if not session_id:
        raise HTTPException(status_code=401, detail="Not connected to QMe MCP")

    try:
        storage = get_storage(session_id)
    except InvalidSessionId:
        raise HTTPException(status_code=401, detail="Not connected to QMe MCP")

    # Fast path: token still valid
    connected = storage.is_connected()

    # Token expired — attempt silent refresh if refresh_token available
    if not connected and storage.can_refresh():
        connected = await storage.refresh()
        if connected:
            log.info("require_qme: silent token refresh OK (session=%s…)", session_id[:8])
        else:
            log.warning("require_qme: silent token refresh FAILED (session=%s…)", session_id[:8])

    if not connected:
        raise HTTPException(status_code=401, detail="Not connected to QMe MCP")

    # Checked on every request, not just at login: removing someone from the
    # access list then takes effect at once instead of when their 7-day
    # session finally expires. 403 (not 401) so the UI shows the reason
    # instead of looping the user back through the login gate.
    if not is_allowed(storage.email):
        log.warning("Access denied for %s (not on access list)", storage.email)
        raise HTTPException(status_code=403, detail=_NOT_AUTHORISED)

    return session_id


async def require_admin(session_id: str = Depends(require_qme)) -> str:
    """Dependency: like require_qme, but the session must also be an admin."""
    from services.mcp_client import get_storage
    email = get_storage(session_id).email
    if not is_admin(email):
        log.warning("Admin-only endpoint refused for %s", email)
        raise HTTPException(status_code=403, detail="Administrator access required")
    return session_id


async def current_email(session_id: str = Depends(require_qme)) -> str:
    """The signed-in account's email — what the workload routes key everything on."""
    from services.mcp_client import get_storage
    return (get_storage(session_id).email or "").strip().lower()


async def require_workload(email: str = Depends(current_email)) -> str:
    """Dependency: the session must be on the workload roster.

    Deliberately narrower than require_qme: everyone in ALLOWED_USERS can use
    SurveyFlow, but only the four accounts in WORKLOAD_MEMBERS belong to the
    team whose workload this tracks.
    """
    if not is_workload_user(email):
        log.warning("Workload access refused for %s (not on workload roster)", email)
        raise HTTPException(status_code=403, detail="This account is not on the workload team")
    return email


async def require_workload_admin(email: str = Depends(require_workload)) -> str:
    """Dependency: workload member *and* administrator (creates/assigns projects)."""
    if not is_workload_admin(email):
        log.warning("Workload admin endpoint refused for %s", email)
        raise HTTPException(status_code=403, detail="Only the manager can change projects")
    return email


def csrf_token_for(session_id: str) -> str:
    """Derive the CSRF token a browser must echo back for *session_id*.

    Just a hash of the session id — no separate secret needed. session_id
    is itself 256 bits from secrets.token_urlsafe(32) and lives in an
    httpOnly cookie, so a cross-origin attacker never has it to hash; a
    same-origin script (e.g. this app's own JS, right after login) can read
    this derived value from /api/qme/status to attach as a header on
    mutating requests. SHA-256 is one-way, so exposing the hash never
    reveals the underlying session id.

    Deliberately worker-independent: a shared HMAC secret would need to be
    generated once and distributed to every Gunicorn worker process, which
    session_id already achieves for free via the existing session cookie.
    """
    return hashlib.sha256(f"csrf:{session_id}".encode()).hexdigest()


async def require_csrf(request: Request, session_id: str = Depends(require_qme)) -> None:
    """Dependency: reject state-changing requests missing a valid CSRF token.

    SameSite=Strict on the session cookie already blocks the classic
    cross-site CSRF request in current browsers — this is defense in depth
    for older browsers or a future SameSite misconfiguration, per SEC-013.
    Only applies to non-safe HTTP methods; GET/HEAD/OPTIONS are read-only
    and pass through unchecked.
    """
    if request.method in _CSRF_SAFE_METHODS:
        return
    sent = request.headers.get(_CSRF_HEADER, "")
    expected = csrf_token_for(session_id)
    if not sent or not hmac.compare_digest(sent, expected):
        log.warning("CSRF check failed for %s %s (session=%s…)",
                    request.method, request.url.path, session_id[:8])
        raise HTTPException(status_code=403, detail="Missing or invalid CSRF token — please reload the page")
