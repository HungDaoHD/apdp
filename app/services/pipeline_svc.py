"""Thin wrapper around the surveyflow Pipeline."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from surveyflow.core.config import PipelineConfig
from surveyflow.pipeline import Pipeline

logger = logging.getLogger(__name__)


def _next_version(base_dir: Path) -> str:
    existing = [
        int(m.group(1))
        for d in base_dir.iterdir()
        if d.is_dir() and (m := re.fullmatch(r"v(\d+)", d.name))
    ] if base_dir.exists() else []
    return f"v{max(existing, default=0) + 1}"


def ingest(survey_dir: Path, definition: dict, rows_pages: list[dict]) -> dict:
    """Run ingestion step only → writes rawdata.csv + metadata.json to survey_dir/data/."""
    data_dir = survey_dir / "data"
    cfg = PipelineConfig(
        definition=definition,
        rows_pages=rows_pages,
        output_dir=str(survey_dir),
        data_dir=str(data_dir),
        skip_ingestion=False,
        datatable_config=None,
    )
    ctx = Pipeline(cfg).run()
    return {
        "rawdata_path": ctx["rawdata_path"],
        "metadata_path": ctx["metadata_path"],
        "n_rows": len(ctx["rawdata"]),
    }


def run_table(survey_dir: Path, version: str | None = None) -> dict:
    """Run table step only (skip ingestion) → writes datatable.xlsx to survey_dir/{version}/."""
    datatable_path = survey_dir / "datatable" / "datatable.json"
    if not datatable_path.exists():
        raise FileNotFoundError(f"datatable.json not found: {datatable_path}")

    if version is None:
        version = _next_version(survey_dir)

    cfg = PipelineConfig(
        output_dir=str(survey_dir),
        data_dir=str(survey_dir / "data"),
        skip_ingestion=True,
        datatable_config=str(datatable_path),
        version=version,
    )
    ctx = Pipeline(cfg).run()

    xlsx_path = Path(ctx["output_dir"]) / "datatable.xlsx"
    return {
        "version": version,
        "xlsx_path": str(xlsx_path),
    }
