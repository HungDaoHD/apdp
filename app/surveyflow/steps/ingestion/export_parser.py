"""Parse data_export.csv (prepare_survey_data_file) → rawdata.csv + annotated metadata."""

from __future__ import annotations

import csv as _csv
import io as _io
import json
import logging
import pathlib
import re

logger = logging.getLogger(__name__)

# ── Personal information columns — always excluded from rawdata.csv ──────────

PERSONAL_COLS: set[str] = {
    "Panel ID", "Panel FB", "Panel Email", "Panel Phone",
    "Panel Age", "Panel Gender", "Panel Area", "Panel Income",
    "Login ID", "User name", "IP address (Public user)",
    "Contact person", "Email", "Telephone number", "Manager",
    "Others 1", "Others 2", "Others 3", "Others 4",
    "User Latitude", "User Longitude",
    "Store Latitude", "Store Longitude",
    "Detect fake GPS",
    "Check in", "Check in system", "Check out", "Distance",
    "PVV",
    "phone",
}

# ── Area-recode constants ────────────────────────────────────────────────────

_AREA_ANCHOR: dict[str, int] = {
    "Hồ Chí Minh": 1, "Ho Chi Minh": 1, "Ho Chi Minh City": 1,
    "TP.HCM": 1, "TP Hồ Chí Minh": 1, "Thành phố Hồ Chí Minh": 1, "TPHCM": 1,
    "Hà Nội": 2, "Ha Noi": 2, "Hanoi": 2,
}

_AREA_CANONICAL: dict[int, dict] = {
    1: {"vi": "Hồ Chí Minh", "en": "Ho Chi Minh City"},
    2: {"vi": "Hà Nội",      "en": "Hanoi"},
}

# ── Column-classification helpers ────────────────────────────────────────────

def is_other_col(col: str, label: str) -> bool:
    """True if *col* is an open-ended 'other' column for *label*.

    The suffix after the label must end with ``_o{digits}``; any characters
    between the label and ``_o{digits}`` must be only underscores / digits
    (e.g. a row-code like ``_01``).  This prevents a column like
    ``TVC_aware_source_o9`` from being claimed by label ``TVC_aware``.
    """
    suffix = col[len(label):]
    m = re.search(r"_o(\d+)$", suffix)
    if not m:
        return False
    pre = suffix[: len(suffix) - len(m.group(0))]
    return bool(re.fullmatch(r"[_\d]*", pre))


def row_num_from_col(col: str, parent_label: str) -> int | None:
    """Extract the leading row number from a column suffix after *parent_label*.

    ``'Foo_01'`` → 1,  ``'Foo_01_3'`` → 1,  ``'Foo_01_o5'`` → 1,  ``'Foo'`` → None
    """
    suffix = col[len(parent_label):]
    m = re.match(r"_(\d+)", suffix)
    return int(m.group(1)) if m else None


def _encode_ma_group(df, cols: list) -> None:
    """In-place binary-MA encoding for a group of choice columns.

    Rule: if ANY column in the group is non-null for a row → fill NaN → 0
          (respondent answered the question but did not select this choice).
          If ALL columns are null → keep as NaN (respondent skipped entirely).
    """
    import numpy as np
    import pandas as pd

    numeric = df[cols].apply(pd.to_numeric, errors="coerce")
    all_nan = numeric.isna().all(axis=1)
    filled  = numeric.fillna(0)
    filled.loc[all_nan] = np.nan
    df[cols] = filled


# ── Multiplenumber group detection ───────────────────────────────────────────

_SUB_IDX_RE = re.compile(r"^Q\d+_(\d+)$")


