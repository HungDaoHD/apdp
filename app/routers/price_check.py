"""Price-check endpoint: upload Guardian.xlsx → compare weeks → return DFI xlsx as base64."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from dependencies import require_qme

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["price-check"])


@router.post("/price-check")
async def price_check(
    file: UploadFile = File(...),
    curr_week: int | None = Form(default=None),
    prev_week: int | None = Form(default=None),
    session_id: str = Depends(require_qme),
):
    if not (file.filename or '').lower().endswith('.xlsx'):
        raise HTTPException(400, "File must be .xlsx")

    try:
        xlsx_bytes = await file.read()
    except Exception:
        raise HTTPException(400, "Failed to read uploaded file")

    try:
        import asyncio
        from services.price_check_svc import process
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: process(xlsx_bytes, curr_week, prev_week)
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:
        log.exception("price_check failed")
        raise HTTPException(500, f"Processing error — {str(exc)[:300]}")

    return result
