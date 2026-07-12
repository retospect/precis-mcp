"""Tests for the shared OAuth-token bootstrap (``utils/claude_oauth``).

Regression guard for the 2026-07-12 incident: ``plan_tick`` / ``claude_quota``
spawned ``claude -p`` with a raw ``dict(os.environ)`` and 401'd off stale
keychain creds because they never loaded ``~/.claude_oauth_token`` the way
``claude_agent`` did.
"""

from __future__ import annotations

from pathlib import Path

from precis.utils.claude_oauth import ENV_VAR, ensure_oauth_token


def test_loads_token_from_file_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude_oauth_token").write_text("sk-ant-oat01-TESTTOKEN\n")
    env: dict[str, str] = {}
    ensure_oauth_token(env)
    assert env[ENV_VAR] == "sk-ant-oat01-TESTTOKEN"  # stripped of trailing \n


def test_existing_env_token_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude_oauth_token").write_text("sk-ant-oat01-FROMFILE")
    env = {ENV_VAR: "sk-ant-oat01-FROMENV"}
    ensure_oauth_token(env)
    assert env[ENV_VAR] == "sk-ant-oat01-FROMENV"  # override not clobbered


def test_missing_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)  # no token file written
    env: dict[str, str] = {}
    ensure_oauth_token(env)
    assert ENV_VAR not in env


def test_empty_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude_oauth_token").write_text("   \n")
    env: dict[str, str] = {}
    ensure_oauth_token(env)
    assert ENV_VAR not in env


def test_empty_env_value_is_treated_as_absent(tmp_path, monkeypatch):
    # An empty ``CLAUDE_CODE_OAUTH_TOKEN`` in the env is useless — fill it
    # from the file rather than sending a blank token that would 401.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude_oauth_token").write_text("sk-ant-oat01-FROMFILE")
    env = {ENV_VAR: ""}
    ensure_oauth_token(env)
    assert env[ENV_VAR] == "sk-ant-oat01-FROMFILE"
