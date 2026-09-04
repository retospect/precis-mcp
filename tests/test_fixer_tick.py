"""Unit + run_tick-level tests for the fixer's model tiering and branch
cleanup (``fixer-spec-tiering-and-split`` (git-only)).

Two load-bearing behaviors:

* **Model resolution.** ``FixerConfig.default_model`` now defaults to
  ``claude-sonnet-5`` (down from ``claude-opus-4-8``); a proposal's
  ``model:`` tier (via ``WorkItem.model``) overrides it through
  ``TIER_MODELS``.
* **Local branch cleanup after a real ship.** ``scripts/ship`` deletes
  only the *remote* copy of a squash-merged branch and resets the
  *local* one to shipped ``main`` — it never deletes the local ref.
  Without ``run_tick`` doing that itself, a shipped predecessor's
  ``fix/<slug>`` branch would persist forever in the fixer's own repo
  clone, and ``blocked-by`` would never unblock. This is exercised
  against a real throwaway git repo (not just a faked ``branch_exists``)
  so the assertion is on the fixer's actual git state.
"""

from __future__ import annotations

import subprocess
import types
from pathlib import Path
from typing import Any

import pytest

from precis.fixer import tick as tick_mod
from precis.fixer.intake import WorkItem
from precis.fixer.report import ReportStatus
from precis.fixer.tick import (
    TIER_MODELS,
    Autonomy,
    FixerConfig,
    _resolve_model,
    branch_exists,
)


def _cfg(
    tmp_path: Path,
    repo_root: Path,
    autonomy: Autonomy,
    *,
    gripe_db_url: str | None = None,
) -> FixerConfig:
    return FixerConfig(
        repo_root=repo_root,
        backlog_dir=repo_root / "docs" / "proposals",
        work_dir=tmp_path / "work",
        claude_bin="claude",
        default_model="claude-sonnet-5",
        autonomy=autonomy,
        build_timeout_s=60,
        gate_cmds=(("true",),),
        discord_webhook=None,
        readyz_url=None,
        gripe_db_url=gripe_db_url,
    )


def _item(**kwargs: Any) -> WorkItem:
    base: dict[str, Any] = dict(
        kind="proposal",
        slug="do-the-thing",
        title="Do the thing",
        branch="fix/do-the-thing",
        spec_text="x",
    )
    base.update(kwargs)
    return WorkItem(**base)


# ── model resolution ────────────────────────────────────────────────


def test_resolve_model_defaults_to_cfg_default_when_item_unset(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path, tmp_path, Autonomy.REPORT)
    assert _resolve_model(cfg, _item(model=None)) == "claude-sonnet-5"


def test_resolve_model_opus_tier_overrides_default(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, tmp_path, Autonomy.REPORT)
    assert _resolve_model(cfg, _item(model="opus")) == TIER_MODELS["opus"]
    assert TIER_MODELS["opus"] == "claude-opus-4-8"


def test_resolve_model_haiku_tier_overrides_default(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, tmp_path, Autonomy.REPORT)
    assert _resolve_model(cfg, _item(model="haiku")) == TIER_MODELS["haiku"]
    assert TIER_MODELS["haiku"] == "claude-haiku-4-5-20251001"


def test_resolve_model_unrecognised_tier_falls_back_to_default(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path, tmp_path, Autonomy.REPORT)
    assert _resolve_model(cfg, _item(model="not-a-real-tier")) == "claude-sonnet-5"


def test_fixer_config_from_env_default_model_is_sonnet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PRECIS_FIXER_CLAUDE_MODEL", raising=False)
    cfg = FixerConfig.from_env(tmp_path)
    assert cfg.default_model == "claude-sonnet-5"


def test_fixer_config_from_env_respects_explicit_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PRECIS_FIXER_CLAUDE_MODEL", "claude-pinned")
    cfg = FixerConfig.from_env(tmp_path)
    assert cfg.default_model == "claude-pinned"


# ── run_tick: local branch cleanup after a real ship ────────────────


def _init_repo(path: Path) -> None:
    """A throwaway repo with a `main` branch and a fake `origin/main`.

    Real remote plumbing isn't needed for `git worktree add ... origin/main`
    to resolve — a remote-tracking ref is enough — so this stays a pure
    local-only fixture.
    """
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, check=True)
    subprocess.run(["git", "branch", "-m", "main"], cwd=path, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", sha], cwd=path, check=True
    )


