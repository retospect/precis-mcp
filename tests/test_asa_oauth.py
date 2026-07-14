"""ensure_oauth_token bootstraps CLAUDE_CODE_OAUTH_TOKEN for the daemon.

Regression for the 2026-07-13 incident: asa's launchd-spawned ``claude -p``
had no CLAUDE_CODE_OAUTH_TOKEN, fell back to the short-lived keychain
credentials, and every turn replied "Failed to authenticate." once those
lapsed. The fix fills the token from ~/.claude_oauth_token.
"""

from asa_bot.oauth import ENV_VAR, ensure_oauth_token


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
    env: dict[str, str] = {}
    ensure_oauth_token(env)
    assert ENV_VAR not in env


def test_empty_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude_oauth_token").write_text("   \n")
    env: dict[str, str] = {}
    ensure_oauth_token(env)
    assert ENV_VAR not in env
