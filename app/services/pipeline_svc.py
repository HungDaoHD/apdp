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
            _filter_rawdata(raw_bytes, profile_status or ["approved"])
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


def generate_xlsx(survey_id: int, profile_status: list[str] | None = None) -> bytes:
    """Run table step in a temp dir and return xlsx bytes — nothing is saved to storage."""
    from services.storage import get_storage
    storage = get_storage()

    with tempfile.TemporaryDirectory() as tmp:
        survey_dir = Path(tmp) / str(survey_id)
        data_dir = survey_dir / "data"
        dt_dir = survey_dir / "datatable"
        data_dir.mkdir(parents=True, exist_ok=True)
        dt_dir.mkdir(parents=True, exist_ok=True)

        raw_bytes = storage.read_bytes(f"{survey_id}/data/rawdata.csv")
        (data_dir / "rawdata.csv").write_bytes(
            _filter_rawdata(raw_bytes, profile_status or ["approved"])
        )
        (data_dir / "metadata.json").write_bytes(storage.read_bytes(f"{survey_id}/data/metadata.json"))
        dt_path = dt_dir / "datatable.json"
        dt_path.write_bytes(storage.read_bytes(f"{survey_id}/datatable/datatable.json"))

        cfg = PipelineConfig(
            output_dir=str(survey_dir),
            data_dir=str(data_dir),
            skip_ingestion=True,
            datatable_config=str(dt_path),
            version="tmp",
        )
        ctx = Pipeline(cfg).run()

        return (Path(ctx["output_dir"]) / "datatable.xlsx").read_bytes()


def _normalize_export_csv(csv_bytes: bytes) -> bytes:
    """Pad blank lines so the header row lands at HEADER_IDX=6 and data at FIRST_DATA_IDX=12.

    read_survey_data_file returns a trimmed format (header at idx ~3) while
    parse_export_csv expects the full QMe export layout (header at idx 6).
    """
    import csv as _csv
    import io as _io

    HEADER_IDX     = 6
    FIRST_DATA_IDX = 12

    lines = csv_bytes.decode("utf-8-sig").splitlines()

    # Locate the header row: first row whose first few cells include "Approve" and "Reject"
    header_idx = None
    for i, line in enumerate(lines[:20]):
        try:
            parts = next(_csv.reader(_io.StringIO(line)))
        except Exception:
            continue
        if "Approve" in parts and "Reject" in parts:
            header_idx = i
            break

    if header_idx is None or header_idx == HEADER_IDX:
        return csv_bytes  # already in expected format, or can't auto-fix

    # Sub-headers: rows after the header that don't look like data
    # A data row has 'x', 'X', or '' in the Approve column (col 0)
    sub_end = header_idx + 1
    for i in range(header_idx + 1, min(header_idx + 10, len(lines))):
        try:
            parts = next(_csv.reader(_io.StringIO(lines[i])))
        except Exception:
            continue
        if parts and parts[0].strip().lower() in ("x", ""):
            sub_end = i
            break
        sub_end = i + 1

    preamble    = lines[:header_idx]
    header_line = lines[header_idx]
    sub_headers = lines[header_idx + 1: sub_end]
    data_lines  = lines[sub_end:]

    # Pad to hit the expected indices
    pre_padding = max(0, HEADER_IDX - len(preamble))
    sub_padding = max(0, FIRST_DATA_IDX - HEADER_IDX - 1 - len(sub_headers))

    normalized = preamble + [""] * pre_padding + [header_line] + sub_headers + [""] * sub_padding + data_lines
    logger.info("_normalize_export_csv: header moved from idx %d → %d", header_idx, HEADER_IDX)
    return "\n".join(normalized).encode("utf-8-sig")


def refresh_csv(survey_id: int, definition: dict, export_csv_bytes: bytes) -> dict:
    """Ingest from export CSV bytes — saves files to storage then ingests.

    Always ingests ALL profile statuses so rawdata.csv can be filtered later.
    """
    from services.storage import get_storage
    from surveyflow.steps.ingestion.export_parser import parse_export_csv

    storage = get_storage()
    storage.write_json(f"{survey_id}/mcp/definition.json", definition)
    storage.write_bytes(f"{survey_id}/mcp/data_export.csv", export_csv_bytes)

    normalized = _normalize_export_csv(export_csv_bytes)
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / "data_export.csv"
        csv_path.write_bytes(normalized)
        export_df = parse_export_csv(csv_path)

    # Drop sub-header rows that slipped through normalization — they always have empty task_id
    if "task_id" in export_df.columns:
        mask = export_df["task_id"].notna()
        if export_df["task_id"].dtype == object:
            mask = mask & (export_df["task_id"].astype(str).str.strip() != "")
        n_dropped = (~mask).sum()
        if n_dropped:
            logger.info("refresh_csv: dropped %d sub-header row(s) with empty task_id", n_dropped)
        export_df = export_df[mask]

    return ingest(survey_id, definition, export_df=export_df, profile_status=["approved", "pending"])


def refresh(survey_id: int, definition: dict, rows_pages: list[dict]) -> dict:
    """Fetch + ingest in one call — writes mcp files then rawdata/metadata to storage.

    Always ingests ALL profile statuses so rawdata.csv can be filtered later
    (by run_table / generate_xlsx) without re-fetching from QMe.
    """
    from services.storage import get_storage
    storage = get_storage()

    # Persist MCP data
    storage.write_json(f"{survey_id}/mcp/definition.json", definition)
    storage.write_json(f"{survey_id}/mcp/rows_pages.json", rows_pages)

    # Ingest all statuses — profile_status filter is applied at table/preview time
    return ingest(survey_id, definition, rows_pages, profile_status=["approved", "pending"])


# ── helpers ────────────────────────────────────────────────────────────────────

_PII_TYPES = frozenset({"user-name", "user-phone"})


def strip_pii_columns(csv_text: str, metadata: list) -> str:
    """Remove user-name and user-phone columns from rawdata before serving to browser.

    Identifies PII columns by matching question labels against known PII answer types.
    Original rawdata on disk is never modified.
    """
    try:
        pii_labels = {
            q.get("label")
            for q in metadata
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