def _fake_spawn_claude(
    cfg: FixerConfig, cwd: Path, prompt: str, item: WorkItem
) -> types.SimpleNamespace:
    """Stand in for a builder that commits one change (no real `claude`)."""
    (cwd / "change.txt").write_text("changed\n", encoding="utf-8")
    tick_mod._commit_if_dirty(cwd, "test: fake build commit")
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def test_run_tick_deletes_local_branch_after_real_ship(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    cfg = _cfg(tmp_path, repo, Autonomy.SHIP)
    item = _item()

    monkeypatch.setattr(tick_mod, "_spawn_claude", _fake_spawn_claude)
    monkeypatch.setattr(tick_mod, "_autofix_lint", lambda worktree: None)
    monkeypatch.setattr(
        tick_mod, "_quick_gate", lambda cfg, worktree: (True, "gate green (fake)")
    )
    monkeypatch.setattr(
        tick_mod,
        "_run_script_in_worktree",
        lambda worktree, script, msg: (True, "shipped (fake)"),
    )
    monkeypatch.setattr(tick_mod, "all_items", lambda backlog_dir, gripe_db_url: [item])

    result = tick_mod.run_tick(cfg)

    assert result.report is not None
    assert result.report.status is ReportStatus.OK
    assert branch_exists(repo, item.branch) is False


def test_run_tick_report_mode_leaves_local_branch_in_place(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    cfg = _cfg(tmp_path, repo, Autonomy.REPORT)
    item = _item()

    monkeypatch.setattr(tick_mod, "_spawn_claude", _fake_spawn_claude)
    monkeypatch.setattr(tick_mod, "_autofix_lint", lambda worktree: None)
    monkeypatch.setattr(
        tick_mod, "_quick_gate", lambda cfg, worktree: (True, "gate green (fake)")
    )
    monkeypatch.setattr(tick_mod, "all_items", lambda backlog_dir, gripe_db_url: [item])

    result = tick_mod.run_tick(cfg)

    assert result.report is not None
    assert result.report.status is ReportStatus.OK
    # Deliberately left behind: a successor's `blocked-by` must keep
    # blocking until an actual ship happens.
    assert branch_exists(repo, item.branch) is True


# ── run_tick: gripe write-back ───────────────────────────────────────


def _gripe_item(**kwargs: Any) -> WorkItem:
    base: dict[str, Any] = dict(
        kind="gripe",
        slug="gr42",
        title="Something broke",
        branch="fix/gr42",
        spec_text="x",
        ref_id=42,
    )
    base.update(kwargs)
    return WorkItem(**base)


def test_run_tick_calls_gripe_writeback_once_on_ok_report_autonomy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    cfg = _cfg(tmp_path, repo, Autonomy.REPORT, gripe_db_url="postgresql://example/db")
    item = _gripe_item()

    monkeypatch.setattr(tick_mod, "_spawn_claude", _fake_spawn_claude)
    monkeypatch.setattr(tick_mod, "_autofix_lint", lambda worktree: None)
    monkeypatch.setattr(
        tick_mod, "_quick_gate", lambda cfg, worktree: (True, "gate green (fake)")
    )
    monkeypatch.setattr(tick_mod, "all_items", lambda backlog_dir, gripe_db_url: [item])

    calls: list[tuple[Any, ...]] = []

    def _record_writeback(*a: Any) -> bool:
        calls.append(a)
        return True

    monkeypatch.setattr(tick_mod, "gripe_writeback", _record_writeback)

    result = tick_mod.run_tick(cfg)

    assert result.report is not None
    assert result.report.status is ReportStatus.OK
    assert len(calls) == 1
    db_url, ref_id, comment, status = calls[0]
    assert db_url == "postgresql://example/db"
    assert ref_id == 42
    assert comment.startswith("FIXER (auto):")
    assert status == "in_review"


def test_run_tick_calls_gripe_writeback_once_on_needs_you(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    cfg = _cfg(tmp_path, repo, Autonomy.REPORT, gripe_db_url="postgresql://example/db")
    item = _gripe_item()

    def _fake_spawn_claude_fails(
        cfg: FixerConfig, cwd: Path, prompt: str, item: WorkItem
    ) -> types.SimpleNamespace:
        return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(tick_mod, "_spawn_claude", _fake_spawn_claude_fails)
    monkeypatch.setattr(tick_mod, "all_items", lambda backlog_dir, gripe_db_url: [item])

    calls: list[tuple[Any, ...]] = []

    def _record_writeback(*a: Any) -> bool:
        calls.append(a)
        return True

    monkeypatch.setattr(tick_mod, "gripe_writeback", _record_writeback)

    result = tick_mod.run_tick(cfg)

    assert result.report is not None
    assert result.report.status is ReportStatus.NEEDS_YOU
    assert len(calls) == 1
    db_url, ref_id, comment, status = calls[0]
    assert db_url == "postgresql://example/db"
    assert ref_id == 42
    assert comment.startswith("FIXER (auto):")
    assert status is None


def test_run_tick_shipped_needs_you_writeback_is_honest_and_in_review(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """gr313409: full autonomy — ``scripts/ship`` succeeds (fix lands on
    main) but the post-ship deploy/prod-check fails. The write-back must
    NOT claim the build "did not land" (it did) and must still flip
    ``STATUS:in_review`` (fix-forward needed, not a re-pick from open)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    cfg = _cfg(tmp_path, repo, Autonomy.FULL, gripe_db_url="postgresql://example/db")
    item = _gripe_item()

    monkeypatch.setattr(tick_mod, "_spawn_claude", _fake_spawn_claude)
    monkeypatch.setattr(tick_mod, "_autofix_lint", lambda worktree: None)
    monkeypatch.setattr(
        tick_mod, "_quick_gate", lambda cfg, worktree: (True, "gate green (fake)")
    )
    monkeypatch.setattr(
        tick_mod,
        "_run_script_in_worktree",
        lambda worktree, script, msg: (True, "shipped (fake)"),
    )
    monkeypatch.setattr(
        tick_mod, "_run_script", lambda cfg, script, *args: (True, "deploy ok (fake)")
    )
    monkeypatch.setattr(
        tick_mod, "_look_at_prod", lambda cfg: (False, "/readyz unreachable")
    )
    monkeypatch.setattr(tick_mod, "all_items", lambda backlog_dir, gripe_db_url: [item])

    calls: list[tuple[Any, ...]] = []

    def _record_writeback(*a: Any) -> bool:
        calls.append(a)
        return True

    monkeypatch.setattr(tick_mod, "gripe_writeback", _record_writeback)

    result = tick_mod.run_tick(cfg)

    assert result.report is not None
    assert result.report.status is ReportStatus.NEEDS_YOU
    assert len(calls) == 1
    db_url, ref_id, comment, status = calls[0]
    assert db_url == "postgresql://example/db"
    assert ref_id == 42
    assert comment.startswith("FIXER (auto):")
    assert "did not land" not in comment
    assert "shipped" in comment.lower()
    assert status == "in_review"


def test_run_tick_skips_writeback_for_proposal_kind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    cfg = _cfg(tmp_path, repo, Autonomy.REPORT, gripe_db_url="postgresql://example/db")
    item = _item()  # kind="proposal"

    monkeypatch.setattr(tick_mod, "_spawn_claude", _fake_spawn_claude)
    monkeypatch.setattr(tick_mod, "_autofix_lint", lambda worktree: None)
    monkeypatch.setattr(
        tick_mod, "_quick_gate", lambda cfg, worktree: (True, "gate green (fake)")
    )
    monkeypatch.setattr(tick_mod, "all_items", lambda backlog_dir, gripe_db_url: [item])

    calls: list[tuple[Any, ...]] = []

    def _record_writeback(*a: Any) -> bool:
        calls.append(a)
        return True

    monkeypatch.setattr(tick_mod, "gripe_writeback", _record_writeback)

    tick_mod.run_tick(cfg)

    assert calls == []


def test_run_tick_skips_writeback_when_gripe_db_url_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    cfg = _cfg(tmp_path, repo, Autonomy.REPORT, gripe_db_url=None)
    item = _gripe_item()

    monkeypatch.setattr(tick_mod, "_spawn_claude", _fake_spawn_claude)
    monkeypatch.setattr(tick_mod, "_autofix_lint", lambda worktree: None)
    monkeypatch.setattr(
        tick_mod, "_quick_gate", lambda cfg, worktree: (True, "gate green (fake)")
    )
    monkeypatch.setattr(tick_mod, "all_items", lambda backlog_dir, gripe_db_url: [item])

    calls: list[tuple[Any, ...]] = []

    def _record_writeback(*a: Any) -> bool:
        calls.append(a)
        return True

    monkeypatch.setattr(tick_mod, "gripe_writeback", _record_writeback)

    tick_mod.run_tick(cfg)

    assert calls == []
