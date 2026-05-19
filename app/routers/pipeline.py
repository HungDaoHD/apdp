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
from services.mcp_client import get_storage as get_token_storage, get_access_token
from services.storage import get_storage

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/surveys", tags=["pipeline"])

_VERSION_RE = re.compile(r"^v\d+$|^tmp$")


# ── Disk-based refresh job status (shared across all Gunicorn workers) ─────────

def _refresh_status_file(survey_id: int):
    import os
    from pathlib import Path
    return Path(os.getenv("DATA_DIR", "data")) / f".refresh_{survey_id}.json"


_REFRESH_TIMEOUT_SECS = 600   # 10 minutes — treat stale "running" as error


def _write_refresh_status(survey_id: int, data: dict) -> None:
    import json as _json
    try:
        f = _refresh_status_file(survey_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(_json.dumps(data))
    except Exception as exc:
        log.warning("_write_refresh_status failed for survey %s: %s", survey_id, exc)


def _read_refresh_status(survey_id: int) -> dict:
    import json as _json
    try:
        data = _json.loads(_refresh_status_file(survey_id).read_text())
    except Exception:
        return {"status": "idle"}
    # Stale-running guard: if worker was killed, status stays "running" forever.
    # Treat as error after REFRESH_TIMEOUT_SECS.
    if data.get("status") == "running":
        started_at = data.get("started_at")
        if started_at:
            try:
                from datetime import timezone as _tz
                age = (datetime.now(_tz.utc) - datetime.fromisoformat(started_at)).total_seconds()
                if age > _REFRESH_TIMEOUT_SECS:
                    return {"status": "error", "detail": f"Refresh timed out after {int(age)}s — worker may have been recycled. Please try again."}
            except Exception:
                pass
    return data


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


@router.post("/{survey_id}/refresh/csv")
async def refresh_survey_csv(survey_id: int, session_id: str = Depends(require_qme)):
    """Fetch export CSV via QMeClient/FetchStep and run ingestion — waits synchronously."""
    from surveyflow import QMeClient
    from config import settings

    access_token = get_access_token(session_id)
    if not access_token:
        raise HTTPException(401, "Not connected to QMe — please reconnect.")

    client = QMeClient(settings.QME_MCP_BASE_URL, access_token)
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: pipeline_svc.refresh_csv(survey_id, client)
        )
        _upsert_info(survey_id, session_id)
        log.info("refresh_csv done for survey %s — %s rows", survey_id, result.get("n_rows"))
        return {"status": "done", **result}
    except Exception as exc:
        log.exception("refresh_csv pipeline failed for survey %s", survey_id)
        raise HTTPException(500, "Pipeline error — check server logs")


@router.get("/{survey_id}/refresh/status")
async def refresh_status(survey_id: int):
    """Poll this endpoint after POST /refresh to check job progress."""
    return _read_refresh_status(survey_id)


@router.post("/{survey_id}/refresh/cancel")
async def refresh_cancel(survey_id: int):
    """Force-reset a stuck refresh job (clears the status file)."""
    _write_refresh_status(survey_id, {"status": "cancelled"})
    return {"status": "cancelled"}


async def _do_refresh(survey_id: int, session_id: str) -> None:
    from surveyflow import QMeClient
    from config import settings

    log.info("[refresh:%s] background task started", survey_id)

    access_token = get_access_token(session_id)
    if not access_token:
        log.warning("[refresh:%s] no access token — aborting", survey_id)
        _write_refresh_status(survey_id, {"status": "error", "detail": "Not connected to QMe"})
        return

    log.info("[refresh:%s] token OK — creating QMeClient (url=%s)", survey_id, settings.QME_MCP_BASE_URL)
    client = QMeClient(settings.QME_MCP_BASE_URL, access_token)
    try:
        log.info("[refresh:%s] submitting pipeline_svc.refresh to executor", survey_id)
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: pipeline_svc.refresh(survey_id, client)
        )
        log.info("[refresh:%s] executor finished — n_rows=%s", survey_id, result.get("n_rows"))
        _upsert_info(survey_id, session_id)
        _write_refresh_status(survey_id, {"status": "done", **result})
        log.info("[refresh:%s] status=done written", survey_id)
    except Exception as exc:
        log.exception("[refresh:%s] pipeline failed: %s", survey_id, exc)
        _write_refresh_status(survey_id, {"status": "error", "detail": str(exc)})


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
async def generate_xlsx(survey_id: int, request: Request,
                        profile_status: str = "approved,pending"):
    """Render datatable.xlsx — reuses cached compute from /preview when available.

    Body (optional): { "datatable_config": [...] }
    profile_status query param: comma-separated, e.g. "approved" or "approved,pending"
    """
    storage = get_storage()
    if not storage.exists(f"{survey_id}/data/rawdata.csv"):
        raise HTTPException(400, "Run Refresh first — rawdata.csv not found")

    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass

    dt_config = body.get("datatable_config")   # None → use stored / cache

    if dt_config is None and not storage.exists(f"{survey_id}/datatable/datatable.json"):
        raise HTTPException(400, "No datatable.json found")

    statuses = [s.strip() for s in profile_status.split(",") if s.strip()]
    try:
        loop = asyncio.get_running_loop()
        xlsx_bytes = await loop.run_in_executor(
            None,
            lambda: pipeline_svc.generate_xlsx(survey_id, dt_config=dt_config,
                                                profile_status=statuses),
        )
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


# ── preview (server-side cross-tab, no xlsx) ─────────────────────────────────

@router.post("/{survey_id}/preview")
async def preview_table(survey_id: int, request: Request,
                        profile_status: str = "approved,pending"):
    """Compute cross-tab server-side → returns JSON table_results.

    Body (all optional):
      datatable_config: list[dict]  — override stored datatable.json
      table_indices:    list[int]   — compute only these tables (None = all)

    profile_status query param: comma-separated, e.g. "approved" or "approved,pending"
    """
    storage = get_storage()
    if not storage.exists(f"{survey_id}/data/rawdata.csv"):
        raise HTTPException(400, "Run Refresh first — rawdata.csv not found")

    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass

    dt_config     = body.get("datatable_config")   # None → use stored
    table_indices = body.get("table_indices")       # None → all

    if dt_config is None and not storage.exists(f"{survey_id}/datatable/datatable.json"):
        raise HTTPException(400, "No datatable.json — provide datatable_config in body")

    statuses = [s.strip() for s in profile_status.split(",") if s.strip()]
    try:
        loop = asyncio.get_running_loop()
        table_results = await loop.run_in_executor(
            None,
            lambda: pipeline_svc.compute_preview(survey_id, dt_config, table_indices, statuses),
        )
        return {"table_results": table_results}
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        log.exception("preview failed for survey %s", survey_id)
        raise HTTPException(500, "Pipeline error — check server logs")


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
