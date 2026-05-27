"""Usage log — append events, compute per-user summaries."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.Lock()

FW_USERS: set[str] = {
    "bichthao.duong@asia-plus.net",
    "huyen.nguyen94@asia-plus.net",
    "pha.dang@asia-plus.net",
    "thuthao.nguyen@asia-plus.net",
    "diem.nguyen@asia-plus.net",
    "huy.le@asia-plus.net",
    "lieu.tran@asia-plus.net",
    "tananh.nguyen@asia-plus.net",
    "tan.nguyen@asia-plus.net",
    "vananh.do@asia-plus.net",
}

# Human-readable action labels
ACTION_LABELS: dict[str, str] = {
    "login":         "Login",
    "logout":        "Logout",
    "page_load":     "Page load / refresh",
    "preview":       "Preview",
    "save":          "Save datatable",
    "download":      "Download XLSX",
    "open_datatable": "Open datatable file",
}


def _log_file() -> Path:
    from config import settings
    p = Path(settings.DATA_DIR)
    if not p.is_absolute():
        try:
            from config import _APP_DIR
            p = _APP_DIR / p
        except ImportError:
            pass
    p.mkdir(parents=True, exist_ok=True)
    return p / "usage_log.jsonl"


def append(
    email: str | None,
    action: str,
    survey_id: int | None = None,
    survey_name: str | None = None,
) -> None:
    """Append one log record (thread-safe)."""
    record = {
        "ts":          datetime.now(timezone.utc).isoformat(),
        "email":       email or "unknown",
        "action":      action,
        "survey_id":   survey_id,
        "survey_name": survey_name,
    }
    with _LOCK:
        with _log_file().open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def summary(fw_only: bool = True) -> dict:
    """Return per-user action counts and last_seen timestamp."""
    path = _log_file()
    users: dict[str, dict] = {}
    total = 0

    if path.exists():
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                email = rec.get("email", "unknown")
                if fw_only and email not in FW_USERS:
                    continue
                total += 1
                if email not in users:
                    users[email] = {
                        "email":     email,
                        "actions":   {},
                        "last_seen": None,
                        "total":     0,
                        "surveys":   set(),
                    }
                u = users[email]
                action = rec.get("action", "unknown")
                u["actions"][action] = u["actions"].get(action, 0) + 1
                u["total"] += 1
                ts = rec.get("ts", "")
                if not u["last_seen"] or ts > u["last_seen"]:
                    u["last_seen"] = ts
                if rec.get("survey_name"):
                    u["surveys"].add(rec["survey_name"])

    # Ensure every FW user appears even with zero events
    if fw_only:
        for em in FW_USERS:
            if em not in users:
                users[em] = {
                    "email":     em,
                    "actions":   {},
                    "last_seen": None,
                    "total":     0,
                    "surveys":   set(),
                }

    # Convert sets → sorted lists for JSON
    rows = []
    for u in users.values():
        rows.append({**u, "surveys": sorted(u["surveys"])})

    rows.sort(key=lambda x: x.get("last_seen") or "", reverse=True)
    return {
        "fw_users":     sorted(FW_USERS),
        "users":        rows,
        "total_events": total,
    }


def recent(limit: int = 300, fw_only: bool = True) -> list[dict]:
    """Return the last *limit* log records (most recent last)."""
    path = _log_file()
    if not path.exists():
        return []
    records: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if fw_only and rec.get("email", "unknown") not in FW_USERS:
                continue
            records.append(rec)
    return records[-limit:]
