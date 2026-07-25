"""Unit tests for the cd-to-primary guard hook's decision logic.

Pure — exercises ``evaluate`` with a stubbed ``_git`` (no real git worktree /
filesystem needed). Mirrors ``tests/test_checkout_in_primary_guard.py``'s
pattern for loading a hyphenated-name hook script by path.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

_HOOK = (
    Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "guard-cd-to-primary.py"
)
_spec = importlib.util.spec_from_file_location("guard_cd_to_primary", _HOOK)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
evaluate = _mod.evaluate
main = _mod.main

MAIN = "/repo"
WT = "/repo/.claude/worktrees/wt1"


def _fake_git(cwd: str, *args: str) -> str:
    if args == ("rev-parse", "--path-format=absolute", "--git-common-dir"):
        return f"{MAIN}/.git"
    if args == ("rev-parse", "--show-toplevel"):
        # cwd inside the worktree resolves to WT; otherwise the primary.
        return WT if (cwd == WT or cwd.startswith(WT + "/")) else MAIN
    return ""


def _patch(monkeypatch) -> None:
    monkeypatch.setattr(_mod, "_git", _fake_git)


# ── the core footgun: `cd <primary> && <cmd>` from a worktree ───────────────


def test_cd_to_primary_then_command_is_denied(monkeypatch) -> None:
    _patch(monkeypatch)
    reason = evaluate(f"cd {MAIN} && ls", WT)
    assert reason is not None
    assert MAIN in reason


def test_cd_to_primary_subdir_then_command_is_denied(monkeypatch) -> None:
    _patch(monkeypatch)
    # A path inside the primary tree but outside the worktree is the same trap.
    reason = evaluate(f"cd {MAIN}/src && pytest", WT)
    assert reason is not None


def test_cd_dotdot_out_of_worktree_is_denied(monkeypatch) -> None:
    _patch(monkeypatch)
    # `cd ..` from the worktree lands in /repo/.claude/worktrees — primary tree.
    reason = evaluate("cd .. && ls", WT)
    assert reason is not None


# ── allowed: no divergence risk ─────────────────────────────────────────────


def test_bare_command_is_allowed(monkeypatch) -> None:
    _patch(monkeypatch)
    assert evaluate("pytest -k foo", WT) is None


def test_cd_within_worktree_is_allowed(monkeypatch) -> None:
    _patch(monkeypatch)
    assert evaluate(f"cd {WT}/src && ls", WT) is None


def test_lone_cd_to_primary_is_allowed(monkeypatch) -> None:
    _patch(monkeypatch)
    # No command chained after the cd — cwd resets next call, so it's harmless.
    assert evaluate(f"cd {MAIN}", WT) is None


def test_cd_to_tmp_is_allowed(monkeypatch) -> None:
    _patch(monkeypatch)
    assert evaluate("cd /tmp/scratch && ls", WT) is None


def test_git_dash_c_to_primary_is_allowed(monkeypatch) -> None:
    _patch(monkeypatch)
    # `git -C` is the *recommended* way to reach the primary — never blocked.
    assert evaluate(f"git -C {MAIN} status", WT) is None


def test_cd_back_into_worktree_ends_safe(monkeypatch) -> None:
    _patch(monkeypatch)
    # Nets out in the worktree, so the command runs there — allowed.
    assert evaluate(f"cd {MAIN} && cd {WT} && ls", WT) is None


def test_primary_session_is_untouched(monkeypatch) -> None:
    _patch(monkeypatch)
    # Not a worktree session → this guard stays out (other guards cover primary).
    assert evaluate(f"cd {MAIN} && ls", MAIN) is None


def test_non_string_command_is_allowed(monkeypatch) -> None:
    _patch(monkeypatch)
    assert evaluate(None, WT) is None  # type: ignore[arg-type]


# ── main() / stdin-JSON wiring + escape hatch ───────────────────────────────


def _run_main(monkeypatch, payload, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = main()
    out = capsys.readouterr().out
    return rc, (json.loads(out) if out.strip() else None)


def test_main_denies_cd_to_primary_in_worktree(monkeypatch, capsys) -> None:
    _patch(monkeypatch)
    monkeypatch.delenv("ALLOW_CD_TO_PRIMARY", raising=False)
    payload = {"cwd": WT, "tool_input": {"command": f"cd {MAIN} && ls"}}
    rc, out = _run_main(monkeypatch, payload, capsys)
    assert rc == 0
    assert out is not None
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert MAIN in hso["permissionDecisionReason"]


def test_main_allows_bare_command(monkeypatch, capsys) -> None:
    _patch(monkeypatch)
    monkeypatch.delenv("ALLOW_CD_TO_PRIMARY", raising=False)
    payload = {"cwd": WT, "tool_input": {"command": "pytest"}}
    rc, out = _run_main(monkeypatch, payload, capsys)
    assert rc == 0
    assert out is None


def test_main_escape_hatch_allows(monkeypatch, capsys) -> None:
    _patch(monkeypatch)
    monkeypatch.setenv("ALLOW_CD_TO_PRIMARY", "1")
    payload = {"cwd": WT, "tool_input": {"command": f"cd {MAIN} && ls"}}
    rc, out = _run_main(monkeypatch, payload, capsys)
    assert rc == 0
    assert out is None


def test_main_unparseable_stdin_allows(monkeypatch, capsys) -> None:
    monkeypatch.delenv("ALLOW_CD_TO_PRIMARY", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    rc = main()
    out = capsys.readouterr().out
    assert rc == 0
    assert out == ""
