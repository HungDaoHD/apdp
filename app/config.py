from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

_APP_DIR = Path(__file__).parent


class Settings(BaseSettings):
    QME_API_KEY: str
    DATA_DIR: str = "data"

    model_config = SettingsConfigDict(
        env_file=str(_APP_DIR / ".env"),
        env_file_encoding="utf-8",
    )

    @property
    def QME_MCP_URL(self) -> str:
        return f"https://retail.qand.me/api/mcp?key={self.QME_API_KEY}"

    def survey_dir(self, survey_id: int) -> Path:
        p = Path(self.DATA_DIR)
        if not p.is_absolute():
            p = _APP_DIR / p
        return p / str(survey_id)


settings = Settings()
