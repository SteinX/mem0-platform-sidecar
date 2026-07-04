from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SidecarSettings(BaseSettings):
    database_url: str = Field(default="sqlite:///./mem0_sidecar.sqlite3")
    mem0_base_url: str = Field(default="http://127.0.0.1:8000")
    mem0_api_key: str | None = Field(default=None)
    mem0_api_key_header_name: str = Field(default="X-API-Key")
    mem0_api_key_prefix: str = Field(default="")
    mem0_extra_headers: dict[str, str] = Field(default_factory=dict)
    mem0_request_timeout_seconds: float = Field(default=30.0, gt=0)
    mem0_connect_timeout_seconds: float | None = Field(default=None, gt=0)
    mem0_verify_tls: bool = Field(default=True)
    mem0_ca_bundle: str | None = Field(default=None)
    mem0_memories_path: str = Field(default="/memories")
    mem0_search_path: str = Field(default="/search")
    default_project_id: str = Field(default="default")
    worker_poll_interval_seconds: float = Field(default=1.0, ge=0.1)
    log_level: str = Field(default="INFO")
    log_format: Literal["text", "json"] = Field(default="text")
    request_id_header: str = Field(default="X-Request-ID")

    model_config = SettingsConfigDict(
        env_prefix="MEM0_SIDECAR_",
        env_file=".env",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def load_settings() -> SidecarSettings:
    return SidecarSettings()
