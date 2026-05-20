"""Build banner column definitions from datatable config."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
import pandas as pd


# ── Module-level utilities ─────────────────────────────────────────────────────

def _sub_col_for_row(meta: dict | None, row_code: str, fallback: str) -> str:
    """Resolve the actual df column for a matrix row from sub_questions.rawdata_columns."""
    if not meta:
        return fallback
    for sm in meta.get("sub_questions", {}).values():
        if str(sm.get("row_index", "")) == str(row_code):
            rc = sm.get("rawdata_columns", [])
            return rc[0] if rc else sm.get("label", fallback)
    return fallback


def _ma_col_map(rawdata_cols: list[str]) -> dict[str, str]:
    """Map choice_code (str) → binary df column, derived from rawdata_columns.

    Example
    -------
    ["S12_1", "S12_2", "S12_9"]  →  {"1": "S12_1", "2": "S12_2", "9": "S12_9"}

    Uses the suffix after the last underscore as the code key.
    Columns whose suffix is not purely numeric (e.g. open-text ``_o9``) are skipped.
    """
    result: dict[str, str] = {}
    for col in rawdata_cols:
        parts = col.rsplit("_", 1)
        if len(parts) == 2 and parts[1].isdigit():
            result[parts[1]] = col
    return result


def _letter(i: int) -> str:
    """0→A, 1→B, …, 25→Z, 26→AA, …"""
    letters = []
    n = i + 1
    while n > 0:
        n, r = divmod(n - 1, 26)
        letters.append(chr(65 + r))
    return "".join(reversed(letters))


@dataclass
class BannerColumn:
    group_label:     str        # e.g. "Q1 x Q2 x Q3" — sig test grouping key
    subgroup_label:  str        # leaf/detail label, always shown in DETAIL row
    letter:          str        # A, B, C … resets when outermost level changes
    mask:            pd.Series
    is_total:        bool       = False   # True → excluded from sig test
    level_labels:    list[str]  = field(default_factory=list)
    matrix_row_code:  str | None       = None  # single row → paired mode
    matrix_row_codes: list[str] | None = None  # multiple rows → stacked paired mode

    def to_dict(self) -> dict:
        """Serialisable representation for JSON/preview API.

        Excludes non-serialisable fields: ``mask`` (pd.Series) and
        paired-mode fields ``matrix_row_code`` / ``matrix_row_codes``
        (internal xlsx rendering detail, not needed by consumers).
        """
        return {
            "label":        self.subgroup_label,
            "group_label":  self.group_label,
            "level_labels": self.level_labels,
            "letter":       self.letter,
            "is_total":     self.is_total,
            "n":            int(self.mask.sum()),
        }

    # Header display rules:
    # level_labels = []                 → plain: group(5) / detail(6)
    # level_labels = ["A"]              → 2-level: group(5) / A(6) / detail(7)
    # level_labels = ["A","B","C","D"]  → 5-level: group(5)/A(6)/B(7)/C(8)/D(9)/detail(10)
    #
    # Sig-test grouping: columns are compared within the same
    # (group_label, level_labels[0], …, level_labels[-1]) bucket.


def build_banner(
    config: dict,
    df: pd.DataFrame,
    col_map: dict[str, str] | None = None,
    q_pos_to_meta: dict[str, dict] | None = None,
) -> list[BannerColumn]:
    """Return one BannerColumn per banner subgroup defined in config.

    Parameters
    ----------
    col_map
        Maps question reference (``"Q1"``) → primary df column name.
        Built by ``_build_lookup_maps``; when ``None`` the reference is used as-is.
    q_pos_to_meta
        Maps question reference → full metadata entry dict.
        Required for correct MA binary handling and choice label look-ups.
    """

    def _resolve(q: str) -> str:
        """Resolve a question reference to its primary df column name."""
        return col_map[q] if col_map and q in col_map else q

    def _get_meta(q_ref: str) -> dict:
        """Return metadata for a question (empty dict if not found)."""
        if not q_pos_to_meta:
            return {}
        return q_pos_to_meta.get(q_ref) or q_pos_to_meta.get(_resolve(q_ref)) or {}

    def _make_mask(q_ref: str, value: int | None, values: list | None) -> pd.Series:
        """Build respondent mask for one banner group.

        Reads rawdata_columns from q_pos_to_meta — no hardcoded column conventions.

        SA / NUM  : single primary column, equality / isin filter.
        MA binary : per-choice binary columns from rawdata_columns (value == 1).
        """
        meta         = _get_meta(q_ref)
        atype        = meta.get("answer_type", "SA")
        rawdata_cols = meta.get("rawdata_columns", [])

        if atype == "MA":
            col_for_code = _ma_col_map(rawdata_cols)
            if value is not None:
                c = col_for_code.get(str(value))
                if c and c in df.columns:
                    return pd.to_numeric(df[c], errors="coerce").fillna(0) == 1
                return pd.Series(False, index=df.index)
            elif values:
                mask = pd.Series(False, index=df.index)
                for v in values:
                    c = col_for_code.get(str(v))
                    if c and c in df.columns:
                        mask = mask | (pd.to_numeric(df[c], errors="coerce").fillna(0) == 1)
                return mask
            return pd.Series(False, index=df.index)

        else:  # SA / NUM / etc.
            col = rawdata_cols[0] if rawdata_cols else _resolve(q_ref)
            if not col or col not in df.columns:
                return pd.Series(False, index=df.index)
            if value is not None:
                return df[col] == value
            elif values:
                return df[col].isin(values)
            return pd.Series(False, index=df.index)

    def _answered_mask(q_ref: str) -> pd.Series:
        """Return mask of respondents who answered q_ref at all.

        Used for show_total sub-columns: covers everyone who has any valid response
        for that question, regardless of which choice they picked.

        SA  : primary column is not null.
        MA  : at least one binary choice column == 1.
        """
        meta         = _get_meta(q_ref)
        atype        = meta.get("answer_type", "SA")
        rawdata_cols = meta.get("rawdata_columns", [])

        if atype == "MA":
            mask = pd.Series(False, index=df.index)
            for c in rawdata_cols:
                if c in df.columns:
                    mask = mask | (pd.to_numeric(df[c], errors="coerce").fillna(0) == 1)
            return mask
        else:
            col = rawdata_cols[0] if rawdata_cols else _resolve(q_ref)
            if col and col in df.columns:
                return df[col].notna()
            return pd.Series(True, index=df.index)   # fallback: all respondents

    def _code_label(q_ref: str, code: int) -> str:
        """Look up the display label for a choice code from metadata."""
        meta = _get_meta(q_ref)
        if not meta:
            return str(code)
        langs = meta.get("choices_i18n", {}).get(str(code), {})
        return langs.get("en") or langs.get("vi") or next(iter(langs.values()), str(code))

    def _expand_with_levels(entry: dict, group_label: str) -> list[BannerColumn]:
        """Handle banner entries with ``levels`` (multi-level nesting) or ``show_total``.

        Mirrors the JS ``buildBannerTree`` / ``buildLevelChildren`` / ``getLeafs``
        logic from *datatable-editor.html* so that Python and the browser preview
        produce identical column structures.

        datatable.json format
        ---------------------
        ::

            {
                "label":      "Store Type",
                "question":   "Q2",
                "groups":     [{"label": "Super", "value": 1}, ...],   # Lv1
                "show_total": true,                                      # add Total before Lv1
                "levels": [
                    {
                        "question":   "Q3",
                        "label":      "City",
                        "groups":     [{"label": "HCM", "value": 1}, ...],  # Lv2
                        "show_total": true                               # add Total before Lv2
                    },
                    ...   # Lv3, Lv4, Lv5 …
                ]
            }

        Tree → ``BannerColumn`` mapping
        --------------------------------
        * depth 0 (root item label)         → ``group_label``
        * depth 1 … N-1 (intermediate)      → ``level_labels[0 … N-2]``
        * depth N (leaf / deepest group)    → ``subgroup_label``

        The mask is the intersection of all ancestor group filters.
        """
        lv1_q       = entry.get("question", "")
        lv1_groups  = entry.get("groups", [])
        levels      = entry.get("levels", [])
        show_total1 = entry.get("show_total", False)

        result:  list[BannerColumn] = []
        counter: list[int]          = [0]

        def _recurse(
            parent_mask: pd.Series,
            remaining:   list[dict],
            path:        list[str],
        ) -> None:
            if not remaining:
                result.append(BannerColumn(
                    group_label    = group_label,
                    subgroup_label = path[-1] if path else (entry.get("label") or group_label),
                    letter         = _letter(counter[0]),
                    mask           = parent_mask,
                    is_total       = False,
                    level_labels   = path[:-1] if path else [],
                ))
                counter[0] += 1
                return

            lv     = remaining[0]
            rest   = remaining[1:]
            lq     = lv.get("question", "")
            lgrps  = lv.get("groups", [])
            show_t = lv.get("show_total", False)

            if show_t and lgrps:
                _recurse(parent_mask, rest, path + ["Total"])

            if not lgrps:
                _recurse(parent_mask, rest, path + [lv.get("label") or lq])
            else:
                for g in lgrps:
                    g_mask = parent_mask & _make_mask(lq, g.get("value"), g.get("values"))
                    _recurse(g_mask, rest, path + [g["label"]])

        all_rows = pd.Series(True, index=df.index)

        if show_total1 and lv1_groups:
            _recurse(_answered_mask(lv1_q), levels, ["Total"])

        if not lv1_groups:
            _recurse(all_rows, levels, [])
        else:
            for g in lv1_groups:
                g_mask = _make_mask(lv1_q, g.get("value"), g.get("values"))
                _recurse(g_mask, levels, [g["label"]])

        return result

    # If any question-level entry has show_total=True, each question already produces
    # its own Total sub-column → suppress the global Total to avoid a redundant column.
    # Mirrors the same hasShowTotal logic in datatable-editor.html buildBannerTree.
    _has_show_total = any(e.get("show_total") for e in config.get("banner", []))

    columns: list[BannerColumn] = []

    for entry in config.get("banner", []):
        # Prefix group_label with question label when a single question is referenced
        if "question" in entry:
            group_label = f"{entry['question']} - {entry['label']}"
        else:
            group_label = entry["label"]

        # ── Total ──────────────────────────────────────────────────────────────
        # Detect Total: no "groups", no "question", no "cross" key.
        # Suppressed when any question-level entry uses show_total (each has its own Total).
        if "groups" not in entry and "question" not in entry and "cross" not in entry:
            if not _has_show_total:
                columns.append(BannerColumn(
                    group_label=group_label,
                    subgroup_label="Total",
                    letter="",
                    mask=pd.Series(True, index=df.index),
                    is_total=True,
                ))
            continue

        # ── N-way cross-tab (via "cross" key) ──────────────────────────────────
        # Format:
        #   { "label": "Q1 x Q2 x Q3", "cross": [
        #       { "question": "Q1", "values": [2, 3] },
        #       { "question": "Q2", "values": [2, 3, 4] },
        #       ...
        #   ]}
        # Generates the cartesian product of all dimensions.
        # level_labels = labels for dims[0..n-2]; subgroup_label = label for dim[-1].
        if "cross" in entry:
            dims = entry["cross"]
            dim_items: list[list[dict]] = []
            for dim in dims:
                q_ref = dim["question"]
                items = []
                for code in dim["values"]:
                    label = _code_label(q_ref, code)
                    mask  = _make_mask(q_ref, value=code, values=None)
                    items.append({"label": label, "mask": mask})
                dim_items.append(items)

            letter_idx  = 0
            prev_outer: str | None = None

            for combo in itertools.product(*dim_items):
                outer = combo[0]["label"]
                if outer != prev_outer:
                    letter_idx = 0
                    prev_outer = outer

                combined_mask = pd.Series(True, index=df.index)
                for item in combo:
                    combined_mask = combined_mask & item["mask"]

                columns.append(BannerColumn(
                    group_label=group_label,
                    subgroup_label=combo[-1]["label"],
                    letter=_letter(letter_idx),
                    mask=combined_mask,
                    is_total=False,
                    level_labels=[item["label"] for item in combo[:-1]],
                ))
                letter_idx += 1
            continue

        # ── Matrix rows as banner columns (via "use_matrix_rows") ────────────────
        # Format:
        #   { "label": "Brand", "question": "Q13_1", "use_matrix_rows": true }
        # Generates one BannerColumn per row in choices_i18n.rows.
        # Mask: respondents where Q13_1_r{code} is not NaN (have any response for that row).
        if entry.get("use_matrix_rows"):
            q_ref = entry["question"]
            meta  = _get_meta(q_ref)
            rows: dict = meta.get("choices_i18n", {}).get("rows", {})

            letter_idx = 0
            for row_code, label_raw in rows.items():
                row_label = (
                    label_raw.get("vi") or label_raw.get("en") or row_code
                    if isinstance(label_raw, dict) else str(label_raw)
                )
                col_name = _sub_col_for_row(meta, row_code, f"{_resolve(q_ref)}_r{row_code}")
                mask = (
                    df[col_name].notna() if col_name in df.columns
                    else pd.Series(False, index=df.index)
                )
                columns.append(BannerColumn(
                    group_label=group_label,
                    subgroup_label=row_label,
                    letter=_letter(letter_idx),
                    mask=mask,
                    is_total=False,
                ))
                letter_idx += 1
            continue

        # ── Multi-level / show_total banner (via "levels" or "show_total" key) ──────
        # Triggered when the entry declares:
        #   "levels": [...]   → multi-level nesting (Lv2, Lv3, …)
        #   "show_total": true → add a Total sub-column before the groups
        # Uses the recursive _expand_with_levels helper (mirrors JS buildBannerTree).
        if "levels" in entry or entry.get("show_total"):
            columns.extend(_expand_with_levels(entry, group_label))
            continue

        # ── Regular banner / manual cross-tab (via "groups" key) ───────────────
        # Supports:
        #   - Single-question banner:  entry has "question" + groups with value/values
        #   - Manual cross-tab:        groups have "conditions" + optional "subgroup"/"subgroup2"
        letter_idx  = 0
        prev_outer: str | None = None

        for grp in entry.get("groups", []):
            sub_mid = grp.get("subgroup2", "")   # outermost intermediate level
            mid_lbl = grp.get("subgroup",  "")   # inner intermediate level

            level_labels = [x for x in [sub_mid, mid_lbl] if x]
            outer = level_labels[0] if level_labels else None
            if outer and outer != prev_outer:
                letter_idx = 0
                prev_outer = outer

            # ── Build mask ──────────────────────────────────────────────────────
            if "conditions" in grp:
                mask = pd.Series(True, index=df.index)
                for cond in grp["conditions"]:
                    cq_ref = cond["question"]
                    mask   = mask & _make_mask(
                        cq_ref,
                        value=cond.get("value"),
                        values=cond.get("values"),
                    )
            else:
                q_ref = entry["question"]
                mask  = _make_mask(
                    q_ref,
                    value=grp.get("value"),
                    values=grp.get("values"),
                )

            columns.append(BannerColumn(
                group_label=group_label,
                subgroup_label=grp["label"],
                letter=_letter(letter_idx),
                mask=mask,
                is_total=False,
                level_labels=level_labels,
            ))
            letter_idx += 1

    return columns


def nest_banner_with_matrix_rows(
    base_columns: list[BannerColumn],
    matrix_question: str,
    df: pd.DataFrame,
    q_pos_to_meta: dict | None = None,
    col_map: dict | None = None,
    groups: list[dict] | None = None,
) -> list[BannerColumn]:
    """Expand each base banner column into sub-columns — one per matrix row (or group).

    Parameters
    ----------
    groups
        Optional explicit group list. Each entry is a dict with:
          - ``label``     : display label for the column header
          - ``row_code``  : single row code  (enables paired-matrix stub mode)
          - ``row_codes`` : list of row codes to union (mask = any row not null;
                            paired-matrix stub mode is disabled for these columns)
        When omitted, every row in ``choices_i18n.rows`` becomes its own column.

    Header levels produced
    ----------------------
    - Total column  → group / brand        [level_labels=[]]
    - Other columns → group / sub-group / brand  [level_labels=[sub-group]]

    ``matrix_row_code`` is set only for single-row columns; multi-row (grouped)
    columns get ``matrix_row_code=None`` and fall back to standard stub expansion.
    """

    def _resolve(q: str) -> str:
        return col_map[q] if col_map and q in col_map else q

    q    = _resolve(matrix_question)
    meta = None
    if q_pos_to_meta:
        meta = q_pos_to_meta.get(matrix_question) or q_pos_to_meta.get(q)

    if not meta:
        return base_columns

    rows: dict = meta.get("choices_i18n", {}).get("rows", {})

    # ── Build brand_items as list of dicts ───────────────────────────────────
    # Each dict: { label, mask, row_code (single), row_codes (grouped) }
    brand_items: list[dict] = []

    if groups:
        for grp in groups:
            label = grp["label"]
            if "row_code" in grp:
                rc       = str(grp["row_code"])
                col_name = _sub_col_for_row(meta, rc, f"{q}_r{rc}")
                mask     = df[col_name].notna() if col_name in df.columns \
                           else pd.Series(False, index=df.index)
                brand_items.append({"label": label, "mask": mask,
                                    "row_code": rc, "row_codes": None})
            else:
                rcs  = [str(c) for c in grp.get("row_codes", [])]
                mask = pd.Series(False, index=df.index)
                for rc in rcs:
                    col_name = _sub_col_for_row(meta, rc, f"{q}_r{rc}")
                    if col_name in df.columns:
                        mask = mask | df[col_name].notna()
                brand_items.append({"label": label, "mask": mask,
                                    "row_code": None, "row_codes": rcs})
    else:
        if not rows:
            return base_columns
        for row_code, label_raw in rows.items():
            label    = label_raw.get("vi") or label_raw.get("en") or row_code \
                       if isinstance(label_raw, dict) else str(label_raw)
            col_name = _sub_col_for_row(meta, row_code, f"{q}_r{row_code}")
            mask     = df[col_name].notna() if col_name in df.columns \
                       else pd.Series(False, index=df.index)
            brand_items.append({"label": label, "mask": mask,
                                "row_code": row_code, "row_codes": None})

    # ── Nest each base column × brand_items ───────────────────────────────────
    result: list[BannerColumn] = []

    for col in base_columns:
        level_prefix: list[str] = [] if col.is_total \
                                   else list(col.level_labels) + [col.subgroup_label]
        letter_idx = 0
        for bi in brand_items:
            combined_mask = col.mask & bi["mask"]
            result.append(BannerColumn(
                group_label      = col.group_label,
                subgroup_label   = bi["label"],
                letter           = _letter(letter_idx),
                mask             = combined_mask,
                is_total         = False,
                level_labels     = level_prefix,
                matrix_row_code  = bi["row_code"],
                matrix_row_codes = bi["row_codes"],
            ))
            letter_idx += 1

    return result
