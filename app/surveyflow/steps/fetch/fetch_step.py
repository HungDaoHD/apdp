"""FetchStep — fetches survey definition + response data from a QMe MCP client.

Two modes
---------
``rows``   (default)
    Calls ``get_survey_definition`` then pages through ``get_survey_rows``
    (offset += limit until ``has_more`` is false).
    Saves:
      - ``<mcp_dir>/definition.json``
      - ``<mcp_dir>/rows_page_1.json``, ``rows_page_2.json``, …

``export``
    Calls ``get_survey_definition`` then triggers a full-export job via
    ``prepare_survey_data_file``, polls until ready, and reads the CSV
    in chunks via ``read_survey_data_file``.
    Saves:
      - ``<mcp_dir>/definition.json``
      - ``<mcp_dir>/data_export.csv``

Client interface (duck-typed)
-----------------------------
The ``client`` object passed via context must expose:

    client.get_survey_definition(survey_id)
    client.get_survey_rows(survey_id, date_from, date_to,
                           offset, limit, format, profile_status)
    client.prepare_survey_data_file(survey_id, format, force_refresh)
    client.get_survey_data_file_status(job_id)
    client.read_survey_data_file(job_id, file, offset, limit)

Context inputs
--------------
client          : object  duck-typed MCP client (required)
survey_id       : int     QMe survey ID (required)
mcp_dir         : str     destination folder for saved files (required)
mode            : str     "rows" (default) or "export"
date_from       : str     YYYY-MM-DD, default "2020-01-01"  (rows mode)
date_to         : str     YYYY-MM-DD, default "2030-12-31"  (rows mode)
rows_limit      : int     page size for rows mode, default 200, max 200
profile_status  : list    e.g. ["approved"], default ["approved"] (rows mode)
force_refresh   : bool    whether to force a fresh export job (export mode)
chunk_limit     : int     rows per chunk in export mode, default 500

Context outputs (rows mode)
---------------------------
mcp_dir         : str     path where files were saved
definition      : dict    parsed definition.json
rows_pages      : list    list of parsed rows_page_*.json dicts

Context outputs (export mode)
------------------------------
mcp_dir         : str     path where files were saved
definition      : dict    parsed definition.json
export_csv      : str     path to saved data_export.csv
"""

from __future__ import annotations

import csv as csv_mod
import io
import json
import logging
import time
from pathlib import Path
from typing import Any

from surveyflow.core.base import Step

logger = logging.getLogger(__name__)


