"""Price-check endpoint: upload Guardian.xlsx → compare weeks → return DFI xlsx as base64."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from dependencies import require_csrf, require_qme
from services.upload_limits import read_upload_capped

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["price-check"])


@router.post("/price-check", dependencies=[Depends(require_csrf)])
async def price_check(
    file: UploadFile = File(...),
    curr_week: int | None = Form(default=None),
    prev_week: int | None = Form(default=None),
    session_id: str = Depends(require_qme),
):
    if not (file.filename or '').lower().endswith('.xlsx'):
        raise HTTPException(400, "File must be .xlsx")

    try:
        xlsx_bytes = await read_upload_capped(file)
    except HTTPException:
        raise   # 413 from read_upload_capped — must not be swallowed below
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
        # Deliberate, user-actionable validation message (see price_check_svc.py) —
        # safe and useful to show as-is, unlike the generic exception below.
        raise HTTPException(422, str(exc))
    except Exception:
        log.exception("price_check failed")
        raise HTTPException(500, "Processing error — see server logs")

    return result
