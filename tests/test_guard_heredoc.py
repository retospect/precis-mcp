"""Unit tests for the guard-heredoc hook (PreToolUse, Bash/exec).

Pure — exercises ``evaluate`` on command strings plus ``main``'s
stdin-JSON wiring. Mirrors ``tests/test_commit_on_main_guard.py``'s
pattern for loading a hyphenated-name hook script by path.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

_HOOK = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "guard-heredoc.py"
_spec = importlib.util.spec_from_file_location("guard_heredoc", _HOOK)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
evaluate = _mod.evaluate
main = _mod.main


class TestHeredocDeny:
    """Every ``<<`` shape the no-heredocs rule names."""

    @pytest.mark.parametrize(
        "command",
        [
            "cat >> tests/test_x.py <<EOF\nbody\nEOF",
            "cat <<'EOF'\nbody\nEOF",
            'cat <<"EOF"\nbody\nEOF',
            "cat <<\\EOF\nbody\nEOF",
            "cat <<- EOF\n\tbody\n\tEOF",
            "python3 - <<'PY'\nprint(1)\nPY",
            "3<<EOF cat\nbody\nEOF",
            "psql <<'SQL'\nselect 1;\nSQL",
            "cat <<EOF",  # truncated still means heredoc
        ],
    )
    def test_denied(self, command: str) -> None:
        reason = evaluate(command)
        assert reason is not None, command
        assert "ALLOW_HEREDOC" in reason

    def test_heredoc_after_separator_denied(self) -> None:
        assert evaluate("echo hi && cat <<EOF\nx\nEOF") is not None


class TestAllow:
    @pytest.mark.parametrize(
        "command",
        [
            "echo hello",
            'cat <<< "one line herestring"',
            'git commit -m "a << b in prose"',
            "git log --format='<<%h'",
            "echo $((1 << 4))",
            "((x = y << 2))",
            "rg -n '<<' src/",
            "",
            None,
        ],
    )
    def test_allowed(self, command: str | None) -> None:
        assert evaluate(command) is None


class TestMain:
    def _run(self, payload: dict, monkeypatch, env: dict[str, str] | None = None):
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        out = io.StringIO()
        monkeypatch.setattr("sys.stdout", out)
        rc = main()
        return rc, out.getvalue()

    def test_deny_json(self, monkeypatch) -> None:
        rc, out = self._run(
            monkeypatch=monkeypatch,
            payload={"tool_input": {"command": "cat <<EOF\nx\nEOF"}},
        )
        assert rc == 0
        hook = json.loads(out)["hookSpecificOutput"]
        assert hook["permissionDecision"] == "deny"

    def test_exec_style_and_cmd_key(self, monkeypatch) -> None:
        rc, out = self._run(
            monkeypatch=monkeypatch,
            payload={
                "tool_name": "exec",
                "tool_input": {"cmd": "cat <<EOF\nx\nEOF"},
            },
        )
        assert rc == 0 and "deny" in out

    def test_clean_command_silent(self, monkeypatch) -> None:
        rc, out = self._run(
            monkeypatch=monkeypatch,
            payload={"tool_input": {"command": "git status"}},
        )
        assert rc == 0 and out == ""

    def test_escape_hatch(self, monkeypatch) -> None:
        rc, out = self._run(
            monkeypatch=monkeypatch,
            payload={"tool_input": {"command": "cat <<EOF\nx\nEOF"}},
            env={"ALLOW_HEREDOC": "1"},
        )
        assert rc == 0 and out == ""

    def test_bad_payload_silent(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
        assert main() == 0
