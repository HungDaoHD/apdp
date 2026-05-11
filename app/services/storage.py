"""Storage backend abstraction — local filesystem or AWS S3."""
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

    @abstractmethod
    def list_survey_ids(self) -> list[int]: ...

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
        p = (self.base / key).resolve()
        if not p.is_relative_to(self.base.resolve()):
            raise ValueError(f"Forbidden path: {key!r}")
        return p

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


# ── AWS S3 ────────────────────────────────────────────────────────────────────

class S3Storage(StorageBackend):
    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        region: str = "us-east-1",
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
    ):
        import boto3
        kwargs: dict = {"region_name": region}
        if aws_access_key_id and aws_secret_access_key:
            kwargs["aws_access_key_id"] = aws_access_key_id
            kwargs["aws_secret_access_key"] = aws_secret_access_key
        self._s3 = boto3.client("s3", **kwargs)
        self.bucket = bucket
        self.prefix = prefix.rstrip("/")  # e.g. "surveyflow"

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def read_bytes(self, key: str) -> bytes:
        resp = self._s3.get_object(Bucket=self.bucket, Key=self._key(key))
        return resp["Body"].read()

    def write_bytes(self, key: str, data: bytes) -> None:
        self._s3.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)

    def exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except Exception:
            return False

    def list_prefix(self, prefix: str) -> list[str]:
        full_prefix = self._key(prefix)
        results: list[str] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                k = obj["Key"]
                # Strip the storage prefix to get relative key
                rel = k[len(self.prefix) + 1:] if self.prefix else k
                results.append(rel)
        return results

    def list_survey_ids(self) -> list[int]:
        # List top-level "folders" (common prefixes with delimiter)
        full_prefix = self.prefix + "/" if self.prefix else ""
        resp = self._s3.list_objects_v2(
            Bucket=self.bucket, Prefix=full_prefix, Delimiter="/"
        )
        ids = []
        for cp in resp.get("CommonPrefixes", []):
            name = cp["Prefix"].rstrip("/").split("/")[-1]
            if name.isdigit():
                ids.append(int(name))
        return sorted(ids)


# ── Factory (cached singleton) ────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _build_storage() -> StorageBackend:
    from config import settings  # imported here to avoid circular import
    if settings.STORAGE_BACKEND == "s3":
        if not settings.AWS_S3_BUCKET:
            raise RuntimeError("AWS_S3_BUCKET must be set when STORAGE_BACKEND=s3")
        return S3Storage(
            bucket=settings.AWS_S3_BUCKET,
            prefix=settings.AWS_S3_PREFIX,
            region=settings.AWS_REGION,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        )
    base = Path(settings.DATA_DIR)
    if not base.is_absolute():
        from config import _APP_DIR
        base = _APP_DIR / base
    return LocalStorage(base)


def get_storage() -> StorageBackend:
    return _build_storage()