def _patch_multiplenumber_headers(header: list, sub_labels: list) -> list:
    """Rename multiplenumber group columns so each choice gets its own label.

    In data_export.csv a multiplenumber question (e.g. D2 with 2 choices)
    appears as:
      header:    ['D2',    '',      ...]
      sub-label: ['Q25_1', 'Q25_2', ...]

    This renames them to ['D2_1', 'D2_2', ...] so all choice columns are
    preserved.  Detection rule: a non-empty header column has sub-label
    ``Q{n}_1`` and the following columns have empty headers with sub-labels
    ``Q{n}_2``, ``Q{n}_3``, … (same Q-base).
    """
    patched = list(header)
    i = 0
    while i < len(patched):
        h = patched[i]
        s = sub_labels[i] if i < len(sub_labels) else ""
        m = _SUB_IDX_RE.match(s)
        if h and m and m.group(1) == "1":
            q_base = s[: s.rfind("_")]   # e.g. 'Q25'
            group  = [i]
            j = i + 1
            while j < len(patched):
                hj = patched[j]
                sj = sub_labels[j] if j < len(sub_labels) else ""
                mj = _SUB_IDX_RE.match(sj)
                if hj == "" and mj and sj.startswith(q_base + "_"):
                    group.append(j)
                    j += 1
                else:
                    break
            if len(group) > 1:
                for k, col_idx in enumerate(group, 1):
                    patched[col_idx] = f"{h}_{k}"
            i = j
        else:
            i += 1
    return patched


# ── Step 1: parse raw export CSV ─────────────────────────────────────────────

def _find_header_idx(lines: list) -> int:
    """Return the 0-indexed line number of the column header row.

    The header row is identified as the first line whose first CSV field is
    ``'Approve'`` — the standard lead column in QMe data_export.csv files.
    Falls back to index 6 (the known default) if not found.
    """
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        first_field = next(_csv.reader(_io.StringIO(line)))[0]
        if first_field.strip() == "Approve":
            return i
    logger.warning("_find_header_idx: 'Approve' column not found — falling back to index 6")
    return 6


def _find_first_data_idx(lines: list, header: list, header_idx: int) -> int:
    """Return the 0-indexed line number of the first actual data row.

    Uses the ``'ID'`` column (QMe task ID, e.g. ``'723380_2999352'``) as
    the detection signal — it is always non-empty for data rows and always
    empty for annotation / sub-header rows.

    This makes the parser robust to both export formats from ``FetchStep``:
    * ``csv`` field  → full native CSV with blank lines between rows
    * ``rows`` field → list of strings joined by ``\\n`` (no blank lines)

    Falls back to ``header_idx + 6`` if ``'ID'`` column is not found.
    """
    try:
        id_col = next(i for i, h in enumerate(header) if h.strip() == "ID")
    except StopIteration:
        logger.warning("_find_first_data_idx: 'ID' column not found — falling back to header+6")
        return header_idx + 6

    for i in range(header_idx + 1, min(header_idx + 30, len(lines))):
        line = lines[i]
        if not line.strip():
            continue
        row = next(_csv.reader(_io.StringIO(line)))
        if id_col < len(row) and row[id_col].strip():
            return i

    logger.warning("_find_first_data_idx: no data row found within 30 lines — falling back to header+6")
    return header_idx + 6


def parse_export_csv(data_export: pathlib.Path) -> "pd.DataFrame":
    """Parse *data_export.csv* (from ``prepare_survey_data_file``) into a clean DataFrame.

    Handles two response formats from ``FetchStep._read_all_chunks``:

    * **csv field** (local / full export): native QMe CSV with blank lines
      between every row.  Header is at a fixed offset; annotations follow
      with interleaved blanks.

    * **rows field** (production): list of strings joined by ``\\n`` — no
      blank lines.  Same annotation rows exist but are consecutive.

    Both formats are handled by auto-detecting the header line
    (``_find_header_idx``) and the first data line (``_find_first_data_idx``).
    """
    import pandas as pd

    with open(data_export, encoding="utf-8-sig") as f:
        lines = f.read().splitlines()

    HEADER_IDX = _find_header_idx(lines)

    # Parse header before computing SUB_LABEL_IDX / FIRST_DATA_IDX so both
    # helpers can reference the column list.
    raw_header = lines[HEADER_IDX].split(",")

    # Sub-label row: scan the 10 lines after the header for the first non-blank
    # line that contains Q{n}_{k} sub-labels (used for multiplenumber detection).
    sub_labels: list = []
    for i in range(HEADER_IDX + 1, min(HEADER_IDX + 10, len(lines))):
        line = lines[i]
        if not line.strip():
            continue
        fields = line.split(",")
        if any(_SUB_IDX_RE.match(f.strip()) for f in fields if f.strip()):
            sub_labels = fields
            break

    header         = _patch_multiplenumber_headers(raw_header, sub_labels)
    FIRST_DATA_IDX = _find_first_data_idx(lines, raw_header, HEADER_IDX)

    logger.debug(
        "parse_export_csv: header=%d  first_data=%d  cols=%d",
        HEADER_IDX, FIRST_DATA_IDX, len(header),
    )

    data_rows = []
    for line in lines[FIRST_DATA_IDX:]:
        if not line.strip():
            continue
        row = next(_csv.reader(_io.StringIO(line)))
        if len(row) < len(header):
            row += [""] * (len(header) - len(row))
        data_rows.append(row[:len(header)])

    return pd.DataFrame(data_rows, columns=header)


