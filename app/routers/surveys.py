"""Survey discovery and data-fetching endpoints."""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException

from dependencies import require_qme
from services.mcp_client import get_mcp_client, get_storage as get_token_storage, MCPError
from services.storage import get_storage

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/surveys", tags=["surveys"])


# ── projects (stored surveys) ─────────────────────────────────────────────────

@router.get("/projects")
async def list_projects():
    """Return all survey IDs in storage with title, code, and status."""
    storage = get_storage()
    ids = storage.list_survey_ids()
    projects = []
    for sid in ids:
        code = None
        title = None
        # Primary source: metadata.json (top-level fields: title, english_title, survey_id, …)
        try:
            meta = storage.read_json(f"{sid}/data/metadata.json")
            title = meta.get("title") or meta.get("english_title")
        except Exception:
            pass
        # Fallback: definition.json  { "survey": { "title": "…" }, "questions": […] }
        if not title:
            try:
                defn = storage.read_json(f"{sid}/mcp/definition.json")
                sv = defn.get("survey") or {}
                title = (
                    sv.get("title") or sv.get("english_title")
                    or defn.get("title") or defn.get("name")
                )
            except Exception:
                pass
        # Code + status field (not in metadata.json — try definition.json survey object)
        status = None
        try:
            defn = storage.read_json(f"{sid}/mcp/definition.json")
            sv = defn.get("survey") or {}
            if not code:
                code = (
                    sv.get("code") or sv.get("survey_code") or sv.get("project_code")
                    or sv.get("external_id") or sv.get("reference_code") or sv.get("project_no")
                )
            status = sv.get("status")
        except Exception:
            pass
        info = {}
        try:
            info = storage.read_json(f"{sid}/info.json")
        except Exception:
            pass
        projects.append({
            "survey_id":     sid,
            "code":          code,
            "title":         title or f"Survey {sid}",
            "status":        status,
            "has_data":      storage.exists(f"{sid}/data/rawdata.csv"),
            "has_datatable": storage.exists(f"{sid}/datatable/datatable.json"),
            "created_by":    info.get("created_by"),
            "created_at":    info.get("created_at"),
            "refreshed_by":  info.get("refreshed_by"),
            "refreshed_at":  info.get("refreshed_at"),
        })
    return projects


# ── debug: list MCP tools ─────────────────────────────────────────────────────

@router.get("/debug/tools")
async def list_mcp_tools(session_id: str = Depends(require_qme)):
    """List all tools available on the QMe MCP server with their schemas."""
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    from config import settings

    storage = get_token_storage(session_id)
    tokens = await storage.get_tokens()
    if not tokens or not tokens.access_token:
        raise HTTPException(401, "Not connected to QMe")

    headers = {"Authorization": f"Bearer {tokens.access_token}"}
    try:
        async with streamablehttp_client(settings.QME_MCP_BASE_URL, headers=headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                tools_result = await session.list_tools()
    except* httpx.HTTPStatusError as eg:
        raise HTTPException(502, f"MCP error: {eg.exceptions[0].response.status_code}")
    except* Exception as eg:
        raise HTTPException(502, str(eg.exceptions[0]))

    return [
        {
            "name": t.name,
            "description": t.description,
            "inputSchema": t.inputSchema,
        }
        for t in tools_result.tools
    ]


# ── search ────────────────────────────────────────────────────────────────────

@router.get("/search")
async def search_surveys(q: str = "", limit: int = 50, session_id: str = Depends(require_qme)):
    try:
        return await get_mcp_client(session_id).search_surveys(query=q, limit=limit)
    except MCPError as exc:
        log.warning("search_surveys failed: %s", exc)
        raise HTTPException(502, "Search unavailable — check server logs")


# ── fetch from QMe ────────────────────────────────────────────────────────────

@router.post("/{survey_id}/fetch")
async def fetch_survey(survey_id: int, session_id: str = Depends(require_qme)):
    """Download definition + all rows pages from QMe MCP and save to storage."""
    mcp = get_mcp_client(session_id)
    try:
        definition = await mcp.get_survey_definition(survey_id)
        rows_pages = await mcp.get_all_rows(survey_id)
    except MCPError as exc:
        raise HTTPException(502, str(exc))

    storage = get_storage()
    storage.write_json(f"{survey_id}/mcp/definition.json", definition)
    storage.write_json(f"{survey_id}/mcp/rows_pages.json", rows_pages)

    total_rows = sum(len(p.get("rows", [])) for p in rows_pages if isinstance(p, dict))
    return {"status": "ok", "pages": len(rows_pages), "total_rows": total_rows}


# ── status ────────────────────────────────────────────────────────────────────

@router.get("/{survey_id}/status")
async def survey_status(survey_id: int):
    storage = get_storage()
    keys = storage.list_prefix(f"{survey_id}/")
    versions = sorted({
        m.group(1)
        for k in keys
        if (m := re.search(r"/(v\d+)/", k))
    })
    has_data = storage.exists(f"{survey_id}/data/rawdata.csv")
    stats: dict = {}
    if has_data:
        try:
            stats = storage.read_json(f"{survey_id}/data/stats.json")
        except Exception:
            pass
    return {
        "has_mcp":       storage.exists(f"{survey_id}/mcp/definition.json"),
        "has_data":      has_data,
        "has_datatable": storage.exists(f"{survey_id}/datatable/datatable.json"),
        "versions":      versions,
        "n_rows":        stats.get("n_rows", 0),
        "n_approved":    stats.get("n_approved", 0),
        "n_pending":     stats.get("n_pending", 0),
    }
