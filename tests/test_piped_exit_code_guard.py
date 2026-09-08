"""Unit tests for the piped-exit-code guard hook's decision logic.

Pure — exercises ``evaluate`` on command strings. Mirrors
``tests/test_cd_to_primary_guard.py``'s pattern for loading a hyphenated-name
hook script by path.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "hooks"
    / "guard-piped-exit-code.py"
)
_spec = importlib.util.spec_from_file_location("guard_piped_exit_code", _HOOK)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
evaluate = _mod.evaluate
main = _mod.main


class TestDenied:
    """The shapes that silently swallow a red gate."""

    @pytest.mark.parametrize(
        "cmd",
        [
            'scripts/ship --impacted "msg" | tail -30',
            "scripts/ship | head -5",
            "scripts/deploy | grep -E 'fatal'",
            "scripts/bump | tail",
            "./scripts/ship 'x' | tail -1",
            # A leading cd/&& chain must not hide the pipe.
            "cd /repo && scripts/ship 'x' | tail -20",
            # Absolute path to the script.
            "/repo/scripts/ship 'x' | tail",
            # tee masks the status just as thoroughly as tail.
            "scripts/ship 'x' | tee /tmp/out.log",
            # Leading env assignments keep it in command position.
            "PRECIS_GATE_N=3 scripts/ship 'x' | tail",
        ],
    )
    def test_denies(self, cmd: str) -> None:
        reason = evaluate(cmd)
        assert reason is not None, cmd
        assert "pipefail" in reason

    def test_reason_names_the_script(self) -> None:
        assert "scripts/deploy" in (evaluate("scripts/deploy | tail") or "")


class TestAllowed:
    """Everything that either preserves the status or never had one at risk."""

    @pytest.mark.parametrize(
        "cmd",
        [
            # The two documented fixes.
            "set -o pipefail; scripts/ship 'x' | tail -30",
            'scripts/ship "x" > /tmp/s.log 2>&1; echo "EXIT=$?"',
            "scripts/ship 'x'; echo ${PIPESTATUS[0]}",
            # No pipe at all.
            "scripts/ship --impacted 'msg'",
            "scripts/deploy",
            # `||` is a fallback, not a pipe.
            "scripts/ship 'x' || echo failed",
            # Not a guarded script: scripts/test prints its own failure summary.
            "scripts/test tests/test_foo.py | tail -8",
            "scripts/inflight | head",
            # Piping *into* the script is not the masking case.
            "echo hi | scripts/prod-psql",
            # Unrelated commands.
            "git log --oneline | head -3",
            "",
            # REGRESSION (2026-09-08): the script named as an ARGUMENT is being
            # READ, not run. The first version fired on these, which blocked
            # inspecting the very scripts the guard protects.
            "grep -n pattern scripts/ship scripts/deploy | head",
            "cat scripts/ship | head -20",
            "wc -l scripts/deploy | awk '{print $1}'",
        ],
    )
    def test_allows(self, cmd: str) -> None:
        assert evaluate(cmd) is None, cmd

    def test_non_string_is_allowed(self) -> None:
        # ``evaluate`` is loaded via importlib, so it is untyped here — the
        # guard still has to survive a non-str command payload at runtime.
        assert evaluate(None) is None


class TestMain:
    """The stdin/stdout hook protocol."""

    def _run(self, monkeypatch: pytest.MonkeyPatch, cmd: str, capsys) -> str:
        monkeypatch.delenv("ALLOW_PIPED_EXIT", raising=False)
        payload = {"tool_input": {"command": cmd}}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        assert main() == 0
        return capsys.readouterr().out

    def test_emits_deny_decision(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        out = self._run(monkeypatch, "scripts/ship 'x' | tail", capsys)
        decision = json.loads(out)["hookSpecificOutput"]
        assert decision["hookEventName"] == "PreToolUse"
        assert decision["permissionDecision"] == "deny"

    def test_silent_when_allowed(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        assert self._run(monkeypatch, "scripts/ship 'x'", capsys) == ""

    def test_escape_hatch(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        monkeypatch.setenv("ALLOW_PIPED_EXIT", "1")
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(json.dumps({"tool_input": {"command": "scripts/ship | tail"}})),
        )
        assert main() == 0
        assert capsys.readouterr().out == ""

    def test_malformed_stdin_is_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.delenv("ALLOW_PIPED_EXIT", raising=False)
        monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
        assert main() == 0
        assert capsys.readouterr().out == ""
