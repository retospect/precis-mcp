"""Unit tests for the autostash-with-staged-index guard's decision logic.

Pure — exercises ``evaluate`` with stubbed ``_git``/``_git_rc`` (no real repo
needed). Mirrors ``tests/test_piped_exit_code_guard.py``'s pattern for loading
a hyphenated-name hook script by path.
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
    / "guard-autostash-staged.py"
)
_spec = importlib.util.spec_from_file_location("guard_autostash_staged", _HOOK)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
evaluate = _mod.evaluate
main = _mod.main

REPO = "/repo"
OTHER = "/other"


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    staged: set[str] | None = None,
    cfg: dict[tuple[str, str], bool] | None = None,
    branch: str = "feat",
) -> None:
    """Stub the hook's two git seams.

    ``staged``: dirs whose ``diff --cached --quiet`` exits 1.
    ``cfg``: ``{(cwd, key): bool}`` for ``config --get --bool`` lookups.
    """
    staged = staged or set()
    cfg = cfg or {}

    def _git(cwd: str, *args: str) -> str:
        if args[:2] == ("rev-parse", "--abbrev-ref"):
            return branch
        if args[:3] == ("config", "--get", "--bool"):
            return "true" if cfg.get((cwd, args[3]), False) else "false"
        return ""

    def _git_rc(cwd: str, *args: str) -> int:
        if args[:3] == ("diff", "--cached", "--quiet"):
            return 1 if cwd in staged else 0
        return 0

    monkeypatch.setattr(_mod, "_git", _git)
    monkeypatch.setattr(_mod, "_git_rc", _git_rc)


class TestDeniedFlag:
    """``--autostash`` on the command line, index non-empty: the gripe itself."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "git rebase --autostash origin/main",
            "git rebase -i --autostash HEAD~3",
            "git pull --rebase --autostash",
            # A merge pull honours --autostash too — same stash, same loss.
            "git pull --autostash",
            "git merge --autostash topic",
            # The last of --no-autostash/--autostash wins.
            "git rebase --no-autostash --autostash origin/main",
            "git rebase --autostash=false --autostash origin/main",
        ],
    )
    def test_denies(self, monkeypatch: pytest.MonkeyPatch, cmd: str) -> None:
        _patch(monkeypatch, staged={REPO})
        reason = evaluate(cmd, REPO)
        assert reason is not None, cmd
        # The refusal names the fix.
        assert "Commit first" in reason

    def test_reason_names_autostash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, staged={REPO})
        assert "--autostash" in (
            evaluate("git rebase --autostash origin/main", REPO) or ""
        )


class TestDeniedConfig:
    """``*.autoStash=true`` config footguns the same way on a bare verb."""

    def test_rebase_autostash_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, staged={REPO}, cfg={(REPO, "rebase.autostash"): True})
        reason = evaluate("git rebase origin/main", REPO)
        assert reason is not None
        assert "rebase.autostash" in reason

    def test_merge_autostash_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, staged={REPO}, cfg={(REPO, "merge.autostash"): True})
        assert evaluate("git merge topic", REPO) is not None

    def test_pull_rebase_mode_reads_rebase_autostash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch(
            monkeypatch,
            staged={REPO},
            cfg={(REPO, "pull.rebase"): True, (REPO, "rebase.autostash"): True},
        )
        assert evaluate("git pull", REPO) is not None

    def test_pull_rebase_flag_with_autostash_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `pull --rebase` + rebase.autoStash=true is the flag-less version of
        # the gripe's footgun.
        _patch(monkeypatch, staged={REPO}, cfg={(REPO, "rebase.autostash"): True})
        assert evaluate("git pull --rebase", REPO) is not None

    def test_rebase_flagless_with_autostash_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch(monkeypatch, staged={REPO}, cfg={(REPO, "rebase.autostash"): True})
        assert evaluate("git rebase origin/main", REPO) is not None

    def test_pull_merge_mode_reads_merge_autostash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch(monkeypatch, staged={REPO}, cfg={(REPO, "merge.autostash"): True})
        assert evaluate("git pull", REPO) is not None

    def test_pull_rebase_mode_ignores_merge_autostash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A rebase pull reads rebase.autoStash; merge.autoStash alone is a
        # false positive here.
        _patch(
            monkeypatch,
            staged={REPO},
            cfg={(REPO, "pull.rebase"): True, (REPO, "merge.autostash"): True},
        )
        assert evaluate("git pull", REPO) is None

    def test_branch_rebase_config_selects_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch(
            monkeypatch,
            staged={REPO},
            cfg={(REPO, "branch.feat.rebase"): True, (REPO, "rebase.autostash"): True},
        )
        assert evaluate("git pull", REPO) is not None

    def test_dash_c_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, staged={REPO})
        reason = evaluate("git -c rebase.autostash=true rebase origin/main", REPO)
        assert reason is not None
        assert "rebase.autostash" in reason

    def test_dash_c_bare_key_is_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, staged={REPO})
        assert evaluate("git -c rebase.autostash rebase origin/main", REPO) is not None

    def test_dash_c_false_overrides_config_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch(monkeypatch, staged={REPO}, cfg={(REPO, "rebase.autostash"): True})
        assert (
            evaluate("git -c rebase.autostash=false rebase origin/main", REPO) is None
        )


