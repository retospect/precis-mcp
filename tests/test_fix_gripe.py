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

import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.utils.claude_agent import ContainerRequiredError
from precis.workers.executors import claude_inproc
from precis.workers.job_types import fix_gripe
from precis.workers.job_types.fix_gripe import (
    FixGripeConfig,
    RunOutcome,
    _compose_prompt,
    _restricted_env,
    load_config_from_env,
)
from tests._gripe import insert_gripe

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

    def test_keeps_oauth_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The subscription token is the preferred credential: the container's
        oauth mode passes ``CLAUDE_CODE_OAUTH_TOKEN`` by KEY, so the value has
        to survive the strip or the container asks for a secret the executor
        process doesn't carry."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-XXX")
        env = _restricted_env(cwd_for_test())
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-XXX"

    def test_default_keeps_both_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default (``prefer_oauth=False``) must NOT scrub the API key: a
        ``bare=True`` caller authenticates strictly off it, so dropping it
        would leave that caller with no credential at all."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-XXX")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-XXX")
        env = _restricted_env(cwd_for_test())
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-api03-XXX"
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-XXX"

    def test_prefer_oauth_scrubs_the_billed_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With a token available, an opted-in caller drops the key so the CLI
        can't pick the per-token-billed path."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-XXX")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-XXX")
        env = _restricted_env(cwd_for_test(), prefer_oauth=True)
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-XXX"
        assert "ANTHROPIC_API_KEY" not in env

    def test_prefer_oauth_keeps_the_key_when_there_is_no_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-XXX")
        monkeypatch.setattr(
            "precis.secrets.get_secret", lambda name, **kw: None, raising=True
        )
        env = _restricted_env(cwd_for_test(), prefer_oauth=True)
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-api03-XXX"

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
        assert "lands it on main" in prompt
        assert "LAST commit becomes" in prompt
        assert "Do NOT touch main" in prompt
        # §H cycle a write-back design: the agent commits only; a trusted
        # process lands it on its behalf (it has no push creds/network route).
        assert "Do NOT push" in prompt
        assert "a trusted process squashes" in prompt.replace("\n", " ")

    def test_diagnosis_comment_narrows_the_brief(self) -> None:
        """A resolvable ``diagnosis_job_id`` swaps the full comment timeline
        for the gripe body + the one DIAGNOSIS comment (Piece C part 3)."""
        prompt = _compose_prompt(
            ref_title="bug",
            blocks=[
                _FakeBlock("the bug body"),
                _FakeBlock("a human comment that should NOT appear"),
            ],
            diagnosis_comment=(
                "DIAGNOSIS (auto, job 99):\nRoot cause: the thing.\nConfidence: 0.90"
            ),
        )
        assert "BODY: the bug body" in prompt
        assert "PRIOR DIAGNOSIS" in prompt
        assert "Root cause: the thing." in prompt
        assert "a human comment that should NOT appear" not in prompt
        assert "COMMENT 1" not in prompt

    def test_no_diagnosis_comment_keeps_full_timeline(self) -> None:
        """``diagnosis_comment=None`` (the default) is byte-for-byte the old
        behaviour — full BODY + numbered COMMENT timeline."""
        prompt = _compose_prompt(
            ref_title="bug",
            blocks=[_FakeBlock("the bug body"), _FakeBlock("a comment")],
        )
        assert "BODY: the bug body" in prompt
        assert "COMMENT 1: a comment" in prompt
        assert "PRIOR DIAGNOSIS" not in prompt


# ── _resolve_diagnosis_comment / _find_diagnosis_comment ───────────


class TestResolveDiagnosisComment:
    def test_no_params_returns_none(self) -> None:
        assert fix_gripe._resolve_diagnosis_comment(_FakeStoreUnused(), 1, None) is None

    def test_no_diagnosis_job_id_key_returns_none(self) -> None:
        assert (
            fix_gripe._resolve_diagnosis_comment(_FakeStoreUnused(), 1, {"gripe_id": 1})
            is None
        )

    def test_malformed_diagnosis_job_id_returns_none(self) -> None:
        params = {"diagnosis_job_id": "not-an-int"}
        assert (
            fix_gripe._resolve_diagnosis_comment(_FakeStoreUnused(), 1, params) is None
        )

    def test_finds_the_matching_comment(self, store: Store) -> None:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT public.file_gripe_readonly(%s)", ("a bug",)
            ).fetchone()
            assert row is not None
            gripe_id = int(row[0])
            conn.commit()
        from precis.workers.executors._common import append_chunk

        with store.pool.connection() as conn:
            append_chunk(
                store,
                gripe_id,
                "gripe_comment",
                "DIAGNOSIS (auto, job 7):\nRoot cause: the actual bug.\n"
                "Confidence: 0.85",
                conn=conn,
            )
            conn.commit()

        found = fix_gripe._resolve_diagnosis_comment(
            store, gripe_id, {"diagnosis_job_id": 7}
        )
        assert found is not None
        assert "Root cause: the actual bug." in found

    def test_wrong_job_id_finds_nothing(self, store: Store) -> None:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT public.file_gripe_readonly(%s)", ("a bug",)
            ).fetchone()
            assert row is not None
            gripe_id = int(row[0])
            conn.commit()
        from precis.workers.executors._common import append_chunk

        with store.pool.connection() as conn:
            append_chunk(
                store,
                gripe_id,
                "gripe_comment",
                "DIAGNOSIS (auto, job 7):\nRoot cause: the actual bug.",
                conn=conn,
            )
            conn.commit()

        assert (
            fix_gripe._resolve_diagnosis_comment(
                store, gripe_id, {"diagnosis_job_id": 8}
            )
            is None
        )


class _FakeStoreUnused:
    """Stand-in for params-shape tests that never reach a DB call."""


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
        the LLM routing seam FRONTIER tier — the consolidated opus-4.8 cloud
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


# ── max_turns: gr451357 ────────────────────────────────────────────
#
# call_claude_agent's own default (20) is a one-shot-tool-call size, not an
# autonomous-engineer-with-tests size — fix_gripe's agent needs its own,
# larger, configurable ceiling (mirrors plan_tick's PRECIS_PLAN_TICK_MAX_TURNS).


