"""QMe MCP client using the official MCP Python SDK."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

_TOKEN_MAX_AGE = 30 * 86400  # 30 days — upper cap; actual TTL comes from QMe's expires_in

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

log = logging.getLogger(__name__)


def _token_dir() -> Path:
    return Path(os.getenv("DATA_DIR", "data"))


def _get_fernet():
    """Return a Fernet instance if TOKEN_SECRET_KEY is configured, else None."""
    try:
        from config import settings
        if not settings.TOKEN_SECRET_KEY:
            return None
        import base64, hashlib
        from cryptography.fernet import Fernet
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.TOKEN_SECRET_KEY.encode()).digest())
        return Fernet(key)
    except Exception:
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
        self._email:         str | None = None

    def _token_file(self) -> Path:
        return _token_dir() / f".qme_token_{self._session_id}.json"

    async def get_tokens(self) -> OAuthToken | None:
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
        self._save_to_disk()
        log.info("QMe token stored for session %s… (expires in %ds / %.1fh)",
                 self._session_id[:8], ttl, ttl / 3600)

    async def refresh(self) -> bool:
        """Exchange refresh_token for a new access_token. Returns True on success."""
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
            log.warning("token refresh failed (%s) for session %s…: %s",
                        r.status_code, self._session_id[:8], r.text[:200])
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
        if not self._access_token:
            return False
        if time.time() >= self._expires_at - 60:
            return False
        return True

    @property
    def email(self) -> str | None:
        return self._email

    @email.setter
    def email(self, value: str | None) -> None:
        self._email = value
        self._save_to_disk()

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
        except Exception as e:
            log.warning("Could not save token to disk: %s", e)

    def _load_from_disk(self) -> None:
        try:
            raw = self._token_file().read_bytes()
            fernet = _get_fernet()
            if fernet:
                try:
                    raw = fernet.decrypt(raw)
                except Exception:
                    pass  # unencrypted legacy file — will be re-saved encrypted on next write
            data = json.loads(raw)
            if data.get("access_token") and time.time() < data.get("expires_at", 0) - 60:
                self._access_token  = data["access_token"]
                self._token_type    = data.get("token_type", "Bearer")
                self._refresh_token = data.get("refresh_token")
                self._scope         = data.get("scope")
                self._expires_at    = data["expires_at"]
                self._email         = data.get("email")
                log.info("QMe token loaded from disk (session=%s… email=%s)",
                         self._session_id[:8], self._email)
        except Exception:
            pass


# ── Session registry ──────────────────────────────────────────────────────────

_storages: dict[str, MemoryTokenStorage] = {}


def get_storage(session_id: str) -> MemoryTokenStorage:
    """Return (or create) token storage for this session."""
    if session_id not in _storages:
        from config import settings
        storage = MemoryTokenStorage(
            session_id=session_id,
            client_id=settings.QME_CLIENT_ID,
            client_secret=settings.QME_CLIENT_SECRET,
            redirect_uri=settings.QME_REDIRECT_URI,
        )
        storage._load_from_disk()
        _storages[session_id] = storage
    return _storages[session_id]


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
    expired = [sid for sid, s in list(_storages.items()) if not s.is_connected()]
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

    # Pre-emptive refresh: token expired locally but we have a refresh_token
    if not storage.is_connected():
        if storage._refresh_token:
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
