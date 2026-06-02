"""Thin wrapper around the surveyflow Pipeline — storage-backend-aware."""
from __future__ import annotations

import io
import logging
import tempfile
from pathlib import Path

import pandas as pd
from surveyflow.core.config import PipelineConfig
from surveyflow.pipeline import Pipeline

logger = logging.getLogger(__name__)



def ingest(survey_id: int, definition: dict,
           rows_pages: list[dict] | None = None,
           export_df: "pd.DataFrame | None" = None,
           profile_status: list[str] | None = None) -> dict:
    """Run ingestion step → writes rawdata.csv + metadata.json to storage.

    Pass either rows_pages (QMe MCP mode) or export_df (CSV export mode).
    profile_status defaults to ["approved"] matching surveyflow's default.
    """
    from services.storage import get_storage
    storage = get_storage()

    with tempfile.TemporaryDirectory() as tmp:
        survey_dir = Path(tmp) / str(survey_id)
        data_dir = survey_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        row_source = {"export_df": export_df} if export_df is not None else {"rows_pages": rows_pages}
        cfg = PipelineConfig(
            definition=definition,
            **row_source,
            output_dir=str(survey_dir),
            data_dir=str(data_dir),
            skip_ingestion=False,
            datatable_config=None,
            **({"profile_status": profile_status} if profile_status is not None else {}),
        )
        ctx = Pipeline(cfg).run()

        storage.write_bytes(f"{survey_id}/data/rawdata.csv",   Path(ctx["rawdata_path"]).read_bytes())
        storage.write_bytes(f"{survey_id}/data/metadata.json", Path(ctx["metadata_path"]).read_bytes())

        rawdata = ctx["rawdata"]
        n_rows  = len(rawdata)
        if "profile_status" in rawdata.columns:
            vc = rawdata["profile_status"].str.lower().value_counts()
            n_approved = int(vc.get("approved", 0))
            n_pending  = int(vc.get("pending",  0))
        else:
            n_approved, n_pending = n_rows, 0

        stats = {"n_rows": n_rows, "n_approved": n_approved, "n_pending": n_pending}
        storage.write_json(f"{survey_id}/data/stats.json", stats)

        return {
            "rawdata_key":   f"{survey_id}/data/rawdata.csv",
            "metadata_key":  f"{survey_id}/data/metadata.json",
            **stats,
        }


def run_table(survey_id: int, version: str | None = None,
              profile_status: list[str] | None = None) -> dict:
    """Run table step only (skip ingestion) → writes datatable.xlsx to storage."""
    from services.storage import get_storage
    storage = get_storage()

    if version is None:
        version = storage.next_version(survey_id)

    with tempfile.TemporaryDirectory() as tmp:
        survey_dir = Path(tmp) / str(survey_id)
        data_dir = survey_dir / "data"
        dt_dir = survey_dir / "datatable"
        data_dir.mkdir(parents=True, exist_ok=True)
        dt_dir.mkdir(parents=True, exist_ok=True)

        # Stage rawdata — optionally filtered by profile_status
        raw_bytes = storage.read_bytes(f"{survey_id}/data/rawdata.csv")
        (data_dir / "rawdata.csv").write_bytes(
            _filter_rawdata(raw_bytes, profile_status or ["approved", "pending"])
        )
        (data_dir / "metadata.json").write_bytes(storage.read_bytes(f"{survey_id}/data/metadata.json"))
        dt_path = dt_dir / "datatable.json"
        dt_path.write_bytes(storage.read_bytes(f"{survey_id}/datatable/datatable.json"))

        cfg = PipelineConfig(
            output_dir=str(survey_dir),
            data_dir=str(data_dir),
            skip_ingestion=True,
            datatable_config=str(dt_path),
            version=version,
        )
        ctx = Pipeline(cfg).run()

        xlsx_bytes = (Path(ctx["output_dir"]) / "datatable.xlsx").read_bytes()
        xlsx_key = f"{survey_id}/{version}/datatable.xlsx"
        storage.write_bytes(xlsx_key, xlsx_bytes)

        return {"version": version, "xlsx_key": xlsx_key}


