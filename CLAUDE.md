# SurveyFlow — Claude Workflow Guide

This project uses the **surveyflow** Python package to process survey data from QMe into:
`rawdata.csv` + `metadata.json` + `datatable.xlsx`

---

## Core concept

`datatable.json` is the **single source of truth** for what the user wants in the output table.
- Claude modifies `datatable.json` based on user requests (natural language)
- Then runs the pipeline to produce updated `datatable.xlsx`
- Users never edit `datatable.json` directly — they tell Claude what they want

---

## Workflow A — First time running a survey

### Step 1 — Find survey
```
search_surveys(query="SURVEY_NAME")
```
Note the `survey_id`.

### Step 2 — Fetch and save MCP data
**First, check if `output/SURVEY_NAME/input/` already exists and contains `definition.json` + `rows_page_*.json`.**

If files exist → ask user:
> "Input data đã có sẵn (`definition.json`, `rows_page_1.json`, …). Bạn muốn dùng data cũ hay fetch lại data mới từ QMe?"

- User says **dùng data cũ / no** → skip to Step 3
- User says **fetch lại / yes** → proceed to fetch below

If files do not exist → fetch immediately (no need to ask):
```
get_survey_definition(survey_id)        →  save to output/SURVEY_NAME/input/definition.json
get_survey_rows(survey_id, offset=0)    →  save to output/SURVEY_NAME/input/rows_page_1.json
get_survey_rows(survey_id, offset=200)  →  save to output/SURVEY_NAME/input/rows_page_2.json
... keep fetching until has_more = false
```

### Step 3 — Create datatable.json
Create `output/SURVEY_NAME/datatable.json` based on:
- The survey definition (question positions, types, labels)
- User's request (if specified)
- If user does not specify → auto-generate (see Auto-generate rules below)

### Step 4 — Run pipeline
```bash
python run_pipeline.py \
  --input-dir        output/SURVEY_NAME/input \
  --output-dir       output/SURVEY_NAME \
  --version          v1 \
  --datatable-config output/SURVEY_NAME/datatable.json
```

---

## Workflow B — User requests changes to the table

When user says things like:
- *"thêm income vào banner"*
- *"bỏ q15 ra khỏi stub"*
- *"thêm mean và std cho q36"*
- *"đổi banner gender thành Male/Female/Other"*
- *"thêm T2B cho tất cả câu singlechoice"*

**Always:**
1. Read current `output/SURVEY_NAME/datatable.json`
2. Modify it according to user request
3. Save `datatable.json`
4. Run pipeline with new version:
```bash
python run_pipeline.py \
  --input-dir        output/SURVEY_NAME/input \
  --output-dir       output/SURVEY_NAME \
  --version          v2 \
  --datatable-config output/SURVEY_NAME/datatable.json
```
5. Increment version each run (v1 → v2 → v3 …) to preserve history

---

## datatable.json structure

```json
{
  "title": "SURVEY_NAME - Data Table",
  "significance_test": {
    "enabled": true,
    "levels": [90, 95],
    "method": "independent"
  },
  "banner": [
    { "label": "Total", "filter": null },
    {
      "label": "Gender",
      "question": "q10",
      "groups": [
        { "label": "Male",   "value": 2 },
        { "label": "Female", "value": 1 }
      ]
    },
    {
      "label": "Age Group",
      "question": "q12",
      "groups": [
        { "label": "Under 30", "values": [3, 4, 5] },
        { "label": "30 - 39",  "values": [1, 2]    },
        { "label": "40+",      "values": [6, 7, 8]  }
      ]
    }
  ],
  "stub": [
    { "question": "q10", "label": "Gender",     "stats": ["base", "percent"] },
    { "question": "q36", "label": "Food Freq",  "stats": ["base", "percent", "t2b", "b2b", "mean", "std", "se"] }
  ],
  "tables": [
    { "sheet": "Count",            "cell_content": "count",      "show_sig": false },
    { "sheet": "Percentage",       "cell_content": "percentage", "show_sig": false },
    { "sheet": "Percentage & Sig", "cell_content": "percentage", "show_sig": true  }
  ]
}
```

### Banner rules
- Always include `{ "label": "Total", "filter": null }` as first entry
- `value` = single integer code from rawdata.csv
- `values` (plural) = list of codes to group together (e.g. age ranges)
- Question codes: `q{position}` e.g. position 10 → `"q10"`
- To find codes → read `output/SURVEY_NAME/vX/metadata.json` → `questions.q10.values`

### Stub rules
- One entry per question
- `stats` options: `"base"`, `"percent"`, `"t2b"`, `"b2b"`, `"mean"`, `"std"`, `"se"`
- Only `singlechoice` and `multiplechoice` questions are codeable (others ignored)
- Excluded automatically: `audio`, `user-name`, `user-phone`, `instruction`, `reward`

### Tables rules
- Always keep all 3 sheets: Count, Percentage, Percentage & Sig
- Do not modify `tables` unless user explicitly requests it

---

## Auto-generate datatable.json rules
When user does not specify — use this logic:

**Banner:** Pick singlechoice questions that look demographic:
- Gender, Age, Location, Income, Marital status, Occupation
- Max 4-5 banner groups + Total

**Stub:** Include all `singlechoice` + `multiplechoice` questions
- Default stats: `["base", "percent"]`
- Skip: questions with `answer_type` not in `["singlechoice", "multiplechoice"]`

---

## Output structure
```
output/SURVEY_NAME/
├── input/
│   ├── definition.json       ← from get_survey_definition (fetch once)
│   ├── rows_page_1.json      ← from get_survey_rows (fetch once)
│   └── rows_page_2.json
├── datatable.json            ← Claude manages this file
├── v1/                       ← first run
│   ├── rawdata.csv
│   ├── metadata.json
│   └── datatable.xlsx
├── v2/                       ← after user requests changes
│   └── datatable.xlsx
└── v3/
    └── datatable.xlsx
```

> **Note:** `input/` data is fetched once and reused across all versions.
> **Always ask before re-fetching:** "Input data đã có sẵn (`definition.json`, `rows_page_*.json`). Bạn muốn dùng data cũ hay fetch lại data mới từ QMe?"
> Only re-fetch if user confirms yes.

---

## Common user requests → datatable.json changes

| User says | Claude does |
|---|---|
| "thêm income vào banner" | Add income question to `banner` array |
| "bỏ q15 khỏi stub" | Remove q15 entry from `stub` array |
| "thêm mean std cho q36" | Add `"mean"`, `"std"` to q36's `stats` |
| "thêm tất cả câu vào stub" | Add all codeable questions to `stub` |
| "tắt sig test" | Set `significance_test.enabled = false` |
| "chỉ chạy 1 sheet percentage" | Modify `tables` to keep only Percentage sheet |
| "refresh data / lấy data mới" | Re-fetch MCP rows → overwrite `input/rows_page_*.json` → re-run |

---

## Profile status
Default: `approved` only.
Override: `--profile-status approved,pending`