class FetchStep(Step):

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        mode = context.get("mode", "rows")
        if mode == "export":
            return self._fetch_export(context)
        return self._fetch_rows(context)

    # ── rows mode ─────────────────────────────────────────────────────────────

    def _fetch_rows(self, context: dict[str, Any]) -> dict[str, Any]:
        client        = context["client"]
        survey_id     = context["survey_id"]
        mcp_dir       = Path(context["mcp_dir"])
        date_from     = context.get("date_from", "2020-01-01")
        date_to       = context.get("date_to",   "2030-12-31")
        limit         = int(context.get("rows_limit", 200))
        profile_status = context.get("profile_status", ["approved"])

        mcp_dir.mkdir(parents=True, exist_ok=True)

        # 1. Definition
        logger.info("[fetch-rows] Fetching definition …")
        definition = client.get_survey_definition(survey_id)
        (mcp_dir / "definition.json").write_text(
            json.dumps(definition, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("  → definition.json saved")

        # 2. Row pages
        logger.info("[fetch-rows] Fetching rows …")
        page, offset = 1, 0
        rows_pages: list[dict] = []
        MAX_PAGES = 500
        while page <= MAX_PAGES:
            rows_data = client.get_survey_rows(
                survey_id,
                date_from=date_from,
                date_to=date_to,
                offset=offset,
                limit=limit,
                format="code",
                profile_status=profile_status,
            )
            page_path = mcp_dir / f"rows_page_{page}.json"
            page_path.write_text(
                json.dumps(rows_data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            rows_pages.append(rows_data)
            count = len(rows_data.get("rows", []))
            logger.info("  → page %d: %d rows", page, count)

            if not rows_data.get("pagination", {}).get("has_more"):
                break
            if count == 0:
                logger.warning("[fetch-rows] has_more=True but 0 rows returned — stopping to prevent infinite loop")
                break
            offset += count
            page   += 1
        else:
            logger.warning("[fetch-rows] Reached MAX_PAGES=%d — stopped early", MAX_PAGES)

        logger.info("  Done → %s (%d pages)", mcp_dir, page)

        context["mcp_dir"]    = str(mcp_dir)
        context["definition"] = definition
        context["rows_pages"] = rows_pages
        return context

    # ── export mode ───────────────────────────────────────────────────────────

    def _fetch_export(self, context: dict[str, Any]) -> dict[str, Any]:
        client        = context["client"]
        survey_id     = context["survey_id"]
        mcp_dir       = Path(context["mcp_dir"])
        force_refresh = context.get("force_refresh", True)
        chunk_limit   = int(context.get("chunk_limit", 500))

        mcp_dir.mkdir(parents=True, exist_ok=True)

        # 1. Definition
        logger.info("[fetch-export] Fetching definition …")
        definition = client.get_survey_definition(survey_id)
        (mcp_dir / "definition.json").write_text(
            json.dumps(definition, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("  → definition.json saved")

        # 2. Trigger export job
        logger.info("[fetch-export] Triggering data export (format=code) …")
        prep   = client.prepare_survey_data_file(survey_id, format="code",
                                                  force_refresh=force_refresh)
        job_id = prep.get("job_id")
        if not job_id:
            logger.debug("[fetch-export] prepare_survey_data_file response: %s", prep)
            raise RuntimeError(
                "[fetch-export] prepare_survey_data_file returned no job_id"
            )
        logger.info("  job_id=%s  status=%s", job_id, prep.get("status"))

        # 3. Poll until ready
        logger.info("[fetch-export] Waiting for export job to be ready …")
        MAX_POLL  = 120          # max polling attempts
        MAX_SLEEP = 60           # cap sleep to 60 s regardless of API suggestion
        for _poll in range(MAX_POLL):
            status_resp = client.get_survey_data_file_status(job_id)
            status      = status_resp.get("status", "")
            logger.info("  status=%s", status)
            if status == "ready":
                break
            if status == "error":
                logger.debug("[fetch-export] job status response: %s", status_resp)
                raise RuntimeError("[fetch-export] Export job failed")
            wait = min(int(status_resp.get("retry_after_seconds", 5)), MAX_SLEEP)
            time.sleep(wait)
        else:
            raise RuntimeError(
                f"[fetch-export] Export job did not complete after {MAX_POLL} polls"
            )

        # 4. Read all CSV chunks
        logger.info("[fetch-export] Reading data CSV …")
        data_csv = self._read_all_chunks(client, job_id, "data", chunk_limit)

        # Write with BOM so Excel/Windows opens as UTF-8 without font errors
        export_path = mcp_dir / "data_export.csv"
        export_path.write_text(data_csv, encoding="utf-8-sig")
        row_count = max(0, data_csv.count("\n") - 1)
        logger.info("  → data_export.csv saved  (~%d data rows)", row_count)

        logger.info("  Done → %s", mcp_dir)

        context["mcp_dir"]    = str(mcp_dir)
        context["definition"] = definition
        context["export_csv"] = str(export_path)
        return context

    # ── chunk reader (shared) ─────────────────────────────────────────────────

    @staticmethod
    def _read_all_chunks(client: Any, job_id: str,
                         file_type: str, limit: int) -> str:
        """Read all chunks for *file_type*, return assembled CSV string.

        Handles the various shapes the MCP tool may return:
          - plain str          → raw CSV text
          - dict.csv / .content → CSV text field
          - dict.rows (str[])  → list of CSV row strings
          - dict.rows (list[]) → list of row arrays / dicts → rendered via csv module
        """
        MAX_CHUNKS = 2000
        parts: list[str] = []
        offset    = 0
        chunk_num = 0

        while chunk_num < MAX_CHUNKS:
            chunk_num += 1
            chunk = client.read_survey_data_file(
                job_id, file=file_type, offset=offset, limit=limit
            )

            if isinstance(chunk, str):
                parts.append(chunk)
                break  # plain text — no pagination info, assume complete

            if isinstance(chunk, dict):
                csv_text = chunk.get("csv") or chunk.get("content") or ""
                rows_val = chunk.get("rows", [])

                if csv_text:
                    # Ensure trailing newline for clean concatenation
                    parts.append(csv_text if csv_text.endswith("\n") else csv_text + "\n")

                elif rows_val:
                    if isinstance(rows_val[0], str):
                        # List of pre-formatted CSV row strings
                        parts.append("\n".join(rows_val) + "\n")
                    elif isinstance(rows_val[0], (list, dict)):
                        buf = io.StringIO()
                        if isinstance(rows_val[0], dict):
                            writer = csv_mod.DictWriter(buf, fieldnames=list(rows_val[0].keys()))
                            if offset == 0:
                                writer.writeheader()
                            writer.writerows(rows_val)
                        else:
                            writer = csv_mod.writer(buf)
                            writer.writerows(rows_val)
                        parts.append(buf.getvalue())

                pagination      = chunk.get("pagination", {})
                rows_remaining  = pagination.get("rows_remaining", 0)
                has_more        = pagination.get("has_more", False)
                next_offset     = pagination.get("next_offset", offset + limit)

                logger.info(
                    "  [%s] chunk %d: offset=%d  remaining=%d",
                    file_type, chunk_num, offset, rows_remaining,
                )

                if not has_more or rows_remaining == 0:
                    break
                offset = next_offset

            else:
                # Unknown shape — stop
                logger.warning(
                    "[fetch-export] Unexpected chunk type %s at offset=%d — stopping.",
                    type(chunk).__name__, offset,
                )
                break

        else:
            logger.warning(
                "[fetch-export] Reached MAX_CHUNKS=%d for file_type=%s — stopped early",
                MAX_CHUNKS, file_type,
            )

        return "".join(parts)