def generate_xlsx(survey_id: int,
                  dt_config: list[dict] | None = None,
                  profile_status: list[str] | None = None) -> bytes:
    """Compute cross-tab and render datatable.xlsx using TableStep.

    Always runs a full compute — no cache is used or written.
    dt_config: override datatable config; None → load from storage.
    profile_status: filter rawdata rows; defaults to ["approved", "pending"].
    """
    from services.storage import get_storage
    from surveyflow.steps.table.table_step import TableStep

    storage = get_storage()
    statuses = profile_status or ["approved", "pending"]

    if dt_config is None:
        dt_config = storage.read_json(f"{survey_id}/datatable/datatable.json")

    raw_bytes = storage.read_bytes(f"{survey_id}/data/rawdata.csv")
    df = pd.read_csv(
        io.BytesIO(_filter_rawdata(raw_bytes, statuses)),
        low_memory=False,
    )
    metadata = storage.read_json(f"{survey_id}/data/metadata.json")

    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp) / str(survey_id)
        output_dir.mkdir(parents=True, exist_ok=True)

        logger.info("generate_xlsx: computing tables (survey %s)", survey_id)
        ctx = TableStep().compute({
            "df":               df,
            "metadata":         metadata,
            "datatable_config": dt_config,
        })
        ctx["output_dir"] = str(output_dir)
        TableStep().render_xlsx(ctx)

        return (output_dir / "datatable.xlsx").read_bytes()


def compute_preview(survey_id: int,
                    dt_config: list[dict] | None = None,
                    table_indices: list[int] | None = None,
                    profile_status: list[str] | None = None) -> list[dict]:
    """Compute cross-tab using TableStep.compute() — no xlsx written.

    dt_config:     override datatable config; None → use stored datatable.json.
    table_indices: compute only these table indices; None → all.
    profile_status: filter rawdata rows; defaults to ["approved", "pending"].

    Returns table_results (JSON-serialisable list).
    """
    from services.storage import get_storage
    from surveyflow.steps.table.table_step import TableStep

    storage = get_storage()

    # Load + filter rawdata
    raw_bytes = storage.read_bytes(f"{survey_id}/data/rawdata.csv")
    df = pd.read_csv(
        io.BytesIO(_filter_rawdata(raw_bytes, profile_status or ["approved", "pending"])),
        low_memory=False,
    )

    # Load metadata
    metadata = storage.read_json(f"{survey_id}/data/metadata.json")

    # Load datatable config (provided or stored)
    if dt_config is None:
        dt_config = storage.read_json(f"{survey_id}/datatable/datatable.json")

    ctx = TableStep().compute({
        "df":               df,
        "metadata":         metadata,
        "datatable_config": dt_config,
        "table_indices":    table_indices,
    })

    return ctx["table_results"]


def refresh_csv(survey_id: int, client) -> dict:
    """Fetch export CSV via QMeClient/FetchStep + ingest.

    Uses surveyflow default profile_status (["approved", "pending"] as of v0.5+).
    Saves definition.json + data_export.csv to storage for audit / re-ingestion.
    """
    from services.storage import get_storage
    from surveyflow import FetchStep
    from surveyflow.steps.ingestion.export_parser import parse_export_csv

    storage = get_storage()

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
        csv_path   = Path(ctx["export_csv"])   # path written by FetchStep
        export_df  = parse_export_csv(csv_path)

        # Persist MCP data to storage
        storage.write_json(f"{survey_id}/mcp/definition.json", definition)
        storage.write_bytes(f"{survey_id}/mcp/data_export.csv", csv_path.read_bytes())

    return ingest(survey_id, definition, export_df=export_df)


def refresh(survey_id: int, client, step_cb=None) -> dict:
    """Fetch export CSV via QMeClient/FetchStep + ingest.

    Uses surveyflow default profile_status (["approved", "pending"] as of v0.5+).
    Saves definition.json + data_export.csv to storage for audit / re-ingestion.
    step_cb: optional callable(msg) to report progress (written to status file).
    """
    from services.storage import get_storage
    from surveyflow import FetchStep
    from surveyflow.steps.ingestion.export_parser import parse_export_csv

    def _cb(msg):
        logger.info("[refresh:%s] %s", survey_id, msg)
        if step_cb:
            step_cb(msg)

    storage = get_storage()

    with tempfile.TemporaryDirectory() as tmp:
        mcp_dir = Path(tmp) / "mcp"
        mcp_dir.mkdir(parents=True, exist_ok=True)

        _cb("Fetching data from QMe (export mode)…")
        ctx = FetchStep().run({
            "client":    client,
            "survey_id": survey_id,
            "mcp_dir":   str(mcp_dir),
            "mode":      "export",
        })
        _cb("FetchStep done — parsing export CSV…")

        definition = ctx["definition"]
        csv_path   = Path(ctx["export_csv"])
        export_df  = parse_export_csv(csv_path)
        _cb(f"Parsed {len(export_df)} rows — saving to storage…")

        storage.write_json(f"{survey_id}/mcp/definition.json", definition)
        storage.write_bytes(f"{survey_id}/mcp/data_export.csv", csv_path.read_bytes())

    _cb("Running ingestion pipeline…")
    result = ingest(survey_id, definition, export_df=export_df)
    _cb(f"Ingestion done — {result.get('n_rows')} rows")
    return result


