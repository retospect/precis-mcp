"""Unit tests for the guard-commit-on-main hook (PreToolUse, Bash).

Pure — exercises ``evaluate`` on command strings with ``_repo_root`` and
``_branch`` monkeypatched, plus ``main``'s stdin-JSON wiring. Mirrors
``tests/test_piped_exit_code_guard.py``'s pattern for loading a
hyphenated-name hook script by path.
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
    / "guard-commit-on-main.py"
)
_spec = importlib.util.spec_from_file_location("guard_commit_on_main", _HOOK)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
evaluate = _mod.evaluate
main = _mod.main


def _patch(monkeypatch, tmp_path, dirs, *, gated=True):
    """Fake ``_repo_root``/``_branch`` lookups over a tiny dir map.

    ``dirs`` maps resolved path -> branch name ("" for detached/unknown);
    keys are normalised the same way ``evaluate`` resolves targets. The
    repo at ``tmp_path/repo`` gets a real ``scripts/ship`` file when
    ``gated`` — the hook's ship-gate check is a real ``isfile`` probe.
    """
    root = tmp_path / "repo"
    root.mkdir()
    if gated:
        (root / "scripts").mkdir()
        (root / "scripts" / "ship").write_text("#!/bin/sh\n", encoding="utf-8")
    dirs = {str(d): b for d, b in dirs.items()}
    monkeypatch.setattr(
        _mod, "_repo_root", lambda cwd: str(root) if cwd in dirs else ""
    )
    monkeypatch.setattr(_mod, "_branch", lambda cwd: dirs.get(cwd, ""))
    return root


class TestBranchWalk:
    """The core deny: `git commit` whose resolved branch is main/master."""

    def test_commit_on_main_denied(self, monkeypatch, tmp_path) -> None:
        root = _patch(monkeypatch, tmp_path, {tmp_path / "repo": "main"})
        reason = evaluate("git commit -m x", str(root))
        assert reason is not None
        assert "`main`" in reason
        assert "ALLOW_COMMIT_ON_MAIN" in reason

    def test_commit_on_master_denied(self, monkeypatch, tmp_path) -> None:
        root = _patch(monkeypatch, tmp_path, {tmp_path / "repo": "master"})
        assert evaluate("git commit --amend", str(root)) is not None

    def test_commit_on_feature_branch_allowed(self, monkeypatch, tmp_path) -> None:
        root = _patch(monkeypatch, tmp_path, {tmp_path / "repo": "worktree-x"})
        assert evaluate("git commit -m x", str(root)) is None

    def test_detached_head_allowed(self, monkeypatch, tmp_path) -> None:
        root = _patch(monkeypatch, tmp_path, {tmp_path / "repo": ""})
        assert evaluate("git commit -m x", str(root)) is None

    def test_repo_without_ship_gate_allowed(self, monkeypatch, tmp_path) -> None:
        # A sibling repo that ships direct-to-main by design: no scripts/ship.
        root = _patch(monkeypatch, tmp_path, {tmp_path / "repo": "main"}, gated=False)
        assert evaluate("git commit -m x", str(root)) is None

    def test_non_git_dir_allowed(self, monkeypatch, tmp_path) -> None:
        _patch(monkeypatch, tmp_path, {tmp_path / "repo": "main"})
        assert evaluate("git commit -m x", str(tmp_path / "elsewhere")) is None


class TestTargetResolution:
    """cd and git -C redirects are followed to the commit's real target."""

    def test_leading_cd_into_main_tree_denied(self, monkeypatch, tmp_path) -> None:
        root = _patch(
            monkeypatch,
            tmp_path,
            {tmp_path / "repo": "feature", tmp_path / "repo" / "main-tree": "main"},
        )
        (root / "main-tree").mkdir()
        cmd = f"cd {root}/main-tree && git commit -m x"
        assert evaluate(cmd, str(root)) is not None

    def test_git_dash_c_into_main_tree_denied(self, monkeypatch, tmp_path) -> None:
        root = _patch(
            monkeypatch,
            tmp_path,
            {tmp_path / "repo": "feature", tmp_path / "repo" / "main-tree": "main"},
        )
        (root / "main-tree").mkdir()
        cmd = f"git -C {root}/main-tree commit -m x"
        assert evaluate(cmd, str(root)) is not None

    def test_git_dash_c_quoted_path(self, monkeypatch, tmp_path) -> None:
        root = _patch(
            monkeypatch,
            tmp_path,
            {tmp_path / "repo": "feature", tmp_path / "repo" / "main-tree": "main"},
        )
        (root / "main-tree").mkdir()
        cmd = f'git -C "{root}/main-tree" commit -m x'
        assert evaluate(cmd, str(root)) is not None

    def test_cd_then_feature_commit_allowed(self, monkeypatch, tmp_path) -> None:
        root = _patch(
            monkeypatch,
            tmp_path,
            {
                tmp_path / "repo": "main",
                tmp_path / "repo" / "wt": "worktree-y",
            },
        )
        (root / "wt").mkdir()
        cmd = f"cd {root}/wt && git commit -m x"
        assert evaluate(cmd, str(root)) is None


