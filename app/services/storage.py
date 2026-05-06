"""Storage backend abstraction — local filesystem or Supabase Storage."""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path


class StorageBackend(ABC):
    @abstractmethod
    def read_bytes(self, key: str) -> bytes: ...

    @abstractmethod
    def write_bytes(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def list_prefix(self, prefix: str) -> list[str]: ...

    # ── convenience helpers ───────────────────────────────────────────────────

    def read_text(self, key: str) -> str:
        return self.read_bytes(key).decode("utf-8")

    def write_text(self, key: str, text: str) -> None:
        self.write_bytes(key, text.encode("utf-8"))

    def read_json(self, key: str):
        return json.loads(self.read_text(key))

    def write_json(self, key: str, data) -> None:
        self.write_text(key, json.dumps(data, ensure_ascii=False, indent=2))

    def next_version(self, survey_id: int) -> str:
        keys = self.list_prefix(f"{survey_id}/")
        existing = [
            int(m.group(1))
            for k in keys
            if (m := re.search(r"/(v(\d+))/", k + "/"))
        ]
        return f"v{max(existing, default=0) + 1}"


# ── Local filesystem ──────────────────────────────────────────────────────────

class LocalStorage(StorageBackend):
    def __init__(self, base_dir: Path):
        self.base = Path(base_dir)

    def _p(self, key: str) -> Path:
        return self.base / key

    def read_bytes(self, key: str) -> bytes:
        return self._p(key).read_bytes()

    def write_bytes(self, key: str, data: bytes) -> None:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def exists(self, key: str) -> bool:
        return self._p(key).exists()

    def list_prefix(self, prefix: str) -> list[str]:
        base = self._p(prefix)
        if not base.exists():
            return []
        return [
            str(p.relative_to(self.base)).replace("\\", "/")
            for p in base.rglob("*")
            if p.is_file()
        ]

    def list_survey_ids(self) -> list[int]:
        if not self.base.exists():
            return []
        return sorted([
            int(p.name)
            for p in self.base.iterdir()
            if p.is_dir() and p.name.isdigit()
        ])


# ── Supabase Storage ──────────────────────────────────────────────────────────

class SupabaseStorage(StorageBackend):
    def __init__(self, url: str, key: str, bucket: str):
        from supabase import create_client
        self._sb = create_client(url, key)
        self.bucket = bucket

    def _store(self):
        return self._sb.storage.from_(self.bucket)

    def read_bytes(self, key: str) -> bytes:
        return self._store().download(key)

    def write_bytes(self, key: str, data: bytes) -> None:
        self._store().upload(key, data, {"upsert": "true"})

    def exists(self, key: str) -> bool:
        parts = key.rsplit("/", 1)
        folder = parts[0] if len(parts) == 2 else ""
        name = parts[-1]
        try:
            files = self._store().list(folder) or []
            return any(f.get("name") == name for f in files)
        except Exception:
            return False

    def list_prefix(self, prefix: str) -> list[str]:
        results: list[str] = []
        self._list_recursive(prefix.rstrip("/"), results)
        return results

    def _list_recursive(self, folder: str, results: list[str]) -> None:
        try:
            items = self._store().list(folder) or []
        except Exception:
            return
        for item in items:
            name = item.get("name", "")
            if not name:
                continue
            full = f"{folder}/{name}" if folder else name
            if item.get("metadata") is not None:  # file
                results.append(full)
            else:  # subfolder
                self._list_recursive(full, results)

    def list_survey_ids(self) -> list[int]:
        try:
            items = self._store().list("") or []
        except Exception:
            return []
        ids = []
        for item in items:
            name = item.get("name", "")
            if name and name.isdigit() and item.get("metadata") is None:
                ids.append(int(name))
        return sorted(ids)


# ── Factory (cached singleton) ────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _build_storage() -> StorageBackend:
    from config import settings  # imported here to avoid circular import
    if settings.STORAGE_BACKEND == "supabase":
        if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
            raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set when STORAGE_BACKEND=supabase")
        return SupabaseStorage(settings.SUPABASE_URL, settings.SUPABASE_KEY, settings.SUPABASE_BUCKET)
    base = Path(settings.DATA_DIR)
    if not base.is_absolute():
        from config import _APP_DIR
        base = _APP_DIR / base
    return LocalStorage(base)


def get_storage() -> StorageBackend:
    return _build_storage()
