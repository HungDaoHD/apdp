"""Disk-backed IP rate limiter — shared across all Gunicorn workers."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path

from fastapi import HTTPException

logger = logging.getLogger(__name__)

_LOCK_STALE_AGE   = 5      # seconds — a lock older than this is assumed abandoned (holder crashed)
_LOCK_RETRY_DELAY = 0.02   # seconds between lock-acquire attempts
_LOCK_TIMEOUT     = 2.0    # seconds — give up waiting and proceed unlocked rather than block a request
_WRITE_RETRIES    = 10     # os.replace() can transiently fail with PermissionError on Windows if
                           # another process has the destination open — retry briefly rather than
                           # drop the update (only reachable if the lock-timeout fallback above
                           # already let two callers race; Linux's os.replace() never has this issue)

# On Windows, creating a lock file that another process is mid-create/mid-delete
# on can raise PermissionError instead of FileExistsError — both mean "busy,
# try again", so both are treated as a normal contended-lock retry rather than
# an unexpected error.
_LOCK_BUSY_ERRORS = (FileExistsError, PermissionError)


class _FileLock:
    """Minimal cross-platform mutex via atomic file creation (O_CREAT|O_EXCL).

    Works identically on Windows (local dev) and Linux (production
    container) — no fcntl/msvcrt needed. Without this, two Gunicorn workers
    racing to update the same counter file produce a lost update: both read
    the same "attempts" list, both append to their own copy, and whichever
    writes last silently discards the other's increment — letting a client
    through past the configured limit. This lock makes the read-check-write
    in check() atomic across workers.

    A lock file older than _LOCK_STALE_AGE is treated as abandoned (its
    holder crashed mid-request) and force-removed, so a dead worker can
    never wedge the limiter shut for everyone else indefinitely.
    """

    def __init__(self, path: Path):
        self._path = path

    def __enter__(self) -> "_FileLock":
        deadline = time.monotonic() + _LOCK_TIMEOUT
        while True:
            try:
                fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return self
            except _LOCK_BUSY_ERRORS:
                try:
                    stale = time.time() - self._path.stat().st_mtime > _LOCK_STALE_AGE
                except FileNotFoundError:
                    continue  # released between our check and now — retry immediately
                if stale:
                    try:
                        self._path.unlink()
                    except FileNotFoundError:
                        pass  # someone else already cleared it — fine, just retry
                    except OSError:
                        pass  # e.g. Windows: legitimate holder deleted/recreated it mid-check
                    continue
                if time.monotonic() >= deadline:
                    # Never block a request indefinitely over a lock — worst
                    # case we fall back to the old unlocked behavior for this
                    # one call instead of hanging.
                    logger.warning("rate_limiter: lock %s busy past timeout — proceeding unlocked",
                                    self._path.name)
                    return self
                time.sleep(_LOCK_RETRY_DELAY)

    def __exit__(self, *exc_info) -> None:
        try:
            self._path.unlink()
        except OSError:
            pass  # already gone, or momentarily contested — never let releasing
                   # our own lock raise and mask the real result of the request


def _write_atomic(rate_file: Path, tmp: Path, payload: str) -> None:
    """Write *payload* to *rate_file* via a temp file + atomic replace.

    Retries the replace step on PermissionError/OSError — reachable only
    when the lock-timeout fallback lets two callers touch the same file at
    once, and even then only on Windows (POSIX rename doesn't fail just
    because another process has the destination open).
    """
    tmp.write_text(payload)
    for attempt in range(_WRITE_RETRIES):
        try:
            tmp.replace(rate_file)
            return
        except (PermissionError, OSError):
            if attempt == _WRITE_RETRIES - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.01)


def check(ip: str, data_dir: str, *,
          limit: int = 5, window: int = 60, prefix: str = "rate") -> None:
    """Raise HTTP 429 if *ip* exceeds *limit* requests within *window* seconds."""
    now = time.time()
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16]
    data_path = Path(data_dir)
    rate_file = data_path / f".{prefix}_{ip_hash}.json"
    lock_file = data_path / f".{prefix}_{ip_hash}.lock"

    try:
        data_path.mkdir(parents=True, exist_ok=True)
        with _FileLock(lock_file):
            try:
                attempts = [t for t in json.loads(rate_file.read_text()).get("a", [])
                            if now - t < window]
            except FileNotFoundError:
                attempts = []
            except Exception:
                logger.warning("rate_limiter: could not read %s — treating as no prior attempts",
                                rate_file.name)
                attempts = []

            if len(attempts) >= limit:
                raise HTTPException(429, f"Too many requests — please wait {window}s")

            attempts.append(now)
            # PID-suffixed so a lock timeout fallback (proceeding unlocked)
            # can't collide with another worker's in-flight tmp file either.
            tmp = rate_file.with_name(f"{rate_file.name}.{os.getpid()}.tmp")
            _write_atomic(rate_file, tmp, json.dumps({"a": attempts}))
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("rate_limiter: unexpected error for ip_hash=%s — allowing request through: %r",
                        ip_hash, exc)
