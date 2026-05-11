"""QMe MCP client using the official MCP Python SDK."""
from __future__ import annotations

import logging
import time
from typing import Any

_TOKEN_MAX_AGE = 86400  # 24 hours

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
        log.info("QMe token stored in memory (expires in 24 h)")

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

    def clear(self) -> None:
        self._access_token  = None
        self._refresh_token = None
        self._expires_at    = 0.0
        self._email         = None
        log.info("QMe token cleared")


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
    return _storage


# ── MCP call helper ───────────────────────────────────────────────────────────

async def call_tool(name: str, arguments: dict) -> Any:
    """Call a QMe MCP tool using the SDK. Raises MCPError if not connected."""
    from config import settings

    storage = get_storage()
    tokens = await storage.get_tokens()
    if not tokens or not tokens.access_token:
        raise MCPError("Not connected to QMe — paste your access token via the Connect button.")

    headers = {"Authorization": f"Bearer {tokens.access_token}"}

    async with streamablehttp_client(settings.QME_MCP_BASE_URL, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)

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
        import logging
        pages: list[dict] = []
        offset = 0
        while True:
            page = await call_tool("get_survey_rows", {
                "survey_id": survey_id,
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
