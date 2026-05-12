"""QMe MCP client using the official MCP Python SDK."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

_TOKEN_MAX_AGE = 86400  # 24 hours
_TOKEN_FILE    = Path(os.getenv("DATA_DIR", "data")) / ".qme_token.json"

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

log = logging.getLogger(__name__)


class MCPError(Exception):
    """Raised when an MCP tool call fails."""


# ── Token storage (in-memory) ─────────────────────────────────────────────────

class MemoryTokenStorage:
    """TokenStorage that lives entirely in RAM — nothing written to disk."""

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str) -> None:
        self._client_id     = client_id
        self._client_secret = client_secret
        self._redirect_uri  = redirect_uri
        self._access_token:  str | None = None
        self._token_type:    str        = "Bearer"
        self._refresh_token: str | None = None
        self._scope:         str | None = None
        self._expires_at:    float      = 0.0
        self._email:         str | None = None

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
        log.info("QMe token stored (expires in 24 h)")

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
            _TOKEN_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        log.info("QMe token cleared")

    def _save_to_disk(self) -> None:
        try:
            _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            _TOKEN_FILE.write_text(json.dumps({
                "access_token":  self._access_token,
                "token_type":    self._token_type,
                "refresh_token": self._refresh_token,
                "scope":         self._scope,
                "expires_at":    self._expires_at,
                "email":         self._email,
            }))
        except Exception as e:
            log.warning("Could not save token to disk: %s", e)

    def _load_from_disk(self) -> None:
        try:
            data = json.loads(_TOKEN_FILE.read_text())
            if data.get("access_token") and time.time() < data.get("expires_at", 0) - 60:
                self._access_token  = data["access_token"]
                self._token_type    = data.get("token_type", "Bearer")
                self._refresh_token = data.get("refresh_token")
                self._scope         = data.get("scope")
                self._expires_at    = data["expires_at"]
                self._email         = data.get("email")
                log.info("QMe token loaded from disk (email=%s)", self._email)
        except Exception:
            pass


# ── Singleton storage ─────────────────────────────────────────────────────────

_storage: MemoryTokenStorage | None = None


def get_storage() -> MemoryTokenStorage:
    global _storage
    if _storage is None:
        from config import settings
        _storage = MemoryTokenStorage(
            client_id=settings.QME_CLIENT_ID,
            client_secret=settings.QME_CLIENT_SECRET,
            redirect_uri=settings.QME_REDIRECT_URI,
        )
        _storage._load_from_disk()
    return _storage


# ── MCP call helper ───────────────────────────────────────────────────────────

async def call_tool(name: str, arguments: dict) -> Any:
    """Call a QMe MCP tool using the SDK. Raises MCPError if not connected."""
    import httpx
    from config import settings

    storage = get_storage()
    tokens = await storage.get_tokens()
    if not tokens or not tokens.access_token:
        raise MCPError("Not connected to QMe — paste your access token via the Connect button.")

    headers = {"Authorization": f"Bearer {tokens.access_token}"}

    try:
        async with streamablehttp_client(settings.QME_MCP_BASE_URL, headers=headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
    except* httpx.HTTPStatusError as eg:
        # ExceptionGroup from anyio TaskGroup — unwrap the first HTTP error
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


# ── Legacy shim (routers still import get_mcp_client) ────────────────────────

class _MCPShim:
    def reset_session(self) -> None:
        pass

    async def search_surveys(self, query: str = "", limit: int = 50) -> dict:
        return await call_tool("search_surveys", {"query": query, "limit": limit})

    async def get_survey_definition(self, survey_id: int) -> dict:
        return await call_tool("get_survey_definition", {"survey_id": survey_id})

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
            })
            pages.append(page)
            rows = page.get("rows", []) if isinstance(page, dict) else []
            log.info("rows page offset=%d → %d rows", offset, len(rows))
            if len(rows) < page_limit:
                break
            offset += page_limit
        return pages


def get_mcp_client() -> _MCPShim:
    return _MCPShim()
