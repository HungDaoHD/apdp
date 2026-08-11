"""FastAPI shared dependencies."""
from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request

from services.authz import is_admin, is_allowed

log = logging.getLogger(__name__)

_NOT_AUTHORISED = "This account is not authorised to use SurveyFlow"


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
