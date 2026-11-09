"""ensure_oauth_token bootstraps CLAUDE_CODE_OAUTH_TOKEN for the daemon.

Regression for the 2026-07-13 incident: asa's launchd-spawned ``claude -p``
had no CLAUDE_CODE_OAUTH_TOKEN, fell back to the short-lived keychain
credentials, and every turn replied "Failed to authenticate." once those
lapsed.

The fix originally read ``~/.claude_oauth_token``; since 2026-08-07 the token
comes from the DB vault only, so asa runs as plain ``deploy`` with no
credential on disk at all.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from asa_bot.oauth import ENV_VAR, ensure_oauth_token


def _vault(monkeypatch: Any, value: str | None) -> None:
    monkeypatch.setattr(
        "asa_bot.secrets.reveal_secret", lambda name, **kw: value, raising=True
    )


def test_fills_token_from_vault(monkeypatch):
    _vault(monkeypatch, ("sk-a" + "nt-oat01-abc"))
    env: dict[str, str] = {}
    ensure_oauth_token(env)
    assert env[ENV_VAR] == ("sk-a" + "nt-oat01-abc")


def test_existing_token_wins(monkeypatch):
    _vault(monkeypatch, "from-vault")
    env = {ENV_VAR: "already-set"}
    ensure_oauth_token(env)
    assert env[ENV_VAR] == "already-set"


def test_vault_unavailable_is_noop(monkeypatch):
    """reveal_secret returning None (no DSN / unreachable) → no-op, no raise."""
    _vault(monkeypatch, None)
    env: dict[str, str] = {}
    ensure_oauth_token(env)
    assert ENV_VAR not in env


def test_home_token_file_is_ignored(tmp_path, monkeypatch):
    """A leftover ``~/.claude_oauth_token`` must NOT shadow the vault — the
    twin of the same guard in ``tests/utils/test_claude_oauth.py``."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude_oauth_token").write_text("stale-file\n", encoding="utf-8")
    _vault(monkeypatch, "rotated-vault")
    env: dict[str, str] = {}
    ensure_oauth_token(env)
    assert env[ENV_VAR] == "rotated-vault"


def test_vault_raising_does_not_break_the_caller(monkeypatch):
    def _boom(name: str, **kw: Any) -> str:
        raise RuntimeError("no DSN")

    monkeypatch.setattr("asa_bot.secrets.reveal_secret", _boom, raising=True)
    env: dict[str, str] = {}
    ensure_oauth_token(env)
    assert ENV_VAR not in env


# ── DSN precedence: config.yaml's precis_system beats a stale env agent_rw ───

_CONFIG_DSN = "postgresql://precis_system@db:6432/precis_prod"
_STALE_ENV_DSN = "postgresql://agent_rw@db:6432/precis_prod"


def _record_connect(monkeypatch: Any) -> list[str]:
    """Stub ``psycopg.connect`` so ``reveal_secret`` records the DSN it dialed."""
    import psycopg

    seen: list[str] = []

    class _Conn:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

        def execute(self, *a: Any, **kw: Any) -> Any:
            return SimpleNamespace(fetchone=lambda: ("tok",))

    def _connect(dsn: str, **kw: Any) -> _Conn:
        seen.append(dsn)
        return _Conn()

    monkeypatch.setattr(psycopg, "connect", _connect)
    return seen


def test_reveal_secret_explicit_dsn_beats_stale_env(monkeypatch):
    from asa_bot.secrets import reveal_secret

    monkeypatch.setenv("PRECIS_DATABASE_URL", _STALE_ENV_DSN)
    seen = _record_connect(monkeypatch)
    assert reveal_secret("X", dsn=_CONFIG_DSN) == "tok"
    assert seen == [_CONFIG_DSN]


def test_reveal_secret_env_is_the_fallback_without_a_dsn(monkeypatch):
    from asa_bot.secrets import reveal_secret

    monkeypatch.setenv("PRECIS_DATABASE_URL", _STALE_ENV_DSN)
    seen = _record_connect(monkeypatch)
    reveal_secret("X")
    assert seen == [_STALE_ENV_DSN]