class TestMaxTurnsConfig:
    def test_default_is_120(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRECIS_FIX_REPO_DIR", "/tmp/repo")
        monkeypatch.setenv("PRECIS_FIX_WORK_DIR", "/tmp/precis-fix-work")
        monkeypatch.delenv("PRECIS_FIX_GRIPE_MAX_TURNS", raising=False)
        cfg = load_config_from_env()
        assert cfg.max_turns == 120

    def test_env_override_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRECIS_FIX_REPO_DIR", "/tmp/repo")
        monkeypatch.setenv("PRECIS_FIX_WORK_DIR", "/tmp/precis-fix-work")
        monkeypatch.setenv("PRECIS_FIX_GRIPE_MAX_TURNS", "40")
        cfg = load_config_from_env()
        assert cfg.max_turns == 40

    def test_malformed_override_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRECIS_FIX_REPO_DIR", "/tmp/repo")
        monkeypatch.setenv("PRECIS_FIX_WORK_DIR", "/tmp/precis-fix-work")
        monkeypatch.setenv("PRECIS_FIX_GRIPE_MAX_TURNS", "not-an-int")
        cfg = load_config_from_env()
        assert cfg.max_turns == 120

    def test_reaches_call_claude_agent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The configured value (not call_claude_agent's own default) is
        the one that reaches the chokepoint."""
        from precis.utils import claude_agent as ca_mod

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("PRECIS_FIX_GRIPE_UNSANDBOXED_ACK", "1")

        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()

        captured: dict[str, Any] = {}

        def _fake_call(prompt, **kw):
            captured.update(kw)
            return object()

        monkeypatch.setattr(ca_mod, "call_claude_agent", _fake_call)

        cfg = FixGripeConfig(
            default_repo_dir=Path("/tmp/precis-mcp"),
            work_dir=Path("/tmp/precis-fix-work"),
            claude_bin="claude",
            claude_model="claude-opus-4-8",
            timeout_seconds=900,
            max_turns=77,
        )
        fix_gripe._spawn_claude(cfg, clone_dir, "the prompt")
        assert captured["max_turns"] == 77


# ── max_usd: gr452384 comment 4 ───────────────────────────────────
#
# Once max_turns lifted the turn ceiling, call_claude_agent's own $2 budget
# default became the binding one ("Exceeded USD budget (2)" on 5 of 6 parked
# fix attempts, 2026-09-27). Same shape as max_turns: configurable, sized to
# the turn ceiling it accompanies, and the configured value must reach the
# chokepoint.


class TestMaxUsdConfig:
    def test_default_is_ten_dollars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRECIS_FIX_REPO_DIR", "/tmp/repo")
        monkeypatch.setenv("PRECIS_FIX_WORK_DIR", "/tmp/precis-fix-work")
        monkeypatch.delenv("PRECIS_FIX_GRIPE_MAX_USD", raising=False)
        cfg = load_config_from_env()
        assert cfg.max_usd == 10.0

    def test_env_override_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRECIS_FIX_REPO_DIR", "/tmp/repo")
        monkeypatch.setenv("PRECIS_FIX_WORK_DIR", "/tmp/precis-fix-work")
        monkeypatch.setenv("PRECIS_FIX_GRIPE_MAX_USD", "2.5")
        cfg = load_config_from_env()
        assert cfg.max_usd == 2.5

    def test_malformed_override_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRECIS_FIX_REPO_DIR", "/tmp/repo")
        monkeypatch.setenv("PRECIS_FIX_WORK_DIR", "/tmp/precis-fix-work")
        monkeypatch.setenv("PRECIS_FIX_GRIPE_MAX_USD", "two dollars")
        cfg = load_config_from_env()
        assert cfg.max_usd == 10.0

    def test_reaches_call_claude_agent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from precis.utils import claude_agent as ca_mod

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("PRECIS_FIX_GRIPE_UNSANDBOXED_ACK", "1")

        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()

        captured: dict[str, Any] = {}

        def _fake_call(prompt, **kw):
            captured.update(kw)
            return object()

        monkeypatch.setattr(ca_mod, "call_claude_agent", _fake_call)

        cfg = FixGripeConfig(
            default_repo_dir=Path("/tmp/precis-mcp"),
            work_dir=Path("/tmp/precis-fix-work"),
            claude_bin="claude",
            claude_model="claude-opus-4-8",
            timeout_seconds=900,
            max_usd=6.5,
        )
        fix_gripe._spawn_claude(cfg, clone_dir, "the prompt")
        assert captured["max_usd"] == 6.5


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


# ── GLM/OpenRouter fleet-flip safety gate (Part 3) ─────────────────
#
# fix_gripe.run()'s agent runs `claude -p` (via the call_claude_agent
# chokepoint) whose --model comes from resolve_model(Tier.FRONTIER) —
# under backend=openai that's an OSS slug the claude CLI can't run. The
# gate must skip cleanly *before* any subprocess is spawned (indeed,
# before the gripe/repo are even resolved) rather than let claude -p 400.


class TestBackendFlipGate:
    @staticmethod
    def _cfg() -> FixGripeConfig:
        return FixGripeConfig(
            default_repo_dir=Path("/tmp/precis-mcp"),
            work_dir=Path("/tmp/precis-fix-work"),
            claude_bin="claude",
            claude_model="z-ai/glm-5.2",
            timeout_seconds=1800,
        )

    def test_skips_under_openai_backend_without_touching_store_or_subprocess(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.utils.llm.router import Backend

        monkeypatch.setattr(fix_gripe, "resolve_backend", lambda: Backend.OPENAI)

        class _BoomStore:
            """Any DB access past the gate is a test failure."""

            def get_ref(self, **_kw: object) -> object:
                raise AssertionError("gate did not skip before store.get_ref")

        spawn_calls: list[object] = []
        monkeypatch.setattr(
            fix_gripe, "_spawn_claude", lambda *a, **kw: spawn_calls.append((a, kw))
        )

        outcome = fix_gripe.run(
            store=_BoomStore(), job_id=1, gripe_id=42, config=self._cfg()
        )

        assert isinstance(outcome, RunOutcome)
        assert outcome.status == "skipped"
        assert "openai" in outcome.summary_text
        assert "openai" in outcome.gripe_comment_text
        assert outcome.branch is None
        assert outcome.sha is None
        assert spawn_calls == []

    def test_proceeds_past_gate_under_default_anthropic_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.utils.llm.router import Backend

        monkeypatch.setattr(fix_gripe, "resolve_backend", lambda: Backend.ANTHROPIC)
        # Ack the unsandboxed-agent gate (gr179498) so we reach store.get_ref.
        monkeypatch.setenv("PRECIS_FIX_GRIPE_UNSANDBOXED_ACK", "1")

        class _FakeStore:
            def get_ref(self, **_kw: object) -> None:
                # Reached — proves the gate did NOT skip. Returning None
                # makes run() raise its own not-found RuntimeError, so we
                # don't need to stand up a full clone/subprocess harness
                # just to prove execution reached the spawn side of the
                # gate.
                return None

        with pytest.raises(RuntimeError, match="gripe id=42 not found"):
            fix_gripe.run(store=_FakeStore(), job_id=1, gripe_id=42, config=self._cfg())


class TestUnsandboxedAckGate:
    """gr179498, §H cycle a: fix_gripe is fail-closed — it won't fall back to
    running its agent full-privilege and unsandboxed unless an operator
    explicitly acks the risk, so enabling backlog_groom alone can't unleash
    it. A containerized run needs no ack (new rule, §H cycle a)."""

    @staticmethod
    def _cfg() -> FixGripeConfig:
        return FixGripeConfig(
            default_repo_dir=Path("/tmp/precis-mcp"),
            work_dir=Path("/tmp/precis-fix-work"),
            claude_bin="claude",
            claude_model="claude-opus-4-8",
            timeout_seconds=1800,
        )

    def test_skips_when_no_container_and_no_ack_without_touching_store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.utils.llm.router import Backend

        monkeypatch.setattr(fix_gripe, "resolve_backend", lambda: Backend.ANTHROPIC)
        monkeypatch.delenv("PRECIS_FIX_GRIPE_UNSANDBOXED_ACK", raising=False)
        monkeypatch.delenv("PRECIS_AGENT_CONTAINER", raising=False)

        class _BoomStore:
            def get_ref(self, **_kw: object) -> object:
                raise AssertionError("fail-closed gate did not skip before store")

        spawn_calls: list[object] = []
        monkeypatch.setattr(
            fix_gripe, "_spawn_claude", lambda *a, **kw: spawn_calls.append((a, kw))
        )

        outcome = fix_gripe.run(
            store=_BoomStore(), job_id=1, gripe_id=42, config=self._cfg()
        )
        assert outcome.status == "skipped"
        assert "gr179498" in outcome.summary_text
        assert spawn_calls == []

    def test_proceeds_when_container_available_even_without_ack(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """New rule (§H cycle a): a containerized run needs no operator ack —
        the pre-check must NOT skip just because the ack env var is unset, as
        long as the host can actually run the container."""
        from precis.utils.llm.router import Backend
        from precis.workers.executors import agent_container as ac

        monkeypatch.setattr(fix_gripe, "resolve_backend", lambda: Backend.ANTHROPIC)
        monkeypatch.delenv("PRECIS_FIX_GRIPE_UNSANDBOXED_ACK", raising=False)
        monkeypatch.setattr(ac, "container_agent_enabled", lambda: True)
        monkeypatch.setattr(ac, "container_capability_ok", lambda *a, **k: True)

        class _FakeStore:
            def get_ref(self, **_kw: object) -> None:
                return None  # reached ⇒ proves the gate did NOT skip

        with pytest.raises(RuntimeError, match="gripe id=42 not found"):
            fix_gripe.run(store=_FakeStore(), job_id=1, gripe_id=42, config=self._cfg())

    def test_spawn_claude_raises_without_container_or_ack(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Defense in depth: even a direct _spawn_claude call is refused when
        # neither a container is available nor the unsandboxed-run ack is
        # set — enforced now by call_claude_agent's require_container
        # (ContainerRequiredError), not a local check in _spawn_claude.
        monkeypatch.delenv("PRECIS_FIX_GRIPE_UNSANDBOXED_ACK", raising=False)
        monkeypatch.delenv("PRECIS_AGENT_CONTAINER", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        with pytest.raises(ContainerRequiredError, match="refusing"):
            fix_gripe._spawn_claude(self._cfg(), Path("/tmp/x"), "prompt")

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="agent containers bind the host clone at its own path inside a "
        "Linux image — a POSIX-host-only arrangement",
    )
    def test_spawn_claude_containerizes_without_ack(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A capable host runs _spawn_claude containerized with no ack set —
        proves ``require_container=False`` reaches call_claude_agent (which
        then selects the container path)."""
        from types import SimpleNamespace

        import precis.utils.claude_agent as ca
        from precis.workers.executors import agent_container as ac

        monkeypatch.delenv("PRECIS_FIX_GRIPE_UNSANDBOXED_ACK", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("PRECIS_AGENT_CONTAINER", "1")
        monkeypatch.setenv("PRECIS_CONTAINER_BIN", "podman")
        monkeypatch.setattr(ac, "container_capability_ok", lambda *a, **k: True)

        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()

        captured: dict[str, object] = {}

        def _fake(argv, **k):
            captured["argv"] = argv
            return SimpleNamespace(stdout="done", stderr="")

        monkeypatch.setattr(ca, "run_claude", _fake)
        fix_gripe._spawn_claude(self._cfg(), clone_dir, "the prompt")
        argv = captured["argv"]
        assert isinstance(argv, list)
        assert argv[0] == "podman" and "run" in argv
        assert f"{clone_dir}:{clone_dir}" in argv
        # repo_dir (origin) is NEVER mounted — the agent commits only; the
        # trusted (host) side pushes after this call returns (§H cycle a).
        assert argv.count("-v") == 1
        assert argv[argv.index("-w") + 1] == str(clone_dir)
        assert "ANTHROPIC_API_KEY" in argv  # bare ⇒ api-key channel, not oauth
        # egress api-only ⇒ bridge networking (the LLM call needs the net),
        # not `--network none` — the local git remote needs no network.
        assert "--network" in argv and "bridge" in argv


class TestSpawnClaudeCallShape:
    """The exact ``call_claude_agent`` call shape §H cycle a specifies:
    ``bare=True``, ``env_base``, mounts, workdir, explicit envelope,
    ``require_container`` — stubbed at the chokepoint so no real subprocess
    or container runs. Mounts are clone-only — the source repo (origin) is
    never mounted; the agent commits only, and ``run()`` performs the
    trusted-side push after ``call_claude_agent`` returns."""

    @staticmethod
    def _cfg() -> FixGripeConfig:
        return FixGripeConfig(
            default_repo_dir=Path("/tmp/precis-mcp"),
            work_dir=Path("/tmp/precis-fix-work"),
            claude_bin="claude",
            claude_model="claude-opus-4-8",
            timeout_seconds=900,
        )

    def test_call_shape(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from precis.utils import claude_agent as ca_mod
        from precis.workers.envelope import Envelope

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.delenv("PRECIS_FIX_GRIPE_UNSANDBOXED_ACK", raising=False)

        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()

        captured: dict[str, Any] = {}

        def _fake_call(prompt, **kw):
            captured["prompt"] = prompt
            captured.update(kw)
            return object()

        # _spawn_claude does a LOCAL import of call_claude_agent at call time,
        # so patching the source module's attribute is what it picks up.
        monkeypatch.setattr(ca_mod, "call_claude_agent", _fake_call)

        fix_gripe._spawn_claude(self._cfg(), clone_dir, "the prompt")

        assert captured["prompt"] == "the prompt"
        assert captured["bare"] is True
        assert captured["cwd"] == clone_dir
        assert captured["workdir"] == str(clone_dir)
        assert captured["require_container"] is True  # no ack set
        assert captured["model"] == "claude-opus-4-8"
        assert captured["timeout_s"] == 900.0
        assert captured["max_turns"] == 120  # gr451357 default (self._cfg())

        env_base = captured["env_base"]
        assert "ANTHROPIC_API_KEY" in env_base
        assert "PRECIS_DATABASE_URL" not in env_base
        assert not any(k.startswith("PG") for k in env_base)

        envelope = captured["envelope"]
        assert isinstance(envelope, Envelope)
        assert envelope.egress == "api-only"

        mounts = captured["mounts"]
        assert len(mounts) == 1  # clone ONLY — no repo_dir mount
        m = mounts[0]
        assert m.host_path == str(clone_dir)
        assert m.container_path == m.host_path  # identical path both sides
        assert m.mode == "rw"

    def test_require_container_false_when_acked(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from precis.utils import claude_agent as ca_mod

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("PRECIS_FIX_GRIPE_UNSANDBOXED_ACK", "1")

        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()

        captured: dict[str, object] = {}

        def _fake_call(prompt, **kw):
            captured.update(kw)
            return object()

        monkeypatch.setattr(ca_mod, "call_claude_agent", _fake_call)
        fix_gripe._spawn_claude(self._cfg(), clone_dir, "p")
        assert captured["require_container"] is False


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


# ── run(): the exception-based _spawn_claude contract (§H cycle a) ─
#
# call_claude_agent RAISES on a real failure and on a fail-closed
# ContainerRequiredError refusal, rather than the old bare subprocess.run's
# always-returns-with-a-returncode contract. run() must map each onto the
# right RunOutcome. Real git plumbing (a throwaway local repo) — only
# _spawn_claude is stubbed.


class TestRunExceptionMapping:
    @staticmethod
    def _make_repo(tmp_path: Path) -> Path:
        import subprocess

        repo = tmp_path / "repo"
        repo.mkdir()

        def _run(*args: str) -> None:
            subprocess.run(
                args, cwd=str(repo), check=True, capture_output=True, text=True
            )

        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(repo)],
            check=True,
            capture_output=True,
        )
        _run("git", "config", "user.email", "t@t")
        _run("git", "config", "user.name", "t")
        (repo / "f.txt").write_text("x", encoding="utf-8")
        _run("git", "add", ".")
        _run("git", "commit", "-q", "-m", "init")
        return repo

    @staticmethod
    def _cfg(repo: Path, tmp_path: Path) -> FixGripeConfig:
        return FixGripeConfig(
            default_repo_dir=repo,
            work_dir=tmp_path / "work",
            claude_bin="claude",
            claude_model="claude-opus-4-8",
            timeout_seconds=60,
        )

    @staticmethod
    def _store() -> object:
        @dataclass(frozen=True)
        class _Ref:
            title: str = "bug"
            id: int = 42

        class _Store:
            chunks = property(
                lambda self: self
            )  # chunks carve: flat fake doubles as its own sub-store

            def get_ref(self, **_kw: object) -> _Ref:
                return _Ref()

            def tags_for(self, _ref_id: int) -> list[str]:
                return []

            def list_chunks_for_ref(self, _ref_id: int) -> list[object]:
                return [_FakeBlock("body text")]

        return _Store()

    def _run_with_spawn(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        spawn_effect,
    ) -> RunOutcome:
        from precis.utils.llm.router import Backend

        repo = self._make_repo(tmp_path)
        monkeypatch.setattr(fix_gripe, "resolve_backend", lambda: Backend.ANTHROPIC)
        monkeypatch.setenv("PRECIS_FIX_GRIPE_UNSANDBOXED_ACK", "1")
        monkeypatch.setattr(fix_gripe, "_spawn_claude", spawn_effect)
        return fix_gripe.run(
            store=self._store(), job_id=1, gripe_id=42, config=self._cfg(repo, tmp_path)
        )

    def test_claude_agent_error_maps_to_failed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from precis.utils.claude_agent import ClaudeAgentError

        def _boom(*_a: object, **_k: object) -> object:
            raise ClaudeAgentError("exited 1", stdout="", stderr="oops", returncode=1)

        outcome = self._run_with_spawn(monkeypatch, tmp_path, _boom)
        assert outcome.status == "failed"
        assert "oops" in outcome.summary_text

    def test_container_required_error_maps_to_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def _boom(*_a: object, **_k: object) -> object:
            raise ContainerRequiredError("container unavailable mid-run")

        outcome = self._run_with_spawn(monkeypatch, tmp_path, _boom)
        assert outcome.status == "skipped"
        assert "gr179498" in outcome.summary_text

    def test_value_error_maps_to_failed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A mount/workdir validation ValueError from ``containerize_claude_argv``
        (via ``call_claude_agent``) must not escape ``run()`` as a raw
        exception — the executor runner expects a RunOutcome (finding 3)."""

        def _boom(*_a: object, **_k: object) -> object:
            raise ValueError("agent_container: mount host_path does not exist")

        outcome = self._run_with_spawn(monkeypatch, tmp_path, _boom)
        assert outcome.status == "failed"
        assert "mount" in outcome.summary_text.lower()

    def test_clean_agent_result_but_no_push_maps_to_failed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """call_claude_agent returns cleanly (no exception — e.g. a resumable
        exhaustion) but the agent never pushed a branch: still a failure,
        judged by git state, not by an exit code."""

        def _noop(*_a: object, **_k: object) -> object:
            return object()

        outcome = self._run_with_spawn(monkeypatch, tmp_path, _noop)
        assert outcome.status == "failed"
        assert "made no commits" in outcome.summary_text

    def test_no_commit_with_already_fixed_line_maps_to_already_fixed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """gr454480: no commit because the defect is gone is not a failure."""

        def _verified(*_a: object, **_k: object) -> object:
            return _FakeAgentResult(
                "Checked nursery.py.\n\nALREADY FIXED: 52a6ed3ed _ask_refs\n"
            )

        outcome = self._run_with_spawn(monkeypatch, tmp_path, _verified)
        assert outcome.status == "already_fixed"
        assert "52a6ed3ed _ask_refs" in outcome.gripe_comment_text
        assert outcome.sha is None

    def test_already_fixed_line_after_exhaustion_is_still_failed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A run cut off by max_turns never verified anything, whatever it
        said along the way."""

        def _cut_off(*_a: object, **_k: object) -> object:
            return _FakeAgentResult(
                "ALREADY FIXED: probably abc1234", terminal_reason="max_turns"
            )

        outcome = self._run_with_spawn(monkeypatch, tmp_path, _cut_off)
        assert outcome.status == "failed"


@dataclass(frozen=True)
class _FakeAgentResult:
    final_text: str
    terminal_reason: str | None = None


@pytest.mark.parametrize(
    ("text", "terminal_reason", "expected"),
    [
        ("done.\nALREADY FIXED: abc1234 in foo.py", None, "abc1234 in foo.py"),
        ("ALREADY FIXED: abc1234\n\n  \n", None, "abc1234"),
        # Not the last line: the agent went on to say something else.
        ("ALREADY FIXED: abc1234\nactually no, still broken", None, None),
        ("ALREADY FIXED:   ", None, None),
        ("could not fix it", None, None),
        ("ALREADY FIXED: abc1234", "max_turns", None),
    ],
)
def test_already_fixed_evidence(
    text: str, terminal_reason: str | None, expected: str | None
) -> None:
    result = _FakeAgentResult(text, terminal_reason)
    assert fix_gripe._already_fixed_evidence(result) == expected


def test_already_fixed_evidence_tolerates_a_bare_result() -> None:
    assert fix_gripe._already_fixed_evidence(object()) is None
    assert fix_gripe._already_fixed_evidence(None) is None


def test_already_fixed_evidence_is_capped() -> None:
    got = fix_gripe._already_fixed_evidence(
        _FakeAgentResult("ALREADY FIXED: " + "x" * 2000)
    )
    assert got is not None and len(got) == fix_gripe._EVIDENCE_MAX


def test_prompt_names_the_already_fixed_marker() -> None:
    prompt = fix_gripe._compose_prompt(ref_title="t", blocks=[_FakeBlock("body")])
    assert fix_gripe.ALREADY_FIXED_MARKER in prompt


# ── run(): a resolved gripe skips clean (gr451170 fix 1) ───────────
#
# A stale/duplicate/re-minted job whose gripe was already resolved — by a
# human, or by an earlier fix attempt — while this one sat queued must not
# attempt a fix on it at all. The check runs right after the gripe is
# resolved, before any clone/agent effort.


class TestTerminalGripeSkip:
    @staticmethod
    def _cfg() -> FixGripeConfig:
        return FixGripeConfig(
            default_repo_dir=Path("/tmp/precis-mcp"),
            work_dir=Path("/tmp/precis-fix-work"),
            claude_bin="claude",
            claude_model="claude-opus-4-8",
            timeout_seconds=1800,
        )

    @staticmethod
    def _terminal_store(status: str) -> object:
        class _Store:
            def get_ref(self, **_kw: object) -> _FakeBlock:
                return _FakeBlock("bug")  # only .text is unused here

            def tags_for(self, _ref_id: int) -> list[str]:
                return [f"STATUS:{status}"]

            def list_chunks_for_ref(self, _ref_id: int) -> list[object]:
                raise AssertionError(
                    "run() must skip before reading the gripe's body chunks"
                )

        return _Store()

    @pytest.mark.parametrize("status", ["done", "wontfix"])
    def test_skips_before_any_repo_or_agent_work(
        self, monkeypatch: pytest.MonkeyPatch, status: str
    ) -> None:
        monkeypatch.setenv("PRECIS_FIX_GRIPE_UNSANDBOXED_ACK", "1")
        spawn_calls: list[object] = []
        monkeypatch.setattr(
            fix_gripe, "_spawn_claude", lambda *a, **kw: spawn_calls.append((a, kw))
        )

        outcome = fix_gripe.run(
            store=self._terminal_store(status),
            job_id=1,
            gripe_id=42,
            config=self._cfg(),
        )

        assert isinstance(outcome, RunOutcome)
        assert outcome.status == "skipped"
        assert status in outcome.summary_text
        assert status in outcome.gripe_comment_text
        assert outcome.branch is None
        assert outcome.sha is None
        assert spawn_calls == []

    def test_live_status_proceeds_past_the_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A live status (``open`` here) must NOT hit the skip path — proven
        by reaching (and erroring inside) the body-chunk read that follows
        it, same probe idiom as the skip test above uses to prove the
        opposite."""

        class _Store:
            chunks = property(lambda self: self)

            def get_ref(self, **_kw: object) -> _FakeBlock:
                return _FakeBlock("bug")

            def tags_for(self, _ref_id: int) -> list[str]:
                return ["STATUS:open"]

            def list_chunks_for_ref(self, _ref_id: int) -> list[object]:
                raise AssertionError("reached-past-terminal-check")

        monkeypatch.setenv("PRECIS_FIX_GRIPE_UNSANDBOXED_ACK", "1")
        with pytest.raises(AssertionError, match="reached-past-terminal-check"):
            fix_gripe.run(store=_Store(), job_id=1, gripe_id=42, config=self._cfg())


# ── trusted-side push: §H cycle a write-back design ────────────────
#
# The agent never has origin mounted or reachable (no repo_dir mount, §H
# cycle a) — it can only commit inside the clone. run() (trusted, host-side)
# performs the actual `git push` after call_claude_agent returns, guarded to
# gripe_<id> branch names.


class TestImportBranchGuard:
    """The import writes into the host checkout's branch namespace, so only
    the exact ``gripe_<digits>`` shape ``run()`` constructs may be named."""

    def test_refuses_main(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="gripe_"):
            fix_gripe._import_branch(tmp_path, tmp_path / "clone", "main")

    def test_refuses_gripe_branch_with_unexpected_shape(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="gripe_"):
            fix_gripe._import_branch(tmp_path, tmp_path / "clone", "gripe_42_evil")

    def test_no_subprocess_when_branch_name_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        called: list[object] = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append((a, k)))
        with pytest.raises(RuntimeError):
            fix_gripe._import_branch(tmp_path, tmp_path / "c", "not-a-gripe-branch")
        assert called == []

    def test_prepush_hook_refuses_every_push(self, tmp_path: Path) -> None:
        """Nothing legitimately pushes from the clone any more — the trusted
        side lands from the host checkout — so the tripwire refuses all."""
        fix_gripe._install_prepush_hook(tmp_path)
        text = (tmp_path / ".git" / "hooks" / "pre-push").read_text(encoding="utf-8")
        assert "exit 1" in text
        assert "gripe_*" not in text


class TestRunPerformsTrustedSidePush:
    """End-to-end (real git, no claude): run() itself pushes the agent's
    commit to origin, and refuses to do so for anything but the constructed
    gripe_<id> branch name."""

    @staticmethod
    def _make_repo(tmp_path: Path) -> Path:
        return TestRunExceptionMapping._make_repo(tmp_path)

    @staticmethod
    def _cfg(repo: Path, tmp_path: Path) -> FixGripeConfig:
        return TestRunExceptionMapping._cfg(repo, tmp_path)

    @staticmethod
    def _store() -> object:
        return TestRunExceptionMapping._store()

    def test_run_pushes_the_agents_commit_via_trusted_side(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import subprocess

        from precis.utils.llm.router import Backend

        repo = self._make_repo(tmp_path)
        monkeypatch.setattr(fix_gripe, "resolve_backend", lambda: Backend.ANTHROPIC)
        monkeypatch.setenv("PRECIS_FIX_GRIPE_UNSANDBOXED_ACK", "1")

        # run() computes this same path internally: work_dir/clones/gripe_<id>.
        clone_dir = tmp_path / "work" / "clones" / "gripe_42"

        def _commit_in_clone(*_a: object, **_k: object) -> object:
            # Simulate the agent's ONLY allowed action: committing locally
            # inside the (already-cloned) sandbox working tree. No push —
            # the agent has no origin mount and no push creds.
            (clone_dir / "fix.txt").write_text("fixed", encoding="utf-8")
            env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "agent",
                "GIT_AUTHOR_EMAIL": "agent@precis",
                "GIT_COMMITTER_NAME": "agent",
                "GIT_COMMITTER_EMAIL": "agent@precis",
            }
            subprocess.run(
                ["git", "add", "."], cwd=str(clone_dir), check=True, capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-q", "-m", "fix"],
                cwd=str(clone_dir),
                check=True,
                capture_output=True,
                env=env,
            )
            return object()

        monkeypatch.setattr(fix_gripe, "_spawn_claude", _commit_in_clone)
        outcome = fix_gripe.run(
            store=self._store(), job_id=1, gripe_id=42, config=self._cfg(repo, tmp_path)
        )

        assert outcome.status == "succeeded"
        assert outcome.branch == "gripe_42"
        assert outcome.sha is not None

        # origin (the ORIGINAL repo, not the clone) now carries the branch —
        # proof the TRUSTED side (run(), not the agent) performed the push.
        check = subprocess.run(
            ["git", "rev-parse", "--verify", "gripe_42"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )
        assert check.returncode == 0
        assert check.stdout.strip() == outcome.sha


class TestDeliveryIsDecidedByTheRemote:
    """The lane may only call a fix delivered once the REMOTE says so.

    gr458326: ``_git_clone_and_branch`` clones the host checkout from a local
    path, so the clone's ``origin`` is that checkout — never its upstream. A
    push "to origin" therefore succeeded into a directory on the worker node,
    git updated the clone's own ``origin/<branch>`` tracking ref, and the
    verification read that ref back and agreed. 43 jobs reported
    "pushed to origin as <sha>", were marked succeeded, and parked their gripes
    at ``in_review`` — which reads as "a fix exists, don't duplicate it". The
    lane failed in the direction that suppresses the real fix.

    These tests pin the contract that makes that unrepresentable: the success
    condition is the branch being present on the publish target, asked of the
    target itself.
    """

    @staticmethod
    def _make_repo(tmp_path: Path) -> Path:
        return TestRunExceptionMapping._make_repo(tmp_path)

    @staticmethod
    def _cfg(repo: Path, tmp_path: Path) -> FixGripeConfig:
        return TestRunExceptionMapping._cfg(repo, tmp_path)

    @staticmethod
    def _store() -> object:
        return TestRunExceptionMapping._store()

    @staticmethod
    def _run_git(cwd: Path, *args: str) -> None:
        import subprocess

        subprocess.run(
            ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
        )

    def _commit_in_clone(self, clone_dir: Path) -> Any:
        """The agent's only allowed action: a local commit inside the clone."""
        import subprocess

        def _spawn(*_a: object, **_k: object) -> object:
            (clone_dir / "fix.txt").write_text("fixed", encoding="utf-8")
            env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "agent",
                "GIT_AUTHOR_EMAIL": "agent@precis",
                "GIT_COMMITTER_NAME": "agent",
                "GIT_COMMITTER_EMAIL": "agent@precis",
            }
            subprocess.run(
                ["git", "add", "."], cwd=str(clone_dir), check=True, capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-q", "-m", "fix"],
                cwd=str(clone_dir),
                check=True,
                capture_output=True,
                env=env,
            )
            return object()

        return _spawn

    def test_publish_target_is_the_checkouts_upstream_not_the_checkout(
        self, tmp_path: Path
    ) -> None:
        """The exact confusion behind the bug, isolated: the thing the clone
        calls ``origin`` and the thing the fix has to reach are two different
        repositories whenever the host checkout has an upstream at all."""
        repo = self._make_repo(tmp_path)
        upstream = tmp_path / "upstream.git"
        self._run_git(tmp_path, "init", "-q", "--bare", str(upstream))
        self._run_git(repo, "remote", "add", "origin", str(upstream))

        assert fix_gripe._publish_target(repo) == str(upstream)
        assert fix_gripe._publish_target(repo) != str(repo)

    def test_publish_target_falls_back_to_the_checkout_when_it_has_no_upstream(
        self, tmp_path: Path
    ) -> None:
        """A single-machine setup, where the host checkout genuinely is the end
        of the line — there is nowhere further to deliver to, so the checkout
        is the target and a push to it is a real delivery."""
        repo = self._make_repo(tmp_path)
        assert fix_gripe._publish_target(repo) == str(repo)

    def _with_upstream(self, tmp_path: Path, repo: Path) -> Path:
        """A bare upstream holding the checkout's main, wired as its origin."""
        upstream = tmp_path / "upstream.git"
        self._run_git(tmp_path, "init", "-q", "--bare", "-b", "main", str(upstream))
        self._run_git(repo, "remote", "add", "origin", str(upstream))
        self._run_git(repo, "push", "-q", "origin", "main:refs/heads/main")
        return upstream

    def _sibling_lands(self, tmp_path: Path, upstream: Path, name: str) -> str:
        """Someone else lands a commit on the upstream's main; returns its sha."""
        import subprocess

        other = tmp_path / f"sibling-{name}"
        if not other.exists():
            self._run_git(tmp_path, "clone", "-q", str(upstream), str(other))
            self._run_git(other, "config", "user.email", "s@s")
            self._run_git(other, "config", "user.name", "s")
        self._run_git(other, "pull", "-q", "origin", "main")
        (other / f"{name}.txt").write_text(name, encoding="utf-8")
        self._run_git(other, "add", ".")
        self._run_git(other, "commit", "-q", "-m", f"sibling {name}")
        self._run_git(other, "push", "-q", "origin", "HEAD:refs/heads/main")
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(other),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def _arm(self, monkeypatch: pytest.MonkeyPatch, spawn: Any) -> None:
        from precis.utils.llm.router import Backend

        monkeypatch.setattr(fix_gripe, "resolve_backend", lambda: Backend.ANTHROPIC)
        monkeypatch.setenv("PRECIS_FIX_GRIPE_UNSANDBOXED_ACK", "1")
        monkeypatch.setattr(fix_gripe, "_spawn_claude", spawn)

    @staticmethod
    def _upstream_main(upstream: Path) -> str:
        return fix_gripe._git_rev_parse(upstream, "refs/heads/main") or ""

    @staticmethod
    def _show(upstream: Path, spec: str) -> str:
        import subprocess

        return subprocess.run(
            ["git", "show", "-s", f"--format={spec}", "main"],
            cwd=str(upstream),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def test_a_push_that_never_reaches_the_upstream_is_a_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The gr458326 shape exactly: the push exits 0 and delivers nothing.
        The job must fail and report no sha, so the gripe is not parked at
        in_review behind a fix nobody can fetch."""
        import subprocess

        repo = self._make_repo(tmp_path)
        upstream = self._with_upstream(tmp_path, repo)
        before = self._upstream_main(upstream)
        clone_dir = tmp_path / "work" / "clones" / "gripe_42"
        self._arm(monkeypatch, self._commit_in_clone(clone_dir))

        real_git = fix_gripe._git

        def _git(cwd: Path, *args: str, **kw: Any) -> Any:
            if args and args[0] == "push":
                return subprocess.CompletedProcess(args, 0, "", "")
            return real_git(cwd, *args, **kw)

        monkeypatch.setattr(fix_gripe, "_git", _git)

        outcome = fix_gripe.run(
            store=self._store(), job_id=1, gripe_id=42, config=self._cfg(repo, tmp_path)
        )

        assert outcome.status == "failed", outcome.summary_text
        assert outcome.sha is None, (
            "reporting a sha for a commit that is not on the remote is the "
            "false claim this exists to prevent"
        )
        assert str(upstream) in outcome.summary_text
        assert self._upstream_main(upstream) == before

    def test_the_commit_survives_a_land_that_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A land that passes the dry run and is then refused for a reason
        other than a lost race (auth, branch protection) must not retry
        blindly and must not lose the work: the fix stays as branch gripe_42
        in the host checkout, and the failure text says so."""
        import subprocess

        repo = self._make_repo(tmp_path)
        upstream = self._with_upstream(tmp_path, repo)
        clone_dir = tmp_path / "work" / "clones" / "gripe_42"
        self._arm(monkeypatch, self._commit_in_clone(clone_dir))

        pushes: list[tuple[str, ...]] = []
        real_git = fix_gripe._git

        def _git(cwd: Path, *args: str, **kw: Any) -> Any:
            if args and args[0] == "push":
                pushes.append(args)
                return subprocess.CompletedProcess(args, 1, "", "denied by rule")
            return real_git(cwd, *args, **kw)

        monkeypatch.setattr(fix_gripe, "_git", _git)

        outcome = fix_gripe.run(
            store=self._store(), job_id=1, gripe_id=42, config=self._cfg(repo, tmp_path)
        )

        assert outcome.status == "failed"
        assert "denied by rule" in outcome.summary_text
        assert len(pushes) == 1, "main did not move, so a retry cannot help"
        assert str(repo) in (outcome.gripe_comment_text or "")
        assert fix_gripe._git_rev_parse(repo, "gripe_42") is not None

    def test_a_real_land_puts_one_squash_commit_on_main(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        repo = self._make_repo(tmp_path)
        upstream = self._with_upstream(tmp_path, repo)
        before = self._upstream_main(upstream)
        clone_dir = tmp_path / "work" / "clones" / "gripe_42"
        self._arm(monkeypatch, self._commit_in_clone(clone_dir))

        outcome = fix_gripe.run(
            store=self._store(), job_id=1, gripe_id=42, config=self._cfg(repo, tmp_path)
        )

        assert outcome.status == "succeeded", outcome.summary_text
        assert outcome.sha is not None
        assert fix_gripe._ls_remote_sha(str(upstream), "main", repo) == outcome.sha
        assert self._show(upstream, "%P") == before, "one commit, parent = main"
        assert self._show(upstream, "%s") == "fix (gr42)"
        assert self._show(upstream, "%an") == "agent"
        assert str(upstream) in outcome.summary_text
        assert "ungated" in outcome.gripe_comment_text.lower()
        # The stash branch and the scratch clone are both reclaimed.
        assert fix_gripe._git_rev_parse(repo, "refs/heads/gripe_42") is None
        assert not clone_dir.exists()

    def test_the_land_is_onto_current_main_not_the_cloned_main(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Main moves while the agent works (the normal case on a busy trunk):
        the fix is re-applied on top of the sibling's commit, which survives."""
        repo = self._make_repo(tmp_path)
        upstream = self._with_upstream(tmp_path, repo)
        clone_dir = tmp_path / "work" / "clones" / "gripe_42"
        commit = self._commit_in_clone(clone_dir)
        sibling: list[str] = []

        def _spawn(*a: object, **k: object) -> object:
            sibling.append(self._sibling_lands(tmp_path, upstream, "during"))
            return commit(*a, **k)

        self._arm(monkeypatch, _spawn)

        outcome = fix_gripe.run(
            store=self._store(), job_id=1, gripe_id=42, config=self._cfg(repo, tmp_path)
        )

        assert outcome.status == "succeeded", outcome.summary_text
        assert self._upstream_main(upstream) == outcome.sha
        assert self._show(upstream, "%P") == sibling[0]

    def test_a_land_that_loses_the_race_resyncs_and_never_clobbers(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A sibling lands between the fetch and the push. The non-force push
        is refused (not a fast-forward); the land re-fetches and retries on
        top of the sibling — the same CAS loop scripts/ship runs."""
        repo = self._make_repo(tmp_path)
        upstream = self._with_upstream(tmp_path, repo)
        clone_dir = tmp_path / "work" / "clones" / "gripe_42"
        self._arm(monkeypatch, self._commit_in_clone(clone_dir))

        sibling: list[str] = []
        real_git = fix_gripe._git

        def _git(cwd: Path, *args: str, **kw: Any) -> Any:
            if args and args[0] == "push" and not sibling:
                sibling.append(self._sibling_lands(tmp_path, upstream, "race"))
            return real_git(cwd, *args, **kw)

        monkeypatch.setattr(fix_gripe, "_git", _git)

        outcome = fix_gripe.run(
            store=self._store(), job_id=1, gripe_id=42, config=self._cfg(repo, tmp_path)
        )

        assert outcome.status == "succeeded", outcome.summary_text
        assert self._upstream_main(upstream) == outcome.sha
        assert self._show(upstream, "%P") == sibling[0], "the sibling survives"

    def test_a_conflict_with_current_main_lands_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import subprocess

        repo = self._make_repo(tmp_path)
        upstream = self._with_upstream(tmp_path, repo)
        clone_dir = tmp_path / "work" / "clones" / "gripe_42"

        def _spawn(*_a: object, **_k: object) -> object:
            # A sibling rewrites f.txt on main; the agent rewrites it too.
            other = tmp_path / "sibling-conflict"
            self._run_git(tmp_path, "clone", "-q", str(upstream), str(other))
            (other / "f.txt").write_text("theirs", encoding="utf-8")
            self._run_git(
                other,
                "-c",
                "user.email=s@s",
                "-c",
                "user.name=s",
                "commit",
                "-qam",
                "theirs",
            )
            self._run_git(other, "push", "-q", "origin", "HEAD:refs/heads/main")
            (clone_dir / "f.txt").write_text("ours", encoding="utf-8")
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=a@a",
                    "-c",
                    "user.name=a",
                    "commit",
                    "-qam",
                    "ours",
                ],
                cwd=str(clone_dir),
                check=True,
                capture_output=True,
            )
            return object()

        self._arm(monkeypatch, _spawn)
        outcome = fix_gripe.run(
            store=self._store(), job_id=1, gripe_id=42, config=self._cfg(repo, tmp_path)
        )
        main_after = self._upstream_main(upstream)

        assert outcome.status == "failed"
        assert "conflicts" in outcome.summary_text
        assert self._show(upstream, "%s") == "theirs", main_after
        assert fix_gripe._git_rev_parse(repo, "refs/heads/gripe_42") is not None

    def test_nothing_the_agent_plants_in_the_clone_runs_on_the_host_side(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The agent owns the clone's .git: hooks and command-shaped config.
        The land holds the push credential, so it must never run a git command
        inside the clone — only fetch its objects from the host checkout."""
        repo = self._make_repo(tmp_path)
        self._with_upstream(tmp_path, repo)
        clone_dir = tmp_path / "work" / "clones" / "gripe_42"
        marker = tmp_path / "pwned"
        commit = self._commit_in_clone(clone_dir)

        def _spawn(*a: object, **k: object) -> object:
            commit(*a, **k)
            evil = f"#!/bin/sh\ntouch {marker}\n"
            hooks = clone_dir / ".git" / "hooks"
            for name in ("pre-push", "reference-transaction", "post-checkout"):
                (hooks / name).write_text(evil, encoding="utf-8")
                (hooks / name).chmod(0o755)
            script = tmp_path / "fsmon.sh"
            script.write_text(evil, encoding="utf-8")
            script.chmod(0o755)
            self._run_git(clone_dir, "config", "core.fsmonitor", str(script))
            return object()

        self._arm(monkeypatch, _spawn)
        outcome = fix_gripe.run(
            store=self._store(), job_id=1, gripe_id=42, config=self._cfg(repo, tmp_path)
        )

        assert outcome.status == "succeeded", outcome.summary_text
        assert not marker.exists()

    def test_an_undeliverable_repo_skips_before_the_agent_is_spawned(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A deployment that cannot publish must not pay for an agent run to
        find that out, and must not consume the gripe's retry budget doing it.

        ``skipped``, not ``failed``: nothing about this gripe was attempted, so
        recording an attempt would be the same kind of false statement the rest
        of this class exists to prevent — just in the other direction.
        """
        from precis.utils.llm.router import Backend

        repo = self._make_repo(tmp_path)
        self._run_git(repo, "remote", "add", "origin", str(tmp_path / "nope.git"))
        monkeypatch.setattr(fix_gripe, "resolve_backend", lambda: Backend.ANTHROPIC)
        monkeypatch.setenv("PRECIS_FIX_GRIPE_UNSANDBOXED_ACK", "1")

        spawned: list[object] = []
        monkeypatch.setattr(
            fix_gripe, "_spawn_claude", lambda *a, **k: spawned.append(a)
        )

        outcome = fix_gripe.run(
            store=self._store(), job_id=1, gripe_id=42, config=self._cfg(repo, tmp_path)
        )

        assert outcome.status == "skipped", outcome.summary_text
        assert spawned == [], "the agent must not run when its output has nowhere to go"
        assert "no budget spent" in (outcome.gripe_comment_text or "")

    def test_preflight_passes_for_a_writable_target(self, tmp_path: Path) -> None:
        """Asked from the host checkout, which has no ``gripe_42`` branch: the
        pre-flight runs before the per-gripe clone exists (gr458899), so it
        must not depend on the branch being there."""
        repo = self._make_repo(tmp_path)
        upstream = tmp_path / "upstream.git"
        self._run_git(tmp_path, "init", "-q", "--bare", str(upstream))

        assert fix_gripe._publish_preflight(repo, "gripe_42", str(upstream)) is None

    def test_a_skip_leaves_no_clone_behind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr458899: the inert lane was still growing disk, ~300MB per skipped
        gripe, because the clone happened before the pre-flight and only the
        next attempt for the *same* gripe ever removed it. A skip now costs no
        clone, and reclaims one an earlier attempt left."""
        from precis.utils.llm.router import Backend

        repo = self._make_repo(tmp_path)
        self._run_git(repo, "remote", "add", "origin", str(tmp_path / "nope.git"))
        monkeypatch.setattr(fix_gripe, "resolve_backend", lambda: Backend.ANTHROPIC)
        monkeypatch.setenv("PRECIS_FIX_GRIPE_UNSANDBOXED_ACK", "1")
        cloned: list[object] = []
        monkeypatch.setattr(
            fix_gripe, "_git_clone_and_branch", lambda *a, **k: cloned.append(a)
        )
        cfg = self._cfg(repo, tmp_path)
        stale = cfg.work_dir / "clones" / "gripe_42"
        stale.mkdir(parents=True)
        (stale / "leftover").write_text("x", encoding="utf-8")

        outcome = fix_gripe.run(store=self._store(), job_id=1, gripe_id=42, config=cfg)

        assert outcome.status == "skipped", outcome.summary_text
        assert cloned == [], "a skip must be decided before anything is cloned"
        assert not stale.exists(), "a clone left by an earlier attempt is reclaimed"

    def test_ls_remote_asks_the_remote_not_the_clones_tracking_ref(
        self, tmp_path: Path
    ) -> None:
        """Why ``git rev-parse origin/<branch>`` could not have caught this: the
        tracking ref exists in the clone and the branch does not exist on the
        upstream, and only one of the two answers is about delivery."""
        repo = self._make_repo(tmp_path)
        upstream = tmp_path / "upstream.git"
        self._run_git(tmp_path, "init", "-q", "--bare", str(upstream))

        clone = tmp_path / "clone"
        self._run_git(tmp_path, "clone", "-q", "--local", str(repo), str(clone))
        self._run_git(clone, "checkout", "-q", "-b", "gripe_42")
        (clone / "f2.txt").write_text("y", encoding="utf-8")
        self._run_git(clone, "config", "user.email", "t@t")
        self._run_git(clone, "config", "user.name", "t")
        self._run_git(clone, "add", ".")
        self._run_git(clone, "commit", "-q", "-m", "fix")
        self._run_git(clone, "push", "-q", "origin", "gripe_42:refs/heads/gripe_42")

        assert fix_gripe._git_rev_parse(clone, "origin/gripe_42") is not None
        assert fix_gripe._ls_remote_sha(str(upstream), "gripe_42", clone) is None

    def test_ls_remote_against_an_unreachable_target_is_not_a_delivery(
        self, tmp_path: Path
    ) -> None:
        repo = self._make_repo(tmp_path)
        target = str(tmp_path / "nope.git")
        assert fix_gripe._ls_remote_sha(target, "gripe_42", repo) is None


# ── executor failure classification (gr456240) ─────────────────────────
#
# The fix_gripe dispatch (claude_inproc._run_fix_gripe) used to hand-roll
# its outcome.status == "failed" finalization, bypassing the transient
# classification every other executor gets via _common.record_failure. An
# infra-class failure (API/usage limit, auth, network — the agent never
# ran) therefore latched a plain child-failed bubble on the 12h·2ᴺ
# cool-down and counted down to a terminal child-failed-final at
# sweeper.UNPARK_CAP, permanently latching the leaf for a reason unrelated
# to the fix. The failure path now stamps meta.retry_after so the sweeper
# backs off to the precondition-clear instant without burning attempts.


class _FakeFixGripeSpec:
    """Stands in for the fix_gripe job_type: its run() just returns a
    pre-baked RunOutcome so the executor's transition logic is exercised
    without a clone/claude/push."""

    def __init__(self, outcome: RunOutcome) -> None:
        self._outcome = outcome

    def run(
        self, *, store: Store, job_id: int, gripe_id: int, params: dict
    ) -> RunOutcome:
        return self._outcome


def _mk_parked_fix_gripe_job(store: Store) -> tuple[int, int, int]:
    """A parent todo (leaf) + a gripe + a running fix_gripe job linked to
    the gripe (rel='fixes') and parented on the todo. Returns
    ``(todo_id, gripe_id, job_id)``."""
    todo = store.insert_ref(kind="todo", slug=None, title="parent leaf", meta={})
    gripe = insert_gripe(store, "the bug")
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="fix_gripe",
        meta={"job_type": "fix_gripe", "executor": "claude_inproc", "params": {}},
        parent_id=todo.id,
    )
    store.add_tag(
        job.id, Tag.closed("STATUS", "running"), set_by="system", replace_prefix=True
    )
    with store.pool.connection() as conn:
        store.add_link(
            src_ref_id=job.id,
            dst_ref_id=gripe.id,
            relation="fixes",
            set_by="system",
            conn=conn,
        )
        conn.commit()
    return todo.id, gripe.id, job.id


def _job_meta(store: Store, job_id: int) -> dict:
    got = store.get_ref(kind="job", id=job_id)
    assert got is not None
    return got.meta


def _open_tag_values(store: Store, ref_id: int) -> set[str]:
    return {str(t) for t in store.tags_for(ref_id)}


def test_run_fix_gripe_api_limit_failure_stamps_retry_after(store: Store) -> None:
    todo_id, gripe_id, job_id = _mk_parked_fix_gripe_job(store)
    now = datetime.now(UTC)
    target = now + timedelta(days=2)
    reason = (
        f"fix_gripe job:{job_id} for gripe:{gripe_id} failed: claude failed "
        f"(API Error: 400 You have reached your specified API usage limits. "
        f"You will regain access on {target:%Y-%m-%d} at {target:%H:%M} UTC.). "
        "Took 3.9s. stderr tail:\n"
    )
    outcome = RunOutcome(
        status="failed",
        summary_text=reason,
        gripe_comment_text="[worker:job] fix attempt failed: claude failed",
        branch=None,
        sha=None,
        wall_seconds=3.9,
    )

    claude_inproc._run_fix_gripe(store, job_id, _FakeFixGripeSpec(outcome))

    meta = _job_meta(store, job_id)
    assert meta["failure_class"] == "transient"
    retry_after = datetime.fromisoformat(meta["retry_after"])
    # Backed off to the named absolute reset, not the generic 2h horizon —
    # so the sweeper won't burn its unpark attempts before access returns.
    assert retry_after > now + timedelta(hours=24)
    # The bubble still tags the parent leaf (uniform child-failed shape).
    assert any("child-failed:" in v for v in _open_tag_values(store, todo_id))


def test_run_fix_gripe_content_failure_leaves_retry_after_unstamped(
    store: Store,
) -> None:
    """A genuine on-the-merits failure (the agent ran and reported it) must
    NOT be classified transient — it should count toward the unpark cap as
    before."""
    todo_id, gripe_id, job_id = _mk_parked_fix_gripe_job(store)
    outcome = RunOutcome(
        status="failed",
        summary_text=(
            f"fix_gripe job:{job_id} for gripe:{gripe_id} failed: no commits "
            "pushed to origin under branch gripe_1. Took 210.4s."
        ),
        gripe_comment_text="[worker:job] claude exited cleanly but made no commit",
        branch="gripe_1",
        sha=None,
        wall_seconds=210.4,
    )

    claude_inproc._run_fix_gripe(store, job_id, _FakeFixGripeSpec(outcome))

    meta = _job_meta(store, job_id)
    assert "retry_after" not in meta
    assert meta.get("failure_class") != "transient"
    # The failure still bubbles — it's a real attempt against the cap.
    assert any("child-failed:" in v for v in _open_tag_values(store, todo_id))


def _already_fixed_outcome() -> RunOutcome:
    return RunOutcome(
        status="already_fixed",
        summary_text="fix_gripe job:1 for gripe:2: already fixed. Evidence: abc1234",
        gripe_comment_text="[worker:job:1] no fix needed: abc1234",
        branch="gripe_2",
        sha=None,
        wall_seconds=40.0,
    )


def test_run_fix_gripe_already_fixed_goes_to_review_without_a_bubble(
    store: Store,
) -> None:
    """gr454480: an already-fixed verdict finishes the job, moves the gripe to
    in_review (out of the groomer's STATUS:open selection, so no re-mint
    loop) and spends nothing against the parent's unpark cap."""
    todo_id, gripe_id, job_id = _mk_parked_fix_gripe_job(store)

    claude_inproc._run_fix_gripe(
        store, job_id, _FakeFixGripeSpec(_already_fixed_outcome())
    )

    assert "STATUS:succeeded" in _open_tag_values(store, job_id)
    assert "STATUS:in_review" in _open_tag_values(store, gripe_id)
    assert not any("child-failed:" in v for v in _open_tag_values(store, todo_id))


def test_run_fix_gripe_already_fixed_leaves_a_closed_gripe_closed(
    store: Store,
) -> None:
    todo_id, gripe_id, job_id = _mk_parked_fix_gripe_job(store)
    store.add_tag(
        gripe_id, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )

    claude_inproc._run_fix_gripe(
        store, job_id, _FakeFixGripeSpec(_already_fixed_outcome())
    )

    assert "STATUS:done" in _open_tag_values(store, gripe_id)
