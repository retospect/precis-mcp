"""ensure_oauth_token bootstraps CLAUDE_CODE_OAUTH_TOKEN for the daemon.

Regression for the 2026-07-13 incident: asa's launchd-spawned ``claude -p``
had no CLAUDE_CODE_OAUTH_TOKEN, fell back to the short-lived keychain
credentials, and every turn replied "Failed to authenticate." once those
lapsed. The fix fills the token from ~/.claude_oauth_token.
"""

import sys

import pytest

from asa_bot.oauth import ENV_VAR, ensure_oauth_token

# ``ensure_oauth_token`` locates the token file via ``Path.home()``, which
# reads ``USERPROFILE`` on Windows, not ``HOME`` — so monkeypatching
# ``HOME`` (as these tests do to sandbox the lookup in tmp_path) is a
# no-op there and the real user profile is consulted instead.
_needs_posix_home_env = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Path.home() reads USERPROFILE on Windows, not the monkeypatched HOME",
)


@_needs_posix_home_env
def test_fills_token_from_home_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude_oauth_token").write_text("sk-ant-oat01-abc\n")
    env: dict[str, str] = {}
    ensure_oauth_token(env)
    assert env[ENV_VAR] == "sk-ant-oat01-abc"


def test_existing_token_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude_oauth_token").write_text("from-file\n")
    env = {ENV_VAR: "already-set"}
    ensure_oauth_token(env)
    assert env[ENV_VAR] == "already-set"


def test_missing_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("asa_bot.secrets.reveal_secret", lambda name, **kw: None)
    env: dict[str, str] = {}
    ensure_oauth_token(env)
    assert ENV_VAR not in env


def test_empty_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude_oauth_token").write_text("   \n")
    monkeypatch.setattr("asa_bot.secrets.reveal_secret", lambda name, **kw: None)
    env: dict[str, str] = {}
    ensure_oauth_token(env)
    assert ENV_VAR not in env


# ── slice 0: vault fallback (run as `deploy`, no ~/.claude_oauth_token) ──


def test_falls_back_to_vault_when_no_file(tmp_path, monkeypatch):
    """No file → source the token from the DB vault (factory slice 0)."""
    monkeypatch.setenv("HOME", str(tmp_path))  # no token file here
    monkeypatch.setattr(
        "asa_bot.secrets.reveal_secret",
        lambda name, **kw: "sk-ant-oat01-vault" if name == ENV_VAR else None,
    )
    env: dict[str, str] = {}
    ensure_oauth_token(env)
    assert env[ENV_VAR] == "sk-ant-oat01-vault"


@_needs_posix_home_env
def test_file_wins_over_vault(tmp_path, monkeypatch):
    """The on-disk token still wins when present (vault is the fallback)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude_oauth_token").write_text("from-file\n")
    monkeypatch.setattr(
        "asa_bot.secrets.reveal_secret", lambda name, **kw: "from-vault"
    )
    env: dict[str, str] = {}
    ensure_oauth_token(env)
    assert env[ENV_VAR] == "from-file"


def test_vault_unavailable_is_noop(tmp_path, monkeypatch):
    """reveal_secret returning None (no DSN / unreachable) → no-op, no raise."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("asa_bot.secrets.reveal_secret", lambda name, **kw: None)
    env: dict[str, str] = {}
    ensure_oauth_token(env)
    assert ENV_VAR not in env
