"""QMe MCP client using the official MCP Python SDK."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

_TOKEN_MAX_AGE = 86400  # 24 hours

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

log = logging.getLogger(__name__)


def _token_dir() -> Path:
    return Path(os.getenv("DATA_DIR", "data"))


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
        self._expires_at    = time.time() + _TOKEN_MAX_AGE
        self._save_to_disk()
        log.info("QMe token stored for session %s… (expires in 24 h)", self._session_id[:8])

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
            f.write_text(json.dumps({
                "access_token":  self._access_token,
                "token_type":    self._token_type,
                "refresh_token": self._refresh_token,
                "scope":         self._scope,
                "expires_at":    self._expires_at,
                "email":         self._email,
            }))
            f.chmod(0o600)  # owner read/write only — no other users on server can read
        except Exception as e:
            log.warning("Could not save token to disk: %s", e)

    def _load_from_disk(self) -> None:
        try:
            data = json.loads(self._token_file().read_text())
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
    """Call a QMe MCP tool using the SDK. Raises MCPError if not connected."""
    import httpx
    from config import settings

    storage = get_storage(session_id)
    tokens = await storage.get_tokens()
    if not tokens or not tokens.access_token:
        raise MCPError("Not connected to QMe — please reconnect.")

    headers = {"Authorization": f"Bearer {tokens.access_token}"}

    try:
        async with streamablehttp_client(settings.QME_MCP_BASE_URL, headers=headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
    except* httpx.HTTPStatusError as eg:
        exc = eg.exceptions[0]
        status = exc.response.status_code
        if status in (401, 403):
            raise MCPError("QMe token expired or unauthorised — please reconnect.") from exc
        raise MCPError(f"QMe MCP server returned {status} — check token or survey ID.") from exc
    except* Exception as eg:
        exc = eg.exceptions[0]
        raise MCPError(f"MCP call failed: {exc}") from exc

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

    async def get_export_csv(self, survey_id: int) -> str:
        """Call prepare_survey_data_file → return raw CSV text."""
        result = await call_tool("prepare_survey_data_file", {"survey_id": survey_id}, self._session_id)
        if isinstance(result, str):
            return result
        raise MCPError(f"prepare_survey_data_file returned unexpected type: {type(result).__name__}")

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