# ── data integrity validation ─────────────────────────────────────────────────

def validate_data_integrity(survey_id: int) -> dict:
    """Cross-check rawdata.csv columns against metadata.json rawdata_columns.

    Returns:
        {"valid": True, "n_questions": N, "n_cols": M}
        {"valid": False, "errors": [...], "n_missing": N}
    """
    from services.storage import get_storage
    storage = get_storage()

    # Load rawdata columns (header only — no need to read all rows)
    try:
        raw_bytes = storage.read_bytes(f"{survey_id}/data/rawdata.csv")
        header_line = raw_bytes.split(b"\n")[0].decode("utf-8-sig", errors="replace")
        import csv as _csv, io as _io
        rawdata_cols = set(next(_csv.reader(_io.StringIO(header_line))))
    except Exception as exc:
        return {"valid": False, "errors": [f"Cannot read rawdata.csv: {exc}"], "n_missing": -1}

    # Load metadata
    try:
        metadata = storage.read_json(f"{survey_id}/data/metadata.json")
    except Exception as exc:
        return {"valid": False, "errors": [f"Cannot read metadata.json: {exc}"], "n_missing": -1}

    questions = metadata.get("questions") or {}
    if isinstance(questions, list):
        questions = {str(i): q for i, q in enumerate(questions)}

    errors = []
    for qid, q in questions.items():
        label   = q.get("label") or qid
        atype   = q.get("answer_type") or "?"
        missing = [
            col for col in (q.get("rawdata_columns") or [])
            if col not in rawdata_cols
        ]
        if missing:
            errors.append(
                f"[{atype}] {label}: missing column(s) {missing}"
            )

    if errors:
        return {"valid": False, "errors": errors, "n_missing": len(errors)}

    return {"valid": True, "n_questions": len(questions), "n_cols": len(rawdata_cols)}


# ── helpers ────────────────────────────────────────────────────────────────────

_PII_TYPES = frozenset({"user-name", "user-phone"})


def strip_pii_columns(csv_text: str, metadata: "dict | list") -> str:
    """Remove user-name and user-phone columns from rawdata before serving to browser.

    Identifies PII columns by matching question labels against known PII answer types.
    Original rawdata on disk is never modified.

    Accepts both metadata formats:
    - New dict format: {"questions": {"1": {...}, "2": {...}}, "system_columns": {...}}
    - Old list format: [{"label": ..., "answer_type": ...}, ...]
    """
    try:
        if isinstance(metadata, dict):
            questions = list(metadata.get("questions", {}).values())
        else:
            questions = metadata

        pii_labels = {
            q.get("label")
            for q in questions
            if (q.get("answer_type") or q.get("question_type") or q.get("type", ""))
            in _PII_TYPES
            and q.get("label")
        }
        if not pii_labels:
            return csv_text
        df = pd.read_csv(io.StringIO(csv_text), low_memory=False)
        cols_to_drop = [c for c in df.columns if c in pii_labels]
        if not cols_to_drop:
            return csv_text
        logger.info("strip_pii: dropping columns %s", cols_to_drop)
        return df.drop(columns=cols_to_drop).to_csv(index=False)
    except Exception as exc:
        logger.warning("strip_pii failed — serving unmodified rawdata: %s", exc)
        return csv_text


def _filter_rawdata(raw_bytes: bytes, profile_status: list[str]) -> bytes:
    """Return CSV bytes filtered to rows whose profile_status column is in the list.

    If the column doesn't exist or the list is empty the original bytes are returned.
    """
    try:
        df = pd.read_csv(io.BytesIO(raw_bytes), low_memory=False)
        if "profile_status" not in df.columns or not profile_status:
            return raw_bytes
        allowed = {s.lower() for s in profile_status}
        df = df[df["profile_status"].str.lower().isin(allowed)]
        return df.to_csv(index=False).encode("utf-8")
    except Exception as exc:
        logger.warning("profile_status filter failed — using unfiltered data: %s", exc)
        return raw_bytes
