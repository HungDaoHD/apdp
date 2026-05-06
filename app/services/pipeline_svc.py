"""Thin wrapper around the surveyflow Pipeline — storage-backend-aware."""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from surveyflow.core.config import PipelineConfig
from surveyflow.pipeline import Pipeline

logger = logging.getLogger(__name__)


def ingest(survey_id: int, definition: dict, rows_pages: list[dict]) -> dict:
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
        )
        ctx = Pipeline(cfg).run()

        storage.write_bytes(f"{survey_id}/data/rawdata.csv",   Path(ctx["rawdata_path"]).read_bytes())
        storage.write_bytes(f"{survey_id}/data/metadata.json", Path(ctx["metadata_path"]).read_bytes())

        return {
            "rawdata_key":   f"{survey_id}/data/rawdata.csv",
            "metadata_key":  f"{survey_id}/data/metadata.json",
            "n_rows": len(ctx["rawdata"]),
        }


def run_table(survey_id: int, version: str | None = None) -> dict:
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

        # Stage files locally for the pipeline
        (data_dir / "rawdata.csv").write_bytes(storage.read_bytes(f"{survey_id}/data/rawdata.csv"))
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


def generate_xlsx(survey_id: int) -> bytes:
    """Run table step in a temp dir and return xlsx bytes — nothing is saved to storage."""
    from services.storage import get_storage
    storage = get_storage()

    with tempfile.TemporaryDirectory() as tmp:
        survey_dir = Path(tmp) / str(survey_id)
        data_dir = survey_dir / "data"
        dt_dir = survey_dir / "datatable"
        data_dir.mkdir(parents=True, exist_ok=True)
        dt_dir.mkdir(parents=True, exist_ok=True)

        (data_dir / "rawdata.csv").write_bytes(storage.read_bytes(f"{survey_id}/data/rawdata.csv"))
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
    """Fetch + ingest in one call — writes mcp files then rawdata/metadata to storage."""
    from services.storage import get_storage
    storage = get_storage()

    # Persist MCP data
    storage.write_json(f"{survey_id}/mcp/definition.json", definition)
    storage.write_json(f"{survey_id}/mcp/rows_pages.json", rows_pages)

    # Run ingestion
    return ingest(survey_id, definition, rows_pages)
