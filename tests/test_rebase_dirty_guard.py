"""Unit tests for the rebase-on-dirty-tree guard's decision logic.

Pure — exercises ``evaluate`` with stubbed ``_git``/``_git_rc`` (no real
repo needed). Mirrors ``tests/test_autostash_staged_guard.py``'s pattern
for loading a hyphenated-name hook script by path.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "guard-rebase-dirty.py"
)
_spec = importlib.util.spec_from_file_location("guard_rebase_dirty", _HOOK)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
evaluate = _mod.evaluate
main = _mod.main

REPO = "/repo"
OTHER = "/other"


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    dirty: set[str] | None = None,
    staged: set[str] | None = None,
    cfg: dict[tuple[str, str], bool] | None = None,
) -> None:
    """Stub the hook's two git seams.

    ``dirty``: dirs whose ``diff --quiet`` exits 1 (unstaged edits).
    ``staged``: dirs whose ``diff --cached --quiet`` exits 1 (staged).
    ``cfg``: ``{(cwd, key): bool}`` for ``config --get --bool`` lookups.
    """
    dirty = dirty or set()
    staged = staged or set()
    cfg = cfg or {}

    def _git(cwd: str, *args: str) -> str:
        if args[:3] == ("config", "--get", "--bool"):
            return "true" if cfg.get((cwd, args[3]), False) else "false"
        return ""

    def _git_rc(cwd: str, *args: str) -> int:
        if args[:3] == ("diff", "--cached", "--quiet"):
            return 1 if cwd in staged else 0
        if args[:2] == ("diff", "--quiet"):
            return 1 if cwd in dirty else 0
        return 0

    monkeypatch.setattr(_mod, "_git", _git)
    monkeypatch.setattr(_mod, "_git_rc", _git_rc)


class TestDeniedDirtyNoAutostash:
    """A rebase git would refuse mid-command anyway (gr514625)."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "git rebase origin/main",
            "git rebase main",
            "git rebase",  # bare: upstream from config, still needs a clean tree
            "git -C /repo rebase origin/main",
            "cd /repo && git rebase origin/main",
            "git -c user.name=x rebase origin/main",
            # An explicit --no-autostash is the same no-autostash case.
            "git rebase --no-autostash origin/main",
            "git -c rebase.autostash=false rebase origin/main",
        ],
    )
    def test_denies_on_unstaged(
        self, monkeypatch: pytest.MonkeyPatch, cmd: str
    ) -> None:
        _patch(monkeypatch, dirty={REPO})
        reason = evaluate(cmd, REPO)
        assert reason is not None, cmd
        assert "ALLOW_REBASE_DIRTY" in reason

    @pytest.mark.parametrize(
        "cmd",
        [
            "git rebase origin/main",
            "git -C /repo rebase origin/main",
        ],
    )
    def test_denies_on_staged_only(
        self, monkeypatch: pytest.MonkeyPatch, cmd: str
    ) -> None:
        _patch(monkeypatch, staged={REPO})
        assert evaluate(cmd, REPO) is not None, cmd

    def test_reason_steers_to_commit_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch(monkeypatch, dirty={REPO})
        reason = evaluate("git rebase origin/main", REPO)
        assert reason is not None
        assert "Commit" in reason and "gr514625" in reason

    def test_cd_moves_the_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, dirty={OTHER})
        # /repo is clean; the rebase runs in /other — dirt there is what counts.
        assert evaluate("cd /other && git rebase origin/main", REPO) is not None
        assert evaluate("git rebase origin/main", REPO) is None


class TestAllowed:
    """Clean trees, autostash in play, and non-rebase commands pass."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "git rebase origin/main",  # clean tree rebases fine
            "git rebase -i HEAD~3",  # interactivity is the harness's rule, not dirt
            "git status",
            "git merge topic",
            "git pull --rebase",
            "echo git rebase origin/main",  # not a git invocation
            "git rebase --continue",  # resume verbs take no autostash flag
            "git rebase --abort",
            "git rebase --skip",
            "git rebase --autostash --continue",  # nav wins even with the flag
        ],
    )
    def test_clean_tree_and_non_rebase_pass(
        self, monkeypatch: pytest.MonkeyPatch, cmd: str
    ) -> None:
        _patch(monkeypatch, dirty=set())
        assert evaluate(cmd, REPO) is None, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "git rebase --autostash origin/main",
            # The last of --no-autostash/--autostash wins.
            "git rebase --no-autostash --autostash origin/main",
            "git -c rebase.autostash=true rebase origin/main",
            "git -c rebase.autostash rebase origin/main",  # bare -c key = true
        ],
    )
    def test_autostash_passes_to_the_sibling_guard(
        self, monkeypatch: pytest.MonkeyPatch, cmd: str
    ) -> None:
        """Autostash over dirt is allowed HERE: a clean index round-trips
        safely, and a dirty index is guard-autostash-staged.py's denial."""
        _patch(monkeypatch, dirty={REPO})
        assert evaluate(cmd, REPO) is None, cmd

    def test_configured_autostash_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, dirty={REPO}, cfg={(REPO, "rebase.autostash"): True})
        assert evaluate("git rebase origin/main", REPO) is None

    def test_cli_config_beats_file_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch(
            monkeypatch,
            dirty={REPO},
            cfg={(REPO, "rebase.autostash"): True},
        )
        assert (
            evaluate("git -c rebase.autostash=false rebase origin/main", REPO)
            is not None
        )

    def test_unreadable_tree_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``diff`` exiting >1 (not a repo) is not this guard's to deny."""
        _patch(monkeypatch)  # all probes return 0/-1-clean
        assert evaluate("git rebase origin/main", "/nonexistent") is None


class TestMain:
    """The stdin-JSON wiring and the escape hatch."""

    def _payload(self, cmd: str, cwd: str = REPO) -> str:
        return json.dumps({"tool_input": {"command": cmd}, "cwd": cwd})

    def test_denies_via_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, dirty={REPO})
        monkeypatch.setattr("sys.stdin", io.StringIO(self._payload("git rebase main")))
        out = io.StringIO()
        monkeypatch.setattr("sys.stdout", out)
        assert main() == 0
        decision = json.loads(out.getvalue())["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"

    def test_allows_via_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch)
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(self._payload("git rebase origin/main"))
        )
        out = io.StringIO()
        monkeypatch.setattr("sys.stdout", out)
        assert main() == 0
        assert out.getvalue() == ""

    def test_escape_hatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, dirty={REPO})
        monkeypatch.setenv("ALLOW_REBASE_DIRTY", "1")
        monkeypatch.setattr("sys.stdin", io.StringIO(self._payload("git rebase main")))
        out = io.StringIO()
        monkeypatch.setattr("sys.stdout", out)
        assert main() == 0
        assert out.getvalue() == ""

    def test_unparseable_stdin_allows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
        assert main() == 0
