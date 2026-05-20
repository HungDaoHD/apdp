"""File I/O helpers for SurveyFlow."""

from __future__ import annotations

import json
from pathlib import Path


def save_csv(df, path: str | Path) -> None:
    """Save a pandas DataFrame to CSV (UTF-8 with BOM for Excel compat)."""
    df.to_csv(path, index=False, encoding="utf-8-sig")


def save_json(data: dict, path: str | Path) -> None:
    """Save a dict to a pretty-printed JSON file (UTF-8)."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_json(path: str | Path) -> dict:
    """Load a JSON file and return the parsed dict."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_csv(path: str | Path):
    """Load a CSV file into a pandas DataFrame."""
    import pandas as pd
    return pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")
