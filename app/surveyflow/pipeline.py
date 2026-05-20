"""SurveyFlow Pipeline — orchestrates Ingestion → Table."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from surveyflow.core.config import PipelineConfig
from surveyflow.steps.ingestion.ingestion_step import IngestionStep
from surveyflow.steps.table.table_step import TableStep

logger = logging.getLogger(__name__)


class Pipeline:
    """Run the full survey data pipeline.

    Folder layout
    -------------
    output_dir/
    ├── data/           ← rawdata.csv + metadata.json (generated once from mcp/)
    ├── datatable/      ← datatable.json (managed by Claude)
    ├── v1/             ← datatable.xlsx only
    └── v2/

    Usage — first run (ingestion + table)
    -------------------------------------
    >>> pipeline = Pipeline(PipelineConfig(
    ...     definition       = definition,
    ...     rows_pages       = rows_pages,
    ...     output_dir       = "output/VN8947",
    ...     datatable_config = "output/VN8947/datatable/datatable.json",
    ...     version          = "v1",
    ... ))
    >>> result = pipeline.run()

    Usage — table-only (data already exists)
    -----------------------------------------
    >>> pipeline = Pipeline(PipelineConfig(
    ...     output_dir       = "output/VN8947",
    ...     skip_ingestion   = True,
    ...     datatable_config = "output/VN8947/datatable/datatable.json",
    ...     version          = "v2",
    ... ))
    >>> result = pipeline.run()
    """

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config

    # ── public ────────────────────────────────────────────────────────────────

    def run(self) -> dict:
        """Execute pipeline steps and return the final context dict."""
        cfg = self.config

        version      = cfg.version or datetime.now().strftime("%Y%m%d_%H%M%S")
        base_dir     = Path(cfg.output_dir)
        data_dir     = Path(cfg.data_dir) if cfg.data_dir else base_dir / "data"
        versioned_dir = base_dir / version   # datatable.xlsx goes here

        logger.info("Pipeline started  →  %s", versioned_dir)

        context: dict = {
            "definition":     cfg.definition,
            "rows_pages":     cfg.rows_pages,
            "export_df":      cfg.export_df,
            "data_dir":       str(data_dir),
            "output_dir":     str(versioned_dir),   # table step writes xlsx here
            "profile_status": cfg.profile_status,
            "skip_render":    cfg.skip_render,
            "table_indices":  cfg.table_indices,
            "version":        version,
        }

        # ── Step 1: Ingestion ──────────────────────────────────────────────
        if not cfg.skip_ingestion:
            if cfg.definition is None:
                raise ValueError(
                    "definition is required when skip_ingestion=False"
                )
            if cfg.rows_pages is None and cfg.export_df is None:
                raise ValueError(
                    "rows_pages or export_df is required when skip_ingestion=False"
                )
            logger.info("── Step 1: Ingestion")
            context = IngestionStep().run(context)
        else:
            # Load existing rawdata + metadata from data_dir
            rawdata_path  = data_dir / "rawdata.csv"
            metadata_path = data_dir / "metadata.json"
            if not rawdata_path.exists():
                raise FileNotFoundError(
                    f"rawdata.csv not found in {data_dir}. "
                    "Run pipeline without skip_ingestion first."
                )
            logger.info("── Step 1: Ingestion (skipped — reading from %s)", data_dir)
            context["rawdata"]       = pd.read_csv(rawdata_path)
            with metadata_path.open(encoding="utf-8") as f:
                context["metadata"]  = json.load(f)
            context["rawdata_path"]  = str(rawdata_path)
            context["metadata_path"] = str(metadata_path)

        # ── Step 2: Table (optional) ───────────────────────────────────────
        if cfg.datatable_config is not None:
            logger.info("── Step 2: Table — compute")
            context["df"]               = context["rawdata"]
            context["datatable_config"] = self._load_datatable_config(
                cfg.datatable_config
            )
            table_step = TableStep()
            context = table_step.compute(context)
            if not cfg.skip_render:
                logger.info("── Step 2: Table — render xlsx")
                context = table_step.render_xlsx(context)

        logger.info("Pipeline complete.")
        return context

    # ── internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _load_datatable_config(source: str | dict) -> dict:
        if isinstance(source, dict):
            return source
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"datatable_config not found: {path}")
        with path.open(encoding="utf-8") as f:
            return json.load(f)
