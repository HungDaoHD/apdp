"""Usage log API — receive client events, serve summary/recent for admin view."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from dependencies import require_admin, require_qme
from services import usage_log_svc
from services.mcp_client import get_storage

router = APIRouter(prefix="/api/log", tags=["usage-log"])

# Closed set of client-submitted actions — anything else is rejected with 422 so
# arbitrary strings can never reach the log file (and from there, the log page).
# Must stay in sync with the logEvent() call sites in static/editor.html.
# "login"/"logout" are appended server-side in qme_auth.py, not through this API.
LogAction = Literal[
    "page_load", "preview", "save", "download", "open_datatable", "upload_zip",
    "login", "logout",
]


class LogBody(BaseModel):
    action: LogAction
    survey_id: int | None = None
    survey_name: str | None = Field(default=None, max_length=200)


@router.post("", dependencies=[Depends(require_qme)])
async def log_event(body: LogBody, request: Request):
    """Called by the frontend to record an action."""
    session_id = request.cookies.get("sf_session", "")
    email: str | None = None
    if session_id:
        try:
            email = get_storage(session_id).email
        except Exception:
            pass
    usage_log_svc.append(email, body.action, body.survey_id, body.survey_name)
    return {"ok": True}


# Admin-only: these expose every user's activity, not just the caller's.
@router.get("/summary", dependencies=[Depends(require_admin)])
async def get_summary():
    return usage_log_svc.summary()


@router.get("/recent", dependencies=[Depends(require_admin)])
async def get_recent():
    return usage_log_svc.recent()