class TestScope:
    """Deliberately narrow: only `git commit`, only the word commit."""

    def test_git_commit_tree_allowed(self, monkeypatch, tmp_path) -> None:
        # scripts/ship's own plumbing is not the footgun.
        root = _patch(monkeypatch, tmp_path, {tmp_path / "repo": "main"})
        assert evaluate("git commit-tree abc -m x", str(root)) is None

    def test_merge_and_status_allowed(self, monkeypatch, tmp_path) -> None:
        root = _patch(monkeypatch, tmp_path, {tmp_path / "repo": "main"})
        assert evaluate("git merge --ff-only origin/main", str(root)) is None
        assert evaluate("git status", str(root)) is None

    @pytest.mark.parametrize(
        "cmd",
        [
            # gr515527: a path that merely contains "commit" is not the
            # subcommand — the gripe's exact refused shape.
            "rtk proxy git check-ignore -v scratch/commit-msg.txt",
            "git check-ignore -v scratch/commit-msg.txt",
            # Other glued shapes that are not `git commit`.
            "git merge --no-commit",
            "git log --grep=commit -5",
            "git show HEAD^{commit}",
            "git tag -l 'commit-*'",
        ],
    )
    def test_commit_substring_tokens_allowed(
        self, cmd: str, monkeypatch, tmp_path
    ) -> None:
        root = _patch(monkeypatch, tmp_path, {tmp_path / "repo": "main"})
        assert evaluate(cmd, str(root)) is None, cmd

    def test_standalone_commit_still_denied(self, monkeypatch, tmp_path) -> None:
        # The whitespace-bounded token still catches the real subcommand.
        root = _patch(monkeypatch, tmp_path, {tmp_path / "repo": "main"})
        assert evaluate("git commit -m x", str(root)) is not None
        assert evaluate("git -c core.x=y commit --amend", str(root)) is not None

    def test_command_without_commit_word_short_circuits(self) -> None:
        assert evaluate("ls -la", "/anywhere") is None
        assert evaluate("", "/anywhere") is None

    def test_non_string_command_allowed(self) -> None:
        assert evaluate(None, "/anywhere") is None


