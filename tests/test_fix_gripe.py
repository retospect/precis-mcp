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

    def test_strips_precis_database_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "PRECIS_DATABASE_URL", "postgresql://precis:s3cret@db/precis"
        )
        env = _restricted_env(cwd_for_test())
        assert "PRECIS_DATABASE_URL" not in env

    def test_strips_other_precis_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Belt and braces: any PRECIS_* var goes — claude doesn't
        need to know about precis internals, and a future
        PRECIS_FOO_DATABASE_URL leaking through the PG-prefix
        filter would be embarrassing."""
        monkeypatch.setenv("PRECIS_WATCH_INBOX", "/tmp/precis-watch")
        env = _restricted_env(cwd_for_test())
        assert "PRECIS_WATCH_INBOX" not in env

    def test_keeps_path_and_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("HOME", "/home/precis")
        env = _restricted_env(cwd_for_test())
        assert env["PATH"] == "/usr/bin:/bin"
        assert env["HOME"] == "/home/precis"

    def test_keeps_anthropic_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ANTHROPIC_API_KEY is the alternate auth path; if the
        operator sets it on the precis container it must flow into
        the subprocess."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-XXX")
        env = _restricted_env(cwd_for_test())
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-XXX"

    def test_sets_pwd_to_cwd(self) -> None:
        env = _restricted_env(cwd_for_test())
        # ``str(Path)`` uses native separators (``\\`` on Windows,
        # ``/`` on POSIX). The runtime stamps the PWD using
        # ``str(cwd)``; compare via the same conversion so the test
        # is cross-platform.
        assert env["PWD"] == str(cwd_for_test())


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
        prompt = _compose_prompt(ref_title="bug", blocks=[_FakeBlock("any body")])
        assert "CONSTRAINTS" in prompt
        assert "gripe_*" in prompt
        assert "Do NOT touch main" in prompt
        assert "push your branch to origin" in prompt


@dataclass(frozen=True)
class _FakeBlock:
    text: str


# ── load_config_from_env: fail fast on missing required env ───────


class TestLoadConfig:
    def test_missing_work_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRECIS_FIX_REPO_DIR", "/tmp/repo")
        monkeypatch.delenv("PRECIS_FIX_WORK_DIR", raising=False)
        with pytest.raises(RuntimeError, match="PRECIS_FIX_WORK_DIR"):
            load_config_from_env()

    def test_missing_both_repo_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """At least one of PRECIS_FIX_REPO_DIR / PRECIS_FIX_REPOS
        must be set, or the runner has no repo to clone."""
        monkeypatch.setenv("PRECIS_FIX_WORK_DIR", "/tmp/precis-fix-work")
        monkeypatch.delenv("PRECIS_FIX_REPO_DIR", raising=False)
        monkeypatch.delenv("PRECIS_FIX_REPOS", raising=False)
        with pytest.raises(RuntimeError, match="neither PRECIS_FIX_REPO_DIR"):
            load_config_from_env()

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRECIS_FIX_REPO_DIR", "/tmp/repo")
        monkeypatch.setenv("PRECIS_FIX_WORK_DIR", "/tmp/precis-fix-work")
        monkeypatch.delenv("PRECIS_FIX_REPOS", raising=False)
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
        # ``load_config_from_env`` calls ``.resolve()`` on the path —
        # which on macOS turns ``/tmp/...`` into ``/private/tmp/...``
        # (the symlink target) and on Windows applies the current drive
        # letter. Compare resolved-form to keep the test cross-platform.
        assert cfg.default_repo_dir == Path("/tmp/repo").resolve()
        assert cfg.repos == {}

    def test_claude_model_resolves_via_tier_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unit 4b: with no bespoke override, claude_model resolves through
        the ADR 0046 CLOUD_SUPER tier — the consolidated opus-4.8 cloud
        reasoning default."""
        monkeypatch.setenv("PRECIS_FIX_REPO_DIR", "/tmp/repo")
        monkeypatch.setenv("PRECIS_FIX_WORK_DIR", "/tmp/precis-fix-work")
        monkeypatch.delenv("PRECIS_FIX_CLAUDE_MODEL", raising=False)
        monkeypatch.delenv("PRECIS_MODEL_OPUS", raising=False)
        cfg = load_config_from_env()
        assert cfg.claude_model == "claude-opus-4-8"

    def test_claude_model_bespoke_override_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bespoke ``PRECIS_FIX_CLAUDE_MODEL`` knob still takes precedence
        over the shared tier default."""
        monkeypatch.setenv("PRECIS_FIX_REPO_DIR", "/tmp/repo")
        monkeypatch.setenv("PRECIS_FIX_WORK_DIR", "/tmp/precis-fix-work")
        monkeypatch.setenv("PRECIS_FIX_CLAUDE_MODEL", "claude-pinned-fix")
        monkeypatch.setenv("PRECIS_MODEL_OPUS", "claude-tier-opus")
        cfg = load_config_from_env()
        assert cfg.claude_model == "claude-pinned-fix"

    def test_claude_model_follows_opus_pin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without the bespoke override, fix_gripe follows the shared opus
        pin (``PRECIS_MODEL_OPUS``) — the point of routing through the
        resolver."""
        monkeypatch.setenv("PRECIS_FIX_REPO_DIR", "/tmp/repo")
        monkeypatch.setenv("PRECIS_FIX_WORK_DIR", "/tmp/precis-fix-work")
        monkeypatch.delenv("PRECIS_FIX_CLAUDE_MODEL", raising=False)
        monkeypatch.setenv("PRECIS_MODEL_OPUS", "claude-opus-pinned")
        cfg = load_config_from_env()
        assert cfg.claude_model == "claude-opus-pinned"

    def test_repos_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRECIS_FIX_WORK_DIR", "/tmp/precis-fix-work")
        monkeypatch.setenv(
            "PRECIS_FIX_REPOS",
            '{"precis-mcp": "/tmp/precis-mcp", "other": "/tmp/other"}',
        )
        monkeypatch.delenv("PRECIS_FIX_REPO_DIR", raising=False)
        cfg = load_config_from_env()
        assert cfg.default_repo_dir is None
        # Symlink + drive normalisation — see test_defaults above.
        assert cfg.repos == {
            "precis-mcp": Path("/tmp/precis-mcp").resolve(),
            "other": Path("/tmp/other").resolve(),
        }

    def test_repos_json_malformed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRECIS_FIX_WORK_DIR", "/tmp/precis-fix-work")
        monkeypatch.setenv("PRECIS_FIX_REPO_DIR", "/tmp/fallback")
        monkeypatch.setenv("PRECIS_FIX_REPOS", "not-json")
        with pytest.raises(RuntimeError, match="not valid JSON"):
            load_config_from_env()


