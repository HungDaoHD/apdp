"""Usage log API — receive client events, serve summary/recent for admin view."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from dependencies import require_qme
from services import usage_log_svc
from services.mcp_client import get_storage

router = APIRouter(prefix="/api/log", tags=["usage-log"])


class LogBody(BaseModel):
    action: str
    survey_id: int | None = None
    survey_name: str | None = None


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


@router.get("/summary", dependencies=[Depends(require_qme)])
async def get_summary():
    return usage_log_svc.summary()


@router.get("/recent", dependencies=[Depends(require_qme)])
async def get_recent():
    return usage_log_svc.recent()