class TestUnparseableCommitShape:
    """gr514943: heredoc/$(…) commits get a named refusal, not a bare
    parse error. These shapes are denied even on a feature branch — the
    worktree guard would refuse them anyway."""

    @pytest.mark.parametrize(
        "cmd",
        [
            # The shape the harness's own commit guidance shows.
            "git commit -m \"$(cat <<'EOF'\nsubject\n\nbody\nEOF\n)\"",
            'git commit -m "$(cat msg.txt)"',
            "git commit -m subject <<'EOF'\nbody\nEOF",
            "git commit -F - <<'EOF'\nsubject\nEOF",
            "cd /repo && git commit -m \"$(cat <<'EOF'\nx\nEOF\n)\"",
        ],
    )
    def test_denied_names_guard_and_fix(self, cmd: str, monkeypatch, tmp_path) -> None:
        root = _patch(monkeypatch, tmp_path, {tmp_path / "repo": "feature"})
        reason = evaluate(cmd, str(root))
        assert reason is not None, cmd
        assert "worktree guard" in reason
        assert "-F" in reason

    @pytest.mark.parametrize(
        "cmd",
        [
            'git commit -m "plain literal"',
            # A left-shift with a numeric operand is not a heredoc opener.
            'git commit -m x && python3 -c "print(1<<2)"',
            # gr515648: a commit in its own segment after an unrelated
            # heredoc is not "wrapped" — the gripe's exact refused shape.
            "python3 <<'PY'\nprint(1)\nPY\ngit commit -F scratch/msg.txt",
            "python3 - <<'PY'\nimport os\nPY\n\ngit commit -m x",
            "msg=$(cat f.txt) && git commit -F msg.txt",
        ],
    )
    def test_literal_and_shift_allowed(self, cmd: str, monkeypatch, tmp_path) -> None:
        root = _patch(monkeypatch, tmp_path, {tmp_path / "repo": "feature"})
        assert evaluate(cmd, str(root)) is None, cmd

    def test_commit_after_heredoc_on_main_is_still_denied(
        self, monkeypatch, tmp_path
    ) -> None:
        # gr515648 relaxes only the "wrapped" claim — a plain commit after an
        # unrelated heredoc is still a commit, refused on main itself.
        root = _patch(monkeypatch, tmp_path, {tmp_path / "repo": "main"})
        cmd = "python3 <<'PY'\nprint(1)\nPY\ngit commit -F scratch/msg.txt"
        reason = evaluate(cmd, str(root))
        assert reason is not None and "`main`" in reason

    def test_commit_inside_heredoc_body_still_wrapped(
        self, monkeypatch, tmp_path
    ) -> None:
        # A commit inside a heredoc BODY is data the worktree guard refuses
        # outright (heredocs may not name git) — the named denial stays.
        root = _patch(monkeypatch, tmp_path, {tmp_path / "repo": "feature"})
        reason = evaluate("bash <<'EOF'\ngit commit -m x\nEOF", str(root))
        assert reason is not None and "worktree guard" in reason


class TestMainWiring:
    def _run(self, monkeypatch, capsys, payload) -> tuple[int, dict | None]:
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        rc = main()
        out = capsys.readouterr().out
        return rc, (json.loads(out) if out.strip() else None)

    def test_main_denies_with_hook_json(self, monkeypatch, capsys, tmp_path) -> None:
        root = _patch(monkeypatch, tmp_path, {tmp_path / "repo": "main"})
        rc, out = self._run(
            monkeypatch,
            capsys,
            {
                "tool_input": {"command": "git commit -m x"},
                "cwd": str(root),
            },
        )
        assert rc == 0
        assert out is not None
        hso = out["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert hso["permissionDecision"] == "deny"
        assert "`main`" in hso["permissionDecisionReason"]

    def test_main_silent_on_feature_branch(self, monkeypatch, capsys, tmp_path) -> None:
        root = _patch(monkeypatch, tmp_path, {tmp_path / "repo": "feature"})
        rc, out = self._run(
            monkeypatch,
            capsys,
            {"tool_input": {"command": "git commit -m x"}, "cwd": str(root)},
        )
        assert rc == 0
        assert out is None

    def test_main_denies_heredoc_commit_with_named_reason(
        self, monkeypatch, capsys, tmp_path
    ) -> None:
        # gr514943: the refusal the builder sees names the worktree guard
        # and the -F alternative, not a bare shell parse error.
        root = _patch(monkeypatch, tmp_path, {tmp_path / "repo": "feature"})
        rc, out = self._run(
            monkeypatch,
            capsys,
            {
                "tool_input": {"command": "git commit -m \"$(cat <<'EOF'\nx\nEOF\n)\""},
                "cwd": str(root),
            },
        )
        assert rc == 0
        assert out is not None
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert "worktree guard" in reason
        assert "git commit -F" in reason

    def test_env_escape_hatch(self, monkeypatch, capsys, tmp_path) -> None:
        root = _patch(monkeypatch, tmp_path, {tmp_path / "repo": "main"})
        monkeypatch.setenv("ALLOW_COMMIT_ON_MAIN", "1")
        rc, out = self._run(
            monkeypatch,
            capsys,
            {"tool_input": {"command": "git commit -m x"}, "cwd": str(root)},
        )
        assert rc == 0
        assert out is None

    def test_unparseable_stdin_is_silent(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
        assert main() == 0
        assert capsys.readouterr().out == ""