class TestAllowed:
    """No staged changes to lose, or no autostash in play."""

    @pytest.mark.parametrize(
        "cmd",
        [
            # Clean index: nothing for the pop to flatten.
            "git rebase --autostash origin/main",
            "git pull --rebase --autostash",
            "git merge --autostash topic",
            "",
        ],
    )
    def test_allows(self, monkeypatch: pytest.MonkeyPatch, cmd: str) -> None:
        _patch(monkeypatch, staged=set())
        assert evaluate(cmd, REPO) is None, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            # No autostash anywhere: no flag, no config — git itself refuses
            # a dirty-index rebase.
            "git rebase origin/main",
            "git pull --rebase",
            "git merge topic",
            # Explicit opt-out wins over the flag tail order.
            "git rebase --autostash --no-autostash origin/main",
            "git rebase --autostash=false origin/main",
            # Lookalikes that are not these git verbs.
            "stgit rebase --autostash",
            "git rebase-migration --autostash",  # a different subcommand
        ],
    )
    def test_allows_even_with_staged_changes(
        self, monkeypatch: pytest.MonkeyPatch, cmd: str
    ) -> None:
        _patch(monkeypatch, staged={REPO})
        assert evaluate(cmd, REPO) is None, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            # Resume verbs can't take the flag and must stay reachable
            # mid-rebase even with staged changes + config autostash.
            "git rebase --continue",
            "git rebase --abort",
            "git rebase --continue --autostash",
        ],
    )
    def test_nav_verbs_allowed_under_autostash_config(
        self, monkeypatch: pytest.MonkeyPatch, cmd: str
    ) -> None:
        _patch(
            monkeypatch,
            staged={REPO},
            cfg={(REPO, "rebase.autostash"): True},
        )
        assert evaluate(cmd, REPO) is None, cmd

    def test_clean_index_allows_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, staged=set())
        assert evaluate("git rebase --autostash origin/main", REPO) is None

    def test_config_set_but_clean_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, staged=set(), cfg={(REPO, "rebase.autostash"): True})
        assert evaluate("git rebase origin/main", REPO) is None

    def test_staged_but_flagless_and_config_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch(monkeypatch, staged={REPO})
        assert evaluate("git rebase origin/main", REPO) is None

    def test_non_string_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, staged={REPO})
        assert evaluate(None, REPO) is None


class TestDirResolution:
    """The index is read where the verb lands, not at the session cwd."""

    def test_git_dash_c_target_is_checked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch(monkeypatch, staged={OTHER})
        reason = evaluate("git -C /other rebase --autostash origin/main", REPO)
        assert reason is not None

    def test_git_dash_c_clean_target_allows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Staged in the session dir, clean at the -C target → allowed.
        _patch(monkeypatch, staged={REPO})
        assert evaluate("git -C /other rebase --autostash", REPO) is None

    def test_leading_cd_target_is_checked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch(monkeypatch, staged={OTHER})
        reason = evaluate("cd /other && git rebase --autostash", REPO)
        assert reason is not None

    def test_not_a_repo_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # diff --cached errors (>1) off-repo: allow — git fails on its own.
        def _git_rc(cwd: str, *args: str) -> int:
            return 128

        monkeypatch.setattr(_mod, "_git_rc", _git_rc)
        assert evaluate("git rebase --autostash", "/tmp") is None


class TestMain:
    """The stdin/stdout hook protocol."""

    def _run(self, monkeypatch: pytest.MonkeyPatch, cmd: str, capsys) -> str:
        payload = {"cwd": REPO, "tool_input": {"command": cmd}}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        assert main() == 0
        return capsys.readouterr().out

    def test_emits_deny_decision(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        monkeypatch.delenv("ALLOW_AUTOSTASH_STAGED", raising=False)
        _patch(monkeypatch, staged={REPO})
        out = self._run(monkeypatch, "git rebase --autostash origin/main", capsys)
        decision = json.loads(out)["hookSpecificOutput"]
        assert decision["hookEventName"] == "PreToolUse"
        assert decision["permissionDecision"] == "deny"

    def test_silent_when_allowed(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        monkeypatch.delenv("ALLOW_AUTOSTASH_STAGED", raising=False)
        _patch(monkeypatch, staged=set())
        assert self._run(monkeypatch, "git rebase --autostash", capsys) == ""

    def test_escape_hatch(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        monkeypatch.setenv("ALLOW_AUTOSTASH_STAGED", "1")
        out = self._run(monkeypatch, "git rebase --autostash", capsys)
        assert out == ""

    def test_malformed_stdin_is_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.delenv("ALLOW_AUTOSTASH_STAGED", raising=False)
        monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
        assert main() == 0
        assert capsys.readouterr().out == ""
