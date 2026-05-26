"""SurveyFlow Web App — FastAPI entry point."""
import logging
import sys
from pathlib import Path

# Ensure app/ directory is on sys.path for local module imports
# (routers, config, services, dependencies).
# surveyflow is installed as a package — editable locally, from GitHub in production.
_APP_DIR = str(Path(__file__).parent)
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from routers import surveys, pipeline, qme_auth
from config import Settings
from dependencies import require_qme

settings = Settings()

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")

app = FastAPI(title="Q&Me SurveyFlow", version="1.0.0")

# M-6: Explicit CORS policy — deny all cross-origin requests (same-origin only)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],        # no cross-origin allowed
    allow_credentials=False,
    allow_methods=[],
    allow_headers=[],
)


@app.on_event("startup")
async def on_startup() -> None:
    """Restore persisted sessions from disk so users don't need to re-login after restart."""
    from services.mcp_client import init_sessions
    init_sessions()
    # H-4: warn if SECURE_COOKIES is off (risky on non-localhost deployments)
    if not settings.SECURE_COOKIES:
        logging.getLogger(__name__).warning(
            "SECURE_COOKIES=False — session cookies sent over HTTP. "
            "Set SECURE_COOKIES=True when deploying behind HTTPS."
        )


_STATIC = Path(__file__).parent / "static"

app.include_router(surveys.router,  dependencies=[Depends(require_qme)])
app.include_router(pipeline.router, dependencies=[Depends(require_qme)])
app.include_router(qme_auth.router)   # auth routes are always public

app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


@app.get("/api/version")
async def get_version():
    try:
        from _version import __version__ as apdp_version
    except Exception:
        apdp_version = "unknown"
    try:
        import surveyflow
        sf_version = surveyflow.__version__
    except Exception:
        sf_version = "unknown"
    return {"apdp": apdp_version, "surveyflow": sf_version}


_NO_CACHE = {"Cache-Control": "no-store"}


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return JSONResponse({"status": "ok"}, headers=_NO_CACHE)


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