# ── Step 2: convert export columns → rawdata layout ─────────────────────────

def convert_export_to_rawdata(
    df: "pd.DataFrame",
    data_dir: pathlib.Path,
) -> "pd.DataFrame":
    """Map data_export columns → table-compatible rawdata.csv.

    Output column order::

        task_id  profile_status  date_time  Task duration
        <question columns — only those matching metadata.json labels>

    Personal-info columns (see ``PERSONAL_COLS``) are always excluded.
    Uses *longest-label-wins* matching to prevent a parent label (e.g.
    ``Usagerate_Catfoodtypes``) from claiming child-label columns
    (``Usagerate_Catfoodtypes_Amount_*``).

    Binary-MA encoding is applied in-place before returning:
    all-null rows stay NaN (skipped question); answered rows have
    unselected choices filled to 0.
    """
    import pandas as pd

    df = df.copy()

    # Rename system columns
    rename_map: dict[str, str] = {}
    if "ID"   in df.columns: rename_map["ID"]   = "task_id"
    if "Date" in df.columns: rename_map["Date"] = "date_time"
    df = df.rename(columns=rename_map)

    # Filter out rejected rows
    if "Reject" in df.columns:
        before = len(df)
        df = df[df["Reject"].str.strip().str.lower() != "x"].reset_index(drop=True)
        removed = before - len(df)
        if removed:
            logger.info("[convert] filtered out %d rejected row(s)", removed)

    # Derive profile_status: approved if Approve=x, else pending
    def _status(row) -> str:
        if str(row.get("Approve", "")).strip().lower() == "x":
            return "approved"
        return "pending"

    df["profile_status"] = df.apply(_status, axis=1)

    # Load question labels from metadata.json
    meta_path = data_dir / "metadata.json"
    question_labels: list[str] = []
    meta: dict = {}
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        for q in meta.get("questions", {}).values():
            lbl = q.get("label", "").strip()
            if lbl:
                question_labels.append(lbl)

    # Longest-label-wins: each export column → best matching question label
    col_to_lbl: dict[str, str] = {}
    for col in df.columns:
        best_lbl, best_len = "", -1
        for lbl in question_labels:
            if (col == lbl or col.startswith(lbl + "_")) and len(lbl) > best_len:
                best_lbl, best_len = lbl, len(lbl)
        if best_lbl:
            col_to_lbl[col] = best_lbl

    question_cols = [
        col for col in df.columns
        if col_to_lbl.get(col) and col not in PERSONAL_COLS
    ]

    SYSTEM_KEEP = ["task_id", "profile_status", "date_time", "Task duration"]
    system_cols = [c for c in SYSTEM_KEEP if c in df.columns]

    final_cols = system_cols + [c for c in question_cols if c not in system_cols]
    logger.info(
        "[convert] keeping %d system + %d question cols (from %d export cols)",
        len(system_cols), len(question_cols), len(df.columns),
    )
    df = df[final_cols]

    # Binary-MA encoding (null→0 for answered rows; all-null stays NaN)
    for q in meta.get("questions", {}).values():
        lbl   = q.get("label", "")
        atype = q.get("answer_type", "")
        if atype == "MA":
            group = [c for c in df.columns
                     if col_to_lbl.get(c) == lbl and not is_other_col(c, lbl)]
            if group:
                _encode_ma_group(df, group)
        elif atype == "Matrix_MA":
            q_cols = [c for c in df.columns
                      if col_to_lbl.get(c) == lbl and not is_other_col(c, lbl)]
            row_groups: dict[int, list] = {}
            for c in q_cols:
                rn = row_num_from_col(c, lbl)
                if rn is not None:
                    row_groups.setdefault(rn, []).append(c)
            for row_cols in row_groups.values():
                _encode_ma_group(df, row_cols)
        elif atype == "multiplenumber":
            group = [c for c in df.columns
                     if col_to_lbl.get(c) == lbl and not is_other_col(c, lbl)]
            for col in group:
                import pandas as _pd
                df[col] = (
                    df[col].astype(str)
                    .str.replace(r"[%\s]", "", regex=True)
                    .replace("", None)
                    .pipe(_pd.to_numeric, errors="coerce")
                )

    return df


