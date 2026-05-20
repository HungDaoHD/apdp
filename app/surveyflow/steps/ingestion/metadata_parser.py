"""Parse Qme survey definition into metadata.json structure."""

from __future__ import annotations

# answer_type codes (stored in metadata.json)
_TYPE_LABEL: dict[int, str] = {
    1:    "FT",          # freetext
    2:    "SA",          # singlechoice
    3:    "MA",          # multiplechoice
    4:    "Matrix_SA",   # matrix — single answer per row
    5:    "Matrix_MA",   # matrix — multiple answers per row
    6:    "ranking",
    8:    "SA",          # singlechoice (alternate type code)
    9:    "MA",          # grid MA (answer coded same as MA)
    28:   "Matrix_SA",   # matrix SA — numeric/rating per row
    29:   "Matrix_MA",   # matrix MA — multiple cols per row
    31:   "instruction", # section header / description block
    40:   "record",
    1100: "instruction", # quota / screener field
    1101: "NUM",         # singlenumber (numeric input)
    1106: "user-name",
    1107: "user-phone",
    1109: "SA",          # area → treated as SA with synthetic choices
}

_INPUT_TYPE_LABEL: dict[int, str] = {
    3:   "NUM",          # singlenumber input
    68:  "reward",
    100: "multiplenumber",
}

# answer_type codes for sub-questions of each matrix type
_MATRIX_SUB_TYPE: dict[str, str] = {
    "Matrix_SA":  "SA",
    "Matrix_MA":  "MA",
    "Matrix_NUM": "NUM",
}

# answer_types that support numeric coding
CODEABLE_TYPES = {"SA", "MA", "ranking"}

# answer_types excluded from rawdata.csv
EXCLUDED_ANSWER_TYPES = {"audio", "record", "reward", "instruction", "user-name", "user-phone", "photo"}

def _detect_other_codes(choices_i18n: dict) -> list[str]:
    """Return choice codes that are "other-specify" inputs (is_other: true)."""
    return [
        str(code)
        for code, i18n in choices_i18n.items()
        if isinstance(i18n, dict) and i18n.get("is_other")
    ]


_TYPE_NAME_LABEL: dict[str, str] = {
    "gender":          "SA",
    "married-status":  "SA",
    "area":            "SA",
    "personal-income": "SA",
    "photo":           "photo",
}

# ── Synthetic choices for special question types ──────────────────────────────

GENDER_CHOICES: dict[str, dict] = {
    "1": {"vi": "Nam",  "en": "Male"},
    "2": {"vi": "Nữ",   "en": "Female"},
}

# Fixed city codes for area questions; unknown values are auto-assigned 5, 6, 7…
AREA_BASE_CHOICES: dict[str, dict] = {
    "1": {"vi": "Hồ Chí Minh", "en": "Ho Chi Minh City"},
    "2": {"vi": "Hà Nội",      "en": "Hanoi"},
    "3": {"vi": "Đà Nẵng",     "en": "Da Nang"},
    "4": {"vi": "Cần Thơ",     "en": "Can Tho"},
}

# personal-income choices are auto-discovered at encode time (actual answer text
# from QMe uses VND number ranges like "10,000,001 - 15,000,000 VND").
# Codes are assigned by sorting ranges by their lower bound (lowest = code 1).
# "Don't know" variants are always assigned code 99.
# The filled-in choices_i18n is persisted back into metadata.json after encode.
PERSONAL_INCOME_CHOICES: dict[str, dict] = {}   # placeholder — populated at runtime

def _resolve_answer_type(q_type: int, input_type: int, type_name: str = "") -> str:
    if type_name in _TYPE_NAME_LABEL:
        return _TYPE_NAME_LABEL[type_name]
    if q_type == 1 and input_type in _INPUT_TYPE_LABEL:
        return _INPUT_TYPE_LABEL[input_type]
    return _TYPE_LABEL.get(q_type, f"unknown_{q_type}")


