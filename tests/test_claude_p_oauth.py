"""Regression guard: ``call_claude_p`` must bootstrap the OAuth token.

The 2026-07-12 incident fixed ``claude_agent`` / ``plan_tick`` /
``claude_quota``, but ``utils/claude_p.call_claude_p`` still spawned
``claude -p`` with a raw inherited env. The ``/figure`` web canvas
(precis-web, run-as ``deploy``) and ``finding_chase`` both go through
``call_claude_p``, so from a shell-less daemon they 401'd off absent /
stale keychain creds. This pins that ``call_claude_p`` runs
``ensure_oauth_token`` on the subprocess env it passes to ``run_claude``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import precis.utils.claude_p as claude_p
from precis.utils.claude_oauth import ENV_VAR


def _capture_run_claude(captured: dict) -> object:
    def _fake(args, *, binary, label, timeout_s, error_cls, env=None):
        captured["env"] = env
        return SimpleNamespace(stdout='{"ok": true}', stderr="cost: $0.0001")

    return _fake


def _vault(monkeypatch: Any, value: str | None) -> None:
    """Seed the token in the vault — the only store since 2026-08-07."""
    monkeypatch.setattr(
        "precis.secrets.get_secret", lambda name, **kw: value, raising=True
    )


def test_call_claude_p_injects_oauth_token_from_vault(monkeypatch):
    _vault(monkeypatch, "sk-ant-oat01-FIGURE")
    # A shell-less daemon has no token in its own env.
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude")

    captured: dict = {}
    monkeypatch.setattr(claude_p, "run_claude", _capture_run_claude(captured))

    res = claude_p.call_claude_p("draw something. reply JSON {}")

    assert res.data == {"ok": True}
    # The subprocess env carried the token revealed from the vault.
    assert captured["env"] is not None
    assert captured["env"][ENV_VAR] == "sk-ant-oat01-FIGURE"


def test_call_claude_p_does_not_clobber_existing_env_token(monkeypatch):
    _vault(monkeypatch, "sk-ant-oat01-FROMVAULT")
    # A plist/interactive-shell token in the process env must win.
    monkeypatch.setenv(ENV_VAR, "sk-ant-oat01-FROMENV")
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude")

    captured: dict = {}
    monkeypatch.setattr(claude_p, "run_claude", _capture_run_claude(captured))

    claude_p.call_claude_p("draw something. reply JSON {}")

    assert captured["env"][ENV_VAR] == "sk-ant-oat01-FROMENV"


# ── prefer_oauth_over_api_key: OAuth (subscription) wins over the billed key ──

from precis.utils.claude_oauth import (
    API_KEY_VAR,
    prefer_oauth_over_api_key,
)


def test_prefer_oauth_scrubs_api_key_when_token_present() -> None:
    # Both present → OAuth mode, and the API key is removed so the CLI can't
    # pick the per-token-billed path.
    env = {ENV_VAR: "oauth-tok", API_KEY_VAR: "sk-ant-xxx", "KEEP": "y"}
    assert prefer_oauth_over_api_key(env) == "oauth"
    assert API_KEY_VAR not in env
    assert env[ENV_VAR] == "oauth-tok" and env["KEEP"] == "y"


def test_prefer_oauth_keeps_api_key_when_no_token() -> None:
    # No OAuth token → the billed fallback stays, and the mode flags it so the
    # caller can warn.
    env = {API_KEY_VAR: "sk-ant-xxx"}
    assert prefer_oauth_over_api_key(env) == "api_key"
    assert env[API_KEY_VAR] == "sk-ant-xxx"


def test_prefer_oauth_none_when_neither() -> None:
    env: dict[str, str] = {}
    assert prefer_oauth_over_api_key(env) == "none"


# ── bare mode: the deliberate opt-in to API-key (billed) auth ──────────
#
# Everything above is the cheap path and must stay the default. These pin the
# escape hatch a chain rung opts into with ``{"bare": true}``: ``--bare`` skips
# keychain reads, so the OAuth token is unusable and the key must be present
# and NOT scrubbed.


def _capture_args(captured: dict) -> object:
    def _fake(args, *, binary, label, timeout_s, error_cls, env=None):
        captured["args"] = args
        captured["env"] = env
        return SimpleNamespace(stdout='{"ok": true}', stderr="")

    return _fake


def test_bare_uses_api_key_from_vault_and_drops_the_token(monkeypatch):
    # A daemon env carrying only the subscription token; the key is in the vault.
    monkeypatch.setenv(ENV_VAR, "sk-ant-oat01-SUBSCRIPTION")
    monkeypatch.delenv(API_KEY_VAR, raising=False)
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude")
    monkeypatch.setattr(
        "precis.secrets.get_secret",
        lambda name, **kw: "sk-ant-api-KEY" if name == API_KEY_VAR else None,
        raising=True,
    )

    captured: dict = {}
    monkeypatch.setattr(claude_p, "run_claude", _capture_args(captured))

    claude_p.call_claude_p("judge this. reply JSON {}", bare=True)

    assert "--bare" in captured["args"]
    assert captured["env"][API_KEY_VAR] == "sk-ant-api-KEY"
    # The token is dropped: --bare can't read it, and leaving it in makes the
    # resolved credential ambiguous in a subprocess dump.
    assert ENV_VAR not in captured["env"]


def test_bare_prefers_an_env_key_over_the_vault(monkeypatch):
    monkeypatch.setenv(API_KEY_VAR, "sk-ant-api-FROMENV")
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude")
    monkeypatch.setattr(
        "precis.secrets.get_secret", lambda name, **kw: "sk-ant-api-FROMVAULT"
    )

    captured: dict = {}
    monkeypatch.setattr(claude_p, "run_claude", _capture_args(captured))

    claude_p.call_claude_p("judge this. reply JSON {}", bare=True)

    assert captured["env"][API_KEY_VAR] == "sk-ant-api-FROMENV"


def test_bare_raises_when_no_key_anywhere(monkeypatch):
    import pytest

    monkeypatch.delenv(API_KEY_VAR, raising=False)
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude")
    monkeypatch.setattr("precis.secrets.get_secret", lambda name, **kw: None)
    monkeypatch.setattr(claude_p, "run_claude", _capture_args({}))

    # Fails loudly rather than spawning claude to exit 1 on an auth error.
    with pytest.raises(claude_p.ClaudePError, match=API_KEY_VAR):
        claude_p.call_claude_p("judge this. reply JSON {}", bare=True)


def test_default_is_not_bare(monkeypatch):
    # The regression that matters: no caller gets billed auth by accident.
    monkeypatch.setenv(ENV_VAR, "sk-ant-oat01-SUBSCRIPTION")
    monkeypatch.setenv(API_KEY_VAR, "sk-ant-api-KEY")
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude")

    captured: dict = {}
    monkeypatch.setattr(claude_p, "run_claude", _capture_args(captured))

    claude_p.call_claude_p("judge this. reply JSON {}")

    assert "--bare" not in captured["args"]
    assert captured["env"][ENV_VAR] == "sk-ant-oat01-SUBSCRIPTION"
    assert API_KEY_VAR not in captured["env"]