# ── Step 3b: patch metadata.json ─────────────────────────────────────────────

_SYSTEM_ENTRIES: dict[str, dict] = {
    "profile_status": {
        "label": "profile_status",
        "answer_type": "system",
        "question_i18n": {"en": "Profile Status", "vi": "Trạng thái"},
    },
    "date_time": {
        "label": "date_time",
        "answer_type": "system",
        "question_i18n": {"en": "Date Time", "vi": "Ngày giờ"},
    },
    "Task duration": {
        "label": "Task duration",
        "answer_type": "system",
        "question_i18n": {"en": "Task Duration", "vi": "Thời lượng"},
    },
}


def patch_metadata(meta_path: pathlib.Path, metadata: dict) -> dict:
    """Inject system_columns, remove personal-info questions, reorder keys.

    Writes the updated metadata back to *meta_path* and returns the dict.
    """
    merged_sys = {**metadata.get("system_columns", {})}
    merged_sys.update({k: v for k, v in _SYSTEM_ENTRIES.items() if k not in merged_sys})

    questions_raw   = metadata.get("questions", {})
    questions_clean = {
        qid: q for qid, q in questions_raw.items()
        if q.get("label", "") not in PERSONAL_COLS
    }

    # Rebuild with explicit key order: all header fields → system_columns → questions
    header_keys  = [k for k in metadata if k not in ("system_columns", "questions")]
    ordered_meta: dict = {k: metadata[k] for k in header_keys}
    ordered_meta["system_columns"] = merged_sys
    ordered_meta["questions"]      = questions_clean

    meta_path.write_text(
        json.dumps(ordered_meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    removed = [q["label"] for q in questions_raw.values()
               if q.get("label", "") in PERSONAL_COLS]
    if removed:
        logger.info("patch_metadata: removed personal questions %s", removed)
    return ordered_meta


# ── Step 4b: recode area questions ───────────────────────────────────────────

def recode_area_questions(
    df: "pd.DataFrame",
    meta: dict,
    meta_path: pathlib.Path,
) -> None:
    """In-place recode ``synthetic_type='area'`` columns; update ``choices_i18n``.

    Rules:
    - Only cities present in data are assigned codes.
    - If HCM variant found → code 1; if HN variant found → code 2.
    - Codes 1 and 2 are always reserved (other cities can never get them).
    - Other cities → next available code ≥ 3 (auto-assigned).
    - Values already numeric → kept as-is; their label is registered if unknown.
    Writes updated metadata to *meta_path*.
    """
    changed = False

    for q in meta.get("questions", {}).values():
        if q.get("synthetic_type") != "area":
            continue
        lbl = q.get("label", "")
        if lbl not in df.columns:
            continue

        used_codes: set[int] = {1, 2}   # always reserved for HCM / HN
        new_choices: dict    = {}
        recode_map:  dict    = {}

        for val in df[lbl].dropna().unique():
            val_str = str(val).strip()

            # Already a numeric code → keep, register label if new
            try:
                code = int(float(val_str))
                recode_map[val] = code
                if str(code) not in new_choices:
                    new_choices[str(code)] = _AREA_CANONICAL.get(
                        code, {"vi": val_str, "en": val_str}
                    )
                used_codes.add(code)
                continue
            except (ValueError, TypeError):
                pass

            # Text → fixed anchor or auto-assign ≥ 3
            if val_str in _AREA_ANCHOR:
                code = _AREA_ANCHOR[val_str]
            else:
                candidate = 3
                while candidate in used_codes:
                    candidate += 1
                code = candidate
                logger.info("  [area recode] %s: '%s' → code %d", lbl, val_str, code)

            used_codes.add(code)
            recode_map[val] = code
            new_choices[str(code)] = _AREA_CANONICAL.get(
                code, {"vi": val_str, "en": val_str}
            )

        if recode_map:
            df[lbl] = df[lbl].map(recode_map)
            q["choices_i18n"] = new_choices
            changed = True
            logger.info("  [area recode] %s: %d value(s) recoded", lbl, len(recode_map))

    if changed:
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )


# ── Step 5: annotate metadata with rawdata_columns ───────────────────────────

def annotate_rawdata_columns(
    meta_path: pathlib.Path,
    rawdata_csv: pathlib.Path,
) -> None:
    """Add ``rawdata_columns`` / ``rawdata_other_columns`` per question in metadata.

    Also:
    - Fixes ``children`` and ``sub_questions`` keys to use label-based format
      (``Check_Br_r1``) with ``rawdata_columns`` per sub-question.
    - Prunes ``other_choice_codes``: removes codes that have no matching
      ``{label}_o{code}`` column in rawdata.csv.

    Rewrites *meta_path* in-place.
    """
    with open(rawdata_csv, encoding="utf-8-sig") as fh:
        rawdata_col_list: list[str] = next(_csv.reader(fh))
    rawdata_col_set = set(rawdata_col_list)

    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    # Longest-label-wins: col → best matching question label
    all_labels = {q.get("label", "") for q in meta["questions"].values() if q.get("label")}
    col_to_best: dict[str, str] = {}
    for col in rawdata_col_list:
        best_lbl, best_len = "", -1
        for lbl in all_labels:
            if (col == lbl or col.startswith(lbl + "_")) and len(lbl) > best_len:
                best_lbl, best_len = lbl, len(lbl)
        if best_lbl:
            col_to_best[col] = best_lbl

    pruned_count = 0

    for q in meta.get("questions", {}).values():
        lbl = q.get("label", "")
        if not lbl:
            continue

        all_cols = [c for c in rawdata_col_list if col_to_best.get(c) == lbl]
        regular  = [c for c in all_cols if not is_other_col(c, lbl)]
        other    = [c for c in all_cols if     is_other_col(c, lbl)]

        q["rawdata_columns"] = regular
        if other:
            q["rawdata_other_columns"] = other
        elif "rawdata_other_columns" in q:
            del q["rawdata_other_columns"]

        # Prune other_choice_codes: keep only codes with a matching _o{code} column
        orig_codes = q.get("other_choice_codes", [])
        if orig_codes:
            pat_base = re.escape(lbl)
            valid = [
                c for c in orig_codes
                if any(
                    re.match(pat_base + r".*_o" + re.escape(str(c)) + r"$", col)
                    for col in rawdata_col_set
                )
            ]
            if len(valid) != len(orig_codes):
                q["other_choice_codes"] = valid
                pruned_count += 1

        if "rawdata_other_columns" in q and not q["rawdata_other_columns"]:
            del q["rawdata_other_columns"]

        # Fix children / sub_questions keys → label-based format + rawdata_columns
        sub_qs = q.get("sub_questions", {})
        if not sub_qs:
            continue

        parent_regular  = q.get("rawdata_columns", [])
        parent_other    = q.get("rawdata_other_columns", [])
        all_parent_cols = parent_regular + parent_other

        new_sub_qs: dict  = {}
        new_children: list = []

        for sq in sub_qs.values():
            sq_lbl  = sq.get("label", "")
            row_idx = sq.get("row_index")
            new_key = sq_lbl or list(sub_qs.keys())[list(sub_qs.values()).index(sq)]

            if row_idx is not None:
                rn     = int(row_idx)
                sq_all = [c for c in all_parent_cols if row_num_from_col(c, lbl) == rn]
                sq_reg = [c for c in sq_all if not is_other_col(c, lbl)]
                sq_oth = [c for c in sq_all if     is_other_col(c, lbl)]
                sq["rawdata_columns"] = sq_reg
                if sq_oth:
                    sq["rawdata_other_columns"] = sq_oth
                elif "rawdata_other_columns" in sq:
                    del sq["rawdata_other_columns"]

            new_sub_qs[new_key] = sq
            new_children.append(new_key)

        q["children"]      = new_children
        q["sub_questions"] = new_sub_qs

    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(
        "annotate_rawdata_columns: %d questions annotated, %d had other_choice_codes pruned",
        len(meta["questions"]), pruned_count,
    )
