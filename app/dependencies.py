"""FastAPI shared dependencies."""
from __future__ import annotations

from fastapi import HTTPException, Request


def require_qme(request: Request) -> str:
    """Dependency: verify QMe is connected for this session.

    Returns session_id so route handlers can pass it to MCP calls.
    """
    from services.mcp_client import get_storage
    session_id = request.cookies.get("sf_session", "")
    if not session_id or not get_storage(session_id).is_connected():
        raise HTTPException(status_code=401, detail="Not connected to QMe MCP")
    return session_id
