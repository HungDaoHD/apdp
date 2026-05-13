from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

_APP_DIR = Path(__file__).parent


class Settings(BaseSettings):
    # QMe MCP (OAuth 2.0)
    QME_MCP_BASE_URL:  str = "https://retail.qand.me/api/mcp"
    QME_CLIENT_ID:     str = ""
    QME_CLIENT_SECRET: str = ""
    QME_REDIRECT_URI:  str = "http://localhost:8000/api/qme/callback"

    # Server
    PORT: int = 8000
    SECURE_COOKIES: bool = False  # Set True when running behind HTTPS

    # Storage
    DATA_DIR:         str = "data"
    STORAGE_BACKEND:  str = "local"

    # AWS S3
    AWS_S3_BUCKET:         Optional[str] = None
    AWS_S3_PREFIX:         str = "surveyflow"
    AWS_REGION:            str = "us-east-1"
    AWS_ACCESS_KEY_ID:     Optional[str] = None
    AWS_SECRET_ACCESS_KEY: Optional[str] = None

    model_config = SettingsConfigDict(
        env_file=str(_APP_DIR / ".env"),
        env_file_encoding="utf-8",
    )


settings = Settings()