# ── resolve_repo_for_gripe: tag-driven multi-repo ─────────────────


class TestResolveRepoForGripe:
    """``repo:<name>`` on the gripe selects the host path through
    ``PRECIS_FIX_REPOS``; un-tagged gripes fall back to
    ``PRECIS_FIX_REPO_DIR``."""

    @staticmethod
    def _store_with_tags(tag_values: list[str]) -> object:
        class _Store:
            def tags_for(self, _ref_id: int) -> list[str]:
                return list(tag_values)

        return _Store()

    def test_tag_lookup(self) -> None:
        from precis.workers.job_types.fix_gripe import (
            FixGripeConfig,
            resolve_repo_for_gripe,
        )

        cfg = FixGripeConfig(
            default_repo_dir=None,
            work_dir=Path("/tmp/work"),
            claude_bin="claude",
            claude_model="claude-opus-4-7",
            timeout_seconds=1800,
            repos={"my-other-project": Path("/tmp/other")},
        )
        store = self._store_with_tags(["STATUS:open", "repo:my-other-project"])
        path = resolve_repo_for_gripe(store, 42, cfg)
        assert path == Path("/tmp/other")

    def test_fallback_when_no_tag(self) -> None:
        from precis.workers.job_types.fix_gripe import (
            FixGripeConfig,
            resolve_repo_for_gripe,
        )

        cfg = FixGripeConfig(
            default_repo_dir=Path("/tmp/precis-mcp"),
            work_dir=Path("/tmp/work"),
            claude_bin="claude",
            claude_model="claude-opus-4-7",
            timeout_seconds=1800,
            repos={},
        )
        store = self._store_with_tags(["STATUS:open"])
        path = resolve_repo_for_gripe(store, 42, cfg)
        assert path == Path("/tmp/precis-mcp")

    def test_unknown_repo_tag_raises(self) -> None:
        from precis.workers.job_types.fix_gripe import (
            FixGripeConfig,
            resolve_repo_for_gripe,
        )

        cfg = FixGripeConfig(
            default_repo_dir=Path("/tmp/precis-mcp"),
            work_dir=Path("/tmp/work"),
            claude_bin="claude",
            claude_model="claude-opus-4-7",
            timeout_seconds=1800,
            repos={"precis-mcp": Path("/tmp/precis-mcp")},
        )
        store = self._store_with_tags(["repo:does-not-exist"])
        with pytest.raises(ValueError, match="not in PRECIS_FIX_REPOS"):
            resolve_repo_for_gripe(store, 42, cfg)

    def test_no_tag_no_fallback_raises(self) -> None:
        from precis.workers.job_types.fix_gripe import (
            FixGripeConfig,
            resolve_repo_for_gripe,
        )

        cfg = FixGripeConfig(
            default_repo_dir=None,
            work_dir=Path("/tmp/work"),
            claude_bin="claude",
            claude_model="claude-opus-4-7",
            timeout_seconds=1800,
            repos={"precis-mcp": Path("/tmp/precis-mcp")},
        )
        store = self._store_with_tags(["STATUS:open"])
        with pytest.raises(ValueError, match="no repo: tag"):
            resolve_repo_for_gripe(store, 42, cfg)


