"""Tests for the fix_gripe job_type's pure helpers.

The full happy-path (clone + claude + push) requires git +
PRECIS_FIX_REPO_DIR + a working claude binary, so it's exercised
manually per the verification section of the plan. These tests
cover the pure / deterministic surface: the env restriction
that strips DB credentials before handing them to claude, the
prompt composition that turns a gripe timeline into a brief, and
the config loader that fails fast when the deployment env is
missing the required vars.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from precis.workers.job_types.fix_gripe import (
    FixGripeConfig,
    _compose_prompt,
    _restricted_env,
    load_config_from_env,
)


# ── _restricted_env: claude must not see the DB ────────────────────


class TestRestrictedEnv:
    """The subprocess env passed to claude is the only safety boundary
    between an autonomous agent and the precis-runtime postgres. Test
    the whitelist hard so a future env addition can't accidentally
    open a hole.
    """

    def test_strips_pg_creds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PGUSER", "precis")
        monkeypatch.setenv("PGPASSWORD", "super-secret")
        monkeypatch.setenv("PGHOST", "db.internal")
        env = _restricted_env(cwd_for_test())
        assert "PGUSER" not in env
        assert "PGPASSWORD" not in env
        assert "PGHOST" not in env

    def test_strips_precis_database_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "PRECIS_DATABASE_URL", "postgresql://precis:s3cret@db/precis"
        )
        env = _restricted_env(cwd_for_test())
        assert "PRECIS_DATABASE_URL" not in env

    def test_strips_other_precis_vars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Belt and braces: any PRECIS_* var goes — claude doesn't
        need to know about precis internals, and a future
        PRECIS_FOO_DATABASE_URL leaking through the PG-prefix
        filter would be embarrassing."""
        monkeypatch.setenv("PRECIS_WATCH_INBOX", "/tmp/precis-watch")
        env = _restricted_env(cwd_for_test())
        assert "PRECIS_WATCH_INBOX" not in env

    def test_keeps_path_and_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("HOME", "/home/precis")
        env = _restricted_env(cwd_for_test())
        assert env["PATH"] == "/usr/bin:/bin"
        assert env["HOME"] == "/home/precis"

    def test_keeps_anthropic_vars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ANTHROPIC_API_KEY is the alternate auth path; if the
        operator sets it on the precis container it must flow into
        the subprocess."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-XXX")
        env = _restricted_env(cwd_for_test())
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-XXX"

    def test_sets_pwd_to_cwd(self) -> None:
        env = _restricted_env(cwd_for_test())
        assert env["PWD"] == "/fake/clone"


def cwd_for_test() -> Path:
    """A stand-in path object with the str()-form we want."""
    return Path("/fake/clone")


# ── _compose_prompt: gripe timeline → claude brief ─────────────────


class TestComposePrompt:
    def test_body_only(self) -> None:
        prompt = _compose_prompt(
            ref_title="paper NotFound has no near-match suggestions",
            blocks=[_FakeBlock("paper NotFound has no near-match suggestions")],
        )
        assert "BUG REPORT" in prompt
        assert "BODY: paper NotFound has no near-match suggestions" in prompt
        # No comment lines when there's only a body.
        assert "COMMENT 1" not in prompt

    def test_body_plus_comments(self) -> None:
        prompt = _compose_prompt(
            ref_title="bug",
            blocks=[
                _FakeBlock("the bug body"),
                _FakeBlock("more detail 1"),
                _FakeBlock("more detail 2"),
            ],
        )
        assert "BODY: the bug body" in prompt
        assert "COMMENT 1: more detail 1" in prompt
        assert "COMMENT 2: more detail 2" in prompt

    def test_constraints_present(self) -> None:
        prompt = _compose_prompt(
            ref_title="bug", blocks=[_FakeBlock("any body")]
        )
        assert "CONSTRAINTS" in prompt
        assert "gripe_*" in prompt
        assert "Do NOT touch main" in prompt
        assert "push your branch to origin" in prompt


@dataclass(frozen=True)
class _FakeBlock:
    text: str


# ── load_config_from_env: fail fast on missing required env ───────


class TestLoadConfig:
    def test_missing_repo_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PRECIS_FIX_REPO_DIR", raising=False)
        monkeypatch.setenv("PRECIS_FIX_WORK_DIR", "/tmp/precis-fix-work")
        with pytest.raises(RuntimeError, match="PRECIS_FIX_REPO_DIR"):
            load_config_from_env()

    def test_missing_work_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRECIS_FIX_REPO_DIR", "/tmp/repo")
        monkeypatch.delenv("PRECIS_FIX_WORK_DIR", raising=False)
        with pytest.raises(RuntimeError, match="PRECIS_FIX_WORK_DIR"):
            load_config_from_env()

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRECIS_FIX_REPO_DIR", "/tmp/repo")
        monkeypatch.setenv("PRECIS_FIX_WORK_DIR", "/tmp/precis-fix-work")
        for var in (
            "PRECIS_FIX_CLAUDE_BIN",
            "PRECIS_FIX_CLAUDE_MODEL",
            "PRECIS_FIX_TIMEOUT_SECONDS",
        ):
            monkeypatch.delenv(var, raising=False)
        cfg = load_config_from_env()
        assert isinstance(cfg, FixGripeConfig)
        assert cfg.claude_bin == "claude"
        assert cfg.timeout_seconds == 1800


# ── job_types registry: lookup paths ───────────────────────────────


class TestJobTypeRegistry:
    def test_known_types_lists_fix_gripe(self) -> None:
        from precis.workers.job_types import known_job_types

        assert "fix_gripe" in known_job_types()

    def test_get_job_type_returns_spec(self) -> None:
        from precis.workers.job_types import get_job_type

        spec = get_job_type("fix_gripe")
        assert spec is not None
        assert spec.name == "fix_gripe"
        assert spec.compatible_executors == frozenset({"claude_inproc"})
        assert "claude_bin" in spec.requires

    def test_get_unknown_returns_none(self) -> None:
        from precis.workers.job_types import get_job_type

        assert get_job_type("simulate_warp_drive") is None


# ── executor registry ──────────────────────────────────────────────


class TestExecutorRegistry:
    def test_claude_inproc_provides(self) -> None:
        from precis.workers.executors import EXECUTOR_PROVIDES

        assert "claude_bin" in EXECUTOR_PROVIDES["claude_inproc"]
        assert "git" in EXECUTOR_PROVIDES["claude_inproc"]

    def test_default_executor_is_claude_inproc(self) -> None:
        from precis.workers.executors import DEFAULT_EXECUTOR

        assert DEFAULT_EXECUTOR == "claude_inproc"
