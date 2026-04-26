"""Server configuration. Loaded once at startup, frozen.

All env vars use the `PRECIS_` prefix. A `.env` file in CWD is consulted
as a lower-precedence source.
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARN", "WARNING", "ERROR"]


class PrecisConfig(BaseSettings):
    """Loaded from env (PRECIS_*) and optional .env file. Frozen."""

    model_config = SettingsConfigDict(
        env_prefix="PRECIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    log_level: LogLevel = "INFO"
    database_url: str | None = None  # required from phase 2 onward
    default_corpus: str = "default"


def load_config() -> PrecisConfig:
    return PrecisConfig()
