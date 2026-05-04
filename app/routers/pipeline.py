"""Ingestion, datatable CRUD, run, and download endpoints."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response

from config import settings
from services import pipeline_svc

router = APIRouter(prefix="/api/surveys", tags=["pipeline"])


def _mcp_dir(survey_id: int) -> Path:
    return settings.survey_dir(survey_id) / "mcp"


def _data_dir(survey_id: int) -> Path:
    return settings.survey_dir(survey_id) / "data"


def _dt_dir(survey_id: int) -> Path:
    return settings.survey_dir(survey_id) / "datatable"


# ── ingestion ─────────────────────────────────────────────────────────────────

@router.post("/{survey_id}/ingest")
async def ingest_survey(survey_id: int):
    mcp_dir = _mcp_dir(survey_id)
    def_path  = mcp_dir / "definition.json"
    rows_path = mcp_dir / "rows_pages.json"

    if not def_path.exists() or not rows_path.exists():
        raise HTTPException(400, "Run /fetch first — MCP data not found")

    definition = json.loads(def_path.read_text("utf-8"))
    rows_pages = json.loads(rows_path.read_text("utf-8"))

    try:
        result = pipeline_svc.ingest(settings.survey_dir(survey_id), definition, rows_pages)
    except Exception as exc:
        raise HTTPException(500, str(exc))

    return result


# ── metadata + rawdata ────────────────────────────────────────────────────────

@router.get("/{survey_id}/metadata")
async def get_metadata(survey_id: int):
    path = _data_dir(survey_id) / "metadata.json"
    if not path.exists():
        raise HTTPException(404, "Run /ingest first")
    return json.loads(path.read_text("utf-8"))


@router.get("/{survey_id}/rawdata")
async def get_rawdata(survey_id: int):
    path = _data_dir(survey_id) / "rawdata.csv"
    if not path.exists():
        raise HTTPException(404, "Run /ingest first")
    return PlainTextResponse(path.read_text("utf-8"), media_type="text/csv")


# ── datatable CRUD ────────────────────────────────────────────────────────────

@router.get("/{survey_id}/datatable")
async def get_datatable(survey_id: int):
    path = _dt_dir(survey_id) / "datatable.json"
    if not path.exists():
        raise HTTPException(404, "No datatable.json yet")
    return json.loads(path.read_text("utf-8"))


@router.post("/{survey_id}/datatable")
async def save_datatable(survey_id: int, request: Request):
    body = await request.json()
    dt_dir = _dt_dir(survey_id)
    dt_dir.mkdir(parents=True, exist_ok=True)
    path = dt_dir / "datatable.json"
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "saved"}


# ── run pipeline (table step) ─────────────────────────────────────────────────

@router.post("/{survey_id}/run")
async def run_pipeline(survey_id: int, version: str | None = None):
    survey_dir = settings.survey_dir(survey_id)
    if not (survey_dir / "data" / "rawdata.csv").exists():
        raise HTTPException(400, "Run /ingest first — rawdata.csv not found")

    try:
        result = pipeline_svc.run_table(survey_dir, version)
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))

    return result


# ── download xlsx ─────────────────────────────────────────────────────────────

@router.get("/{survey_id}/download/{version}")
async def download_xlsx(survey_id: int, version: str):
    path = settings.survey_dir(survey_id) / version / "datatable.xlsx"
    if not path.exists():
        raise HTTPException(404, f"datatable.xlsx not found for {version}")
    return FileResponse(
        str(path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"datatable_{survey_id}_{version}.xlsx",
    )
