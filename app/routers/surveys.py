"""Survey discovery and data-fetching endpoints."""
from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, Depends, HTTPException

from dependencies import require_qme
from services.mcp_client import get_storage as get_token_storage, get_access_token
from services.storage import get_storage

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/surveys", tags=["surveys"])


# ── Auth-error helpers ────────────────────────────────────────────────────────

def _unwrap_exc(exc: BaseException) -> BaseException:
    """Unwrap ExceptionGroup (anyio TaskGroup / Python 3.11+) to the first leaf."""
    if hasattr(exc, "exceptions") and exc.exceptions:
        return _unwrap_exc(exc.exceptions[0])
    return exc


def _is_auth_error(exc: BaseException) -> bool:
    """Return True if exc (possibly wrapped in ExceptionGroup) signals a 401/403."""
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


def _raise_if_auth(exc: BaseException, session_id: str | None = None) -> None:
    """If *exc* is an auth error: clear the stale token and raise HTTP 401."""
    if _is_auth_error(exc):
        if session_id:
            try:
                get_token_storage(session_id).clear()
            except Exception:
                pass
        raise HTTPException(401, "QMe authentication failed — please reconnect")


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
    from surveyflow import QMeClient
    from config import settings

    access_token = get_access_token(session_id)
    if not access_token:
        raise HTTPException(401, "Not connected to QMe")

    client = QMeClient(settings.QME_MCP_BASE_URL, access_token)
    try:
        loop = asyncio.get_running_loop()
        tools = await loop.run_in_executor(None, client.list_tools)
    except Exception as exc:
        log.warning("list_mcp_tools failed: %s", exc)
        _raise_if_auth(exc, session_id)
        raise HTTPException(502, f"MCP error: {exc}")

    return [
        {
            "name": t.name,
            "description": t.description,
            "inputSchema": t.inputSchema,
        }
        for t in tools
    ]


# ── search ────────────────────────────────────────────────────────────────────

@router.get("/search")
async def search_surveys(q: str = "", limit: int = 50, session_id: str = Depends(require_qme)):
    limit = min(max(limit, 1), 200)  # M6: clamp to [1, 200]
    from surveyflow import QMeClient
    from config import settings

    access_token = get_access_token(session_id)
    if not access_token:
        raise HTTPException(401, "Not connected to QMe")

    client = QMeClient(settings.QME_MCP_BASE_URL, access_token)
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: client._call("search_surveys", {"query": q, "limit": limit})
        )
        # Ensure result is JSON-serializable (raw MCP objects are not)
        if isinstance(result, (dict, list, str, int, float, bool, type(None))):
            return result
        # Fallback: try converting via str
        return {"raw": str(result)}
    except Exception as exc:
        log.warning("search_surveys failed: %s", exc)
        _raise_if_auth(exc, session_id)
        raise HTTPException(502, f"Search unavailable: {exc}")


# ── fetch from QMe ────────────────────────────────────────────────────────────

@router.post("/{survey_id}/fetch")
async def fetch_survey(survey_id: int, session_id: str = Depends(require_qme)):
    """Download definition + all rows pages from QMe MCP and save to storage."""
    import tempfile
    from pathlib import Path
    from surveyflow import QMeClient, FetchStep
    from config import settings

    access_token = get_access_token(session_id)
    if not access_token:
        raise HTTPException(401, "Not connected to QMe")

    client  = QMeClient(settings.QME_MCP_BASE_URL, access_token)
    storage = get_storage()

    def _fetch():
        from surveyflow.steps.ingestion.export_parser import parse_export_csv
        with tempfile.TemporaryDirectory() as tmp:
            mcp_dir = Path(tmp) / "mcp"
            mcp_dir.mkdir(parents=True, exist_ok=True)
            ctx = FetchStep().run({
                "client":    client,
                "survey_id": survey_id,
                "mcp_dir":   str(mcp_dir),
                "mode":      "export",
            })
            definition = ctx["definition"]
            csv_path   = Path(ctx["export_csv"])
            export_df  = parse_export_csv(csv_path)
            storage.write_json(f"{survey_id}/mcp/definition.json", definition)
            storage.write_bytes(f"{survey_id}/mcp/data_export.csv", csv_path.read_bytes())
            return {"status": "ok", "total_rows": len(export_df)}

    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _fetch)
    except Exception as exc:
        log.warning("fetch_survey failed for survey %s: %s", survey_id, exc)
        _raise_if_auth(exc, session_id)
        raise HTTPException(502, str(exc))


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
