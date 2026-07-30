"""Unit tests for the checkout-in-primary guard hook's decision logic.

Pure — exercises ``evaluate`` with a stubbed ``_git`` (no real git worktree /
filesystem needed). Mirrors ``tests/test_worktree_path_guard.py``'s pattern for
loading a hyphenated-name hook script by path. Also see
``guard-commit-on-main.py``, the sibling this hook mirrors structurally.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

_needs_posix_paths = pytest.mark.skipif(
    sys.platform == "win32",
    reason="hardcoded POSIX-style paths ('/repo', '/somewhere/else') — the"
    " hook's cwd-resolution path comparison doesn't match on Windows",
)

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "hooks"
    / "guard-checkout-in-primary.py"
)
_spec = importlib.util.spec_from_file_location("guard_checkout_in_primary", _HOOK)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
evaluate = _mod.evaluate
main = _mod.main

MAIN = "/repo"
WT = "/repo/.claude/worktrees/wt1"

# Branches that "exist" in this fake repo (for the bare `checkout <name>`
# ambiguous-with-a-file case).
_EXISTING_BRANCHES = {"feature-x", "worktree-wt1"}


def _fake_git(cwd: str, *args: str) -> str:
    if args == ("rev-parse", "--path-format=absolute", "--git-common-dir"):
        return f"{MAIN}/.git"
    if args == ("rev-parse", "--show-toplevel"):
        return MAIN if cwd == MAIN else WT
    return ""


def _fake_git_ok(cwd: str, *args: str) -> bool:
    # show-ref --verify --quiet is silent on success — real _git_ok reads the
    # exit code, not stdout; the fake mirrors that by returning a bool.
    if len(args) == 4 and args[:3] == ("show-ref", "--verify", "--quiet"):
        ref = args[3]
        name = ref.removeprefix("refs/heads/")
        return name in _EXISTING_BRANCHES
    return False


def _patch(monkeypatch) -> None:
    monkeypatch.setattr(_mod, "_git", _fake_git)
    monkeypatch.setattr(_mod, "_git_ok", _fake_git_ok)


# ── checkout -b / -B (unconditional branch target) ─────────────────────────


def test_checkout_dash_b_in_primary_is_denied(monkeypatch) -> None:
    _patch(monkeypatch)
    reason = evaluate("git checkout -b new-feature", MAIN)
    assert reason is not None
    assert "new-feature" in reason


def test_checkout_dash_b_to_main_is_allowed(monkeypatch) -> None:
    _patch(monkeypatch)
    assert evaluate("git checkout -b main", MAIN) is None


def test_checkout_dash_b_in_worktree_is_allowed(monkeypatch) -> None:
    _patch(monkeypatch)
    assert evaluate("git checkout -b new-feature", WT) is None


# ── bare `checkout <name>` — ambiguous branch-vs-file ───────────────────────


def test_checkout_existing_branch_in_primary_is_denied(monkeypatch) -> None:
    _patch(monkeypatch)
    reason = evaluate("git checkout feature-x", MAIN)
    assert reason is not None
    assert "feature-x" in reason


def test_checkout_nonexistent_name_is_allowed_as_file(monkeypatch) -> None:
    _patch(monkeypatch)
    # "some_file.py" isn't in _EXISTING_BRANCHES -> treated as a pathspec.
    assert evaluate("git checkout some_file.py", MAIN) is None


def test_checkout_dashdash_file_is_always_allowed(monkeypatch) -> None:
    _patch(monkeypatch)
    # Even if the name happens to collide with a branch, `--` disambiguates.
    assert evaluate("git checkout -- feature-x", MAIN) is None


def test_checkout_dash_previous_branch_is_allowed(monkeypatch) -> None:
    _patch(monkeypatch)
    assert evaluate("git checkout -", MAIN) is None


# ── git switch ───────────────────────────────────────────────────────────


def test_switch_branch_in_primary_is_denied(monkeypatch) -> None:
    _patch(monkeypatch)
    reason = evaluate("git switch feature-x", MAIN)
    assert reason is not None
    assert "feature-x" in reason


def test_switch_to_main_is_allowed(monkeypatch) -> None:
    _patch(monkeypatch)
    assert evaluate("git switch main", MAIN) is None


def test_switch_dash_previous_branch_is_allowed(monkeypatch) -> None:
    _patch(monkeypatch)
    assert evaluate("git switch -", MAIN) is None


def test_switch_dash_c_new_branch_in_primary_is_denied(monkeypatch) -> None:
    _patch(monkeypatch)
    # switch never checks out files: `-c <name>` unconditionally creates+
    # switches, same footgun as `checkout -b`.
    reason = evaluate("git switch -c brand-new", MAIN)
    assert reason is not None
    assert "brand-new" in reason


def test_switch_in_worktree_is_allowed(monkeypatch) -> None:
    _patch(monkeypatch)
    assert evaluate("git switch feature-x", WT) is None


# ── cd / git -C resolution (mirrors guard-commit-on-main.py) ────────────────


@_needs_posix_paths
def test_leading_cd_into_primary_is_denied(monkeypatch) -> None:
    _patch(monkeypatch)
    reason = evaluate(f"cd {MAIN} && git checkout -b new-feature", "/somewhere/else")
    assert reason is not None


def test_leading_cd_into_worktree_is_allowed(monkeypatch) -> None:
    _patch(monkeypatch)
    assert evaluate(f"cd {WT} && git checkout -b new-feature", MAIN) is None


@_needs_posix_paths
def test_git_dash_c_into_primary_is_denied(monkeypatch) -> None:
    _patch(monkeypatch)
    reason = evaluate(f"git -C {MAIN} checkout -b new-feature", WT)
    assert reason is not None


def test_git_dash_c_into_worktree_is_allowed(monkeypatch) -> None:
    _patch(monkeypatch)
    assert evaluate(f"git -C {WT} checkout -b new-feature", MAIN) is None


# ── unrelated commands ──────────────────────────────────────────────────


def test_unrelated_command_is_allowed(monkeypatch) -> None:
    _patch(monkeypatch)
    assert evaluate("git status", MAIN) is None


def test_non_string_command_is_allowed(monkeypatch) -> None:
    _patch(monkeypatch)
    assert evaluate(None, MAIN) is None  # type: ignore[arg-type]


# ── main() / stdin-JSON wiring + escape hatch ───────────────────────────


def _run_main(monkeypatch, payload, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = main()
    out = capsys.readouterr().out
    return rc, (json.loads(out) if out.strip() else None)


def test_main_denies_checkout_in_primary(monkeypatch, capsys) -> None:
    _patch(monkeypatch)
    monkeypatch.delenv("ALLOW_CHECKOUT_IN_PRIMARY", raising=False)
    payload = {
        "cwd": MAIN,
        "tool_input": {"command": "git checkout -b new-feature"},
    }
    rc, out = _run_main(monkeypatch, payload, capsys)
    assert rc == 0
    assert out is not None
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "new-feature" in hso["permissionDecisionReason"]


def test_main_allows_checkout_in_worktree(monkeypatch, capsys) -> None:
    _patch(monkeypatch)
    monkeypatch.delenv("ALLOW_CHECKOUT_IN_PRIMARY", raising=False)
    payload = {
        "cwd": WT,
        "tool_input": {"command": "git checkout -b new-feature"},
    }
    rc, out = _run_main(monkeypatch, payload, capsys)
    assert rc == 0
    assert out is None


def test_main_escape_hatch_allows_checkout_in_primary(monkeypatch, capsys) -> None:
    _patch(monkeypatch)
    monkeypatch.setenv("ALLOW_CHECKOUT_IN_PRIMARY", "1")
    payload = {
        "cwd": MAIN,
        "tool_input": {"command": "git checkout -b new-feature"},
    }
    rc, out = _run_main(monkeypatch, payload, capsys)
    assert rc == 0
    assert out is None


def test_main_unparseable_stdin_allows(monkeypatch, capsys) -> None:
    monkeypatch.delenv("ALLOW_CHECKOUT_IN_PRIMARY", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    rc = main()
    out = capsys.readouterr().out
    assert rc == 0
    assert out == ""
