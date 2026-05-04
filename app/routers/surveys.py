"""Survey discovery and data-fetching endpoints."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

from config import settings
from services.mcp_client import MCPClient, MCPError

router = APIRouter(prefix="/api/surveys", tags=["surveys"])


def _mcp() -> MCPClient:
    return MCPClient(settings.QME_MCP_URL)


# ── search ────────────────────────────────────────────────────────────────────

@router.get("/search")
async def search_surveys(q: str = "", limit: int = 50):
    try:
        result = await _mcp().search_surveys(query=q, limit=limit)
        return result
    except MCPError as exc:
        raise HTTPException(502, str(exc))


# ── fetch from QMe ────────────────────────────────────────────────────────────

@router.post("/{survey_id}/fetch")
async def fetch_survey(survey_id: int):
    """Download definition + all rows pages from QMe MCP and save locally."""
    mcp = _mcp()
    try:
        definition = await mcp.get_survey_definition(survey_id)
        rows_pages = await mcp.get_all_rows(survey_id)
    except MCPError as exc:
        raise HTTPException(502, str(exc))

    mcp_dir = settings.survey_dir(survey_id) / "mcp"
    mcp_dir.mkdir(parents=True, exist_ok=True)
    (mcp_dir / "definition.json").write_text(
        json.dumps(definition, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (mcp_dir / "rows_pages.json").write_text(
        json.dumps(rows_pages, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    total_rows = sum(len(p.get("rows", [])) for p in rows_pages if isinstance(p, dict))
    return {"status": "ok", "pages": len(rows_pages), "total_rows": total_rows}


# ── status ────────────────────────────────────────────────────────────────────

@router.get("/{survey_id}/status")
async def survey_status(survey_id: int):
    d = settings.survey_dir(survey_id)
    return {
        "has_mcp":       (d / "mcp" / "definition.json").exists(),
        "has_data":      (d / "data" / "rawdata.csv").exists(),
        "has_datatable": (d / "datatable" / "datatable.json").exists(),
        "versions": sorted(
            p.name for p in d.iterdir() if p.is_dir() and p.name.startswith("v")
        ) if d.exists() else [],
    }
