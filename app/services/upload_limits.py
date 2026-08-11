"""Shared cap on uploaded file size — see SEC-008 in the security audit."""
from __future__ import annotations

from fastapi import HTTPException, UploadFile

MAX_UPLOAD_BYTES = 10 * 1024 * 1024   # 10 MB
_CHUNK_SIZE = 1024 * 1024


async def read_upload_capped(file: UploadFile, max_bytes: int = MAX_UPLOAD_BYTES) -> bytes:
    """Read *file* fully, aborting once more than *max_bytes* has been read.

    Reads in chunks and checks the running total rather than trusting the
    Content-Length header — a client can send whatever it wants there, so the
    cap only holds if it is enforced against bytes actually received.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(413, f"File too large — max {max_bytes // (1024 * 1024)}MB")
        chunks.append(chunk)
    return b"".join(chunks)
