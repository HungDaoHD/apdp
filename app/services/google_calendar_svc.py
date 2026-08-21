"""Google Calendar sync — each workload member can connect their own Google
account (OAuth2 Authorization Code flow) so their WFH/day-off/partial-day
bookings mirror onto their personal Google Calendar automatically.

One-way (webapp -> Google) and best-effort: every sync call here swallows its
own errors (see sync_create/sync_delete) — a Calendar hiccup must never fail
the booking itself, which is the actual source of truth.

Mirrors the OAuth2 mechanics already used in routers/qme_auth.py (state
CSRF binding, server-side code exchange, refresh-on-expiry) but persists
tokens in MongoDB rather than per-browser-session disk files, since a Google
Calendar connection belongs to a person's account, not a login session.

The app's own OAuth client_id/client_secret live in MongoDB too (see
get_app_config/set_app_config below), set once by the workload admin through
the UI, rather than in app/.env — this app is deployed by a separate ops
team via auto git-pull with no secret-injection step, and the admin isn't
one of the people with server access. The secret is encrypted at rest with
the same key as everything else here (see _encrypt/_decrypt) so this trades
nothing on security for not needing a server login.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from services.authz import WORKLOAD_ADMINS, display_name
from services.mcp_client import _get_fernet
from services.workload_svc import _db, set_leave_google_event_id

log = logging.getLogger(__name__)

_AUTH_URL   = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL  = "https://oauth2.googleapis.com/token"
_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
_SCOPE      = "https://www.googleapis.com/auth/calendar.events"

_STATE_TTL_MINUTES = 10
# Refresh a bit before actual expiry so a slow request never straddles it.
_EXPIRY_SLACK = timedelta(minutes=2)

_KIND_LABEL = {"wfh": "WFH", "off": "Day off", "partial": "Partial day"}


class GoogleCalendarError(Exception):
    """Message safe to show the user."""


# ── Collections ──────────────────────────────────────────────────────────────

def _tokens():
    return _db()["google_calendar_tokens"]


def _oauth_states():
    return _db()["google_oauth_states"]


def _app_config():
    """Singleton document (_id='app') holding this app's own OAuth client
    credentials — one per deployment, set once by the workload admin."""
    return _db()["google_oauth_app"]


async def init_db() -> None:
    """A pending OAuth state is only ever needed for a few minutes — Mongo's
    TTL index reaps expired ones on its own, no cron/cleanup job needed."""
    await _oauth_states().create_index("expires_at", expireAfterSeconds=0)


# ── App-level OAuth client config (admin-set, stored in Mongo) ──────────────

async def is_configured() -> bool:
    """Cheap existence check for every member's Connect button — unlike
    get_credentials(), never decrypts the secret."""
    doc = await _app_config().find_one({"_id": "app"}, {"client_secret": 1})
    return bool(doc and "client_secret" in doc)


async def get_app_config() -> dict:
    """Status only — the secret itself is never sent back to the browser
    once saved, same UX as any "API key" field: you can tell it's set, you
    can't read it back."""
    doc = await _app_config().find_one({"_id": "app"})
    if not doc:
        return {"configured": False, "client_id": "", "redirect_uri": ""}
    return {
        "configured": True,
        "client_id": doc.get("client_id", ""),
        "redirect_uri": doc.get("redirect_uri", ""),
    }


async def set_app_config(client_id: str, client_secret: str, redirect_uri: str, updated_by: str) -> None:
    client_id, client_secret, redirect_uri = client_id.strip(), client_secret.strip(), redirect_uri.strip()
    if not client_id or not redirect_uri:
        raise GoogleCalendarError("Client ID and Redirect URI are both required")
    now = datetime.now(timezone.utc)
    update: dict = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "updated_by": updated_by,
        "updated_at": now,
    }
    # An empty secret means "keep the one already saved" — lets the admin
    # fix a typo'd client_id/redirect_uri without having to re-paste the
    # secret, which the form never shows back to them in the first place.
    if client_secret:
        update["client_secret"] = _encrypt(client_secret)
    elif not await _app_config().find_one({"_id": "app", "client_secret": {"$exists": True}}):
        raise GoogleCalendarError("Client Secret is required")
    await _app_config().update_one(
        {"_id": "app"}, {"$set": update, "$setOnInsert": {"created_at": now}}, upsert=True,
    )


# ── Notification config (admin-set: extra attendees + line-manager CC) ──────

async def get_notify_config() -> dict:
    doc = await _app_config().find_one({"_id": "app"}, {"hr_emails": 1, "line_manager_email": 1})
    return {
        "hr_emails": (doc or {}).get("hr_emails", []),
        "line_manager_email": (doc or {}).get("line_manager_email", ""),
    }


