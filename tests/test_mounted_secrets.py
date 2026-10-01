"""Bootstrap secrets can come from a mounted directory instead of env (gr458350).

The shared MCP server was launched with ``-e PRECIS_DATABASE_URL=…`` and the
API keys, so ``docker inspect`` printed them in cleartext. The API keys
already resolve through :func:`precis.secrets.get_secret`'s file layer; these
pin the two values that need to be read before the vault is reachable: the
DSN and the MCP bearer token. Both read the file only when
``PRECIS_SECRETS_FILE_DIR`` is set explicitly, so a host process never picks
up the prod DSN from ``~/.secrets/pw`` by accident.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis import mcp_supervisor
from precis.config import PrecisConfig
from precis.secrets import mounted_secret


@pytest.fixture
def secrets_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "PRECIS_DATABASE_URL").write_text(
        "postgresql://u:pw@db/precis\n", encoding="utf-8"
    )
    (tmp_path / "PRECIS_MCP_TOKEN").write_text("tok\n", encoding="utf-8")
    monkeypatch.delenv("PRECIS_DATABASE_URL", raising=False)
    monkeypatch.delenv("PRECIS_MCP_TOKEN", raising=False)
    monkeypatch.setenv("PRECIS_SECRETS_FILE_DIR", str(tmp_path))
    return tmp_path


def test_mounted_secret_reads_the_file_stripped(secrets_dir: Path) -> None:
    assert mounted_secret("PRECIS_MCP_TOKEN") == "tok"
    assert mounted_secret("MISSING") is None


def test_no_mounted_dir_reads_nothing(
    secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRECIS_SECRETS_FILE_DIR")
    assert mounted_secret("PRECIS_MCP_TOKEN") is None
    assert PrecisConfig(_env_file=None).database_url is None  # type: ignore[call-arg]


def test_config_takes_the_dsn_from_the_mounted_dir(secrets_dir: Path) -> None:
    cfg = PrecisConfig(_env_file=None)  # type: ignore[call-arg]
    assert cfg.database_url == "postgresql://u:pw@db/precis"


def test_env_dsn_still_wins_over_the_file(
    secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_DATABASE_URL", "postgresql://env/precis")
    cfg = PrecisConfig(_env_file=None)  # type: ignore[call-arg]
    assert cfg.database_url == "postgresql://env/precis"


def test_supervisor_reads_the_token_from_the_mounted_dir(
    secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert mcp_supervisor._mcp_token() == "tok"
    monkeypatch.setenv("PRECIS_MCP_TOKEN", "from-env")
    assert mcp_supervisor._mcp_token() == "from-env"
    monkeypatch.delenv("PRECIS_MCP_TOKEN")
    monkeypatch.delenv("PRECIS_SECRETS_FILE_DIR")
    assert mcp_supervisor._mcp_token() is None
