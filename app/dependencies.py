"""FastAPI shared dependencies."""
from __future__ import annotations

import logging

from fastapi import HTTPException, Request

log = logging.getLogger(__name__)


async def require_qme(request: Request) -> str:
    """Dependency: verify QMe is connected for this session.

    If the access_token has expired but a refresh_token is available,
    automatically refreshes the token (transparent to the user).
    Session is valid for up to 7 days from original login.

    Returns session_id so route handlers can pass it to MCP calls.
    """
    from services.mcp_client import get_storage
    session_id = request.cookies.get("sf_session", "")
    if not session_id:
        raise HTTPException(status_code=401, detail="Not connected to QMe MCP")

    storage = get_storage(session_id)

    # Fast path: token still valid
    if storage.is_connected():
        return session_id

    # Token expired — attempt silent refresh if refresh_token available
    if storage.can_refresh():
        ok = await storage.refresh()
        if ok:
            log.info("require_qme: silent token refresh OK (session=%s…)", session_id[:8])
            return session_id
        log.warning("require_qme: silent token refresh FAILED (session=%s…)", session_id[:8])

    raise HTTPException(status_code=401, detail="Not connected to QMe MCP")
