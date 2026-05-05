"""HTTP client for the QMe MCP server (JSON-RPC 2.0)."""
from __future__ import annotations

import json
import itertools
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)
_id_counter = itertools.count(1)


class MCPError(Exception):
    pass


class MCPClient:
    def __init__(self, url: str) -> None:
        self.url = url

    async def call_tool(self, name: str, arguments: dict) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
            "id": next(_id_counter),
        }
        logger.debug("MCP call: %s %s", name, arguments)
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                self.url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            resp.raise_for_status()

        ct = resp.headers.get("content-type", "")
        if "text/event-stream" in ct:
            result = self._parse_sse(resp.text)
        else:
            body = resp.json()
            if "error" in body:
                raise MCPError(f"MCP error: {body['error']}")
            result = body.get("result", {})

        return self._extract(result)

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_sse(text: str) -> dict:
        for line in text.splitlines():
            if line.startswith("data: "):
                try:
                    data = json.loads(line[6:])
                    if "result" in data:
                        return data["result"]
                except json.JSONDecodeError:
                    pass
        return {}

    @staticmethod
    def _extract(result: dict) -> Any:
        """Unwrap MCP content envelope → parsed dict/list/str."""
        content = result.get("content") if isinstance(result, dict) else None
        if content and isinstance(content, list) and content[0].get("type") == "text":
            text = content[0]["text"]
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return text
        return result

    # ── high-level helpers ────────────────────────────────────────────────────

    async def search_surveys(self, query: str = "", limit: int = 50) -> dict:
        return await self.call_tool(
            "search_surveys", {"query": query, "limit": limit}
        )

    async def get_survey_definition(self, survey_id: int) -> dict:
        return await self.call_tool(
            "get_survey_definition", {"survey_id": survey_id}
        )

    async def get_all_rows(self, survey_id: int, page_limit: int = 200) -> list[dict]:
        """Fetch all pages of rows (format=code) and return them as a list of page dicts."""
        pages: list[dict] = []
        offset = 0
        while True:
            page = await self.call_tool(
                "get_survey_rows",
                {
                    "survey_id": survey_id,
                    "format": "code",
                    "limit": page_limit,
                    "offset": offset,
                },
            )
            pages.append(page)
            rows = page.get("rows", []) if isinstance(page, dict) else []
            logger.info(
                "  rows page offset=%d → %d rows", offset, len(rows)
            )
            if len(rows) < page_limit:
                break
            offset += page_limit
        return pages
