"""Unit tests for the worktree edit-path guard hook's decision logic.

Pure — exercises ``evaluate`` with injected existence predicates, so no git
worktree / filesystem is needed. The hook script has a hyphenated name (it's an
executable, not a package module), so it's loaded by path.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_HOOK = (
    Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "guard-worktree-path.py"
)
_spec = importlib.util.spec_from_file_location("guard_worktree_path", _HOOK)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
evaluate = _mod.evaluate

MAIN = "/repo"
WT = "/repo/.claude/worktrees/wt1"


def _always(_: str) -> bool:
    return True


def _never(_: str) -> bool:
    return False


def test_main_path_with_worktree_twin_is_denied() -> None:
    reason = evaluate(
        f"{MAIN}/src/precis/handlers/x.py", WT, MAIN, exists=_always, isdir=_always
    )
    assert reason is not None
    # The corrected worktree path is offered in the message.
    assert f"{WT}/src/precis/handlers/x.py" in reason


def test_worktree_path_is_allowed() -> None:
    assert (
        evaluate(
            f"{WT}/src/precis/handlers/x.py", WT, MAIN, exists=_always, isdir=_always
        )
        is None
    )


def test_external_root_is_allowed() -> None:
    # ~/work/cluster and friends live outside the main checkout — never guarded.
    assert (
        evaluate(
            "/Users/reto/work/cluster/foo.yml", WT, MAIN, exists=_always, isdir=_always
        )
        is None
    )


def test_relative_path_is_allowed() -> None:
    # Relative paths resolve against the worktree cwd — safe by construction.
    assert evaluate("src/x.py", WT, MAIN, exists=_always, isdir=_always) is None


def test_not_a_worktree_session_allows_everything() -> None:
    # wt_root == main_root → a plain main checkout, nothing to guard.
    assert (
        evaluate(f"{MAIN}/src/x.py", MAIN, MAIN, exists=_always, isdir=_always) is None
    )


def test_main_only_path_without_twin_is_allowed() -> None:
    # A main-root path whose worktree twin (and its dir) don't exist is a
    # deliberate main-only / sibling-worktree write, not a mis-target.
    assert evaluate(f"{MAIN}/src/new.py", WT, MAIN, exists=_never, isdir=_never) is None


def test_sibling_worktree_path_is_allowed() -> None:
    sib = f"{MAIN}/.claude/worktrees/other/src/x.py"
    assert evaluate(sib, WT, MAIN, exists=_never, isdir=_never) is None


def test_new_file_in_existing_worktree_dir_is_denied() -> None:
    # Creating a new file via the main path, where the worktree dir exists:
    # still a mis-target — deny and suggest the twin.
    reason = evaluate(
        f"{MAIN}/src/precis/new.py", WT, MAIN, exists=_never, isdir=_always
    )
    assert reason is not None
