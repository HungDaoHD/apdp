"""Parse Qme survey rows into flat records, then encode answers to numeric codes."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Step 1 – raw-text serialisers (row → str)
# ─────────────────────────────────────────────

def _fmt_text(answer: Any) -> str:
    return str(answer).strip() if answer else ""


def _fmt_multiplechoice(answer: Any) -> str:
    """Semicolon-separated answer_names."""
    if not isinstance(answer, list):
        return _fmt_text(answer)
    return ";".join(a["answer_name"] for a in answer if a.get("answer_name"))


def _fmt_ranking(answer: Any) -> str:
    """Ordered semicolon-separated answer_names (rank-1 first)."""
    if not isinstance(answer, list):
        return _fmt_text(answer)
    return ";".join(a["answer_name"] for a in answer if a.get("answer_name"))


def _fmt_multiplenumber(answer: Any) -> str:
    """answer1:num1|answer2:num2"""
    if not isinstance(answer, list):
        return _fmt_text(answer)
    return "|".join(
        f"{i.get('answer_name','')}:{i.get('number','')}"
        for i in answer
    )


def _fmt_photo(rq: dict) -> str:
    return ";".join(rq.get("images", []))


def _fmt_location(loc: Any) -> str:
    """[lat, lon] → 'lat,lon'"""
    if isinstance(loc, (list, tuple)) and len(loc) == 2:
        return f"{loc[0]},{loc[1]}"
    return str(loc).strip() if loc else ""


_SCALAR_ROW_TYPES = {
    "user-name", "user-phone", "freetext", "singlechoice",
    "date", "area", "singlenumber", "reward",
    "gender", "personal-income",   # special types — answer is text, recoded later
}

# Integer type codes that indicate a matrix question in format=code row data
_MATRIX_ROW_TYPES = {4, 5, 28, 29}

_ROW_TYPE_FORMATTERS = {
    "multiplechoice":  _fmt_multiplechoice,
    "ranking":         _fmt_ranking,
    "multiplenumber":  _fmt_multiplenumber,
}


def _raw_answer(rq: dict) -> str:
    rtype = rq.get("type", "")
    if rtype == "photo":
        return _fmt_photo(rq)
    if rtype in _SCALAR_ROW_TYPES:
        return _fmt_text(rq.get("answer", ""))
    fmt = _ROW_TYPE_FORMATTERS.get(rtype)
    if fmt:
        return fmt(rq.get("answer", ""))
    return _fmt_text(rq.get("answer", ""))


# ─────────────────────────────────────────────
# Question map
# ─────────────────────────────────────────────

def build_question_id_map(definition_questions: list[dict]) -> dict[int, int]:
    """question_id → position."""
    return {q["question_id"]: q["position"] for q in definition_questions if "question_id" in q}


def _get_other_col_code(
    raw_ans: Any,
    other_codes: list[str],
) -> str | None:
    """Determine the 'other' choice code to use for the other_text column name.

    For SA (scalar answer): the answer value IS the selected code.
    For MA semicolon string: parse and find the matching other code.
    For MA (list answer):    find which selected code is in ``other_codes``.
    Returns the code string (e.g. ``"9"``), or ``None`` when undecidable.
    """
    if not other_codes:
        return None
    if isinstance(raw_ans, (int, float)):
        code = str(int(raw_ans))
        return code if code in other_codes else other_codes[0]
    if isinstance(raw_ans, str):
        # format=code MA: semicolon-separated codes e.g. "1;5;11"
        for part in raw_ans.split(";"):
            code = part.strip()
            if code in other_codes:
                return code
        return other_codes[0]   # fallback
    if isinstance(raw_ans, list):
        # format=code MA: items are dicts with "answer_name" = code string
        for item in raw_ans:
            code = str(item.get("answer_name") or item.get("answer_id") or "").strip()
            if code in other_codes:
                return code
        return other_codes[0]   # fallback: first detected other code
    return other_codes[0] if other_codes else None


# ─────────────────────────────────────────────
# Step 1 – parse rows → raw-text records
# ─────────────────────────────────────────────

_ROW_META = (
    "task_id", "date_time", "Key_in_date", "lastmodified_date",
    "profile_status",
)


def _parse_single_row(
    row: dict,
    question_id_map: dict[int, int],
    other_codes_map: dict[int, list[str]] | None = None,
) -> dict:
    record: dict[str, Any] = {
        "task_id":           row.get("task_id", ""),
        "date_time":         row.get("date_time", ""),
        "Key_in_date":       row.get("Key_in_date", ""),
        "lastmodified_date": row.get("lastmodified_date", ""),
        "profile_status":    row.get("profile_status", ""),
    }
    used_pos: set[int] = set()
    for rq in row.get("questions", []):
        # ── resolve position by question_id ───────────────────────────────────
        qid = rq.get("question_id")
        pos: int | None = question_id_map.get(qid) if qid else None

        if pos is None:
            if qid:
                logger.warning("question_id=%s not found in definition — skipped", qid)
            continue

        if pos in used_pos:
            continue
        used_pos.add(pos)

        # ── matrix: expand to q{pos}_r{row} sub-columns ──────────────────────
        # format=code returns {"row": int, "col": int}
        rtype_int = rq.get("type")
        if rtype_int in _MATRIX_ROW_TYPES:
            answer_raw = rq.get("answer")
            row_cols: dict[str, list[str]] = defaultdict(list)
            # Per-cell other_text: some matrix questions embed other_text directly
            # inside each answer item as {"row": r, "col": c, "other_text": "..."}
            # rather than at the top-level rq["other_text"] field.
            cell_others: dict[tuple[str, str], str] = {}
            if isinstance(answer_raw, list):
                for item in answer_raw:
                    r = str(item.get("row") or item.get("vertical_answer", "")).strip()
                    c = str(item.get("col") or item.get("horizontal_answer", "")).strip()
                    if r and c:
                        row_cols[r].append(c)
                        ot_cell = str(item.get("other_text") or "").strip()
                        if ot_cell:
                            cell_others[(r, c)] = ot_cell
                for row_code, col_codes in row_cols.items():
                    record[f"q{pos}_r{row_code}"] = ";".join(col_codes)
            # Write per-cell other_text (new format: other_text inside answer items)
            for (r, c), ot_val in cell_others.items():
                record[f"q{pos}_r{r}_o{c}"] = ot_val

            # ── matrix other_text (legacy format: top-level rq["other_text"]) ──
            _ot = rq.get("other_text") or ""
            other = (";".join(_ot) if isinstance(_ot, list) else _ot).strip()
            if other and row_cols:
                q_other_codes = (other_codes_map or {}).get(qid, [])
                # Find rows that selected an "other" column code
                other_rows = [
                    (rc, next(c for c in cols if c in q_other_codes))
                    for rc, cols in row_cols.items()
                    if any(c in q_other_codes for c in cols)
                ]
                if other_rows:
                    row_code, other_code = other_rows[0]
                    record[f"q{pos}_r{row_code}_o{other_code}"] = other
                else:
                    record[f"q{pos}_other"] = other   # fallback: can't determine row
            continue

        # ── all other types ───────────────────────────────────────────────────
        answer = _raw_answer(rq)
        record[f"q{pos}"] = answer

        # ── other_text ────────────────────────────────────────────────────────
        _ot = rq.get("other_text") or ""
        q_other_codes = (other_codes_map or {}).get(qid, [])

        if isinstance(_ot, list) and _ot and q_other_codes:
            # Multiple other texts — zip with selected other codes (in answer order)
            raw_ans_str = str(rq.get("answer") or "")
            selected_others = [c for c in raw_ans_str.split(";") if c.strip() in q_other_codes]
            for i, code in enumerate(selected_others):
                val = str(_ot[i]).strip() if i < len(_ot) else ""
                if val:
                    record[f"q{pos}_o{code}"] = val
        else:
            other = (";".join(str(x) for x in _ot) if isinstance(_ot, list) else str(_ot)).strip()
            if other:
                code = _get_other_col_code(rq.get("answer"), q_other_codes)
                col_name = f"q{pos}_o{code}" if code else f"q{pos}_other"
                record[col_name] = other

    return record


def parse_rows(
    rows_pages: list[dict],
    definition: dict,
    profile_status: list[str] | None = None,
    other_codes_map: dict[int, list[str]] | None = None,
) -> list[dict]:
    """Return list of raw records (one per respondent).

    Parameters
    ----------
    profile_status
        Whitelist of statuses to keep. Defaults to ``["approved"]``.
        Pass an empty list ``[]`` to include all statuses.
    other_codes_map
        Mapping ``{question_id: [other_choice_code, ...]}``.
        Used to name other_text columns as ``q{pos}_o{code}``.
        Built from metadata by the caller (IngestionStep).
    """
    if profile_status is None:
        profile_status = ["approved", "pending"]

    allowed         = {s.lower() for s in profile_status} if profile_status else None
    def_questions   = definition.get("questions", [])
    question_id_map = build_question_id_map(def_questions)

    records = []
    for page in rows_pages:
        for row in page.get("rows", []):
            if allowed and row.get("profile_status", "").lower() not in allowed:
                continue
            records.append(_parse_single_row(row, question_id_map, other_codes_map))
    return records


# ─────────────────────────────────────────────
# Step 2 – encode answers → numeric codes
# ─────────────────────────────────────────────

from surveyflow.steps.ingestion.metadata_parser import CODEABLE_TYPES, AREA_BASE_CHOICES


def _parse_number(value: Any) -> int | float | str:
    """Parse formatted number strings: '1,200,000' → 1200000, '32' → 32."""
    if value is None or value == "":
        return ""
    s = str(value).replace(",", "").replace(" ", "").strip()
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return str(value).strip()


# ── Old-API helpers (used when definition has no `choices`) ───────────────────

def build_encoding_map(
    raw_records: list[dict],
    metadata: dict,
) -> dict[str, dict[str, int]]:
    """Deprecated — kept for backward compatibility; always returns empty dict."""
    return {}


def enrich_metadata_values(
    metadata: dict,
    encoding_map: dict[str, dict[str, int]],
) -> None:
    """Deprecated — kept for backward compatibility; no-op."""
    pass


# ── encode_records ────────────────────────────────────────────────────────────

# ── normalised city aliases for area recoding ─────────────────────────────────
_AREA_ALIASES: dict[str, int] = {
    # Hồ Chí Minh
    "hồ chí minh": 1, "ho chi minh": 1, "tp.hcm": 1, "tp hcm": 1,
    "hcm": 1, "tphcm": 1, "sài gòn": 1, "sai gon": 1,
    # Hà Nội
    "hà nội": 2, "ha noi": 2, "hn": 2, "hanoi": 2,
    # Đà Nẵng
    "đà nẵng": 3, "da nang": 3, "dn": 3, "danang": 3,
    # Cần Thơ
    "cần thơ": 4, "can tho": 4, "ct": 4, "cantho": 4,
}

_GENDER_RECODE: dict[str, int] = {
    "nam": 1, "male": 1, "1": 1, "m": 1,
    "nữ": 2, "nu": 2, "female": 2, "2": 2, "f": 2,
}

# "don't know" variants for personal-income → always code 99
_INCOME_DONT_KNOW: frozenset[str] = frozenset({
    "tôi không biết", "toi khong biet", "không biết", "khong biet",
    "don't know", "dont know", "i don't know", "i dont know",
    "prefer not to say", "prefer not to answer",
})


def _recode_gender(value: Any) -> Any:
    """'Nam'/'Nữ' / 'Male'/'Female' → 1 / 2."""
    return _GENDER_RECODE.get(str(value).strip().lower(), value)


def _income_lower_bound(text: str) -> float:
    """Extract the lower-bound number from a VND range string.

    '10,000,001 - 15,000,000 VND' → 10000001.0
    'Under 5,000,000 VND'         → 0.0
    'Over 70,000,000 VND'         → 70000000.0
    Unrecognised                   → inf  (sorts to end)
    """
    import re
    nums = re.findall(r'[\d,]+', text)
    if not nums:
        return float('inf')
    try:
        return float(nums[0].replace(',', ''))
    except ValueError:
        return float('inf')


def _col_to_meta_map(metadata: dict) -> dict[str, dict]:
    """Build {q{position}: metadata_entry} lookup from metadata."""
    return {
        f"q{meta['position']}": meta
        for meta in metadata["questions"].values()
        if "position" in meta
    }


def encode_records(
    raw_records: list[dict],
    metadata: dict,
    encoding_map: dict[str, dict[str, int]] | None = None,   # kept for compat, ignored
) -> list[dict]:
    """Convert answers to numeric codes.

    SA  (singlechoice)      → int            (API already returns integer code)
    SA  gender              → int  1/2       (text → code via _GENDER_RECODE)
    SA  area                → int  1-4+      (text → code; new cities auto-assigned 5+)
    SA  personal-income     → int  1-12/99   (text matched against choices_i18n)
    MA  (multiplechoice)    → "1;3;5"        (API already returns code string)
    ranking                 → "2;1;3"        (same)
    NUM                     → int / float    (parse numeric string)
    """
    col_to_meta = _col_to_meta_map(metadata)

    import copy as _copy

    # ── Pre-pass: discover area city values → build per-column code maps ──────
    # Mutates metadata["questions"][...]["choices_i18n"] in-place to record
    # any auto-assigned city codes so they appear in metadata.json.
    area_code_maps: dict[str, dict[str, int]] = {}   # col → {text_lower: code}

    for col, q in col_to_meta.items():
        if q.get("synthetic_type") != "area":
            continue
        # Collect every unique (non-empty) text answer for this column
        seen: set[str] = set()
        for rec in raw_records:
            val = str(rec.get(col, "")).strip()
            if val:
                seen.add(val)

        # Build code map: known aliases first, then auto-assign 5+
        code_map: dict[str, int] = {}   # text_lower → int code
        next_code = 5
        choices: dict[str, dict] = _copy.deepcopy(AREA_BASE_CHOICES)

        for text in sorted(seen):
            tl = text.lower()
            if tl in code_map:
                continue
            if tl in _AREA_ALIASES:
                code_map[tl] = _AREA_ALIASES[tl]
            else:
                code_map[tl] = next_code
                choices[str(next_code)] = {"vi": text, "en": text}
                next_code += 1

        # Persist discovered choices back into metadata (for metadata.json output)
        q["choices_i18n"] = choices
        area_code_maps[col] = code_map

    # ── Pre-pass: discover personal-income values → sorted code map ──────────
    # Codes are assigned in ascending income order (lowest range = 1).
    # "Don't know" variants are always mapped to 99.
    income_code_maps: dict[str, dict[str, int]] = {}   # col → {text_lower: code}

    for col, q in col_to_meta.items():
        if q.get("synthetic_type") != "personal-income":
            continue

        seen_income: set[str] = set()
        for rec in raw_records:
            val = str(rec.get(col, "")).strip()
            if val:
                seen_income.add(val)

        # Separate "don't know" from actual range strings
        dont_know_texts: list[str] = []
        range_texts: list[str] = []
        for text in seen_income:
            if text.strip().lower() in _INCOME_DONT_KNOW:
                dont_know_texts.append(text)
            else:
                range_texts.append(text)

        # Sort ranges by lower bound (smallest income first)
        range_texts.sort(key=_income_lower_bound)

        code_map_inc: dict[str, int] = {}
        choices_inc: dict[str, dict] = {}
        for i, text in enumerate(range_texts, 1):
            code_map_inc[text.strip().lower()] = i
            choices_inc[str(i)] = {"vi": text, "en": text}

        for text in dont_know_texts:
            code_map_inc[text.strip().lower()] = 99
        choices_inc["99"] = {"vi": "Tôi không biết", "en": "Don't know"}

        q["choices_i18n"] = choices_inc
        income_code_maps[col] = code_map_inc

    # ── Main encode pass ──────────────────────────────────────────────────────
    encoded: list[dict] = []

    for record in raw_records:
        new_rec: dict[str, Any] = {}
        for col, value in record.items():
            q = col_to_meta.get(col)
            if q is None or col in _ROW_META or not value:
                new_rec[col] = value
                continue

            atype          = q["answer_type"]
            synthetic_type = q.get("synthetic_type", "")

            if atype == "SA":
                if synthetic_type == "gender":
                    new_rec[col] = _recode_gender(value)
                elif synthetic_type == "area":
                    tl = str(value).strip().lower()
                    cmap = area_code_maps.get(col, {})
                    new_rec[col] = cmap.get(tl, value)
                elif synthetic_type == "personal-income":
                    tl = str(value).strip().lower()
                    cmap = income_code_maps.get(col, {})
                    new_rec[col] = cmap.get(tl, value)
                else:
                    try:
                        new_rec[col] = int(value)
                    except (ValueError, TypeError):
                        new_rec[col] = value
            elif atype == "NUM":
                new_rec[col] = _parse_number(value)
            else:
                new_rec[col] = value

        encoded.append(new_rec)

    return encoded


# ─────────────────────────────────────────────
# Step 3 – records → DataFrame
# ─────────────────────────────────────────────

# answer_types excluded entirely from rawdata.csv
EXCLUDED_ANSWER_TYPES = {"audio", "record", "reward", "instruction", "user-name", "user-phone", "photo"}


def records_to_dataframe(records: list[dict], definition: dict, metadata: dict):
    """Ordered DataFrame: meta cols + q1…qN (+ _other and matrix sub-cols).

    Question columns are renamed from internal ``q{pos}`` keys to the
    question's ``label`` field.  Duplicate labels raise ``ValueError``.
    """
    import pandas as pd

    col_to_meta = _col_to_meta_map(metadata)

    meta_cols = [
        "task_id", "date_time", "Key_in_date", "lastmodified_date",
        "profile_status",
    ]

    _MATRIX_TYPES = {"Matrix_SA", "Matrix_MA", "Matrix_NUM"}

    # Matches:  q7_o9        (regular other)
    #           q7_other     (regular other fallback)
    #           q29_r1_o9    (matrix sub other)
    #           q29_r1_other (matrix sub other fallback)
    _OTHER_COL_RE = re.compile(
        r"^q(\d+)"              # group 1: pos
        r"(?:_r(\d+))?"         # group 2: row (matrix only, optional)
        r"_(o(\d+)|other)$"     # group 3: full suffix, group 4: code (if _o{N})
    )

    # Collect other-text cols, grouped by their "parent" internal col name
    # {parent_col: [other_col, ...]}
    # e.g. "q7"     → ["q7_o9"]
    #      "q29_r1" → ["q29_r1_o9"]
    other_by_parent: dict[str, list[str]] = {}

    # Seed from metadata other_choice_codes — ensures columns exist even with no data
    col_to_meta_map = _col_to_meta_map(metadata)
    for q in definition.get("questions", []):
        col   = f"q{q['position']}"
        meta  = col_to_meta_map.get(col)
        if meta is None:
            continue
        atype = meta.get("answer_type", "")
        if atype in _MATRIX_TYPES:
            # matrix: other codes live on each sub-question
            for sub_key, sub_meta in meta.get("sub_questions", {}).items():
                for code in sub_meta.get("other_choice_codes", []):
                    other_col = f"{sub_key}_o{code}"
                    bucket = other_by_parent.setdefault(sub_key, [])
                    if other_col not in bucket:
                        bucket.append(other_col)
        else:
            for code in meta.get("other_choice_codes", []):
                other_col = f"{col}_o{code}"
                bucket = other_by_parent.setdefault(col, [])
                if other_col not in bucket:
                    bucket.append(other_col)

    # Supplement with any actual other cols found in records (e.g. _other fallback)
    for rec in records:
        for k in rec:
            m = _OTHER_COL_RE.match(k)
            if m:
                pos_str = m.group(1)
                row_str = m.group(2)
                parent  = f"q{pos_str}_r{row_str}" if row_str else f"q{pos_str}"
                bucket  = other_by_parent.setdefault(parent, [])
                if k not in bucket:
                    bucket.append(k)

    def _append_other_cols(parent_col: str, parent_label: str) -> None:
        """Append other-text cols that belong to parent_col, with rename entries."""
        for other_col in sorted(other_by_parent.get(parent_col, [])):
            q_cols.append(other_col)
            m = _OTHER_COL_RE.match(other_col)
            code = m.group(4) if m else None
            rename[other_col] = f"{parent_label}_o{code}" if code else f"{parent_label}_other"

    q_cols: list[str] = []          # internal names  (q{pos} / q{pos}_r{row})
    rename: dict[str, str] = {}     # internal → label

    for q in definition.get("questions", []):
        col = f"q{q['position']}"
        meta = col_to_meta.get(col)
        if meta is None:
            continue
        atype  = meta.get("answer_type", "")
        plabel = meta.get("label") or col   # parent label, fallback to q{pos}

        if atype in _MATRIX_TYPES:
            sub_qs = meta.get("sub_questions", {})
            for sub_key in meta.get("children", []):
                q_cols.append(sub_key)
                row_idx   = sub_qs.get(sub_key, {}).get("row_index", sub_key)
                sub_label = f"{plabel}_r{row_idx}"
                rename[sub_key] = sub_label
                _append_other_cols(sub_key, sub_label)
        else:
            q_cols.append(col)
            rename[col] = plabel
            _append_other_cols(col, plabel)

    # ── duplicate label check ─────────────────────────────────────────────────
    label_counts: dict[str, int] = {}
    for lbl in rename.values():
        label_counts[lbl] = label_counts.get(lbl, 0) + 1
    duplicates = sorted(lbl for lbl, cnt in label_counts.items() if cnt > 1)
    if duplicates:
        raise ValueError(
            f"Duplicate question labels detected — rawdata column names must be unique. "
            f"Duplicates: {duplicates}"
        )

    # ── build DataFrame ───────────────────────────────────────────────────────
    all_internal = meta_cols + q_cols

    df = pd.DataFrame(records)
    missing = [col for col in all_internal if col not in df.columns]
    if missing:
        df = pd.concat([df, pd.DataFrame("", index=df.index, columns=missing)], axis=1)
    df = df[all_internal].fillna("")

    # Rename question columns to labels
    df = df.rename(columns=rename)
    all_cols = meta_cols + [rename.get(c, c) for c in q_cols]
    return df[all_cols]