async def set_notify_config(hr_emails: list[str], line_manager_email: str, updated_by: str) -> None:
    hr_emails = sorted({e.strip().lower() for e in hr_emails if e.strip()})
    line_manager_email = line_manager_email.strip().lower()
    now = datetime.now(timezone.utc)
    await _app_config().update_one(
        {"_id": "app"},
        {"$set": {
            "hr_emails": hr_emails,
            "line_manager_email": line_manager_email,
            "notify_updated_by": updated_by,
            "notify_updated_at": now,
        }, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )


async def get_credentials() -> dict | None:
    """This app's own client_id/client_secret/redirect_uri, or None if the
    admin hasn't set them up yet."""
    doc = await _app_config().find_one({"_id": "app"})
    if not doc or "client_secret" not in doc:
        return None
    return {
        "client_id": doc["client_id"],
        "client_secret": _decrypt(doc["client_secret"]),
        "redirect_uri": doc["redirect_uri"],
    }


# ── Encryption at rest ───────────────────────────────────────────────────────
# Reuses the same TOKEN_SECRET_KEY-derived Fernet key already used to encrypt
# QMe's own OAuth tokens on disk (services/mcp_client.py) — one key, one
# place it's configured, fail-closed in production if it's missing.

def _encrypt(value: str) -> str:
    fernet = _get_fernet()
    return fernet.encrypt(value.encode()).decode() if fernet else value


def _decrypt(value: str) -> str:
    fernet = _get_fernet()
    if not fernet:
        return value
    try:
        return fernet.decrypt(value.encode()).decode()
    except Exception:
        return value  # unencrypted legacy value (dev mode without a key)


def _as_aware(dt: datetime) -> datetime:
    """Motor/PyMongo hand back naive UTC datetimes — Mongo only ever stores UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── OAuth state (CSRF binding for the connect → callback round trip) ────────

async def save_oauth_state(state: str, email: str) -> None:
    await _oauth_states().insert_one({
        "_id": state,
        "email": email,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=_STATE_TTL_MINUTES),
    })


async def consume_oauth_state(state: str) -> str | None:
    """One-time use: the state is deleted whether or not it turns out valid,
    so a captured/replayed value can never be exchanged twice."""
    doc = await _oauth_states().find_one_and_delete({"_id": state})
    if not doc:
        return None
    if _as_aware(doc["expires_at"]) < datetime.now(timezone.utc):
        return None
    return doc["email"]


def authorize_url(state: str, email: str, creds: dict) -> str:
    return _AUTH_URL + "?" + urlencode({
        "client_id":     creds["client_id"],
        "redirect_uri":  creds["redirect_uri"],
        "response_type": "code",
        "scope":         _SCOPE,
        "access_type":   "offline",   # required to get a refresh_token at all
        "prompt":        "consent",   # required to get one on every connect, not just the first
        "state":         state,
        "login_hint":    email,
    })


# ── Token exchange / refresh / storage ───────────────────────────────────────

async def exchange_code(code: str, creds: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(_TOKEN_URL, data={
            "grant_type":    "authorization_code",
            "code":          code,
            "client_id":     creds["client_id"],
            "client_secret": creds["client_secret"],
            "redirect_uri":  creds["redirect_uri"],
        })
    if r.status_code != 200:
        log.warning("Google token exchange failed (%s)", r.status_code)
        raise GoogleCalendarError("Google token exchange failed — please try connecting again")
    return r.json()


async def save_tokens(email: str, token_data: dict) -> None:
    now = datetime.now(timezone.utc)
    update = {
        "access_token": _encrypt(token_data["access_token"]),
        "token_expiry": now + timedelta(seconds=int(token_data.get("expires_in", 3600))),
        "scope":        token_data.get("scope", _SCOPE),
        "status":       "connected",
        "updated_at":   now,
    }
    # Google only sends a refresh_token on the very first consent per client
    # (or whenever prompt=consent is used, which authorize_url always sets) —
    # a refresh response in particular usually omits it, so never overwrite
    # an already-stored one with nothing.
    if token_data.get("refresh_token"):
        update["refresh_token"] = _encrypt(token_data["refresh_token"])
    await _tokens().update_one(
        {"_id": email},
        {"$set": update, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )


async def get_status(email: str) -> dict:
    doc = await _tokens().find_one({"_id": email})
    return {"connected": bool(doc and doc.get("status") == "connected")}


async def disconnect(email: str) -> None:
    """Revoke on Google's side (best-effort) and always clear the local
    record — local state must end up clean even if the revoke call fails."""
    doc = await _tokens().find_one({"_id": email})
    if doc and doc.get("refresh_token"):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                await client.post(_REVOKE_URL, params={"token": _decrypt(doc["refresh_token"])})
        except Exception:
            log.warning("Google token revoke request failed for %s (clearing local record anyway)", email)
    await _tokens().delete_one({"_id": email})


async def _valid_access_token(email: str) -> str | None:
    """A usable access token for *email*, refreshing it first if it's stale.

    Returns None if the member never connected, or Google has revoked access
    (an `invalid_grant` on refresh) — callers treat that the same as "nothing
    to sync", never as an error to surface.
    """
    doc = await _tokens().find_one({"_id": email})
    if not doc or doc.get("status") != "connected":
        return None
    if _as_aware(doc["token_expiry"]) - _EXPIRY_SLACK > datetime.now(timezone.utc):
        return _decrypt(doc["access_token"])

    refresh_token = _decrypt(doc["refresh_token"]) if doc.get("refresh_token") else ""
    if not refresh_token:
        return None
    creds = await get_credentials()
    if not creds:
        # The admin removed/never finished the app-level setup — nothing to
        # refresh with. Leave the stored tokens alone (harmless either way);
        # just report "nothing usable right now".
        return None
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(_TOKEN_URL, data={
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
            "client_id":     creds["client_id"],
            "client_secret": creds["client_secret"],
        })
    if r.status_code != 200:
        error = ""
        try:
            error = str(r.json().get("error", ""))
        except Exception:
            pass
        log.warning("Google token refresh failed for %s (%s) error=%s", email, r.status_code, error or "(unknown)")
        if error == "invalid_grant":
            # The user revoked access from myaccount.google.com — stop retrying.
            await _tokens().update_one({"_id": email}, {"$set": {"status": "revoked"}})
        return None

    data = r.json()
    await save_tokens(email, data)
    return data["access_token"]


# ── Event sync ────────────────────────────────────────────────────────────────

async def _event_body(leave: dict) -> dict:
    detail = ""
    if leave.get("half"):
        detail = f" ({leave['half'].upper()})"
    elif leave.get("arrive_time") or leave.get("leave_time"):
        parts = [p for p in (
            leave.get("arrive_time") and f"in {leave['arrive_time']}",
            leave.get("leave_time") and f"out {leave['leave_time']}",
        ) if p]
        detail = f" ({'/'.join(parts)})"
    summary = f"{display_name(leave['email'])} - {_KIND_LABEL.get(leave['kind'], leave['kind'])}{detail}"
    # Google's all-day "date" end is exclusive (RFC 5545), unlike this app's
    # own inclusive end_date.
    end_exclusive = (date.fromisoformat(leave["end_date"]) + timedelta(days=1)).isoformat()
    body = {
        "summary": summary,
        "start": {"date": leave["start_date"]},
        "end":   {"date": end_exclusive},
    }
    if leave.get("note"):
        body["description"] = leave["note"]

    # Everyone who should hear about a booking (and get emailed — sync_create
    # sends with sendUpdates=all): the admin(s) always, plus any HR emails
    # the admin has added under Config. Included even on the admin's own
    # booking so it's never a silent, self-organized event.
    notify_cfg = await get_notify_config()
    attendee_set = dict.fromkeys(list(WORKLOAD_ADMINS) + notify_cfg["hr_emails"])
    # An admin booking for themselves also CCs their line manager — a plain
    # attendee, since Google only emails people it lists that way.
    if leave["email"] in WORKLOAD_ADMINS and notify_cfg["line_manager_email"]:
        attendee_set[notify_cfg["line_manager_email"]] = None
    attendees = [{"email": a} for a in attendee_set]
    if attendees:
        body["attendees"] = attendees
    return body


async def sync_create(leave: dict) -> None:
    """Create the Google Calendar event for a just-created booking, if its
    owner has connected their calendar. Called from a FastAPI BackgroundTask
    — never lets an exception escape, since that must not affect a response
    that has already been sent."""
    try:
        token = await _valid_access_token(leave["email"])
        if not token:
            return
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                _EVENTS_URL, json=await _event_body(leave), params={"sendUpdates": "all"},
                headers={"Authorization": f"Bearer {token}"},
            )
        if r.status_code not in (200, 201):
            log.warning("Google Calendar create-event failed for %s (%s): %s",
                        leave["email"], r.status_code, r.text[:200])
            return
        event_id = r.json().get("id")
        if event_id:
            await set_leave_google_event_id(leave["id"], event_id)
    except Exception:
        log.exception("Google Calendar sync (create) failed for booking %s", leave.get("id"))


async def sync_delete(leave: dict) -> None:
    """Delete the Google Calendar event for a just-cancelled booking, if one
    was ever created for it."""
    event_id = leave.get("google_event_id")
    if not event_id:
        return
    try:
        token = await _valid_access_token(leave["email"])
        if not token:
            return
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.delete(
                f"{_EVENTS_URL}/{event_id}", params={"sendUpdates": "all"},
                headers={"Authorization": f"Bearer {token}"},
            )
        # 404/410 means the user already deleted it on the Google side — that
        # is the outcome we wanted anyway, not a failure.
        if r.status_code not in (200, 204, 404, 410):
            log.warning("Google Calendar delete-event failed for %s (%s)", leave["email"], r.status_code)
    except Exception:
        log.exception("Google Calendar sync (delete) failed for booking %s", leave.get("id"))
