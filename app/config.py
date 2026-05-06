from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

_APP_DIR = Path(__file__).parent


class Settings(BaseSettings):
    QME_API_KEY: str
    DATA_DIR: str = "data"

    # Storage backend: "local" (default) or "supabase"
    STORAGE_BACKEND: str = "local"
    SUPABASE_URL: Optional[str] = None
    SUPABASE_KEY: Optional[str] = None
    SUPABASE_BUCKET: str = "surveyflow"

    model_config = SettingsConfigDict(
        env_file=str(_APP_DIR / ".env"),
        env_file_encoding="utf-8",
    )

    @property
    def QME_MCP_URL(self) -> str:
        return f"https://retail.qand.me/api/mcp?key={self.QME_API_KEY}"


settings = Settings()