# ── validate_submit: pre-submit rejection paths ───────────────────


class TestValidateSubmit:
    """``validate_submit`` is the JobHandler-side hook that turns
    deployment misconfiguration into a clear ``BadInput`` at
    ``put(kind='job', ...)`` time. Verifies the three rejection
    paths we documented."""

    @staticmethod
    def _store(tag_values: list[str] | None = None) -> object:
        class _Store:
            def tags_for(self, _ref_id: int) -> list[str]:
                return list(tag_values or [])

        return _Store()

    def test_rejects_when_env_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from precis.workers.job_types.fix_gripe import validate_submit

        monkeypatch.delenv("PRECIS_FIX_REPO_DIR", raising=False)
        monkeypatch.delenv("PRECIS_FIX_REPOS", raising=False)
        monkeypatch.delenv("PRECIS_FIX_WORK_DIR", raising=False)
        err = validate_submit(self._store(), gripe_id=42, params={})
        assert err is not None and "PRECIS_FIX_WORK_DIR" in err

    def test_rejects_unknown_repo_tag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from precis.workers.job_types.fix_gripe import validate_submit

        monkeypatch.setenv("PRECIS_FIX_WORK_DIR", "/tmp/precis-fix-work")
        monkeypatch.setenv("PRECIS_FIX_REPOS", '{"precis-mcp": "/tmp/precis-mcp"}')
        monkeypatch.delenv("PRECIS_FIX_REPO_DIR", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        err = validate_submit(self._store(["repo:nope"]), gripe_id=42, params={})
        assert err is not None and "not in PRECIS_FIX_REPOS" in err

    def test_rejects_when_api_key_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.workers.job_types.fix_gripe import validate_submit

        monkeypatch.setenv("PRECIS_FIX_WORK_DIR", "/tmp/precis-fix-work")
        monkeypatch.setenv("PRECIS_FIX_REPO_DIR", "/tmp/precis-mcp")
        monkeypatch.delenv("PRECIS_FIX_REPOS", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        err = validate_submit(self._store(), gripe_id=42, params={})
        assert err is not None and "ANTHROPIC_API_KEY" in err

    def test_accepts_valid_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from precis.workers.job_types.fix_gripe import validate_submit

        monkeypatch.setenv("PRECIS_FIX_WORK_DIR", "/tmp/precis-fix-work")
        monkeypatch.setenv("PRECIS_FIX_REPO_DIR", "/tmp/precis-mcp")
        monkeypatch.delenv("PRECIS_FIX_REPOS", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        err = validate_submit(self._store(), gripe_id=42, params={})
        assert err is None


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
