from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SidecarSettings(BaseSettings):
    database_url: str = Field(default="sqlite:///./mem0_sidecar.sqlite3")
    mem0_base_url: str = Field(default="http://127.0.0.1:8000")
    mem0_api_key: str | None = Field(default=None)
    default_project_id: str = Field(default="default")
    worker_poll_interval_seconds: float = Field(default=1.0, ge=0.1)

    model_config = SettingsConfigDict(
        env_prefix="MEM0_SIDECAR_",
        env_file=".env",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def load_settings() -> SidecarSettings:
    return SidecarSettings()
