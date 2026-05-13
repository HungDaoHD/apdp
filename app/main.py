"""SurveyFlow Web App — FastAPI entry point."""
import logging
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from routers import surveys, pipeline, qme_auth
from config import Settings
from dependencies import require_qme

settings = Settings()

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")

app = FastAPI(title="Q&Me SurveyFlow", version="1.0.0")


@app.on_event("startup")
async def on_startup() -> None:
    """Restore persisted sessions from disk so users don't need to re-login after restart."""
    from services.mcp_client import init_sessions
    init_sessions()


_STATIC = Path(__file__).parent / "static"

app.include_router(surveys.router,  dependencies=[Depends(require_qme)])
app.include_router(pipeline.router, dependencies=[Depends(require_qme)])
app.include_router(qme_auth.router)   # auth routes are always public

app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


_NO_CACHE = {"Cache-Control": "no-store"}


@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse((_STATIC / "projects.html").read_text(encoding="utf-8"), headers=_NO_CACHE)


@app.get("/projects", response_class=HTMLResponse)
async def projects():
    return HTMLResponse((_STATIC / "projects.html").read_text(encoding="utf-8"), headers=_NO_CACHE)


@app.get("/editor", response_class=HTMLResponse)
async def editor():
    return HTMLResponse((_STATIC / "editor.html").read_text(encoding="utf-8"), headers=_NO_CACHE)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", port=settings.PORT, reload=True)
