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

    @staticmethod
    async def _tool(session: Any, tool_name: str, arguments: dict) -> Any:
        """Call *tool_name* on an already-initialised *session* and parse the result."""
        result = await session.call_tool(tool_name, arguments)
        if result.content and hasattr(result.content[0], "text"):
            try:
                return json.loads(result.content[0].text)
            except Exception:
                return result.content[0].text
        return result

    # ── single-session export (prepare + poll + read all chunks) ─────────────

    def export_csv(
        self,
        survey_id: int,
        force_refresh: bool = True,
        chunk_limit: int = 500,
    ) -> str:
        """Prepare + poll + read all chunks in **one** MCP session.

        The legacy approach opens a new ``ClientSession`` (and calls
        ``session.initialize()``) for every individual tool call — prepare,
        each poll, each chunk read.  For large surveys that take tens of
        poll cycles and dozens of chunk reads this accumulates to hundreds of
        seconds of overhead and triggers the 600 s stale-connection guard.

        This method keeps the transport open for the entire export workflow,
        eliminating the per-call ``initialize()`` cost.

        Returns the assembled CSV text (UTF-8, no BOM).
        """
        return asyncio.run(
            self._export_csv_async(survey_id, force_refresh, chunk_limit)
        )

    async def _export_csv_async(
        self,
        survey_id: int,
        force_refresh: bool = True,
        chunk_limit: int = 500,
        max_poll: int = 120,
        max_sleep: int = 60,
    ) -> str:
        import asyncio as _aio
        import csv as _csv
        import io as _io
        import logging as _log
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        _logger = _log.getLogger(__name__)
        headers = {"Authorization": f"Bearer {self.token}"}

        async with streamablehttp_client(self.url, headers=headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()

                # ── 1. trigger export job ─────────────────────────────────────
                prep = await self._tool(session, "prepare_survey_data_file", {
                    "survey_id": survey_id,
                    "format": "code",
                    "force_refresh": force_refresh,
                })
                job_id = prep.get("job_id")
                if not job_id:
                    raise RuntimeError(
                        f"prepare_survey_data_file returned no job_id: {prep}"
                    )
                _logger.info("[export_csv] job_id=%s  status=%s", job_id, prep.get("status"))

                # ── 2. poll until ready ───────────────────────────────────────
                for _ in range(max_poll):
                    status_resp = await self._tool(
                        session, "get_survey_data_file_status", {"job_id": job_id}
                    )
                    status = status_resp.get("status", "")
                    _logger.info("[export_csv] poll status=%s", status)
                    if status == "ready":
                        break
                    if status == "error":
                        raise RuntimeError(f"Export job failed: {status_resp}")
                    wait = min(int(status_resp.get("retry_after_seconds", 5)), max_sleep)
                    await _aio.sleep(wait)
                else:
                    raise RuntimeError(
                        f"Export job did not complete after {max_poll} polls"
                    )

                # ── 3. read all chunks ────────────────────────────────────────
                parts: list[str] = []
                offset = 0
                for chunk_num in range(1, 2001):
                    chunk = await self._tool(session, "read_survey_data_file", {
                        "job_id": job_id,
                        "file":   "data",
                        "offset": offset,
                        "limit":  chunk_limit,
                    })

                    if isinstance(chunk, str):
                        parts.append(chunk)
                        break

                    if isinstance(chunk, dict):
                        csv_text = chunk.get("csv") or chunk.get("content") or ""
                        rows_val = chunk.get("rows", [])

                        if csv_text:
                            parts.append(
                                csv_text if csv_text.endswith("\n") else csv_text + "\n"
                            )
                        elif rows_val:
                            if isinstance(rows_val[0], str):
                                parts.append("\n".join(rows_val) + "\n")
                            elif isinstance(rows_val[0], (list, dict)):
                                buf = _io.StringIO()
                                if isinstance(rows_val[0], dict):
                                    writer = _csv.DictWriter(
                                        buf, fieldnames=list(rows_val[0].keys())
                                    )
                                    if offset == 0:
                                        writer.writeheader()
                                    writer.writerows(rows_val)
                                else:
                                    writer = _csv.writer(buf)
                                    writer.writerows(rows_val)
                                parts.append(buf.getvalue())

                        pagination     = chunk.get("pagination", {})
                        rows_remaining = pagination.get("rows_remaining", 0)
                        has_more       = pagination.get("has_more", False)
                        next_offset    = pagination.get("next_offset", offset + chunk_limit)

                        _logger.info(
                            "[export_csv] chunk %d: offset=%d  remaining=%d",
                            chunk_num, offset, rows_remaining,
                        )

                        if not has_more:
                            break
                        offset = next_offset
                    else:
                        break

        return "".join(parts)

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
