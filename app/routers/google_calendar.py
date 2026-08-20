"""Google Calendar OAuth2 connect/disconnect — one calendar connection per
workload member, so their own bookings mirror onto their personal Google
Calendar. See services/google_calendar_svc.py for the token and event-sync
mechanics; this router only handles the browser-facing OAuth redirect dance.
"""
from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from dependencies import require_csrf, require_workload, require_workload_admin
from services import google_calendar_svc

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/workload/google", tags=["google-calendar"])


class AppConfigBody(BaseModel):
    """The workload admin's one-time OAuth client setup — see
    services/google_calendar_svc.py's module docstring for why this lives in
    Mongo (admin-settable from the UI) instead of app/.env."""
    client_id:     str = Field(min_length=1, max_length=200)
    client_secret: str = Field(default="", max_length=200, description="Blank keeps the currently saved secret")
    redirect_uri:  str = Field(min_length=1, max_length=300)


async def _log(**kw) -> None:
    try:
        from services import workload_svc
        await workload_svc.log_activity(**kw)
    except Exception:
        log.exception("Failed to write workload activity entry (%s)", kw.get("action"))


@router.get("/status")
async def status(email: str = Depends(require_workload)):
    return await google_calendar_svc.get_status(email)


@router.get("/app-config")
async def app_config(email: str = Depends(require_workload_admin)):
    """Whether the app-level OAuth client is set up, plus the non-secret
    fields — never the client_secret itself, see AppConfigBody."""
    return await google_calendar_svc.get_app_config()


@router.put("/app-config", dependencies=[Depends(require_csrf)])
async def set_app_config(body: AppConfigBody, email: str = Depends(require_workload_admin)):
    try:
        await google_calendar_svc.set_app_config(
            body.client_id, body.client_secret, body.redirect_uri, updated_by=email,
        )
    except google_calendar_svc.GoogleCalendarError as exc:
        raise HTTPException(422, str(exc))
    await _log(actor=email, action="google.app_config", target="Google Calendar app credentials updated")
    return await google_calendar_svc.get_app_config()


@router.get("/connect")
async def connect(email: str = Depends(require_workload)):
    """Kicks off the OAuth dance. *email* comes from the existing session
    cookie here — the callback below can't rely on that (see its docstring),
    so it's captured now in the server-side `state` record instead."""
    creds = await google_calendar_svc.get_credentials()
    if not creds:
        raise HTTPException(409, "Google Calendar isn't set up yet — ask the admin to configure it under the Users tab")
    state = secrets.token_urlsafe(32)
    await google_calendar_svc.save_oauth_state(state, email)
    return RedirectResponse(google_calendar_svc.authorize_url(state, email, creds))


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """No auth dependency here on purpose: Google's redirect to this endpoint
    is a cross-site top-level navigation, so the SameSite=Strict session
    cookie is *not* sent along with it. The caller's identity instead comes
    from the signed, one-time `state` recorded at /connect — only this
    server could have issued it, and it's bound to the account that started
    the flow, not to whatever cookie (if any) shows up here.
    """
    if error:
        log.warning("Google OAuth callback returned error=%s", error)
        return RedirectResponse("/workload?google=error")
    if not code or not state:
        raise HTTPException(400, "Missing code or state")

    email = await google_calendar_svc.consume_oauth_state(state)
    if not email:
        log.warning("Google OAuth callback with an unknown/expired state")
        return RedirectResponse("/workload?google=error")

    creds = await google_calendar_svc.get_credentials()
    if not creds:
        # Only reachable if the admin cleared the app config mid-flow.
        log.warning("Google OAuth callback with no app-level credentials configured")
        return RedirectResponse("/workload?google=error")

    try:
        token_data = await google_calendar_svc.exchange_code(code, creds)
    except google_calendar_svc.GoogleCalendarError:
        return RedirectResponse("/workload?google=error")

    await google_calendar_svc.save_tokens(email, token_data)
    await _log(actor=email, action="google.connect", target="Google Calendar connected")
    log.info("Google Calendar connected for %s", email)
    return RedirectResponse("/workload?google=connected")


@router.post("/disconnect", dependencies=[Depends(require_csrf)])
async def disconnect(email: str = Depends(require_workload)):
    await google_calendar_svc.disconnect(email)
    await _log(actor=email, action="google.disconnect", target="Google Calendar disconnected")
    log.info("Google Calendar disconnected for %s", email)
    return {"ok": True}
