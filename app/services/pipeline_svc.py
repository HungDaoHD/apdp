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


def ingest(survey_id: int, definition: dict, rows_pages: list[dict],
           profile_status: list[str] | None = None) -> dict:
    """Run ingestion step → writes rawdata.csv + metadata.json to storage."""
    from services.storage import get_storage
    storage = get_storage()

    with tempfile.TemporaryDirectory() as tmp:
        survey_dir = Path(tmp) / str(survey_id)
        data_dir = survey_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        cfg = PipelineConfig(
            definition=definition,
            rows_pages=rows_pages,
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
