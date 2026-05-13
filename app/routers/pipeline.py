"""Ingestion, datatable CRUD, run, and download endpoints."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

from dependencies import require_qme
from services import pipeline_svc
from services.mcp_client import get_mcp_client, get_storage as get_token_storage, MCPError
from services.storage import get_storage

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/surveys", tags=["pipeline"])

_VERSION_RE = re.compile(r"^v\d+$|^tmp$")


# ── Disk-based refresh job status (shared across all Gunicorn workers) ─────────

def _refresh_status_file(survey_id: int):
    import os
    from pathlib import Path
    return Path(os.getenv("DATA_DIR", "data")) / f".refresh_{survey_id}.json"


def _write_refresh_status(survey_id: int, data: dict) -> None:
    import json as _json
    f = _refresh_status_file(survey_id)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(_json.dumps(data))


def _read_refresh_status(survey_id: int) -> dict:
    import json as _json
    try:
        return _json.loads(_refresh_status_file(survey_id).read_text())
    except Exception:
        return {"status": "idle"}


def _validate_version(version: str) -> str:
    if not _VERSION_RE.match(version):
        raise HTTPException(400, "Invalid version format")
    return version


# ── refresh (fetch + ingest as background job) ────────────────────────────────

@router.post("/{survey_id}/refresh")
async def refresh_survey(survey_id: int, background_tasks: BackgroundTasks,
                         session_id: str = Depends(require_qme)):
    """Start refresh as a background job — returns immediately to avoid proxy timeout."""
    # Guard: reject if a job is already running for this survey
    if _read_refresh_status(survey_id).get("status") == "running":
        return {"status": "running", "detail": "Refresh already in progress"}
    _write_refresh_status(survey_id, {
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
    })
    background_tasks.add_task(_do_refresh, survey_id, session_id)
    return {"status": "started"}


@router.get("/{survey_id}/refresh/status")
async def refresh_status(survey_id: int):
    """Poll this endpoint after POST /refresh to check job progress."""
    return _read_refresh_status(survey_id)


async def _do_refresh(survey_id: int, session_id: str) -> None:
    mcp = get_mcp_client(session_id)
    try:
        definition = await mcp.get_survey_definition(survey_id)
        rows_pages = await mcp.get_all_rows(survey_id)
    except MCPError as exc:
        log.warning("refresh MCP failed for survey %s: %s", survey_id, exc)
        _write_refresh_status(survey_id, {"status": "error", "detail": str(exc)})
        return

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: pipeline_svc.refresh(survey_id, definition, rows_pages)
        )
        _upsert_info(survey_id, session_id)
        _write_refresh_status(survey_id, {"status": "done", **result})
        log.info("refresh done for survey %s — %s rows", survey_id, result.get("n_rows"))
    except Exception as exc:
        log.exception("refresh pipeline failed for survey %s", survey_id)
        _write_refresh_status(survey_id, {"status": "error", "detail": "Pipeline error — check server logs"})


def _upsert_info(survey_id: int, session_id: str) -> None:
    """Write/update {survey_id}/info.json with creator and last-refresh metadata."""
    storage = get_storage()
    email = get_token_storage(session_id).email or "unknown"
    now = datetime.now(timezone.utc).isoformat()
    key = f"{survey_id}/info.json"
    try:
        info = storage.read_json(key)
    except Exception:
        info = {}
    if not info.get("created_by"):
        info["created_by"] = email
        info["created_at"] = now
    info["refreshed_by"] = email
    info["refreshed_at"] = now
    storage.write_json(key, info)


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
        log.exception("generate_xlsx failed for survey %s", survey_id)
        raise HTTPException(500, "Pipeline error — check server logs")

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
        log.exception("ingest failed for survey %s", survey_id)
        raise HTTPException(500, "Pipeline error — check server logs")


# ── metadata + rawdata ────────────────────────────────────────────────────────

_NO_CACHE = {"Cache-Control": "no-store"}


@router.get("/{survey_id}/metadata")
async def get_metadata(survey_id: int):
    storage = get_storage()
    key = f"{survey_id}/data/metadata.json"
    if not storage.exists(key):
        raise HTTPException(404, "Run /ingest first")
    import json as _json
    return Response(
        content=_json.dumps(storage.read_json(key), ensure_ascii=False),
        media_type="application/json",
        headers=_NO_CACHE,
    )


@router.get("/{survey_id}/rawdata")
async def get_rawdata(survey_id: int, request: Request,
                      session_id: str = Depends(require_qme)):
    storage = get_storage()
    key = f"{survey_id}/data/rawdata.csv"
    if not storage.exists(key):
        raise HTTPException(404, "Run /ingest first")

    # Audit log — who accessed raw data and from where
    email = get_token_storage(session_id).email or "unknown"
    ip    = request.client.host if request.client else "unknown"
    log.warning("AUDIT rawdata access: survey=%s user=%s ip=%s", survey_id, email, ip)

    # Strip PII columns (user-name, user-phone) before sending to browser
    csv_text = storage.read_text(key)
    try:
        meta = storage.read_json(f"{survey_id}/data/metadata.json")
        if isinstance(meta, list):
            csv_text = pipeline_svc.strip_pii_columns(csv_text, meta)
    except Exception:
        pass  # metadata unavailable — serve without stripping (still no-cache)

    return PlainTextResponse(csv_text, media_type="text/csv", headers=_NO_CACHE)


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

    if version is not None:
        version = _validate_version(version)
    statuses = [s.strip() for s in profile_status.split(",") if s.strip()]
    try:
        return pipeline_svc.run_table(survey_id, version, profile_status=statuses)
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        log.exception("run_table failed for survey %s", survey_id)
        raise HTTPException(500, "Pipeline error — check server logs")


# ── download xlsx ─────────────────────────────────────────────────────────────

@router.get("/{survey_id}/download/{version}")
async def download_xlsx(survey_id: int, version: str = "v1"):
    version = _validate_version(version)
    storage = get_storage()
    key = f"{survey_id}/{version}/datatable.xlsx"
    if not storage.exists(key):
        raise HTTPException(404, f"datatable.xlsx not found for {version}")
    return Response(
        content=storage.read_bytes(key),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="datatable_{survey_id}_{version}.xlsx"'},
    )
