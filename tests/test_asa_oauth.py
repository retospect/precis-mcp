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
from typing import Any

from asa_bot.oauth import ENV_VAR, ensure_oauth_token


def _vault(monkeypatch: Any, value: str | None) -> None:
    monkeypatch.setattr(
        "asa_bot.secrets.reveal_secret", lambda name, **kw: value, raising=True
    )


def test_fills_token_from_vault(monkeypatch):
    _vault(monkeypatch, "sk-ant-oat01-abc")
    env: dict[str, str] = {}
    ensure_oauth_token(env)
    assert env[ENV_VAR] == "sk-ant-oat01-abc"


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
    (tmp_path / ".claude_oauth_token").write_text("stale-file\n")
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
