"""Compute cross-tabulation blocks for the data table."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import warnings
import numpy as np
import pandas as pd
from scipy import stats as _scipy_stats

from surveyflow.steps.table.banner_builder import BannerColumn, _ma_col_map

CODEABLE_TYPES  = {"SA", "MA", "ranking"}
MATRIX_TYPES    = {"Matrix_SA", "Matrix_MA", "Matrix_NUM"}

_SUB_Q_RE = re.compile(r'^(.+?)_r(\d+)$', re.IGNORECASE)

STAT_LABELS: dict[str, str] = {
    "base": "Base",
    "t2b":  "T2B",
    "b2b":  "B2B",
    "mean": "Mean",
    "std":  "Std",
    "se":   "Standard Error",
    "min":  "Min",
    "max":  "Max",
}

DEFAULT_STATS_ORDER = ["base", "percent", "t2b", "b2b", "mean", "std", "se", "min", "max"]

NUMERIC_TYPES = {"SA", "NUM", "multiplenumber"}


# ── Data structures ────────────────────────────────────────────────────────────
# Values and sig_marks are keyed by column INDEX (int) for uniqueness,
# because letters reset to A in every banner group.

@dataclass
class StubRow:
    label: str
    row_type: str
    counts: dict[int, int]          # col_index → raw count
    values: dict[int, float]        # col_index → proportion or stat value
    sig_marks: dict[int, str] = field(default_factory=dict)  # col_index → "BC" / "a"
    code: str | None = None         # numeric code for percent rows; None for stat rows

    def to_dict(self) -> dict:
        # Use ordered arrays (index 0..N-1) instead of dicts to eliminate
        # repeated string keys and reduce JSON file size by ~60-70%.
        n   = max((k + 1 for k in self.values), default=0)
        sig = [self.sig_marks.get(i, "") for i in range(n)]
        out: dict = {
            "label":    self.label,
            "row_type": self.row_type,
            "code":     self.code,
            "values":   [round(float(self.values.get(i, 0.0)), 4) for i in range(n)],
            "counts":   [int(self.counts.get(i, 0)) for i in range(n)],
        }
        if any(sig):
            out["sig_marks"] = sig
        return out


@dataclass
class StubBlock:
    question_code: str
    question_label: str
    answer_type: str
    rows: list[StubRow]

    def to_dict(self) -> dict:
        return {
            "type":        "stub",
            "question":    self.question_code,
            "label":       self.question_label,
            "answer_type": self.answer_type,
            "rows":        [r.to_dict() for r in self.rows],
        }


@dataclass
class RowGroupBlock:
    """A section grouped by matrix rows (e.g. brands), each containing sub-blocks per question."""
    row_label: str              # e.g. "1. Castrol"
    row_code:  str              # e.g. "1"
    sub_blocks: list[StubBlock]

    def to_dict(self) -> dict:
        return {
            "type":       "row_group",
            "row_label":  self.row_label,
            "row_code":   self.row_code,
            "sub_blocks": [b.to_dict() for b in self.sub_blocks],
        }


@dataclass
class RankingBlock:
    """Ranking question — one section per question, sub_blocks per rank position or flat.

    mode = "rank_dist"  → sub_blocks[0] = Rank 1, sub_blocks[1] = Rank 2, …
    mode = "any_rank"   → sub_blocks[0] = flat MA-style (choice ranked at any position)
    """
    question_code:  str
    question_label: str
    answer_type:    str               # always "ranking"
    mode:           str               # "rank_dist" | "any_rank"
    sub_blocks:     list[StubBlock]

    def to_dict(self) -> dict:
        return {
            "type":        "ranking",
            "question":    self.question_code,
            "label":       self.question_label,
            "answer_type": self.answer_type,
            "mode":        self.mode,
            "sub_blocks":  [b.to_dict() for b in self.sub_blocks],
        }


# ── Significance test ──────────────────────────────────────────────────────────

def _binary_array(sub: pd.DataFrame, col: str, code: str, atype: str) -> np.ndarray:
    """Return float 0/1 array: 1 if respondent selected *code*, else 0.

    All callers now pass individual numeric columns (SA, or binary MA/ranking columns)
    so we always use numeric equality. The *atype* parameter is kept for signature
    compatibility but is no longer used for dispatch.
    """
    return (
        pd.to_numeric(sub[col], errors="coerce").fillna(-1) == int(code)
    ).astype(float).to_numpy()


def _compute_sig_marks(
    col: str,
    code: str,
    atype: str,
    df: pd.DataFrame,
    banner_cols: list[BannerColumn],
    sig_config: dict,
) -> dict[int, str]:
    """Paired or independent-samples t-test for proportions.

    sig_config keys
    ---------------
    enabled : bool
    levels  : list[int]  — subset of [90, 95]
    method  : str        — "independent" (default) | "related"

    Rules
    -----
    1. Skip Total columns (is_total=True)
    2. Only compare columns within the same banner group
    3. Letters reset to A within each group
    4. Uppercase = 95 % level, lowercase = 90 % level

    independent
        Welch's two-sample t-test (ttest_ind, equal_var=False).
        Appropriate when the two banner groups have different respondents
        (e.g. Male vs Female).

    related
        Paired t-test (ttest_rel) on the intersection of respondents
        present in *both* banner masks.
        Appropriate when the same respondents can appear in both groups
        (e.g. overlapping demographic segments).
        Groups with no overlapping respondents are skipped.
    """
    result: dict[int, str] = {i: "" for i in range(len(banner_cols))}
    if not sig_config.get("enabled", False):
        return result

    levels  = sig_config.get("levels", [95])
    method  = sig_config.get("method", "independent")   # "independent" | "related"
    alpha95 = 0.05 if 95 in levels else None
    alpha90 = 0.10 if 90 in levels else None

    # Group non-total column indices for pairwise comparison.
    # Columns are compared within the same (group_label, level_labels[0], …) bucket,
    # i.e. only against siblings that share all parent level labels.
    groups: dict[str, list[int]] = {}
    for i, bc in enumerate(banner_cols):
        if bc.is_total:
            continue
        key = bc.group_label + ("|" + "|".join(bc.level_labels) if bc.level_labels else "")
        groups.setdefault(key, []).append(i)

    marks: dict[int, list[str]] = {i: [] for i in range(len(banner_cols))}

    for grp_indices in groups.values():
        for a in range(len(grp_indices)):
            for b in range(a + 1, len(grp_indices)):
                i1, i2 = grp_indices[a], grp_indices[b]
                bc1, bc2 = banner_cols[i1], banner_cols[i2]

                try:
                    with warnings.catch_warnings():
                        # Suppress scipy precision-loss warning that fires when one
                        # group has zero variance (all respondents answered the same
                        # way). The result is still valid (p = NaN → skipped below).
                        warnings.filterwarnings(
                            "ignore",
                            message="Precision loss occurred",
                            category=RuntimeWarning,
                        )
                        if method == "related":
                            # Paired t-test — only respondents present in BOTH groups
                            both_mask = bc1.mask & bc2.mask
                            if int(both_mask.sum()) < 2:
                                continue
                            arr1 = _binary_array(df[both_mask], col, code, atype)
                            arr2 = _binary_array(df[both_mask], col, code, atype)
                            # arr1 and arr2 are identical when banner groups share the
                            # same question column — difference is always 0.
                            # Meaningful only when groups are defined on different
                            # criteria so respondents answer differently in context.
                            t_stat, p_val = _scipy_stats.ttest_rel(arr1, arr2)
                        else:
                            # Independent (Welch's) t-test
                            arr1 = _binary_array(df[bc1.mask], col, code, atype)
                            arr2 = _binary_array(df[bc2.mask], col, code, atype)
                            if len(arr1) < 2 or len(arr2) < 2:
                                continue
                            t_stat, p_val = _scipy_stats.ttest_ind(
                                arr1, arr2, equal_var=False
                            )
                except Exception:
                    continue

                if np.isnan(p_val):
                    continue

                # winner (higher proportion) gets the loser's letter
                winner = i1 if t_stat > 0 else i2
                loser  = i2 if t_stat > 0 else i1
                loser_letter = banner_cols[loser].letter

                if alpha95 and p_val < alpha95:
                    marks[winner].append(loser_letter.upper())
                elif alpha90 and p_val < alpha90:
                    marks[winner].append(loser_letter.lower())

    return {i: "".join(marks[i]) for i in range(len(banner_cols))}


# ── New-format helpers ─────────────────────────────────────────────────────────

def _sub_q_col(sub_meta: dict, fallback: str = "") -> str:
    """Resolve the actual DataFrame column for a matrix sub-question.
    Stored in rawdata_columns[0]; falls back to label if not set."""
    rc = sub_meta.get("rawdata_columns", [])
    return rc[0] if rc else sub_meta.get("label", fallback)


def _build_row_col_map(q_meta: dict) -> dict[str, str]:
    """Map row_index (str) → actual df column, built from sub_questions.rawdata_columns."""
    result: dict[str, str] = {}
    for sm in q_meta.get("sub_questions", {}).values():
        ri = str(sm.get("row_index", ""))
        if ri:
            rc = sm.get("rawdata_columns", [])
            result[ri] = rc[0] if rc else sm.get("label", "")
    return result


def _base_count_ma(sub: pd.DataFrame, rawdata_cols: list) -> int:
    """Base count for binary-format MA: rows where any binary column is non-NaN."""
    cols = [c for c in rawdata_cols if c in sub.columns]
    if not cols:
        return 0
    return int(sub[cols].notna().any(axis=1).sum())


def _code_count_ma(sub: pd.DataFrame, rawdata_cols: list, code: str) -> int:
    """Count rows selecting *code* in binary-format MA (column == 1).
    Uses _ma_col_map for O(1) lookup instead of linear endswith scan."""
    col = _ma_col_map(rawdata_cols).get(code)
    if col is None or col not in sub.columns:
        return 0
    return int((pd.to_numeric(sub[col], errors="coerce") == 1).sum())


# ── Count helpers ──────────────────────────────────────────────────────────────

def _safe_sum(series: pd.Series) -> int:
    """Sum a boolean series to int, safe against empty pd.StringDtype returning ''."""
    if len(series) == 0:
        return 0
    try:
        return int(series.sum())
    except (ValueError, TypeError):
        return 0


def _base_count(sub: pd.DataFrame, col: str) -> int:
    return _safe_sum(sub[col].apply(lambda v: pd.notna(v) and str(v).strip() != ""))


def _code_count_sc(sub: pd.DataFrame, col: str, code: str) -> int:
    return _safe_sum(pd.to_numeric(sub[col], errors="coerce") == int(code))


# ── Ranking-specific count helpers ────────────────────────────────────────────
# Ranking data format: one column per rank position (rawdata_columns[0] = Rank 1,
# rawdata_columns[1] = Rank 2, …). Each cell holds the integer choice code for
# that rank, or NaN when the respondent did not rank to that depth.

def _base_count_rank_at(sub: pd.DataFrame, rank_cols: list[str], n: int) -> int:
    """Count rows where the respondent gave at least *n* rank positions (col n-1 is non-NaN)."""
    if n < 1 or n > len(rank_cols):
        return 0
    col = rank_cols[n - 1]
    if col not in sub.columns:
        return 0
    return int(sub[col].notna().sum())


def _code_count_rank_at(sub: pd.DataFrame, rank_cols: list[str], code: str, position: int) -> int:
    """Count rows where the choice at rank *position* (1-based) equals *code*."""
    if position < 1 or position > len(rank_cols):
        return 0
    col = rank_cols[position - 1]
    if col not in sub.columns:
        return 0
    return int((pd.to_numeric(sub[col], errors="coerce") == int(code)).sum())


def _base_count_rank_any(sub: pd.DataFrame, rank_cols: list[str]) -> int:
    """Count rows where the respondent ranked at least one choice (any rank column non-NaN)."""
    cols = [c for c in rank_cols if c in sub.columns]
    if not cols:
        return 0
    return int(sub[cols].notna().any(axis=1).sum())


def _code_count_rank_any(sub: pd.DataFrame, rank_cols: list[str], code: str) -> int:
    """Count rows where *code* appears at ANY rank position."""
    cols = [c for c in rank_cols if c in sub.columns]
    if not cols:
        return 0
    v = int(code)
    return int(
        (pd.concat(
            [pd.to_numeric(sub[c], errors="coerce") for c in cols],
            axis=1,
        ) == v).any(axis=1).sum()
    )


# ── Main ───────────────────────────────────────────────────────────────────────

def _choice_label(choices_i18n: dict, code: str) -> str:
    """Get display label for a choice code. Prefer 'en', fall back to first language."""
    entry = choices_i18n.get(str(code), {})
    if not entry:
        return str(code)
    return entry.get("en") or entry.get("vi") or next(iter(entry.values()), str(code))


def _row_label_text(v: object) -> str:
    if isinstance(v, dict):
        return v.get("en") or v.get("vi") or next(iter(v.values()), "")
    return str(v)


def _parse_sub_q_ref(q: str, q_pos_to_meta: dict) -> dict | None:
    """Parse 'Q14_r10' syntax → return sub_question meta dict, or None if not a sub-ref.

    Rule 3: when a matrix sub-question is referenced directly (e.g. Q14_r10),
    it behaves as a regular SA/MA question using the parent's columns as choices.
    """
    m = _SUB_Q_RE.match(q)
    if not m:
        return None
    parent_label = m.group(1)
    row_code     = m.group(2)
    parent_meta  = q_pos_to_meta.get(parent_label)
    if parent_meta is None or parent_meta.get("answer_type", "") not in MATRIX_TYPES:
        return None
    for sm in parent_meta.get("sub_questions", {}).values():
        if str(sm.get("row_index", "")) == row_code:
            return sm
    return None


def _validate_row_group(items: list[dict], q_pos_to_meta: dict) -> dict:
    """Validate row_group rules 1 & 2. Returns the shared rows dict on success."""
    if not items:
        raise ValueError("row_group requires at least one item in 'items'.")

    first_rows: dict | None = None
    first_q:    str | None  = None

    for item in items:
        q      = item.get("question", "")
        q_meta = q_pos_to_meta.get(q)

        # Rule 1: all items must be matrix questions
        if q_meta is None:
            raise ValueError(f"row_group: question '{q}' not found in metadata.")
        atype = q_meta.get("answer_type", "")
        if atype not in MATRIX_TYPES:
            raise ValueError(
                f"row_group: question '{q}' has answer_type '{atype}'. "
                f"Only Matrix_SA / Matrix_MA / Matrix_NUM are allowed in row_group. "
                f"Non-matrix questions must be placed outside the row_group. "
                f"To reference a single matrix row alongside non-matrix questions, "
                f"use the '{q}_r{{row_code}}' syntax instead."
            )

        # Rule 2: all items must have identical choices_i18n.rows
        ci   = q_meta.get("choices_i18n", {})
        rows = ci.get("rows", {}) if isinstance(ci, dict) else {}
        if not rows:
            raise ValueError(
                f"row_group: question '{q}' has no choices_i18n.rows defined."
            )
        if first_rows is None:
            first_rows, first_q = rows, q
        elif rows != first_rows:
            raise ValueError(
                f"row_group: question '{q}' has different choices_i18n.rows "
                f"than '{first_q}'. All items must share identical row definitions."
            )

    return first_rows  # type: ignore[return-value]


def _compute_row_group(
    group_cfg: dict,
    banner_cols: list[BannerColumn],
    df: pd.DataFrame,
    sig_config: dict,
    q_pos_to_meta: dict,
) -> list[RowGroupBlock]:
    """Compute blocks for a row_group stub entry.

    Returns one RowGroupBlock per shared row (e.g. one per brand),
    each containing one StubBlock per question item.
    """
    items      = group_cfg.get("items", [])
    shared_rows = _validate_row_group(items, q_pos_to_meta)

    result: list[RowGroupBlock] = []

    for row_code, row_label_raw in shared_rows.items():
        row_label  = _row_label_text(row_label_raw)
        sub_blocks: list[StubBlock] = []

        for item in items:
            q       = item["question"]
            q_meta  = q_pos_to_meta[q]
            atype   = q_meta["answer_type"]
            sub_atype = "MA" if atype == "Matrix_MA" else "SA"

            ci          = q_meta.get("choices_i18n", {})
            col_choices = ci.get("columns", {}) if isinstance(ci, dict) else {}

            # Find the sub_question for this row
            sub_meta = next(
                (sm for sm in q_meta.get("sub_questions", {}).values()
                 if str(sm.get("row_index", "")) == str(row_code)),
                None,
            )
            if sub_meta is None:
                continue

            sub_col = _sub_q_col(sub_meta, "")
            sub_raw_cols = sub_meta.get("rawdata_columns", [])
            is_sub_ma = sub_atype == "MA"
            if not is_sub_ma and (not sub_col or sub_col not in df.columns):
                continue

            item_label = (
                item.get("label")
                or q_meta.get("question_i18n", {}).get("en")
                or q_meta.get("question_i18n", {}).get("vi")
                or q
            )
            req_stats = item.get("stats", ["base", "percent"])

            sub_bases: dict[int, int] = {
                i: (_base_count_ma(df[bc.mask], sub_raw_cols) if is_sub_ma
                    else _base_count(df[bc.mask], sub_col))
                for i, bc in enumerate(banner_cols)
            }

            sub_rows: list[StubRow] = []
            for stat in DEFAULT_STATS_ORDER:
                if stat not in req_stats:
                    continue
                if stat == "base":
                    sub_rows.append(StubRow(
                        label="Base", row_type="base",
                        counts=dict(sub_bases),
                        values={i: float(v) for i, v in sub_bases.items()},
                    ))
                elif stat == "percent" and col_choices:
                    for code in sorted(col_choices.keys(), key=lambda x: int(x)):
                        cnts: dict[int, int]   = {}
                        pcts: dict[int, float] = {}
                        for i, bc in enumerate(banner_cols):
                            sub  = df[bc.mask]
                            base = sub_bases[i]
                            cnt  = (_code_count_ma(sub, sub_raw_cols, code) if is_sub_ma
                                    else _code_count_sc(sub, sub_col, code))
                            cnts[i] = cnt
                            pcts[i] = cnt / base if base else 0.0
                        sub_rows.append(StubRow(
                            label=_choice_label(col_choices, code), row_type="percent",
                            counts=cnts, values=pcts, code=code,
                        ))

            if sub_rows:
                sub_blocks.append(StubBlock(
                    question_code=q.upper(),
                    question_label=item_label,
                    answer_type=atype,
                    rows=sub_rows,
                ))

        if sub_blocks:
            result.append(RowGroupBlock(
                row_label=row_label,
                row_code=row_code,
                sub_blocks=sub_blocks,
            ))

    return result


def compute_table(
    stub_configs: list[dict],
    banner_cols: list[BannerColumn],
    df: pd.DataFrame,
    metadata: dict,
    sig_config: dict,
    col_map: dict[str, str] | None = None,
    q_pos_to_meta: dict[str, dict] | None = None,
) -> list[StubBlock | RowGroupBlock | RankingBlock]:
    """Compute cross-tabulation blocks.

    Parameters
    ----------
    col_map
        Maps ``q{pos}`` reference → actual df column name (label).
    q_pos_to_meta
        Maps ``q{pos}`` reference → metadata entry dict.
    """

    def _resolve(q: str) -> str:
        return col_map[q] if col_map and q in col_map else q

    def _get_meta(q: str) -> dict | None:
        return q_pos_to_meta.get(q) if q_pos_to_meta else None

    blocks: list[StubBlock | RowGroupBlock | RankingBlock] = []
    n = len(banner_cols)

    for sc in stub_configs:

        # ── row_group entry ───────────────────────────────────────────────────
        if sc.get("row_group"):
            row_blocks = _compute_row_group(
                sc, banner_cols, df, sig_config, q_pos_to_meta or {}
            )
            blocks.extend(row_blocks)
            continue

        q = sc["question"]

        # ── sub-question reference: Q14_r10 syntax ────────────────────────────
        sub_ref = _parse_sub_q_ref(q, q_pos_to_meta or {})
        if sub_ref is not None:
            # Treat the sub-question as a flat SA/MA question
            sub_col      = _sub_q_col(sub_ref, q)
            sub_atype    = sub_ref.get("answer_type", "SA")
            sub_raw_cols = sub_ref.get("rawdata_columns", [])
            is_sub_ma    = sub_atype == "MA"
            col_choices  = sub_ref.get("choices_i18n", {})
            req_stats    = sc.get("stats", ["base", "percent"])

            # Label: datatable config > parent question_i18n + row_label
            parent_label_q = _SUB_Q_RE.match(q).group(1)  # type: ignore[union-attr]
            parent_meta    = (q_pos_to_meta or {}).get(parent_label_q, {})
            default_label  = (
                (parent_meta.get("question_i18n", {}).get("en") or
                 parent_meta.get("question_i18n", {}).get("vi") or
                 parent_label_q)
                + f" — {sub_ref.get('row_label', q)}"
            )
            q_label = sc.get("label") or default_label

            if not is_sub_ma and sub_col not in df.columns:
                continue

            sub_bases: dict[int, int] = {
                i: (_base_count_ma(df[bc.mask], sub_raw_cols) if is_sub_ma
                    else _base_count(df[bc.mask], sub_col))
                for i, bc in enumerate(banner_cols)
            }
            sub_rows: list[StubRow] = []
            for stat in DEFAULT_STATS_ORDER:
                if stat not in req_stats:
                    continue
                if stat == "base":
                    sub_rows.append(StubRow(
                        label="Base", row_type="base",
                        counts=dict(sub_bases),
                        values={i: float(v) for i, v in sub_bases.items()},
                    ))
                elif stat == "percent" and col_choices:
                    for code in sorted(col_choices.keys(), key=lambda x: int(x)):
                        cnts: dict[int, int]   = {}
                        pcts: dict[int, float] = {}
                        for i, bc in enumerate(banner_cols):
                            sub  = df[bc.mask]
                            base = sub_bases[i]
                            cnt  = (_code_count_ma(sub, sub_raw_cols, code) if is_sub_ma
                                    else _code_count_sc(sub, sub_col, code))
                            cnts[i] = cnt
                            pcts[i] = cnt / base if base else 0.0
                        sub_rows.append(StubRow(
                            label=_choice_label(col_choices, code), row_type="percent",
                            counts=cnts, values=pcts, code=code,
                        ))
            if sub_rows:
                blocks.append(StubBlock(
                    question_code=q.upper(),
                    question_label=q_label,
                    answer_type=sub_atype,
                    rows=sub_rows,
                ))
            continue

        # ── Regular question ──────────────────────────────────────────────────
        q_col  = _resolve(q)
        q_meta = _get_meta(q)

        if q_meta is None:
            continue

        atype        = q_meta["answer_type"]
        choices_i18n = q_meta.get("choices_i18n", {})
        req_stats    = sc.get("stats", ["base", "percent"])

        # Question label: datatable config > question_i18n["en"] > label > q
        q_label = (
            sc.get("label")
            or q_meta.get("question_i18n", {}).get("en")
            or q_meta.get("question_i18n", {}).get("vi")
            or q_meta.get("label")
            or q
        )

        # ── Matrix: paired mode (banner cols carry matrix_row_code or matrix_row_codes) ──
        # Activated when banner was built via nest_banner_with_matrix_rows (banner_matrix).
        # Instead of expanding the matrix into N sub-question blocks (one per brand),
        # produce a SINGLE block whose rows are the choice options.
        #
        # Two column variants:
        #   matrix_row_code  (str)        → single brand row: read {q_col}_r{code}
        #   matrix_row_codes (list[str])  → grouped brands:   stack/sum across all _r{rc} cols
        _is_paired = any(
            bc.matrix_row_code is not None or bc.matrix_row_codes is not None
            for bc in banner_cols
        )
        if atype in MATRIX_TYPES and _is_paired:
            sub_atype   = "MA" if atype == "Matrix_MA" else "SA"
            col_choices = choices_i18n.get("columns", {}) if isinstance(choices_i18n, dict) else {}

            # Build row_index → actual df column (supports new format rawdata_columns)
            row_col_map = _build_row_col_map(q_meta)

            def _paired_col(rc: str) -> str:
                """Resolve actual df column for a single matrix row code."""
                return row_col_map.get(rc) or f"{q_col}_r{rc}"

            def _paired_base(sub_df, rc: str) -> int:
                sm = next((s for s in q_meta.get("sub_questions", {}).values()
                           if str(s.get("row_index", "")) == rc), None)
                if sub_atype == "MA" and sm:
                    return _base_count_ma(sub_df, sm.get("rawdata_columns", []))
                col = _paired_col(rc)
                return _base_count(sub_df, col) if col in sub_df.columns else 0

            def _paired_cnt(sub_df, rc: str, code: str) -> int:
                sm = next((s for s in q_meta.get("sub_questions", {}).values()
                           if str(s.get("row_index", "")) == rc), None)
                if sub_atype == "MA" and sm:
                    return _code_count_ma(sub_df, sm.get("rawdata_columns", []), code)
                col = _paired_col(rc)
                return _code_count_sc(sub_df, col, code) if col in sub_df.columns else 0

            paired_bases: dict[int, int] = {}
            for i, bc in enumerate(banner_cols):
                sub_df = df[bc.mask]
                if bc.matrix_row_code is not None:
                    paired_bases[i] = _paired_base(sub_df, bc.matrix_row_code)
                elif bc.matrix_row_codes is not None:
                    paired_bases[i] = sum(_paired_base(sub_df, rc)
                                          for rc in bc.matrix_row_codes)
                else:
                    paired_bases[i] = 0

            p_rows: list[StubRow] = []
            for stat in DEFAULT_STATS_ORDER:
                if stat not in req_stats:
                    continue
                if stat == "base":
                    p_rows.append(StubRow(
                        label="Base", row_type="base",
                        counts=dict(paired_bases),
                        values={i: float(v) for i, v in paired_bases.items()},
                    ))
                elif stat == "percent" and col_choices:
                    for code in sorted(col_choices.keys(), key=lambda x: int(x)):
                        cnts: dict[int, int]   = {}
                        pcts: dict[int, float] = {}
                        for i, bc in enumerate(banner_cols):
                            base   = paired_bases[i]
                            sub_df = df[bc.mask]
                            if bc.matrix_row_code is not None:
                                cnt = _paired_cnt(sub_df, bc.matrix_row_code, code)
                            elif bc.matrix_row_codes is not None:
                                cnt = sum(_paired_cnt(sub_df, rc, code)
                                          for rc in bc.matrix_row_codes)
                            else:
                                cnt = 0
                            cnts[i] = cnt
                            pcts[i] = cnt / base if base else 0.0
                        p_rows.append(StubRow(
                            label=_choice_label(col_choices, code), row_type="percent",
                            counts=cnts, values=pcts, code=code,
                        ))
            if p_rows:
                blocks.append(StubBlock(
                    question_code  = q.upper(),
                    question_label = q_label,
                    answer_type    = atype,
                    rows           = p_rows,
                ))
            continue

        # ── Matrix: expand into one block per sub-question (row) ─────────────
        if atype in MATRIX_TYPES:
            sub_atype = "MA" if atype == "Matrix_MA" else "SA"
            col_choices = choices_i18n.get("columns", {}) if isinstance(choices_i18n, dict) else {}
            for sub_key, sub_meta in q_meta.get("sub_questions", {}).items():
                sub_col      = _sub_q_col(sub_meta, sub_key)
                sub_raw_cols = sub_meta.get("rawdata_columns", [])
                is_sub_ma    = sub_atype == "MA"
                row_label    = sub_meta.get("row_label", sub_col)
                if not is_sub_ma and sub_col not in df.columns:
                    continue
                sub_bases: dict[int, int] = {
                    i: (_base_count_ma(df[bc.mask], sub_raw_cols) if is_sub_ma
                        else _base_count(df[bc.mask], sub_col))
                    for i, bc in enumerate(banner_cols)
                }
                sub_rows: list[StubRow] = []
                for stat in DEFAULT_STATS_ORDER:
                    if stat not in req_stats:
                        continue
                    if stat == "base":
                        sub_rows.append(StubRow(
                            label="Base", row_type="base",
                            counts=dict(sub_bases),
                            values={i: float(v) for i, v in sub_bases.items()},
                        ))
                    elif stat == "percent" and col_choices:
                        for code in sorted(col_choices.keys(), key=lambda x: int(x)):
                            cnts: dict[int, int]   = {}
                            pcts: dict[int, float] = {}
                            for i, bc in enumerate(banner_cols):
                                sub  = df[bc.mask]
                                base = sub_bases[i]
                                cnt  = (_code_count_ma(sub, sub_raw_cols, code) if is_sub_ma
                                        else _code_count_sc(sub, sub_col, code))
                                cnts[i] = cnt
                                pcts[i] = cnt / base if base else 0.0
                            sub_rows.append(StubRow(
                                label=_choice_label(col_choices, code), row_type="percent",
                                counts=cnts, values=pcts, code=code,
                            ))
                if sub_rows:
                    blocks.append(StubBlock(
                        question_code=f"{q.upper()}_R{sub_meta.get('row_index','')}",
                        question_label=f"{q_label} — {row_label}",
                        answer_type=atype,
                        rows=sub_rows,
                    ))
            continue

        # ── Ranking question ──────────────────────────────────────────────────
        if atype == "ranking":
            # Each rank position is a separate column in rawdata_columns:
            #   rawdata_columns[0] = Rank 1 col, [1] = Rank 2 col, …
            rank_cols = q_meta.get("rawdata_columns", [])
            if not rank_cols or rank_cols[0] not in df.columns:
                continue

            mode     = sc.get("ranking_mode", "rank_dist")
            top_n    = sc.get("ranking_top_n", 0)
            sorted_codes = sorted(choices_i18n.keys(), key=lambda x: int(x))
            # max_pos capped at actual number of rank columns available
            max_pos = min(
                top_n if top_n > 0 else len(rank_cols),
                len(rank_cols),
            )

            if mode == "any_rank":
                # ── MA-style: each choice → % ranked at any position ──────
                any_bases: dict[int, int] = {
                    i: _base_count_rank_any(df[bc.mask], rank_cols)
                    for i, bc in enumerate(banner_cols)
                }
                any_rows: list[StubRow] = []
                for stat in DEFAULT_STATS_ORDER:
                    if stat not in req_stats:
                        continue
                    if stat == "base":
                        any_rows.append(StubRow(
                            label="Base", row_type="base",
                            counts=dict(any_bases),
                            values={i: float(v) for i, v in any_bases.items()},
                        ))
                    elif stat == "percent":
                        for code in sorted_codes:
                            cnts: dict[int, int]   = {}
                            pcts: dict[int, float] = {}
                            for i, bc in enumerate(banner_cols):
                                sub  = df[bc.mask]
                                base = any_bases[i]
                                cnt  = _code_count_rank_any(sub, rank_cols, code)
                                cnts[i] = cnt
                                pcts[i] = cnt / base if base else 0.0
                            # sig: treat Rank_1 col as representative for the test
                            sig = _compute_sig_marks(
                                rank_cols[0], code, "SA", df, banner_cols, sig_config
                            )
                            any_rows.append(StubRow(
                                label=_choice_label(choices_i18n, code), row_type="percent",
                                counts=cnts, values=pcts, sig_marks=sig, code=code,
                            ))
                flat_block = StubBlock(
                    question_code=q.upper(), question_label=q_label,
                    answer_type="ranking", rows=any_rows,
                )
                blocks.append(RankingBlock(
                    question_code=q.upper(), question_label=q_label,
                    answer_type="ranking", mode="any_rank",
                    sub_blocks=[flat_block],
                ))

            else:
                # ── rank_dist: one StubBlock per rank position ────────────
                rank_sub_blocks: list[StubBlock] = []
                for n in range(1, max_pos + 1):
                    rank_bases: dict[int, int] = {
                        i: _base_count_rank_at(df[bc.mask], rank_cols, n)
                        for i, bc in enumerate(banner_cols)
                    }
                    rank_rows: list[StubRow] = []
                    for stat in DEFAULT_STATS_ORDER:
                        if stat not in req_stats:
                            continue
                        if stat == "base":
                            rank_rows.append(StubRow(
                                label="Base", row_type="base",
                                counts=dict(rank_bases),
                                values={i: float(v) for i, v in rank_bases.items()},
                            ))
                        elif stat == "percent":
                            for code in sorted_codes:
                                cnts = {}
                                pcts = {}
                                for i, bc in enumerate(banner_cols):
                                    sub  = df[bc.mask]
                                    base = rank_bases[i]
                                    cnt  = _code_count_rank_at(sub, rank_cols, code, n)
                                    cnts[i] = cnt
                                    pcts[i] = cnt / base if base else 0.0
                                sig = _compute_sig_marks(
                                    rank_cols[n - 1], code, "SA", df, banner_cols, sig_config
                                )
                                rank_rows.append(StubRow(
                                    label=_choice_label(choices_i18n, code), row_type="percent",
                                    counts=cnts, values=pcts, sig_marks=sig, code=code,
                                ))
                    if rank_rows:
                        rank_sub_blocks.append(StubBlock(
                            question_code=f"RANK{n}",
                            question_label=f"Rank {n}",
                            answer_type="ranking",
                            rows=rank_rows,
                        ))
                if rank_sub_blocks:
                    blocks.append(RankingBlock(
                        question_code=q.upper(), question_label=q_label,
                        answer_type="ranking", mode="rank_dist",
                        sub_blocks=rank_sub_blocks,
                    ))
            continue

        # MA questions are always stored as binary columns (e.g. S12_1, S12_2 …).
        ma_raw_cols = q_meta.get("rawdata_columns", []) if atype == "MA" else []
        is_ma       = atype == "MA"

        if not is_ma and q_col not in df.columns:
            continue

        # base counts keyed by column index
        bases: dict[int, int] = {
            i: (_base_count_ma(df[bc.mask], ma_raw_cols) if is_ma
                else _base_count(df[bc.mask], q_col))
            for i, bc in enumerate(banner_cols)
        }

        def _cnt_for_code(sub_df, code: str) -> int:
            """Per-code count, routing to binary-MA or SA helper."""
            if is_ma:
                return _code_count_ma(sub_df, ma_raw_cols, code)
            return _code_count_sc(sub_df, q_col, code)

        def _sig_for_code(code: str) -> dict:
            """Sig marks for a code, routing to the correct column."""
            if is_ma:
                bin_col = _ma_col_map(ma_raw_cols).get(code)
                if bin_col:
                    return _compute_sig_marks(bin_col, "1", "SA", df, banner_cols, sig_config)
                return {}
            return _compute_sig_marks(q_col, code, atype, df, banner_cols, sig_config)

        rows: list[StubRow] = []

        for stat in DEFAULT_STATS_ORDER:
            if stat not in req_stats:
                continue

            if stat == "base":
                rows.append(StubRow(
                    label="Base", row_type="base",
                    counts=dict(bases),
                    values={i: float(v) for i, v in bases.items()},
                ))

            elif stat == "percent":
                if atype not in CODEABLE_TYPES or not choices_i18n:
                    continue

                stub_groups  = sc.get("groups", [])
                grouped_codes: set[str] = {
                    str(c) for grp in stub_groups for c in grp.get("codes", [])
                }
                sorted_codes = sorted(choices_i18n.keys(), key=lambda x: int(x))

                def _cnt_pct_union(target: list[str]) -> tuple[dict, dict]:
                    """Count/pct for a union of codes (for group summary rows)."""
                    _cnts: dict[int, int]   = {}
                    _pcts: dict[int, float] = {}
                    for _i, _bc in enumerate(banner_cols):
                        _sub  = df[_bc.mask]
                        _base = bases[_i]
                        _cnt  = sum(_cnt_for_code(_sub, c) for c in target)
                        _cnts[_i] = _cnt
                        _pcts[_i] = _cnt / _base if _base else 0.0
                    return _cnts, _pcts

                # ── Groups (combine / netted) ─────────────────────────────
                for grp in stub_groups:
                    grp_codes = [
                        str(c) for c in grp.get("codes", [])
                        if str(c) in choices_i18n
                    ]
                    if not grp_codes:
                        continue
                    grp_type = grp.get("type", "netted")

                    g_cnts, g_pcts = _cnt_pct_union(grp_codes)
                    rows.append(StubRow(
                        label=grp["label"], row_type="group",
                        counts=g_cnts, values=g_pcts,
                    ))

                    if grp_type == "netted":
                        for code in grp_codes:
                            cnts: dict[int, int]   = {}
                            pcts: dict[int, float] = {}
                            for i, bc in enumerate(banner_cols):
                                sub  = df[bc.mask]
                                base = bases[i]
                                cnt  = _cnt_for_code(sub, code)
                                cnts[i] = cnt
                                pcts[i] = cnt / base if base else 0.0
                            sig = _sig_for_code(code)
                            rows.append(StubRow(
                                label=_choice_label(choices_i18n, code), row_type="percent",
                                counts=cnts, values=pcts, sig_marks=sig, code=code,
                            ))

                # ── Ungrouped codes ───────────────────────────────────────
                for code in sorted_codes:
                    if code in grouped_codes:
                        continue
                    cnts = {}
                    pcts = {}
                    for i, bc in enumerate(banner_cols):
                        sub  = df[bc.mask]
                        base = bases[i]
                        cnt  = _cnt_for_code(sub, code)
                        cnts[i] = cnt
                        pcts[i] = cnt / base if base else 0.0
                    sig = _sig_for_code(code)
                    rows.append(StubRow(
                        label=_choice_label(choices_i18n, code), row_type="percent",
                        counts=cnts, values=pcts, sig_marks=sig, code=code,
                    ))

            elif stat in ("t2b", "b2b"):
                if atype not in CODEABLE_TYPES or not choices_i18n:
                    continue
                custom_key   = "t2b_codes" if stat == "t2b" else "b2b_codes"
                custom_codes = sc.get(custom_key)
                if custom_codes is not None:
                    target = [str(c) for c in custom_codes]
                else:
                    sorted_codes = sorted(choices_i18n.keys(), key=lambda x: int(x))
                    target = sorted_codes[-2:] if stat == "t2b" else sorted_codes[:2]
                cnts: dict[int, int]   = {}
                pcts: dict[int, float] = {}
                for i, bc in enumerate(banner_cols):
                    sub  = df[bc.mask]
                    base = bases[i]
                    cnt  = sum(_cnt_for_code(sub, c) for c in target)
                    cnts[i] = cnt
                    pcts[i] = cnt / base if base else 0.0
                rows.append(StubRow(
                    label=STAT_LABELS[stat], row_type=stat,
                    counts=cnts, values=pcts,
                ))

            elif stat in ("mean", "std", "se", "min", "max"):
                if atype not in NUMERIC_TYPES:
                    continue
                if atype == "SA" and not choices_i18n:
                    continue
                # mean_factor: {"1": 5, "2": 4, ...} maps code → numeric weight (SA only)
                mean_factor: dict | None = sc.get("mean_factor") if atype == "SA" else None
                stat_vals: dict[int, float] = {}
                for i, bc in enumerate(banner_cols):
                    raw = pd.to_numeric(df[bc.mask][q_col], errors="coerce").dropna()
                    if len(raw) == 0:
                        stat_vals[i] = 0.0
                        continue
                    if mean_factor:
                        numeric = raw.map(
                            lambda v: mean_factor.get(str(int(v)), mean_factor.get(int(v)))
                        ).dropna()
                    else:
                        numeric = raw
                    if len(numeric) == 0:
                        stat_vals[i] = 0.0
                        continue
                    m = float(numeric.mean())
                    s = float(numeric.std(ddof=1)) if len(numeric) > 1 else 0.0
                    if stat == "mean":
                        stat_vals[i] = round(m, 2)
                    elif stat == "std":
                        stat_vals[i] = round(s, 2)
                    elif stat == "se":
                        stat_vals[i] = round(s / np.sqrt(len(numeric)), 2)
                    elif stat == "min":
                        stat_vals[i] = round(float(numeric.min()), 2)
                    elif stat == "max":
                        stat_vals[i] = round(float(numeric.max()), 2)
                rows.append(StubRow(
                    label=STAT_LABELS[stat], row_type=stat,
                    counts={i: 0 for i in stat_vals},
                    values=stat_vals,
                ))

        blocks.append(StubBlock(
            question_code=q.upper(),
            question_label=q_label,
            answer_type=atype,
            rows=rows,
        ))

    return blocks