def parse_metadata(definition: dict) -> dict:
    """Convert get_survey_definition response → metadata dict.

    Structure:
    {
      "survey_id": ...,
      "questions": {
        "795699": {                          ← key = str(question_id)
          "col":          "q6",             ← rawdata column name
          "position":     6,
          "question_id":  795699,
          "label":        "S1_1",           ← short question label from MCP
          "question_i18n": {"vi": "...", "en": "...", "ja": "..."},
          "answer_type":  "SA",
          "mandatory":    true,
          "status":       1,
          "choices_i18n": {"1": {"vi": "...", "en": "..."}, ...}
        },
        ...
      }
    }
    """
    from surveyflow.steps.ingestion.data_parser import EXCLUDED_ANSWER_TYPES as _EX

    survey = definition["survey"]

    questions: dict[str, dict] = {}
    for q in definition.get("questions", []):
        q_type   = q["type"]
        inp_type = q.get("input_type", 0)
        atype    = _resolve_answer_type(q_type, inp_type, q.get("type_name", ""))

        if atype in _EX:
            continue

        raw_choices  = q.get("choices", {})
        choices_i18n = q.get("choices_i18n", {})
        pos          = q["position"]
        qid          = q["question_id"]
        col          = f"q{pos}"
        key          = str(qid)

        # ── matrix: build sub_questions (one per row) ────────────────────────
        # rows/columns can be in `choices` (old format) or `choices_i18n` (new format)
        _ci_rows = choices_i18n.get("rows", {}) if isinstance(choices_i18n, dict) else {}
        _raw_rows = (raw_choices.get("rows") if isinstance(raw_choices, dict) else None) or _ci_rows

        if atype in _MATRIX_SUB_TYPE and _raw_rows:
            def _row_label(v: object) -> str:
                if isinstance(v, dict):
                    return v.get("vi") or v.get("en") or str(v)
                return str(v)

            rows_map    = {str(k): _row_label(v) for k, v in _raw_rows.items()}
            cols_i18n   = {str(k): v for k, v in choices_i18n.get("columns", {}).items()} if isinstance(choices_i18n, dict) else {}
            sub_atype   = _MATRIX_SUB_TYPE[atype]

            # detect "other" column codes once (shared across all rows)
            matrix_other_codes = _detect_other_codes(cols_i18n) if cols_i18n else []

            parent_label = q.get("label") or col
            sub_questions: dict[str, dict] = {}
            for row_code, row_label in rows_map.items():
                sub_key      = f"{col}_r{row_code}"
                sub_col_name = f"{parent_label}_r{row_code}"   # matches rawdata column name
                sub_entry: dict = {
                    "parent":       key,
                    "row_index":    row_code,
                    "answer_type":  sub_atype,
                    "label":        sub_col_name,   # = rawdata column name (e.g. Q9_1_r1)
                    "row_label":    row_label,       # choice text (e.g. "Wakodo")
                    "choices_i18n": cols_i18n,
                }
                if matrix_other_codes:
                    sub_entry["other_choice_codes"] = matrix_other_codes
                sub_questions[sub_key] = sub_entry
        else:
            matrix_other_codes = []
            sub_questions = {}

        # For regular questions: detect from flat choices_i18n
        # For matrix questions:  reuse matrix_other_codes (detected from cols_i18n above)
        if atype in _MATRIX_SUB_TYPE:
            other_codes = matrix_other_codes   # already computed above
        elif isinstance(choices_i18n, dict) and atype in ("SA", "MA", "ranking"):
            other_codes = _detect_other_codes(choices_i18n)
        else:
            other_codes = []

        # ── synthetic choices for special types ─────────────────────────────
        import copy as _copy
        type_name_str = q.get("type_name", "")
        synthetic_type = ""
        if type_name_str == "gender":
            choices_i18n  = _copy.deepcopy(GENDER_CHOICES)
            synthetic_type = "gender"
        elif type_name_str == "area" or q_type == 1109:
            choices_i18n  = _copy.deepcopy(AREA_BASE_CHOICES)
            synthetic_type = "area"
        elif type_name_str == "personal-income":
            choices_i18n  = _copy.deepcopy(PERSONAL_INCOME_CHOICES)
            synthetic_type = "personal-income"

        q_entry: dict = {
            "position":     pos,
            "question_id":  qid,
            "label":        q.get("label", ""),
            "question_i18n": q.get("question_i18n", {}),
            "answer_type":  atype,
            "mandatory":    q.get("mandatory", False),
            "status":       q.get("status", 1),
            "choices_i18n": choices_i18n,
        }
        if synthetic_type:
            q_entry["synthetic_choices"] = True
            q_entry["synthetic_type"]    = synthetic_type
        if other_codes:
            q_entry["other_choice_codes"] = other_codes
        if sub_questions:
            q_entry["children"]      = list(sub_questions.keys())
            q_entry["sub_questions"] = sub_questions

        questions[key] = q_entry

    return {
        "survey_id":     survey["survey_id"],
        "title":         survey.get("title", ""),
        "english_title": survey.get("english_title", ""),
        "status":        survey.get("status", ""),
        "start_date":    survey.get("start_date", ""),
        "end_date":      survey.get("end_date", ""),
        "questions":     questions,
    }
