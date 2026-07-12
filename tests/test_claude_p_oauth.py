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

from pathlib import Path
from types import SimpleNamespace

import precis.utils.claude_p as claude_p
from precis.utils.claude_oauth import ENV_VAR


def _capture_run_claude(captured: dict) -> object:
    def _fake(args, *, binary, label, timeout_s, error_cls, env=None):
        captured["env"] = env
        return SimpleNamespace(stdout='{"ok": true}', stderr="cost: $0.0001")

    return _fake


def test_call_claude_p_injects_oauth_token_from_file(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude_oauth_token").write_text("sk-ant-oat01-FIGURE\n")
    # A shell-less daemon has no token in its own env.
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude")

    captured: dict = {}
    monkeypatch.setattr(claude_p, "run_claude", _capture_run_claude(captured))

    res = claude_p.call_claude_p("draw something. reply JSON {}")

    assert res.data == {"ok": True}
    # The subprocess env carried the token loaded from the file.
    assert captured["env"] is not None
    assert captured["env"][ENV_VAR] == "sk-ant-oat01-FIGURE"


def test_call_claude_p_does_not_clobber_existing_env_token(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude_oauth_token").write_text("sk-ant-oat01-FROMFILE")
    # A plist/interactive-shell token in the process env must win.
    monkeypatch.setenv(ENV_VAR, "sk-ant-oat01-FROMENV")
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude")

    captured: dict = {}
    monkeypatch.setattr(claude_p, "run_claude", _capture_run_claude(captured))

    claude_p.call_claude_p("draw something. reply JSON {}")

    assert captured["env"][ENV_VAR] == "sk-ant-oat01-FROMENV"
