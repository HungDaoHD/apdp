"""QMe MCP client — thin async wrapper around the MCP SDK (streamable HTTP).

Usage
-----
    from surveyflow.steps.fetch.client import QMeClient

    client = QMeClient(url, access_token)
    definition = client.get_survey_definition(survey_id)

The client is **stateless with respect to auth** — it receives a plain bearer
token at construction time and never refreshes it.  Token acquisition and
refresh logic belongs to the calling application (e.g. ``main.py`` or APDP).

Duck-type compatibility
-----------------------
``QMeClient`` satisfies the interface expected by ``FetchStep``:

    client.get_survey_definition(survey_id)
    client.get_survey_rows(survey_id, date_from, date_to,
                           offset, limit, format, profile_status)
    client.prepare_survey_data_file(survey_id, format, force_refresh)
    client.get_survey_data_file_status(job_id)
    client.read_survey_data_file(job_id, file, offset, limit)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


class QMeClient:
    """QMe MCP client using the official MCP SDK (streamable HTTP transport).

    Parameters
    ----------
    url:
        Base URL of the QMe MCP endpoint, e.g.
        ``"https://retail.qand.me/api/mcp"``.
    access_token:
        Bearer token for the current session.  The caller is responsible for
        providing a valid token and handling expiry / re-login.
    """

    def __init__(self, url: str, access_token: str) -> None:
        self.url   = url
        self.token = access_token

    # ── low-level transport ───────────────────────────────────────────────────

    def _call(self, tool_name: str, arguments: dict) -> Any:
        return asyncio.run(self._call_async(tool_name, arguments))

    async def _call_async(self, tool_name: str, arguments: dict) -> Any:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        headers = {"Authorization": f"Bearer {self.token}"}
        async with streamablehttp_client(self.url, headers=headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)

        if result.content and hasattr(result.content[0], "text"):
            try:
                return json.loads(result.content[0].text)
            except Exception:
                return result.content[0].text
        return result

    def list_tools(self) -> list:
        return asyncio.run(self._list_tools_async())

    async def _list_tools_async(self) -> list:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        headers = {"Authorization": f"Bearer {self.token}"}
        async with streamablehttp_client(self.url, headers=headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.list_tools()
                return result.tools

    # ── QMe tools ─────────────────────────────────────────────────────────────

    def get_survey_definition(self, survey_id: int) -> dict:
        return self._call("get_survey_definition", {"survey_id": survey_id})

    def get_survey_rows(self, survey_id: int,
                        date_from: str = "2020-01-01",
                        date_to: str = "2030-12-31",
                        offset: int = 0,
                        limit: int = 200,
                        format: str = "code",
                        profile_status: list | None = None) -> dict:
        args: dict = {
            "survey_id": survey_id,
            "date_from": date_from,
            "date_to":   date_to,
            "format":    format,
            "offset":    offset,
            "limit":     limit,
        }
        if profile_status is not None:
            args["profile_status"] = profile_status
        return self._call("get_survey_rows", args)

    def prepare_survey_data_file(self, survey_id: int,
                                  format: str = "code",
                                  force_refresh: bool = False) -> dict:
        """Trigger async CSV export. Returns ``{job_id, status, …}``."""
        return self._call("prepare_survey_data_file", {
            "survey_id":     survey_id,
            "format":        format,
            "force_refresh": force_refresh,
        })

    def get_survey_data_file_status(self, job_id: int) -> dict:
        """Poll export job. Returns ``{status, retry_after_seconds, …}``."""
        return self._call("get_survey_data_file_status", {"job_id": job_id})

    def read_survey_data_file(self, job_id: int,
                               file: str = "data",
                               offset: int = 0,
                               limit: int = 500) -> dict:
        """Read one chunk of the prepared CSV. Returns ``{rows/csv, pagination}``."""
        return self._call("read_survey_data_file", {
            "job_id": job_id,
            "file":   file,
            "offset": offset,
            "limit":  limit,
        })
