"""Pipeline configuration."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class PipelineConfig:
    """Configuration for a SurveyFlow pipeline run.

    Parameters
    ----------
    definition : dict | None
        Raw response from get_survey_definition. Required when skip_ingestion=False.
    rows_pages : list[dict] | None
        Paginated raw responses from get_survey_rows. Required when skip_ingestion=False.
    output_dir : str
        Base output directory. Version subfolder (vX/) is created inside for datatable.xlsx.
    data_dir : str | None
        Directory where rawdata.csv + metadata.json are stored (generated once).
        Defaults to ``{output_dir}/data``.
    skip_ingestion : bool
        When True, skip ingestion and read rawdata/metadata from data_dir.
        When False (default), run ingestion and write to data_dir.
    profile_status : list[str]
        Filter rows by task profile status.
        Allowed: "pending", "approved", "rejected", "screen_out".
        Default ["approved"]. Empty list = no filter (all statuses).
    datatable_config : str | dict | None
        Path to datatable.json  OR  already-parsed dict.
        When provided the TableStep runs and produces datatable.xlsx in the version folder.
        When None only rawdata.csv + metadata.json are produced (ingestion only).
    version : str | None
        Version tag for the output folder.
        - If provided (e.g. "v1", "v2"): datatable.xlsx goes to ``{output_dir}/v1/``
        - If None: auto-generates a timestamp folder ``{output_dir}/20260408_091623/``
    """

    definition:       Optional[dict]       = None
    rows_pages:       Optional[list[dict]] = None
    export_df:        Optional[Any]        = None   # pd.DataFrame from parse_export_csv
    output_dir:       str                  = "."
    data_dir:         Optional[str]        = None
    skip_ingestion:   bool                 = False
    skip_render:      bool                 = False  # True → compute only, no xlsx output
    profile_status:   list[str]            = field(default_factory=lambda: ["approved", "pending"])
    datatable_config: Optional[str | dict] = None
    version:          Optional[str]        = None
    table_indices:    Optional[list[int]]  = None   # None = all tables; [0,2] = only table 0 and 2
