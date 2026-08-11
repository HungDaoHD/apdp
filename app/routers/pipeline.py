"""Ingestion, datatable CRUD, run, and download endpoints."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, Response

from config import settings
from dependencies import require_qme
from services import pipeline_svc
from services.authz import is_admin
from services.mcp_client import get_storage as get_token_storage, get_access_token
from services.storage import get_storage

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/surveys", tags=["pipeline"])


def _unwrap_exc(exc: BaseException) -> BaseException:
    """Unwrap ExceptionGroup (Python 3.11+ / anyio TaskGroup) to the first leaf cause."""
    if hasattr(exc, "exceptions") and exc.exceptions:  # type: ignore[attr-defined]
        return _unwrap_exc(exc.exceptions[0])  # type: ignore[attr-defined]
    return exc


def _is_auth_error(exc: BaseException) -> bool:
    """Return True if exc signals a QMe 401/403 auth failure."""
    root = _unwrap_exc(exc)
    msg  = str(root).lower()
    if any(kw in msg for kw in ("401", "403", "unauthorized", "unauthorised",
                                 "token expired", "reconnect")):
        return True
    try:
        import httpx
        if isinstance(root, httpx.HTTPStatusError) and root.response.status_code in (401, 403):
            return True
    except ImportError:
        pass
    return False


def _clear_token(session_id: str) -> None:
    try:
        get_token_storage(session_id).clear()
    except Exception:
        pass

_VERSION_RE = re.compile(r"^v\d+$|^tmp$")

# H3 — profile_status whitelist
_ALLOWED_STATUSES = frozenset({"approved", "pending", "rejected"})


def _parse_profile_status(raw: str) -> list[str]:
    """Parse and whitelist profile_status query param."""
    result = [s.strip().lower() for s in raw.split(",") if s.strip().lower() in _ALLOWED_STATUSES]
    return result if result else ["approved"]


# ── Disk-based refresh job status (shared across all Gunicorn workers) ─────────

def _refresh_status_file(survey_id: int):
    from pathlib import Path
    return Path(settings.DATA_DIR) / f".refresh_{survey_id}.json"


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
                    return {"status": "error", "detail": "Refresh timed out — worker may have been recycled. Please try again."}
            except Exception:
                pass
    # Strip internal fields before returning to client
    return {k: v for k, v in data.items() if k != "started_by_session"}


def _sanitize_error(detail: str) -> str:
    """Return a safe error message for the client (H2).

    Auth errors are kept recognisable so the frontend modal can trigger;
    all other details are replaced with a generic message.
    """
    if any(kw in detail.lower() for kw in ("401", "unauthorized", "unauthorised")):
        return "QMe authentication failed — please reconnect"
    return "Refresh failed — see server logs"


def _validate_version(version: str) -> str:
    if not _VERSION_RE.match(version):
        raise HTTPException(400, "Invalid version format")
    return version


# ── fetch definition only (used when creating a new project) ─────────────────

@router.post("/{survey_id}/fetch-definition")
async def fetch_definition(survey_id: int, session_id: str = Depends(require_qme)):
    """Fetch only the survey definition from QMe and save to mcp/definition.json.
    Creates info.json so the project appears in the projects list immediately.
    Does NOT fetch data — user chooses Refresh or Upload ZIP afterwards.
    """
    access_token = get_access_token(session_id)
    if not access_token:
        raise HTTPException(401, "Not connected to QMe")

    storage = get_storage()
    try:
        from surveyflow import QMeClient
        client  = QMeClient(settings.QME_MCP_BASE_URL, access_token)
        loop    = asyncio.get_running_loop()
        definition = await loop.run_in_executor(
            None, lambda: client._call("get_survey_definition", {"survey_id": survey_id})
        )
    except Exception as exc:
        log.warning("fetch_definition failed for survey %s: %s", survey_id, exc)
        _raise_if_auth(exc, session_id)
        raise HTTPException(502, _sanitize_mcp_error(exc))

    storage.write_json(f"{survey_id}/mcp/definition.json", definition)
    _upsert_info(survey_id, session_id)
    log.info("fetch_definition done: survey=%s", survey_id)
    return {"status": "ok"}


# ── refresh (fetch + ingest as background job) ────────────────────────────────

@router.post("/{survey_id}/refresh")
async def refresh_survey(survey_id: int, background_tasks: BackgroundTasks,
                         session_id: str = Depends(require_qme)):
    """Start refresh as a background job — returns immediately to avoid proxy timeout."""
    # Guard: reject if a job is already running for this survey
    existing = _read_refresh_status(survey_id)
    if existing.get("status") == "running":
        return {"status": "running", "detail": "Refresh already in progress"}
    _write_refresh_status(survey_id, {
        "status":             "running",
        "started_at":         datetime.now(timezone.utc).isoformat(),
        "started_by_session": session_id,   # H1: ownership tracking
    })
    # Create info.json immediately so the project appears in the projects list
    # before the background job completes (important for new surveys)
    _upsert_info(survey_id, session_id)
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
        if _is_auth_error(exc):
            _clear_token(session_id)
            raise HTTPException(401, "QMe authentication failed — please reconnect")
        raise HTTPException(500, "Pipeline error — check server logs")


@router.get("/{survey_id}/refresh/status")
async def refresh_status(survey_id: int, session_id: str = Depends(require_qme)):
    """Poll this endpoint after POST /refresh to check job progress."""
    return _read_refresh_status(survey_id)


@router.post("/{survey_id}/refresh/cancel")
async def refresh_cancel(survey_id: int, session_id: str = Depends(require_qme)):
    """Force-reset a stuck refresh job — only the session that started it may cancel."""
    import json as _json
    try:
        raw = _json.loads(_refresh_status_file(survey_id).read_text())
        owner = raw.get("started_by_session")
        if owner and owner != session_id:
            raise HTTPException(403, "Not authorised to cancel this refresh job")
    except HTTPException:
        raise
    except Exception:
        pass  # file missing or unreadable — allow cancel
    _write_refresh_status(survey_id, {"status": "cancelled"})
    return {"status": "cancelled"}


async def _do_refresh(survey_id: int, session_id: str) -> None:
    from surveyflow import QMeClient
    from config import settings

    log.info("[refresh:%s] background task started", survey_id)

    storage = get_token_storage(session_id)
    access_token = get_access_token(session_id)
    if not access_token:
        log.warning("[refresh:%s] no access token — aborting", survey_id)
        _write_refresh_status(survey_id, {"status": "error", "detail": "Not connected to QMe"})
        return

    log.info("[refresh:%s] token OK — creating QMeClient (url=%s)", survey_id, settings.QME_MCP_BASE_URL)

    started_at = datetime.now(timezone.utc).isoformat()
    _log_entries: list[str] = []

    def _step(msg: str):
        """Update status file with current step + accumulated log (visible in UI polling)."""
        log.info("[refresh:%s] %s", survey_id, msg)
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        _log_entries.append(f"[{ts}] {msg}")
        _write_refresh_status(survey_id, {
            "status":     "running",
            "step":       msg,
            "log":        list(_log_entries),
            "started_at": started_at,
        })

    async def _run(token: str) -> dict:
        c = QMeClient(settings.QME_MCP_BASE_URL, token)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: pipeline_svc.refresh(survey_id, c, _step)
        )

    try:
        _step("Connecting to QMe…")
        result = await _run(access_token)
    except Exception as first_exc:
        if _is_auth_error(first_exc):
            # QMeClient (sync) cannot auto-refresh — do it here then retry once
            log.info("[refresh:%s] 401 mid-task — refreshing token and retrying once", survey_id)
            ok = await storage.refresh()
            new_token = get_access_token(session_id) if ok else None
            if new_token:
                try:
                    _step("Token refreshed — retrying…")
                    result = await _run(new_token)
                except Exception as retry_exc:
                    root = _unwrap_exc(retry_exc)
                    log.exception("[refresh:%s] retry failed: %s", survey_id, root)
                    if _is_auth_error(retry_exc):
                        _clear_token(session_id)
                    _write_refresh_status(survey_id, {"status": "error", "detail": _sanitize_error(str(root)), "log": list(_log_entries)})
                    return
            else:
                _clear_token(session_id)
                _write_refresh_status(survey_id, {"status": "error", "detail": "QMe authentication failed — please reconnect", "log": list(_log_entries)})
                return
        else:
            root = _unwrap_exc(first_exc)
            detail = str(root)
            log.exception("[refresh:%s] pipeline failed: %s", survey_id, detail)
            _write_refresh_status(survey_id, {"status": "error", "detail": _sanitize_error(detail), "log": list(_log_entries)})
            return

    log.info("[refresh:%s] executor finished — n_rows=%s", survey_id, result.get("n_rows"))
    _upsert_info(survey_id, session_id)
    _write_refresh_status(survey_id, {"status": "done", "log": list(_log_entries), **result})
    log.info("[refresh:%s] status=done written", survey_id)


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


# ── ingest from uploaded ZIP (alternative to refresh) ────────────────────────

@router.post("/{survey_id}/ingest-zip")
async def ingest_zip(survey_id: int, file: UploadFile = File(...),
                     session_id: str = Depends(require_qme)):
    """Upload code_retail_report_*.zip, extract CSV, run ingestion.

    Replaces the data-fetch step of /refresh.
    - Extracts the first file matching code_retail_report_*.csv from the zip.
    - If definition.json is not cached, fetches it from QMe automatically.
    - Runs ingestion to produce rawdata.csv + metadata.json.
    """
    import fnmatch, io, pathlib, tempfile, zipfile as _zipfile
    from surveyflow.steps.ingestion.export_parser import parse_export_csv
    from services.upload_limits import read_upload_capped

    storage = get_storage()

    # ── 1. Read and validate the ZIP ─────────────────────────────────────────
    zip_bytes = await read_upload_capped(file)
    try:
        zf = _zipfile.ZipFile(io.BytesIO(zip_bytes))
    except _zipfile.BadZipFile:
        raise HTTPException(400, "Uploaded file is not a valid ZIP archive")

    import csv as _csv, re as _re
    from pathlib import PurePosixPath

    # Match by basename so files in subdirectories inside the ZIP also work
    csv_names = [
        n for n in zf.namelist()
        if fnmatch.fnmatch(PurePosixPath(n).name, "code_retail_report_*.csv")
    ]
    if not csv_names:
        all_files = ", ".join(zf.namelist()) or "(empty)"
        raise HTTPException(
            400,
            f"No code_retail_report_*.csv found inside the ZIP. "
            f"Files found: {all_files[:300]}"
        )

    csv_bytes = zf.read(csv_names[0])
    log.info("ingest_zip: survey=%s extracted %s (%d bytes)", survey_id, csv_names[0], len(csv_bytes))

    # ── 1b. Extract survey code from CSV and validate against definition ──────
    def _extract_csv_code(raw: bytes) -> str | None:
        try:
            text = raw.decode("utf-8-sig", errors="replace")
            rows = list(_csv.reader(text.splitlines()))
            if len(rows) > 1:
                for cell in rows[1]:
                    m = _re.search(r"Data for task:\s*(\S+)", cell, _re.IGNORECASE)
                    if m:
                        return m.group(1).strip()
        except Exception:
            pass
        return None

    csv_code = _extract_csv_code(csv_bytes)
    log.info("ingest_zip: CSV survey code = %s", csv_code)
    if not csv_code:
        raise HTTPException(
            400,
            f"Cannot identify survey code from CSV (expected 'Data for task: CODE ...' in row 2). "
            f"Please verify this is a valid QMe export file."
        )

    # ── 2. Get definition — use cached or auto-fetch from QMe ────────────────
    if storage.exists(f"{survey_id}/mcp/definition.json"):
        definition = storage.read_json(f"{survey_id}/mcp/definition.json")
        log.info("ingest_zip: using cached definition for survey %s", survey_id)
    else:
        log.info("ingest_zip: definition not found for survey %s — fetching from QMe", survey_id)
        access_token = get_access_token(session_id)
        if not access_token:
            raise HTTPException(401, "Not connected to QMe — please reconnect")
        try:
            from surveyflow import QMeClient
            client = QMeClient(settings.QME_MCP_BASE_URL, access_token)
            loop = asyncio.get_running_loop()
            definition = await loop.run_in_executor(
                None, lambda: client._call("get_survey_definition", {"survey_id": survey_id})
            )
            storage.write_json(f"{survey_id}/mcp/definition.json", definition)
            log.info("ingest_zip: definition fetched and saved for survey %s", survey_id)
        except Exception as exc:
            _raise_if_auth(exc, session_id)
            raise HTTPException(502, f"Failed to fetch survey definition: {_sanitize_mcp_error(exc)}")

    # ── 2b. Validate CSV matches definition ──────────────────────────────────
    sv = definition.get("survey") or {}
    def_code = (
        sv.get("code") or sv.get("survey_code") or sv.get("project_code")
        or sv.get("external_id") or sv.get("reference_code") or sv.get("project_no")
        # Fallback: first word of title often contains the survey code (e.g. "VN8982 Survey title")
        or (sv.get("title") or sv.get("english_title") or sv.get("display_title") or "").split()[0]
    )
    log.info("ingest_zip: definition code = '%s' (survey_id=%s)", def_code, survey_id)

    if csv_code and def_code:
        if csv_code.upper() != str(def_code).upper():
            raise HTTPException(
                400,
                f"CSV mismatch: file is for survey '{csv_code}' "
                f"but this project is '{def_code}' (survey_id={survey_id})"
            )
        log.info("ingest_zip: CSV code '%s' matches definition code '%s' ✓", csv_code, def_code)
    else:
        log.warning("ingest_zip: skipping code validation — csv_code=%s def_code=%s", csv_code, def_code)

    # ── 3. Parse CSV + run ingestion ─────────────────────────────────────────
    t0 = datetime.now(timezone.utc)
    try:
        loop = asyncio.get_running_loop()

        def _run():
            with tempfile.TemporaryDirectory() as tmp:
                csv_path = pathlib.Path(tmp) / csv_names[0]
                csv_path.write_bytes(csv_bytes)
                export_df = parse_export_csv(str(csv_path))
            log.info("ingest_zip: parsed %d rows from CSV", len(export_df))
            return pipeline_svc.ingest(survey_id, definition, export_df=export_df)

        result = await loop.run_in_executor(None, _run)
    except Exception:
        elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
        log.exception("ingest_zip failed for survey %s after %.1fs", survey_id, elapsed)
        # exc detail stays in the server log only — it can carry file paths,
        # library internals, or other implementation detail not meant for the client.
        raise HTTPException(500, f"Ingestion error after {elapsed:.0f}s — see server logs")

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    log.info("ingest_zip done: survey=%s elapsed=%.1fs rows=%s", survey_id, elapsed, result.get("n_rows"))

    # ── 4. Persist CSV to storage for audit + update info.json ───────────────
    storage.write_bytes(f"{survey_id}/mcp/data_export.csv", csv_bytes)
    _upsert_info(survey_id, session_id)

    # ── 5. Validate data integrity (Option A) ────────────────────────────────
    integrity = pipeline_svc.validate_data_integrity(survey_id)
    if not integrity["valid"]:
        errors = integrity.get("errors", [])
        log.warning("ingest_zip: integrity check failed — %d question(s) with missing columns", len(errors))
        raise HTTPException(
            422,
            f"Ingestion completed but data integrity check failed — "
            f"{len(errors)} question(s) have missing rawdata columns: "
            + "; ".join(errors[:5])
            + ("…" if len(errors) > 5 else "")
        )
    log.info("ingest_zip: integrity check passed — %d questions, %d cols", integrity["n_questions"], integrity["n_cols"])

    return result


# ── generate xlsx (no storage write) ─────────────────────────────────────────

@router.post("/{survey_id}/generate")
async def generate_xlsx(survey_id: int, request: Request,
                        profile_status: str = "approved,pending",
                        lang: str = "vi",
                        session_id: str = Depends(require_qme)):
    """Render datatable.xlsx — reuses cached compute from /preview when available.

    Body (optional): { "datatable_config": [...] }
    profile_status query param: comma-separated, e.g. "approved" or "approved,pending"
    """
    ip = request.client.host if request.client else "unknown"
    from services.rate_limiter import check as _rl_check
    _rl_check(ip, settings.DATA_DIR, limit=10, window=60, prefix="rate_gen")
    email = get_token_storage(session_id).email or "unknown"
    log.warning("AUDIT generate_xlsx: survey=%s user=%s ip=%s", survey_id, email, ip)
    storage = get_storage()
    if not storage.exists(f"{survey_id}/data/rawdata.csv"):
        raise HTTPException(400, "Run Refresh first — rawdata.csv not found")

    # Option B: validate data integrity before generating
    integrity = pipeline_svc.validate_data_integrity(survey_id)
    if not integrity["valid"]:
        errors = integrity.get("errors", [])
        raise HTTPException(
            422,
            f"Data integrity check failed — {len(errors)} question(s) have missing rawdata columns: "
            + "; ".join(errors[:5])
            + ("…" if len(errors) > 5 else "")
        )

    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass

    dt_config = body.get("datatable_config")   # None → use stored / cache

    if dt_config is None and not storage.exists(f"{survey_id}/datatable/datatable.json"):
        raise HTTPException(400, "No datatable.json found")

    statuses = _parse_profile_status(profile_status)
    t0 = datetime.now(timezone.utc)
    try:
        loop = asyncio.get_running_loop()
        xlsx_bytes = await loop.run_in_executor(
            None,
            lambda: pipeline_svc.generate_xlsx(survey_id, dt_config=dt_config,
                                                profile_status=statuses, lang=lang),
        )
    except FileNotFoundError:
        raise HTTPException(400, "Required data file not found — run Refresh first")
    except Exception as exc:
        elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
        log.exception("generate_xlsx failed for survey %s after %.1fs", survey_id, elapsed)
        raise HTTPException(500, f"Pipeline error after {elapsed:.0f}s — check server logs")

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    log.info("generate_xlsx done: survey=%s elapsed=%.1fs size=%d bytes", survey_id, elapsed, len(xlsx_bytes))

    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="datatable_{survey_id}.xlsx"'},
    )


# ── preview (server-side cross-tab, no xlsx) ─────────────────────────────────

@router.post("/{survey_id}/preview")
async def preview_table(survey_id: int, request: Request,
                        profile_status: str = "approved,pending",
                        lang: str = "vi",
                        session_id: str = Depends(require_qme)):
    """Compute cross-tab server-side → returns JSON table_results.

    Body (all optional):
      datatable_config: list[dict]  — override stored datatable.json
      table_indices:    list[int]   — compute only these tables (None = all)

    profile_status query param: comma-separated, e.g. "approved" or "approved,pending"
    """
    storage = get_storage()
    if not storage.exists(f"{survey_id}/data/rawdata.csv"):
        raise HTTPException(400, "Run Refresh first — rawdata.csv not found")

    # Option B: validate data integrity before preview
    integrity = pipeline_svc.validate_data_integrity(survey_id)
    if not integrity["valid"]:
        errors = integrity.get("errors", [])
        raise HTTPException(
            422,
            f"Data integrity check failed — {len(errors)} question(s) have missing rawdata columns: "
            + "; ".join(errors[:5])
            + ("…" if len(errors) > 5 else "")
        )

    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass

    dt_config     = body.get("datatable_config")   # None → use stored
    table_indices = body.get("table_indices")       # None → all

    if dt_config is None and not storage.exists(f"{survey_id}/datatable/datatable.json"):
        raise HTTPException(400, "No datatable.json — provide datatable_config in body")

    statuses = _parse_profile_status(profile_status)
    try:
        loop = asyncio.get_running_loop()
        table_results = await loop.run_in_executor(
            None,
            lambda: pipeline_svc.compute_preview(survey_id, dt_config, table_indices, statuses, lang),
        )
        return {"table_results": table_results}
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        log.exception("preview failed for survey %s", survey_id)
        raise HTTPException(500, "Pipeline error — check server logs")


# ── ingestion ─────────────────────────────────────────────────────────────────

@router.post("/{survey_id}/ingest")
async def ingest_survey(survey_id: int, session_id: str = Depends(require_qme)):
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
async def get_metadata(survey_id: int, session_id: str = Depends(require_qme)):
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


_DOWNLOAD_FILES = {
    "rawdata.csv":           ("data/rawdata.csv",                "text/csv"),
    "metadata.json":         ("data/metadata.json",              "application/json"),
    "data_export.csv":       ("mcp/data_export.csv",             "text/csv"),
    "definition.json":       ("mcp/definition.json",             "application/json"),
    "flagged_profiles.csv":  ("quality/flagged_profiles.csv",    "text/csv"),
}

_DOWNLOAD_NOT_FOUND_HINT = {
    "flagged_profiles.csv": "run Quality Check first",
}

# Raw source files — respondent-level data, and data_export.csv bypasses the
# PII stripping applied to rawdata.csv. Reachable only from the admin-only
# Data menu in the editor; enforced here so hiding the menu is not the control.
# flagged_profiles.csv is deliberately absent: the Quality panel offers it to
# every user.
_ADMIN_ONLY_DOWNLOADS = {
    "rawdata.csv", "metadata.json", "data_export.csv", "definition.json",
}


# ── quality check ─────────────────────────────────────────────────────────────

@router.post("/{survey_id}/quality")
async def run_quality_check(survey_id: int, session_id: str = Depends(require_qme)):
    """Run quality check (show_condition + contradiction validation) on existing rawdata.
    Returns full report including violations."""
    storage = get_storage()
    if not storage.exists(f"{survey_id}/data/rawdata.csv"):
        raise HTTPException(400, "Run Refresh first — rawdata.csv not found")
    try:
        loop   = asyncio.get_running_loop()
        report = await loop.run_in_executor(
            None, lambda: pipeline_svc.run_quality(survey_id)
        )
    except Exception as exc:
        log.exception("run_quality failed for survey %s", survey_id)
        raise HTTPException(500, f"Quality check error — {exc}")
    return report


@router.get("/{survey_id}/quality")
async def get_quality_report(survey_id: int, session_id: str = Depends(require_qme)):
    """Return stored quality_report.json (run POST /quality first)."""
    storage = get_storage()
    key = f"{survey_id}/quality/quality_report.json"
    if not storage.exists(key):
        raise HTTPException(404, "No quality report — run Quality Check first")
    return storage.read_json(key)


@router.get("/{survey_id}/download-data/{filename}")
async def download_data_file(survey_id: int, filename: str,
                             session_id: str = Depends(require_qme)):
    """Download a raw data file (rawdata.csv, metadata.json, data_export.csv, definition.json)."""
    if filename not in _DOWNLOAD_FILES:
        raise HTTPException(404, f"Unknown file: {filename}")
    if filename in _ADMIN_ONLY_DOWNLOADS:
        email = get_token_storage(session_id).email
        if not is_admin(email):
            log.warning("AUDIT download refused: survey=%s file=%s user=%s",
                        survey_id, filename, email)
            raise HTTPException(403, "Administrator access required for this file")
    rel_key, media_type = _DOWNLOAD_FILES[filename]
    storage = get_storage()
    key = f"{survey_id}/{rel_key}"
    if not storage.exists(key):
        hint = _DOWNLOAD_NOT_FOUND_HINT.get(filename, "run Refresh first")
        raise HTTPException(404, f"{filename} not found — {hint}")

    # Strip PII columns before download. Fail-closed: if stripping can't be
    # confirmed, refuse rather than risk serving names/phone numbers (SEC-007).
    if filename == "rawdata.csv":
        csv_text = storage.read_text(key)
        try:
            meta = storage.read_json(f"{survey_id}/data/metadata.json")
            csv_text = pipeline_svc.strip_pii_columns(csv_text, meta)
        except Exception as exc:
            log.exception("PII stripping failed for survey %s — refusing download", survey_id)
            raise HTTPException(500, "Could not verify PII was removed — download refused. See server logs.") from exc
        content = csv_text.encode("utf-8-sig")
    elif filename == "data_export.csv":
        try:
            csv_text = pipeline_svc.strip_system_pii_columns(storage.read_bytes(key))
        except Exception as exc:
            log.exception("System PII stripping failed for survey %s — refusing download", survey_id)
            raise HTTPException(500, "Could not verify PII was removed — download refused. See server logs.") from exc
        content = csv_text.encode("utf-8-sig")
    else:
        content = storage.read_bytes(key)

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{survey_id}_{filename}"',
            **_NO_CACHE,
        },
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

    # Strip PII columns (user-name, user-phone) before sending to browser.
    # Fail-closed: if stripping can't be confirmed, refuse rather than risk
    # serving names/phone numbers (SEC-007).
    csv_text = storage.read_text(key)
    try:
        meta = storage.read_json(f"{survey_id}/data/metadata.json")
        csv_text = pipeline_svc.strip_pii_columns(csv_text, meta)
    except Exception as exc:
        log.exception("PII stripping failed for survey %s — refusing rawdata response", survey_id)
        raise HTTPException(500, "Could not verify PII was removed — request refused. See server logs.") from exc

    return PlainTextResponse(csv_text, media_type="text/csv", headers=_NO_CACHE)


# ── datatable CRUD ────────────────────────────────────────────────────────────

@router.get("/{survey_id}/datatable")
async def get_datatable(survey_id: int, session_id: str = Depends(require_qme)):
    storage = get_storage()
    key = f"{survey_id}/datatable/datatable.json"
    if not storage.exists(key):
        raise HTTPException(404, "No datatable.json yet")
    return storage.read_json(key)


@router.post("/{survey_id}/datatable")
async def save_datatable(survey_id: int, request: Request,
                         session_id: str = Depends(require_qme)):
    body = await request.json()
    email = get_token_storage(session_id).email or "unknown"
    ip = request.client.host if request.client else "unknown"
    log.warning("AUDIT save_datatable: survey=%s user=%s ip=%s", survey_id, email, ip)
    get_storage().write_json(f"{survey_id}/datatable/datatable.json", body)
    return {"status": "saved"}


# ── run pipeline (table step) ─────────────────────────────────────────────────

@router.post("/{survey_id}/run")
async def run_pipeline(survey_id: int, request: Request,
                       version: str | None = None,
                       profile_status: str = "approved",
                       session_id: str = Depends(require_qme)):
    """Run table step and save versioned datatable.xlsx.

    profile_status: comma-separated list, e.g. "approved" or "approved,pending"
    """
    ip = request.client.host if request.client else "unknown"
    from services.rate_limiter import check as _rl_check
    _rl_check(ip, settings.DATA_DIR, limit=10, window=60, prefix="rate_run")
    email = get_token_storage(session_id).email or "unknown"
    log.warning("AUDIT run_pipeline: survey=%s version=%s user=%s ip=%s", survey_id, version, email, ip)
    storage = get_storage()
    if not storage.exists(f"{survey_id}/data/rawdata.csv"):
        raise HTTPException(400, "Run /ingest first — rawdata.csv not found")
    if not storage.exists(f"{survey_id}/datatable/datatable.json"):
        raise HTTPException(400, "No datatable.json found")

    if version is not None:
        version = _validate_version(version)
    statuses = _parse_profile_status(profile_status)
    try:
        return pipeline_svc.run_table(survey_id, version, profile_status=statuses)
    except FileNotFoundError:
        raise HTTPException(400, "Required data file not found — run Refresh first")
    except Exception as exc:
        log.exception("run_table failed for survey %s", survey_id)
        raise HTTPException(500, "Pipeline error — check server logs")


# ── download xlsx ─────────────────────────────────────────────────────────────

@router.get("/{survey_id}/download/{version}")
async def download_xlsx(survey_id: int, version: str = "v1",
                        session_id: str = Depends(require_qme)):
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
