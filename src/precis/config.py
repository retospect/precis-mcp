"""Server configuration. Loaded once at startup, frozen.

All env vars use the `PRECIS_` prefix. A `.env` file in CWD is consulted
as a lower-precedence source.
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARN", "WARNING", "ERROR"]
EmbedderName = Literal["mock", "bge-m3"]


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
    embedder: EmbedderName = "mock"
    """Which `Embedder` implementation to load.

    - ``"mock"`` (default): deterministic, no model load. Use for tests
      and local smoke runs.
    - ``"bge-m3"``: load `BAAI/bge-m3` via `sentence-transformers`
      (heavy; requires the optional `paper` extra). Use for production.
    """

    markdown_root: str | None = None
    """Root directory for the ``markdown`` kind (phase 6).

    Files under this path are addressable as ``markdown:<slug>`` where
    the slug encodes the file's relative path (``foo/bar.md`` becomes
    ``foo--bar``). When unset, the ``markdown`` kind is hidden.
    Set via ``PRECIS_MARKDOWN_ROOT`` in the env.
    """

    python_roots: str | None = None
    """Python repos exposed to the ``python`` kind.

    Format: ``alias1:/abs/path1,alias2:/abs/path2``. Each alias is the
    repo's short identifier used in addresses (e.g. ``precis::pkg.mod``);
    each path is an absolute directory. Unparseable entries (missing
    ``:``, non-existent path, duplicate alias) are dropped with a
    warning; the remaining valid entries form the handler's known
    roots. When unset (or zero valid entries), the ``python`` kind is
    hidden. Set via ``PRECIS_PYTHON_ROOTS`` in the env.
    """


def load_config() -> PrecisConfig:
    return PrecisConfig()
