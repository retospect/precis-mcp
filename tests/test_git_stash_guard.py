"""Unit tests for the guard-git-stash hook (PreToolUse, Bash).

Pure — exercises ``evaluate`` on command strings, plus ``main``'s
stdin-JSON wiring. Mirrors ``tests/test_piped_exit_code_guard.py``'s
pattern for loading a hyphenated-name hook script by path.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

_HOOK = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "guard-git-stash.py"
_spec = importlib.util.spec_from_file_location("guard_git_stash", _HOOK)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
evaluate = _mod.evaluate
main = _mod.main


class TestDenied:
    """Bare/untagged stashes and pop on the shared stack."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "git stash",
            "git stash push",
            "git stash push -u",
            "git stash save",
            "git -C /repo stash",
            "git -c user.name=x stash",
            # A pop anywhere, tagged or not, can take another session's entry.
            "git stash pop",
            "git stash pop stash@{0}",
            "git -C /repo stash pop",
        ],
    )
    def test_denied(self, cmd: str) -> None:
        assert evaluate(cmd) is not None, cmd

    def test_pop_reason_prints_recovery_sequence(self) -> None:
        """gr525771: the refusal is the only doc most agents see — it must
        carry the full apply-by-sha recovery and the positional-index hazard."""
        reason = evaluate("git stash pop")
        assert reason is not None
        assert "git stash list --format='%gd %H %gs'" in reason
        assert "git stash apply <sha>" in reason
        assert "git stash drop stash@{n}" in reason
        assert "POSITIONAL" in reason
        assert "ALLOW_GIT_STASH" in reason

    def test_bare_reason_demands_a_tag(self) -> None:
        reason = evaluate("git stash")
        assert reason is not None
        assert '-m "' in reason
        assert "ALLOW_GIT_STASH" in reason


class TestAllowed:
    """Tagged pushes, apply, and the read/manage verbs pass untouched."""

    @pytest.mark.parametrize(
        "cmd",
        [
            'git stash push -m "wt-foo wip"',
            'git stash push -u -m "wt-foo wip"',
            "git stash push -m=tag",
            "git stash apply stash@{0}",
            "git stash apply abc123",
            "git stash list",
            "git stash show",
            "git stash show -p stash@{1}",
            "git stash drop stash@{2}",
            "git stash branch fix-wip stash@{0}",
            "git stash clear",
            "git stash store -m x abc123",
            "git stash create",
            # Unrelated commands; "stash" only as a substring of a word.
            "git status",
            "echo unstashed",
            "git stash-list",  # not a real verb; treated as an arg, allowed
        ],
    )
    def test_allowed(self, cmd: str) -> None:
        assert evaluate(cmd) is None, cmd

    def test_non_string_command_allowed(self) -> None:
        assert evaluate(None) is None

    def test_apply_by_sha_stays_allowed(self) -> None:
        """Regression (gr525771): pop's replacement must never be denied —
        apply is the non-destructive half and passes for any target."""
        for cmd in (
            "git stash apply",
            "git stash apply stash@{0}",
            "git stash apply 9624515120a1524d91f88b71c9514e461883eb53",
        ):
            assert evaluate(cmd) is None, cmd


class TestMainWiring:
    def _run(self, monkeypatch, capsys, payload) -> tuple[int, dict | None]:
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        rc = main()
        out = capsys.readouterr().out
        return rc, (json.loads(out) if out.strip() else None)

    def test_main_denies_with_hook_json(self, monkeypatch, capsys) -> None:
        rc, out = self._run(
            monkeypatch, capsys, {"tool_input": {"command": "git stash pop"}}
        )
        assert rc == 0
        assert out is not None
        hso = out["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert hso["permissionDecision"] == "deny"
        assert "git stash apply" in hso["permissionDecisionReason"]

    def test_main_silent_on_tagged_push(self, monkeypatch, capsys) -> None:
        rc, out = self._run(
            monkeypatch,
            capsys,
            {"tool_input": {"command": 'git stash push -m "tag"'}},
        )
        assert rc == 0
        assert out is None

    def test_env_escape_hatch(self, monkeypatch, capsys) -> None:
        monkeypatch.setenv("ALLOW_GIT_STASH", "1")
        rc, out = self._run(
            monkeypatch, capsys, {"tool_input": {"command": "git stash pop"}}
        )
        assert rc == 0
        assert out is None

    def test_unparseable_stdin_is_silent(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
        assert main() == 0
        assert capsys.readouterr().out == ""
