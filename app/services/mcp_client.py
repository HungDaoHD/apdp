"""QMe MCP client using the official MCP Python SDK."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

_SESSION_ID_RE = re.compile(r'^[A-Za-z0-9_-]{20,64}$')

_TOKEN_MAX_AGE    = 7 * 86400   # 7 days — upper cap for access token TTL on disk
_SESSION_MAX_AGE  = 7 * 86400   # 7 days — max session age before forced re-login

from mcp import ClientSession
try:
    from mcp.client.streamable_http import streamablehttp_client
except ImportError:
    # newer mcp SDK releases dropped the deprecated `streamablehttp_client`
    # alias in favor of `streamable_http_client`
    from mcp.client.streamable_http import streamable_http_client as streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

log = logging.getLogger(__name__)


def _token_dir() -> Path:
    return Path(os.getenv("DATA_DIR", "data"))


class TokenEncryptionRequired(Exception):
    """TOKEN_SECRET_KEY is missing/broken in a deployment that requires it (SEC-005).

    Only raised when SECURE_COOKIES=True (production) — local dev keeps the old
    fall-back-to-plaintext behavior so a fresh checkout still runs without extra
    config, matching the existing SECURE_COOKIES=False dev-mode posture.
    """


def _get_fernet():
    """Return a Fernet instance if TOKEN_SECRET_KEY is configured.

    Fail-closed in production: a missing/invalid key must never result in
    tokens being written to disk unencrypted, so this raises instead of
    silently falling back to None there.
    """
    from config import settings
    if not settings.TOKEN_SECRET_KEY:
        if settings.SECURE_COOKIES:
            raise TokenEncryptionRequired(
                "TOKEN_SECRET_KEY is not set — refusing to store QMe credentials "
                "unencrypted. Set TOKEN_SECRET_KEY in the server's app/.env."
            )
        return None
    try:
        import base64, hashlib
        from cryptography.fernet import Fernet
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.TOKEN_SECRET_KEY.encode()).digest())
        return Fernet(key)
    except Exception as exc:
        if settings.SECURE_COOKIES:
            raise TokenEncryptionRequired(f"Failed to initialize token encryption: {exc}") from exc
        return None


def _restrict_windows(path: Path) -> None:
    """Restrict file permissions to current user on Windows via icacls."""
    try:
        import subprocess
        if os.name != "nt":
            return
        username = os.environ.get("USERNAME", "")
        if not username:
            return
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{username}:(R,W)"],
            check=False, capture_output=True,
        )
    except Exception:
        pass


class MCPError(Exception):
    """Raised when an MCP tool call fails."""


# ── Per-session token storage ─────────────────────────────────────────────────

class MemoryTokenStorage:
    """Per-session token storage: in-memory primary, disk for persistence across restarts."""

    def __init__(self, session_id: str, client_id: str, client_secret: str, redirect_uri: str) -> None:
        self._session_id    = session_id
        self._client_id     = client_id
        self._client_secret = client_secret
        self._redirect_uri  = redirect_uri
        self._access_token:  str | None = None
        self._token_type:    str        = "Bearer"
        self._refresh_token: str | None = None
        self._scope:         str | None = None
        self._expires_at:    float      = 0.0
        self._login_at:      float      = 0.0   # time of original login (not refresh)
        self._email:         str | None = None
        self._loaded_mtime:  float      = 0.0   # mtime of disk file as last read into memory
        self._refresh_lock               = asyncio.Lock()

    def _token_file(self) -> Path:
        if not _SESSION_ID_RE.match(self._session_id):
            raise ValueError(f"Invalid session_id format: {self._session_id!r}")
        return _token_dir() / f".qme_token_{self._session_id}.json"

    async def get_tokens(self) -> OAuthToken | None:
        self._sync_with_disk()
        if not self._access_token:
            return None
        return OAuthToken(
            access_token=self._access_token,
            token_type=self._token_type,
            expires_in=_TOKEN_MAX_AGE,
            refresh_token=self._refresh_token,
            scope=self._scope,
        )

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._access_token  = tokens.access_token
        self._token_type    = tokens.token_type or "Bearer"
        self._refresh_token = tokens.refresh_token
        self._scope         = tokens.scope
        # Use QMe's expires_in if provided; cap at _TOKEN_MAX_AGE
        ttl = int(tokens.expires_in) if tokens.expires_in else _TOKEN_MAX_AGE
        ttl = min(ttl, _TOKEN_MAX_AGE)
        self._expires_at    = time.time() + ttl
        # Record original login time (do NOT update on refresh calls)
        if not self._login_at:
            self._login_at = time.time()
        self._save_to_disk()
        log.info("QMe token stored for session %s… (expires in %ds / %.1fh)",
                 self._session_id[:8], ttl, ttl / 3600)

    async def refresh(self) -> bool:
        """Exchange refresh_token for a new access_token. Returns True on success."""
        async with self._refresh_lock:
            return await self._do_refresh()

    async def _do_refresh(self) -> bool:
        """Inner refresh — must be called with _refresh_lock held."""
        if self.is_connected():
            return True  # another concurrent caller already refreshed
        if not self._refresh_token:
            log.warning("refresh: no refresh_token for session %s…", self._session_id[:8])
            return False
        from config import settings
        import httpx
        token_url = f"{settings.QME_MCP_BASE_URL}/oauth/token"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(token_url, data={
                    "grant_type":    "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id":     self._client_id,
                    "client_secret": self._client_secret or "",
                })
            log.info("token refresh → %s (session=%s…)", r.status_code, self._session_id[:8])
            if r.status_code == 200:
                data = r.json()
                await self.set_tokens(OAuthToken(
                    access_token  = data["access_token"],
                    token_type    = data.get("token_type", "Bearer"),
                    expires_in    = data.get("expires_in"),
                    refresh_token = data.get("refresh_token") or self._refresh_token,
                    scope         = data.get("scope"),
                ))
                return True
            # Body omitted deliberately — a refresh response can carry tokens.
            log.warning("token refresh failed (%s) for session %s…",
                        r.status_code, self._session_id[:8])
        except Exception as exc:
            log.warning("token refresh error for session %s…: %s", self._session_id[:8], exc)
        return False

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        if not self._client_id:
            return None
        return OAuthClientInformationFull(
            client_id=self._client_id,
            client_secret=self._client_secret or None,
            redirect_uris=[self._redirect_uri],
            grant_types=["authorization_code"],
            response_types=["code"],
        )

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        pass  # client credentials come from config, not from the server

    def is_connected(self) -> bool:
        self._sync_with_disk()
        if not self._access_token:
            return False
        if time.time() >= self._expires_at - 60:
            return False
        return True

    def can_refresh(self) -> bool:
        """True if a refresh_token is available and session is within 7-day limit."""
        self._sync_with_disk()
        if not self._refresh_token:
            return False
        if not self._login_at or time.time() - self._login_at > _SESSION_MAX_AGE:
            age_days = (time.time() - self._login_at) / 86400 if self._login_at else 0
            log.info("session expired/unknown age (%.1fd) — re-login required (session=%s…)",
                     age_days, self._session_id[:8])
            return False
        return True

    @property
    def email(self) -> str | None:
        return self._email

    @email.setter
    def email(self, value: str | None) -> None:
        self._email = value
        self._save_to_disk()

    @property
    def has_refresh_token(self) -> bool:
        return bool(self._refresh_token)

    def clear(self) -> None:
        self._access_token  = None
        self._refresh_token = None
        self._expires_at    = 0.0
        self._email         = None
        try:
            self._token_file().unlink(missing_ok=True)
        except Exception:
            pass
        _storages.pop(self._session_id, None)
        log.info("QMe token cleared for session %s…", self._session_id[:8])

    def _save_to_disk(self) -> None:
        try:
            f = self._token_file()
            f.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({
                "access_token":  self._access_token,
                "token_type":    self._token_type,
                "refresh_token": self._refresh_token,
                "scope":         self._scope,
                "expires_at":    self._expires_at,
                "login_at":      self._login_at,
                "email":         self._email,
            }).encode()
            fernet = _get_fernet()
            if fernet:
                payload = fernet.encrypt(payload)
            tmp = f.with_suffix(".tmp")
            tmp.write_bytes(payload)
            try:
                tmp.chmod(0o600)
            except Exception:
                _restrict_windows(tmp)
            tmp.replace(f)
            try:
                self._loaded_mtime = f.stat().st_mtime
            except Exception:
                pass
        except TokenEncryptionRequired as e:
            log.error("Refusing to persist QMe token unencrypted (session=%s…): %s",
                       self._session_id[:8], e)
        except Exception as e:
            log.warning("Could not save token to disk: %s", e)

    def _sync_with_disk(self) -> None:
        """Reload from disk if a sibling Gunicorn worker wrote a newer token.

        Each worker is a separate process that only loads the token file once
        (on first use), then keeps it purely in memory — a refresh performed
        by another worker (or a disconnect that deletes the file) would
        otherwise never be seen here, leaving this worker stuck reporting a
        stale/expired session (or a revoked one as still valid) until process
        restart or cache eviction. Called before every connected/refresh
        decision so disk stays the source of truth across workers.
        """
        try:
            f = self._token_file()
            if not f.exists():
                if self._access_token or self._refresh_token:
                    self._access_token  = None
                    self._refresh_token = None
                    self._expires_at    = 0.0
                    self._loaded_mtime  = 0.0
                return
            if f.stat().st_mtime > self._loaded_mtime:
                self._load_from_disk()
        except Exception:
            pass

    def _load_from_disk(self) -> None:
        try:
            f = self._token_file()
            raw = f.read_bytes()
            fernet = _get_fernet()
            if fernet:
                try:
                    raw = fernet.decrypt(raw)
                except Exception:
                    pass  # unencrypted legacy file — will be re-saved encrypted on next write
            data = json.loads(raw)
            # Always restore refresh_token + session metadata (even if access_token expired)
            self._refresh_token = data.get("refresh_token")
            self._scope         = data.get("scope")
            self._login_at      = data.get("login_at", 0.0)
            self._email         = data.get("email")
            # Only restore access_token if still valid
            if data.get("access_token") and time.time() < data.get("expires_at", 0) - 60:
                self._access_token  = data["access_token"]
                self._token_type    = data.get("token_type", "Bearer")
                self._expires_at    = data["expires_at"]
                log.info("QMe token loaded from disk (session=%s… email=%s expires_in=%.0fs)",
                         self._session_id[:8], self._email, self._expires_at - time.time())
            elif self._refresh_token:
                log.info("QMe access token expired — refresh_token available (session=%s… email=%s)",
                         self._session_id[:8], self._email)
            self._loaded_mtime = f.stat().st_mtime
        except Exception:
            pass


# ── Session registry ──────────────────────────────────────────────────────────

# OrderedDict (not plain dict) so get_storage() can track recency and evict the
# least-recently-used entry once _MAX_STORAGES is hit — a well-formed but
# never-issued session id (any random 20-64 char token) still passes
# _SESSION_ID_RE, so format validation alone does not bound memory use; this
# cap is the actual bound.
_storages: "OrderedDict[str, MemoryTokenStorage]" = OrderedDict()
_MAX_STORAGES = 2000   # generous multiple of real team size; caps worst-case RAM


class InvalidSessionId(Exception):
    """Raised when a caller-supplied session id does not match the format
    this app ever issues — used to reject it before it can be cached."""


def get_storage(session_id: str) -> MemoryTokenStorage:
    """Return (or create) token storage for this session.

    Real session ids are `secrets.token_urlsafe(32)` from qme_auth.py, which
    always match `_SESSION_ID_RE`. A public endpoint (/api/qme/status) calls
    this with whatever cookie value a client sends, so an unrecognised format
    is rejected *before* an entry is cached — otherwise a client sending a
    fresh garbage cookie on every request grows `_storages` without bound.
    """
    if session_id in _storages:
        _storages.move_to_end(session_id)   # mark as most-recently-used
        return _storages[session_id]

    if not _SESSION_ID_RE.match(session_id):
        raise InvalidSessionId(session_id)

    from config import settings
    storage = MemoryTokenStorage(
        session_id=session_id,
        client_id=settings.QME_CLIENT_ID,
        client_secret=settings.QME_CLIENT_SECRET,
        redirect_uri=settings.QME_REDIRECT_URI,
    )
    storage._load_from_disk()

    if len(_storages) >= _MAX_STORAGES:
        evicted_id, _ = _storages.popitem(last=False)   # drop least-recently-used
        log.warning("Session registry at cap (%d) — evicted %s…", _MAX_STORAGES, evicted_id[:8])

    _storages[session_id] = storage
    return storage


def init_sessions() -> None:
    """Load all persisted sessions from disk. Call once at app startup."""
    data_dir = _token_dir()
    if not data_dir.exists():
        return
    prefix, suffix = ".qme_token_", ".json"
    for token_file in data_dir.glob(f"{prefix}*{suffix}"):
        session_id = token_file.name[len(prefix):-len(suffix)]
        if session_id:
            get_storage(session_id)
    log.info("Restored %d session(s) from disk", len(_storages))


def cleanup_expired_sessions() -> None:
    """Remove sessions with expired tokens from the in-memory registry and disk."""
    expired = [sid for sid, s in list(_storages.items())
               if not s.is_connected() and not s.can_refresh()]
    for sid in expired:
        storage = _storages.pop(sid, None)
        if storage:
            try:
                storage._token_file().unlink(missing_ok=True)
            except Exception:
                pass
    if expired:
        log.info("Cleaned up %d expired session(s)", len(expired))


def invalidate_sessions_for_email(email: str) -> None:
    """Clear all existing sessions for a given email (call before issuing a new one)."""
    to_clear = [s for s in list(_storages.values()) if s.email == email]
    for storage in to_clear:
        storage.clear()  # removes from _storages + deletes disk file
    if to_clear:
        log.info("Invalidated %d old session(s) for %s", len(to_clear), email)


# ── MCP call helper ───────────────────────────────────────────────────────────

async def call_tool(name: str, arguments: dict, session_id: str) -> Any:
    """Call a QMe MCP tool using the SDK. Auto-refreshes token on expiry. Raises MCPError if not connected."""
    import httpx
    from config import settings

    storage = get_storage(session_id)

    # Pre-emptive refresh: token expired locally but session is still within 7-day limit
    if not storage.is_connected():
        if storage.can_refresh():
            log.info("call_tool: token expired, refreshing (session=%s…)", session_id[:8])
            ok = await storage.refresh()
            if not ok:
                raise MCPError("Not connected to QMe — please reconnect.")
        else:
            raise MCPError("Not connected to QMe — please reconnect.")

    async def _do_call(access_token: str):
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            async with streamablehttp_client(settings.QME_MCP_BASE_URL, headers=headers) as (r, w, _):
                async with ClientSession(r, w) as sess:
                    await sess.initialize()
                    return await sess.call_tool(name, arguments)
        except* httpx.HTTPStatusError as eg:
            exc = eg.exceptions[0]
            status = exc.response.status_code
            if status in (401, 403):
                raise MCPError(f"__auth_{status}__") from exc
            raise MCPError(f"QMe MCP server returned {status} — check token or survey ID.") from exc
        except* Exception as eg:
            exc = eg.exceptions[0]
            raise MCPError(f"MCP call failed: {exc}") from exc

    tokens = await storage.get_tokens()
    if not tokens or not tokens.access_token:
        raise MCPError("Not connected to QMe — please reconnect.")

    try:
        result = await _do_call(tokens.access_token)
    except MCPError as first_err:
        # On 401/403 mid-call: try refresh once then retry
        if "__auth_" in str(first_err) and storage._refresh_token:
            log.info("call_tool: got %s mid-call, refreshing token (session=%s…)",
                     str(first_err), session_id[:8])
            ok = await storage.refresh()
            if not ok:
                raise MCPError("QMe token expired or unauthorised — please reconnect.") from first_err
            tokens = await storage.get_tokens()
            if not tokens or not tokens.access_token:
                raise MCPError("QMe token expired or unauthorised — please reconnect.")
            result = await _do_call(tokens.access_token)
        else:
            # Re-raise with clean message (strip internal marker)
            msg = str(first_err).replace("__auth_401__", "QMe token expired or unauthorised — please reconnect.") \
                                 .replace("__auth_403__", "QMe token unauthorised — please reconnect.")
            raise MCPError(msg) from first_err

    if result.content and hasattr(result.content[0], "text"):
        import json as _json
        try:
            return _json.loads(result.content[0].text)
        except Exception:
            return result.content[0].text
    return result


# ── MCP shim ──────────────────────────────────────────────────────────────────

class _MCPShim:
    def __init__(self, session_id: str) -> None:
        self._session_id = session_id

    def reset_session(self) -> None:
        pass

    async def search_surveys(self, query: str = "", limit: int = 50) -> dict:
        return await call_tool("search_surveys", {"query": query, "limit": limit}, self._session_id)

    async def get_survey_definition(self, survey_id: int) -> dict:
        return await call_tool("get_survey_definition", {"survey_id": survey_id}, self._session_id)

    async def prepare_survey_data_file(self, survey_id: int, format: str = "code",
                                        force_refresh: bool = False) -> dict:
        return await call_tool("prepare_survey_data_file", {
            "survey_id":     survey_id,
            "format":        format,
            "force_refresh": force_refresh,
        }, self._session_id)

    async def get_survey_data_file_status(self, job_id: int) -> dict:
        return await call_tool("get_survey_data_file_status", {"job_id": job_id}, self._session_id)

    async def read_survey_data_file(self, job_id: int, file: str = "data",
                                     offset: int = 0, limit: int = 500) -> dict:
        return await call_tool("read_survey_data_file", {
            "job_id": job_id,
            "file":   file,
            "offset": offset,
            "limit":  limit,
        }, self._session_id)

    async def get_export_csv(self, survey_id: int) -> bytes:
        """prepare → poll until ready → read all chunks → return assembled CSV bytes."""
        import asyncio
        import csv as _csv
        import io

        MAX_WAIT = 300  # seconds

        meta = await self.prepare_survey_data_file(survey_id, format="code", force_refresh=False)
        if not isinstance(meta, dict):
            raise MCPError(f"prepare_survey_data_file unexpected type: {type(meta).__name__}")

        job_id  = meta.get("job_id")
        elapsed = 0

        while meta.get("status") != "ready":
            retry_after = int(meta.get("retry_after_seconds") or 5)
            if elapsed >= MAX_WAIT:
                raise MCPError(f"export timed out after {MAX_WAIT}s (status={meta.get('status')})")
            log.info("export status=%s, retry in %ds… (job_id=%s)", meta.get("status"), retry_after, job_id)
            await asyncio.sleep(retry_after)
            elapsed += retry_after
            meta = await self.get_survey_data_file_status(job_id)

        log.info("export ready, reading chunks (job_id=%s)…", job_id)
        parts: list[str] = []
        offset    = 0
        limit     = 500
        chunk_num = 0

        while True:
            chunk_num += 1
            chunk = await self.read_survey_data_file(job_id, file="data", offset=offset, limit=limit)

            if isinstance(chunk, str):
                parts.append(chunk)
            elif isinstance(chunk, dict):
                csv_text = chunk.get("csv") or chunk.get("content") or ""
                rows_val = chunk.get("rows", [])
                if csv_text:
                    parts.append(csv_text if csv_text.endswith("\n") else csv_text + "\n")
                elif rows_val:
                    if isinstance(rows_val[0], str):
                        parts.append("\n".join(rows_val) + "\n")
                    elif isinstance(rows_val[0], (list, dict)):
                        buf = io.StringIO()
                        if isinstance(rows_val[0], dict):
                            writer = _csv.DictWriter(buf, fieldnames=list(rows_val[0].keys()))
                            if offset == 0:
                                writer.writeheader()
                            writer.writerows(rows_val)
                        else:
                            _csv.writer(buf).writerows(rows_val)
                        parts.append(buf.getvalue())

                pagination     = chunk.get("pagination", {})
                rows_remaining = pagination.get("rows_remaining", 0)
                has_more       = pagination.get("has_more", False)
                next_offset    = pagination.get("next_offset", offset + limit)
                log.info("export chunk %d: offset=%d remaining=%d", chunk_num, offset, rows_remaining)
                if not has_more or rows_remaining == 0:
                    break
                offset = next_offset
            else:
                log.warning("read_survey_data_file unexpected type: %s", type(chunk))
                break

        csv_text = "".join(parts)
        return csv_text.encode("utf-8-sig")

    async def get_all_rows(self, survey_id: int, page_limit: int = 200) -> list[dict]:
        from datetime import date
        pages: list[dict] = []
        offset = 0
        date_to = date.today().isoformat()
        while True:
            page = await call_tool("get_survey_rows", {
                "survey_id": survey_id,
                "date_from": "2000-01-01",
                "date_to": date_to,
                "format": "code",
                "limit": page_limit,
                "offset": offset,
            }, self._session_id)
            pages.append(page)
            rows = page.get("rows", []) if isinstance(page, dict) else []
            log.info("rows page offset=%d → %d rows", offset, len(rows))
            if len(rows) < page_limit:
                break
            offset += page_limit
        return pages


def get_mcp_client(session_id: str) -> _MCPShim:
    return _MCPShim(session_id)


def get_access_token(session_id: str) -> str | None:
    """Return the current access token for a session, or None if not connected."""
    storage = get_storage(session_id)
    if storage.is_connected():
        return storage._access_token
    return None
