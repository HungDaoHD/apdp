"""Ingestion, datatable CRUD, run, and download endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

from services import pipeline_svc
from services.mcp_client import get_mcp_client, MCPError
from services.storage import get_storage

router = APIRouter(prefix="/api/surveys", tags=["pipeline"])


# ── refresh (fetch + ingest combined) ────────────────────────────────────────

@router.post("/{survey_id}/refresh")
async def refresh_survey(survey_id: int):
    """Fetch fresh data from QMe then run ingestion — replaces separate fetch+ingest."""
    mcp = get_mcp_client()
    try:
        definition = await mcp.get_survey_definition(survey_id)
        rows_pages = await mcp.get_all_rows(survey_id)
    except MCPError as exc:
        raise HTTPException(502, str(exc))

    try:
        result = pipeline_svc.refresh(survey_id, definition, rows_pages)
        return {"status": "ok", **result}
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ── generate xlsx (no storage write) ─────────────────────────────────────────

@router.post("/{survey_id}/generate")
async def generate_xlsx(survey_id: int, profile_status: str = "approved"):
    """Run table step and return xlsx bytes directly — nothing written to storage.

    profile_status: comma-separated list, e.g. "approved" or "approved,pending"
    """
    storage = get_storage()
    if not storage.exists(f"{survey_id}/data/rawdata.csv"):
        raise HTTPException(400, "Run Refresh first — rawdata.csv not found")
    if not storage.exists(f"{survey_id}/datatable/datatable.json"):
        raise HTTPException(400, "No datatable.json found")

    statuses = [s.strip() for s in profile_status.split(",") if s.strip()]
    try:
        xlsx_bytes = pipeline_svc.generate_xlsx(survey_id, profile_status=statuses)
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))

    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="datatable_{survey_id}.xlsx"'},
    )


# ── ingestion ─────────────────────────────────────────────────────────────────

@router.post("/{survey_id}/ingest")
async def ingest_survey(survey_id: int):
    storage = get_storage()
    if not storage.exists(f"{survey_id}/mcp/definition.json") or \
       not storage.exists(f"{survey_id}/mcp/rows_pages.json"):
        raise HTTPException(400, "Run /fetch first — MCP data not found")

    definition = storage.read_json(f"{survey_id}/mcp/definition.json")
    rows_pages  = storage.read_json(f"{survey_id}/mcp/rows_pages.json")

    try:
        return pipeline_svc.ingest(survey_id, definition, rows_pages)
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ── metadata + rawdata ────────────────────────────────────────────────────────

@router.get("/{survey_id}/metadata")
async def get_metadata(survey_id: int):
    storage = get_storage()
    key = f"{survey_id}/data/metadata.json"
    if not storage.exists(key):
        raise HTTPException(404, "Run /ingest first")
    return storage.read_json(key)


@router.get("/{survey_id}/rawdata")
async def get_rawdata(survey_id: int):
    storage = get_storage()
    key = f"{survey_id}/data/rawdata.csv"
    if not storage.exists(key):
        raise HTTPException(404, "Run /ingest first")
    return PlainTextResponse(storage.read_text(key), media_type="text/csv")


# ── datatable CRUD ────────────────────────────────────────────────────────────

@router.get("/{survey_id}/datatable")
async def get_datatable(survey_id: int):
    storage = get_storage()
    key = f"{survey_id}/datatable/datatable.json"
    if not storage.exists(key):
        raise HTTPException(404, "No datatable.json yet")
    return storage.read_json(key)


@router.post("/{survey_id}/datatable")
async def save_datatable(survey_id: int, request: Request):
    body = await request.json()
    get_storage().write_json(f"{survey_id}/datatable/datatable.json", body)
    return {"status": "saved"}


# ── run pipeline (table step) ─────────────────────────────────────────────────

@router.post("/{survey_id}/run")
async def run_pipeline(survey_id: int, version: str | None = None,
                       profile_status: str = "approved"):
    """Run table step and save versioned datatable.xlsx.

    profile_status: comma-separated list, e.g. "approved" or "approved,pending"
    """
    storage = get_storage()
    if not storage.exists(f"{survey_id}/data/rawdata.csv"):
        raise HTTPException(400, "Run /ingest first — rawdata.csv not found")
    if not storage.exists(f"{survey_id}/datatable/datatable.json"):
        raise HTTPException(400, "No datatable.json found")

    statuses = [s.strip() for s in profile_status.split(",") if s.strip()]
    try:
        return pipeline_svc.run_table(survey_id, version, profile_status=statuses)
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ── download xlsx ─────────────────────────────────────────────────────────────

@router.get("/{survey_id}/download/{version}")
async def download_xlsx(survey_id: int, version: str):
    storage = get_storage()
    key = f"{survey_id}/{version}/datatable.xlsx"
    if not storage.exists(key):
        raise HTTPException(404, f"datatable.xlsx not found for {version}")
    return Response(
        content=storage.read_bytes(key),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="datatable_{survey_id}_{version}.xlsx"'},
    )
