"""Tests for the shared OAuth-token bootstrap (``utils/claude_oauth``).

Regression guard for the 2026-07-12 incident: ``plan_tick`` / ``claude_quota``
spawned ``claude -p`` with a raw ``dict(os.environ)`` and 401'd off stale
keychain creds because they never bootstrapped ``CLAUDE_CODE_OAUTH_TOKEN`` the
way ``claude_agent`` did.

The token now comes from the vault only — the per-user ``~/.claude_oauth_token``
file is retired (2026-08-07), so ``test_home_token_file_is_ignored`` is the
guard that a leftover copy on a node can no longer shadow a vault rotation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from precis.utils.claude_oauth import ENV_VAR, ensure_oauth_token


def _vault(monkeypatch: Any, value: str | None, *, unbound: str | None = None) -> None:
    """Point ``get_secret`` at ``value`` and the unbound one-shot reveal at
    ``unbound`` for every name."""
    monkeypatch.setattr(
        "precis.secrets.get_secret", lambda name, **kw: value, raising=True
    )
    monkeypatch.setattr(
        "precis.secrets.reveal_unbound", lambda name: unbound, raising=True
    )


def test_loads_token_from_vault(monkeypatch):
    _vault(monkeypatch, ("sk-a" + "nt-oat01-TESTTOKEN"))
    env: dict[str, str] = {}
    ensure_oauth_token(env)
    assert env[ENV_VAR] == ("sk-a" + "nt-oat01-TESTTOKEN")


def test_existing_env_token_wins(monkeypatch):
    _vault(monkeypatch, ("sk-a" + "nt-oat01-FROMVAULT"))
    env = {ENV_VAR: ("sk-a" + "nt-oat01-FROMENV")}
    ensure_oauth_token(env)
    assert env[ENV_VAR] == ("sk-a" + "nt-oat01-FROMENV")  # override not clobbered


def test_unresolvable_vault_is_noop(monkeypatch):
    """No store bound / vault down → leave ``claude``'s own resolution alone."""
    _vault(monkeypatch, None)
    env: dict[str, str] = {}
    ensure_oauth_token(env)
    assert ENV_VAR not in env


def test_empty_env_value_is_treated_as_absent(monkeypatch):
    # An empty ``CLAUDE_CODE_OAUTH_TOKEN`` in the env is useless — fill it
    # from the vault rather than sending a blank token that would 401.
    _vault(monkeypatch, ("sk-a" + "nt-oat01-FROMVAULT"))
    env = {ENV_VAR: ""}
    ensure_oauth_token(env)
    assert env[ENV_VAR] == ("sk-a" + "nt-oat01-FROMVAULT")


def test_home_token_file_is_ignored(tmp_path, monkeypatch):
    """A leftover ``~/.claude_oauth_token`` must NOT shadow the vault.

    This is the whole point of the retirement: while the file was read first, a
    rotated vault token was silently ignored on every node that still had a
    stale copy — and the fleet had five of them across two machines. If this
    test ever goes green on the file's value, rotation is broken again.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude_oauth_token").write_text(
        ("sk-a" + "nt-oat01-STALEFILE\n"), encoding="utf-8"
    )
    _vault(monkeypatch, ("sk-a" + "nt-oat01-ROTATED"))
    env: dict[str, str] = {}
    ensure_oauth_token(env)
    assert env[ENV_VAR] == ("sk-a" + "nt-oat01-ROTATED")


def test_vault_raising_does_not_break_the_caller(monkeypatch):
    """``ensure_oauth_token`` sits in a subprocess-spawn path; it must not raise."""

    def _boom(name: str, **kw: Any) -> str:
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr("precis.secrets.get_secret", _boom, raising=True)
    env: dict[str, str] = {}
    ensure_oauth_token(env)  # must not propagate
    assert ENV_VAR not in env


def test_cli_one_shot_reads_the_vault_when_no_store_is_bound(monkeypatch):
    """gr260050: an ssh user's CLI one-shot binds no store, so ``get_secret``
    finds nothing and ``claude`` said "Not logged in". The token must come
    from the vault through the one-shot reveal."""
    _vault(monkeypatch, None, unbound=("sk-a" + "nt-oat01-ONESHOT"))
    env: dict[str, str] = {}
    ensure_oauth_token(env)
    assert env[ENV_VAR] == ("sk-a" + "nt-oat01-ONESHOT")


def test_bound_store_answer_skips_the_one_shot(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "precis.secrets.get_secret", lambda name, **kw: "sk-bound", raising=True
    )
    monkeypatch.setattr(
        "precis.secrets.reveal_unbound", lambda name: calls.append(name), raising=True
    )
    env: dict[str, str] = {}
    ensure_oauth_token(env)
    assert env[ENV_VAR] == "sk-bound"
    assert calls == []
