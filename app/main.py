"""SurveyFlow Web App — FastAPI entry point."""
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from routers import surveys, pipeline, qme_auth
from config import Settings

settings = Settings()

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")

app = FastAPI(title="Q&Me SurveyFlow", version="1.0.0")


def require_qme():
    """Dependency: reject request if QMe MCP is not connected."""
    from services.mcp_client import get_storage
    if not get_storage().is_connected():
        raise HTTPException(status_code=401, detail="Not connected to QMe MCP")

_STATIC = Path(__file__).parent / "static"

app.include_router(surveys.router,  dependencies=[Depends(require_qme)])
app.include_router(pipeline.router, dependencies=[Depends(require_qme)])
app.include_router(qme_auth.router)   # auth routes are always public

app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


@app.get("/", response_class=HTMLResponse)
async def root():
    return (_STATIC / "projects.html").read_text(encoding="utf-8")


@app.get("/editor", response_class=HTMLResponse)
async def editor():
    return (_STATIC / "editor.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", port=settings.PORT, reload=True)
