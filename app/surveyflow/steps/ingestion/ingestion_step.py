"""Ingestion step: definition + rows → rawdata.csv + metadata.json."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from surveyflow.core.base import Step
from surveyflow.steps.ingestion.data_parser import (
    encode_records,
    parse_rows,
    records_to_dataframe,
)
from surveyflow.steps.ingestion.metadata_parser import parse_metadata
from surveyflow.utils.io import save_csv, save_json

logger = logging.getLogger(__name__)


class IngestionStep(Step):
    """
    Flow
    ----
    1. parse_metadata(definition)        → metadata keyed by str(question_id)
    2. parse_rows(pages, definition)     → raw text/code records
    3. encode_records(records, metadata) → SA → int, NUM → number, rest as-is
    4. records_to_dataframe(records)     → ordered DataFrame
    5. save rawdata.csv + metadata.json

    Context inputs
    --------------
    definition   : dict   raw get_survey_definition response
    rows_pages   : list   raw get_survey_rows responses (one per page)
    data_dir     : str    destination folder for rawdata.csv + metadata.json

    Context outputs
    ---------------
    rawdata        : pd.DataFrame
    metadata       : dict
    rawdata_path   : str
    metadata_path  : str
    """

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        if context.get("export_df") is not None:
            return self._run_export(context)
        return self._run_rows(context)

    def _run_export(self, context: dict[str, Any]) -> dict[str, Any]:
        """Export-format ingestion: definition + export_df → rawdata.csv + metadata.json."""
        from surveyflow.steps.ingestion.export_parser import (
            annotate_rawdata_columns,
            convert_export_to_rawdata,
            patch_metadata,
            recode_area_questions,
        )

        import pandas as _pd

        definition = context["definition"]
        export_df  = context["export_df"]
        if not isinstance(export_df, _pd.DataFrame):
            raise TypeError(
                f"export_df must be a pd.DataFrame, got {type(export_df).__name__}. "
                "Use parse_export_csv() to parse data_export.csv first."
            )
        output_dir = Path(context.get("data_dir") or context.get("output_dir", "."))
        output_dir.mkdir(parents=True, exist_ok=True)

        rawdata_path  = output_dir / "rawdata.csv"
        metadata_path = output_dir / "metadata.json"

        logger.info("Parsing metadata (export mode) …")
        metadata = parse_metadata(definition)
        save_json(metadata, metadata_path)

        metadata = patch_metadata(metadata_path, metadata)

        logger.info("Converting export to rawdata …")
        df = convert_export_to_rawdata(export_df, output_dir)

        # Apply profile_status filter (mirrors old-mode parse_rows behaviour)
        profile_status = context.get("profile_status", ["approved", "pending"])
        if profile_status and "profile_status" in df.columns:
            allowed = {s.lower() for s in profile_status}
            before  = len(df)
            df      = df[df["profile_status"].str.lower().isin(allowed)].reset_index(drop=True)
            logger.info(
                "profile_status filter %s: %d → %d rows", allowed, before, len(df)
            )

        recode_area_questions(df, metadata, metadata_path)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

        save_csv(df, rawdata_path)
        logger.info("Saved → %s", rawdata_path)

        annotate_rawdata_columns(metadata_path, rawdata_path)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        logger.info("Saved → %s", metadata_path)

        context["rawdata"]       = df
        context["metadata"]      = metadata
        context["rawdata_path"]  = str(rawdata_path)
        context["metadata_path"] = str(metadata_path)
        return context

    def _run_rows(self, context: dict[str, Any]) -> dict[str, Any]:
        """Row-pages ingestion: definition + rows_pages → rawdata.csv + metadata.json."""
        definition = context["definition"]
        rows_pages = context["rows_pages"]
        output_dir = Path(context.get("data_dir") or context.get("output_dir", "."))
        output_dir.mkdir(parents=True, exist_ok=True)

        # 1. metadata
        logger.info("Parsing metadata …")
        metadata = parse_metadata(definition)

        # 2. raw records (API returns numeric codes directly)
        profile_status = context.get("profile_status", ["approved", "pending"])
        logger.info(
            "Parsing %d row page(s) [filter: %s] …",
            len(rows_pages),
            profile_status or "all",
        )
        # Build {question_id: [other_choice_codes]} for other_text column naming
        other_codes_map: dict[int, list[str]] = {
            meta["question_id"]: meta["other_choice_codes"]
            for meta in metadata["questions"].values()
            if meta.get("other_choice_codes")
        }
        raw_records = parse_rows(
            rows_pages, definition, profile_status,
            other_codes_map or None,
        )
        logger.info("  → %d respondent rows", len(raw_records))

        # 3. encode answers to numeric codes
        encoded_records = encode_records(raw_records, metadata)

        # 4. DataFrame
        df = records_to_dataframe(encoded_records, definition, metadata)

        # 6. save
        rawdata_path  = output_dir / "rawdata.csv"
        metadata_path = output_dir / "metadata.json"
        save_csv(df, rawdata_path)
        save_json(metadata, metadata_path)

        logger.info("Saved → %s", rawdata_path)
        logger.info("Saved → %s", metadata_path)

        context["rawdata"]       = df
        context["metadata"]      = metadata
        context["rawdata_path"]  = str(rawdata_path)
        context["metadata_path"] = str(metadata_path)
        return context
        