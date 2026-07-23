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
main = _mod.main

MAIN = "/repo"
WT = "/repo/.claude/worktrees/wt1"


def _always(_: str) -> bool:
    return True


def _never(_: str) -> bool:
    return False


def test_main_path_with_worktree_twin_is_corrected() -> None:
    twin = evaluate(
        f"{MAIN}/src/precis/handlers/x.py", WT, MAIN, exists=_always, isdir=_always
    )
    # evaluate() now returns the corrected worktree-twin path (auto-correct,
    # not a deny reason).
    assert twin == f"{WT}/src/precis/handlers/x.py"


def test_worktree_path_is_allowed_untouched() -> None:
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


def test_new_file_in_existing_worktree_dir_is_corrected() -> None:
    # Creating a new file via the main path, where the worktree dir exists:
    # still a mis-target — correct to the twin rather than deny.
    twin = evaluate(f"{MAIN}/src/precis/new.py", WT, MAIN, exists=_never, isdir=_always)
    assert twin == f"{WT}/src/precis/new.py"


def _run_main(monkeypatch, payload, capsys):
    import io
    import json as _json

    monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps(payload)))
    rc = main()
    out = capsys.readouterr().out
    return rc, (_json.loads(out) if out.strip() else None)


def test_main_emits_allow_with_updated_input_for_file_path(monkeypatch, capsys) -> None:
    # Isolate main()'s payload-parsing / JSON-construction from evaluate()'s
    # path logic (already covered above) by stubbing _git and evaluate directly.
    monkeypatch.setattr(
        _mod,
        "_git",
        lambda cwd, *args: (
            WT if args == ("rev-parse", "--show-toplevel") else f"{MAIN}/.git"
        ),
    )
    monkeypatch.setattr(
        _mod, "evaluate", lambda *a, **k: f"{WT}/src/precis/handlers/x.py"
    )

    payload = {
        "cwd": WT,
        "tool_name": "Edit",
        "tool_input": {
            "file_path": f"{MAIN}/src/precis/handlers/x.py",
            "old_string": "a",
            "new_string": "b",
        },
    }
    rc, out = _run_main(monkeypatch, payload, capsys)
    assert rc == 0
    assert out is not None
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert hso["updatedInput"]["file_path"] == f"{WT}/src/precis/handlers/x.py"
    # Sibling keys (old_string/new_string) survive the rewrite.
    assert hso["updatedInput"]["old_string"] == "a"
    assert f"{WT}/src/precis/handlers/x.py" in hso["permissionDecisionReason"]


def test_main_reads_notebook_path_for_notebook_edit(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        _mod,
        "_git",
        lambda cwd, *args: (
            WT if args == ("rev-parse", "--show-toplevel") else f"{MAIN}/.git"
        ),
    )
    monkeypatch.setattr(_mod, "evaluate", lambda *a, **k: f"{WT}/notebooks/foo.ipynb")

    payload = {
        "cwd": WT,
        "tool_name": "NotebookEdit",
        "tool_input": {
            "notebook_path": f"{MAIN}/notebooks/foo.ipynb",
            "new_source": "print(1)",
        },
    }
    rc, out = _run_main(monkeypatch, payload, capsys)
    assert rc == 0
    assert out is not None
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert hso["updatedInput"]["notebook_path"] == f"{WT}/notebooks/foo.ipynb"
    assert hso["updatedInput"]["new_source"] == "print(1)"


def test_main_no_op_when_no_correction_needed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        _mod,
        "_git",
        lambda cwd, *args: (
            WT if args == ("rev-parse", "--show-toplevel") else f"{MAIN}/.git"
        ),
    )
    monkeypatch.setattr(_mod, "evaluate", lambda *a, **k: None)

    payload = {
        "cwd": WT,
        "tool_name": "Edit",
        "tool_input": {"file_path": f"{WT}/src/precis/handlers/x.py"},
    }
    rc, out = _run_main(monkeypatch, payload, capsys)
    assert rc == 0
    assert out is None
