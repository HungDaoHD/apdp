"""Survey discovery and data-fetching endpoints."""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException

from services.mcp_client import get_mcp_client, MCPError
from services.storage import get_storage

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
        # Code field (not in metadata.json — try definition.json survey object)
        try:
            if not code:
                defn = storage.read_json(f"{sid}/mcp/definition.json")
                sv = defn.get("survey") or {}
                code = (
                    sv.get("code") or sv.get("survey_code") or sv.get("project_code")
                    or sv.get("external_id") or sv.get("reference_code") or sv.get("project_no")
                )
        except Exception:
            pass
        projects.append({
            "survey_id":     sid,
            "code":          code,
            "title":         title or f"Survey {sid}",
            "has_data":      storage.exists(f"{sid}/data/rawdata.csv"),
            "has_datatable": storage.exists(f"{sid}/datatable/datatable.json"),
        })
    return projects


# ── search ────────────────────────────────────────────────────────────────────

@router.get("/search")
async def search_surveys(q: str = "", limit: int = 50):
    try:
        return await get_mcp_client().search_surveys(query=q, limit=limit)
    except MCPError as exc:
        raise HTTPException(502, str(exc))


# ── fetch from QMe ────────────────────────────────────────────────────────────

@router.post("/{survey_id}/fetch")
async def fetch_survey(survey_id: int):
    """Download definition + all rows pages from QMe MCP and save to storage."""
    mcp = get_mcp_client()
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
    return {
        "has_mcp":       storage.exists(f"{survey_id}/mcp/definition.json"),
        "has_data":      storage.exists(f"{survey_id}/data/rawdata.csv"),
        "has_datatable": storage.exists(f"{survey_id}/datatable/datatable.json"),
        "versions":      versions,
    }