def test_ensure_oauth_token_threads_the_dsn(monkeypatch):
    seen: list[Any] = []

    def _reveal(name: str, **kw: Any) -> str:
        seen.append(kw.get("dsn"))
        return "tok"

    monkeypatch.setattr("asa_bot.secrets.reveal_secret", _reveal, raising=True)
    env: dict[str, str] = {}
    ensure_oauth_token(env, dsn=_CONFIG_DSN)
    assert seen == [_CONFIG_DSN]
    assert env[ENV_VAR] == "tok"


def test_config_load_yaml_dsn_beats_stale_env(tmp_path, monkeypatch):
    from asa_bot.config import Config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(f'precis:\n  database_url: "{_CONFIG_DSN}"\n', encoding="utf-8")
    monkeypatch.setenv("PRECIS_DATABASE_URL", _STALE_ENV_DSN)
    assert Config.load(str(cfg_file)).precis.database_url == _CONFIG_DSN


def test_config_load_env_dsn_is_the_fallback(tmp_path, monkeypatch):
    from asa_bot.config import Config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("precis: {}\n", encoding="utf-8")
    monkeypatch.setenv("PRECIS_DATABASE_URL", _STALE_ENV_DSN)
    assert Config.load(str(cfg_file)).precis.database_url == _STALE_ENV_DSN


def test_config_load_yaml_notify_dsn_beats_stale_env(tmp_path, monkeypatch):
    """gr605686: notify_database_url follows the same config-wins rule."""
    from asa_bot.config import Config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        f'precis:\n  notify_database_url: "{_CONFIG_DSN}"\n', encoding="utf-8"
    )
    monkeypatch.setenv("PRECIS_NOTIFY_DATABASE_URL", _STALE_ENV_DSN)
    assert Config.load(str(cfg_file)).precis.notify_database_url == _CONFIG_DSN


def test_config_load_env_notify_dsn_is_the_fallback(tmp_path, monkeypatch):
    from asa_bot.config import Config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("precis: {}\n", encoding="utf-8")
    monkeypatch.setenv("PRECIS_NOTIFY_DATABASE_URL", _STALE_ENV_DSN)
    assert Config.load(str(cfg_file)).precis.notify_database_url == _STALE_ENV_DSN


def test_precis_child_env_gets_config_dsn_not_stale_env(monkeypatch):
    import asyncio

    from asa_bot.config import PrecisConfig
    from asa_bot.precis_client import PrecisClient

    monkeypatch.setenv("PRECIS_DATABASE_URL", _STALE_ENV_DSN)
    captured: dict[str, str] = {}

    async def _fake_exec(*argv: Any, **kw: Any) -> Any:
        captured.update(kw["env"])
        return SimpleNamespace(pid=1, returncode=None)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    client = PrecisClient(PrecisConfig(command=["x"], database_url=_CONFIG_DSN))
    asyncio.run(client._spawn())
    assert captured["PRECIS_DATABASE_URL"] == _CONFIG_DSN


def test_invoke_reveals_over_the_config_dsn(tmp_path, monkeypatch):
    """invoke(dsn=...) reaches the OAuth reveal, not the env var."""
    import asyncio
    import dataclasses

    import precis.utils.claude_agent as claude_agent
    from asa_bot.claude_invoke import invoke
    from asa_bot.config import LLMConfig

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PRECIS_DATABASE_URL", _STALE_ENV_DSN)
    monkeypatch.setattr("asa_bot.claude_invoke._open_turn_log", lambda *a, **kw: None)
    seen: list[Any] = []

    def _reveal(name: str, **kw: Any) -> None:
        seen.append(kw.get("dsn"))

    monkeypatch.setattr("asa_bot.secrets.reveal_secret", _reveal, raising=True)

    async def _fake_run(argv: list[str], **kw: Any) -> Any:
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(claude_agent, "run_claude_async", _fake_run)
    cfg = dataclasses.replace(
        LLMConfig(), command=["claude", "-p", "--model", "claude-opus-4-8"]
    )
    asyncio.run(invoke(cfg, "sys", "hi", conv_slug="c-dsn", dsn=_CONFIG_DSN))
    assert _CONFIG_DSN in seen
    assert _STALE_ENV_DSN not in seen
