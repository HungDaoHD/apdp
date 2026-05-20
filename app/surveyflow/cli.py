"""CLI entry point for surveyflow.

Installed as ``surveyflow-run`` after ``pip install surveyflow``.
Also callable directly via ``python run_pipeline.py`` in the project root.

Folder layout
-------------
output/SURVEY_NAME/
├── mcp/                ← definition.json + rows_page_*.json OR data_export.csv
├── data/               ← rawdata.csv + metadata.json (generated once from mcp/)
├── datatable/          ← datatable.json (managed by Claude)
├── v1/                 ← datatable.xlsx only
└── v2/

Usage — old mode (get_survey_rows)::

    surveyflow-run \\
        --mcp-dir    output/VN8947/mcp \\
        --output-dir output/VN8947     \\
        --version    v1

Usage — new mode (prepare_survey_data_file)::

    surveyflow-run \\
        --mcp-dir    output/VN8947/mcp              \\
        --export-csv output/VN8947/mcp/data_export.csv \\
        --output-dir output/VN8947                  \\
        --version    v1

Usage — table-only (data/ already exists)::

    surveyflow-run \\
        --output-dir output/VN8947 \\
        --version    v2

Usage — force re-ingestion::

    surveyflow-run \\
        --mcp-dir         output/VN8947/mcp \\
        --output-dir      output/VN8947     \\
        --version         v3                \\
        --force-ingestion
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def _load_mcp_rows(mcp_dir: Path) -> tuple[dict, list[dict]]:
    """Old mode: load definition.json + rows_page_*.json from *mcp_dir*."""
    def_path = mcp_dir / "definition.json"
    if not def_path.exists():
        raise FileNotFoundError(f"definition.json not found in {mcp_dir}")

    with def_path.open(encoding="utf-8") as f:
        definition = json.load(f)
    with def_path.open("w", encoding="utf-8") as f:
        json.dump(definition, f, ensure_ascii=False, indent=2)

    rows_files = sorted(
        p for p in mcp_dir.glob("rows_page_*.json")
        if not p.stem.endswith("_text")
    )
    if not rows_files:
        raise FileNotFoundError(f"No rows_page_*.json found in {mcp_dir}")

    rows_pages = []
    for p in rows_files:
        with p.open(encoding="utf-8") as f:
            page = json.load(f)
        with p.open("w", encoding="utf-8") as f:
            json.dump(page, f, ensure_ascii=False, indent=2)
        rows_pages.append(page)

    logging.info("Loaded definition + %d row page(s) from %s", len(rows_pages), mcp_dir)
    return definition, rows_pages


def _load_mcp_export(mcp_dir: Path, export_csv: Path) -> tuple[dict, "pd.DataFrame"]:
    """New mode: load definition.json + parse data_export.csv."""
    from surveyflow.steps.ingestion.export_parser import parse_export_csv

    def_path = mcp_dir / "definition.json"
    if not def_path.exists():
        raise FileNotFoundError(f"definition.json not found in {mcp_dir}")
    if not export_csv.exists():
        raise FileNotFoundError(f"export CSV not found: {export_csv}")

    with def_path.open(encoding="utf-8") as f:
        definition = json.load(f)
    with def_path.open("w", encoding="utf-8") as f:
        json.dump(definition, f, ensure_ascii=False, indent=2)

    export_df = parse_export_csv(export_csv)
    logging.info("Loaded definition + export CSV (%d rows) from %s", len(export_df), mcp_dir)
    return definition, export_df


def main(argv: list[str] | None = None) -> None:
    from surveyflow import Pipeline, PipelineConfig

    parser = argparse.ArgumentParser(
        prog="surveyflow-run",
        description="SurveyFlow Pipeline Runner",
    )
    parser.add_argument(
        "--mcp-dir",
        default=None,
        help="Folder with definition.json (required for ingestion in both modes)",
    )
    parser.add_argument(
        "--export-csv",
        default=None,
        help="Path to data_export.csv (new mode: prepare_survey_data_file). "
             "If omitted, old mode (rows_page_*.json) is used.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Base output folder (data/, datatable/, vX/ are created inside)",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Folder for rawdata.csv + metadata.json (default: {output_dir}/data)",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="Version tag for datatable.xlsx output, e.g. v1 (default: auto timestamp)",
    )
    parser.add_argument(
        "--datatable-config",
        default=None,
        help="Path to datatable.json (default: {output_dir}/datatable/datatable.json)",
    )
    parser.add_argument(
        "--force-ingestion",
        action="store_true",
        help="Re-run ingestion even if data/ already exists",
    )
    parser.add_argument(
        "--profile-status",
        default="approved",
        help="Comma-separated statuses e.g. approved,pending",
    )
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    data_dir   = Path(args.data_dir) if args.data_dir else output_dir / "data"
    statuses   = [s.strip() for s in args.profile_status.split(",") if s.strip()]

    rawdata_exists = (data_dir / "rawdata.csv").exists()
    skip_ingestion = rawdata_exists and not args.force_ingestion

    # ── Load source files when ingestion is needed ─────────────────────────────
    definition, rows_pages, export_df = None, None, None

    if not skip_ingestion:
        if not args.mcp_dir:
            parser.error(
                "--mcp-dir is required when ingestion needs to run "
                f"(data not found at {data_dir} or --force-ingestion was set)"
            )
        mcp_dir = Path(args.mcp_dir)

        if args.export_csv:
            # New mode: definition + data_export.csv
            definition, export_df = _load_mcp_export(mcp_dir, Path(args.export_csv))
        else:
            # Old mode: definition + rows_page_*.json
            definition, rows_pages = _load_mcp_rows(mcp_dir)

    # Resolve datatable config path
    datatable_config = args.datatable_config
    if datatable_config is None:
        default_dt = output_dir / "datatable" / "datatable.json"
        if default_dt.exists():
            datatable_config = str(default_dt)

    result = Pipeline(PipelineConfig(
        definition       = definition,
        rows_pages       = rows_pages,
        export_df        = export_df,
        output_dir       = str(output_dir),
        data_dir         = str(data_dir),
        skip_ingestion   = skip_ingestion,
        profile_status   = statuses,
        version          = args.version,
        datatable_config = datatable_config,
    )).run()

    print("\n--- Output ---")
    print(f"  rawdata  : {result['rawdata_path']}")
    print(f"  metadata : {result['metadata_path']}")
    if "datatable_path" in result:
        print(f"  datatable: {result['datatable_path']}")
    print(f"  rows     : {result['rawdata'].shape[0]}")
    print(f"  version  : {result['version']}")


if __name__ == "__main__":
    main()
