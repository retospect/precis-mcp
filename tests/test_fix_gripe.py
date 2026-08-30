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
from pathlib import Path
from typing import Any

import pytest

from precis.utils.claude_agent import ContainerRequiredError
from precis.workers.job_types import fix_gripe
from precis.workers.job_types.fix_gripe import (
    FixGripeConfig,
    RunOutcome,
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
        assert "gripe_*" in prompt
        assert "Do NOT touch main" in prompt
        # §H cycle a write-back design: the agent commits only; a trusted
        # process pushes on its behalf (it has no push creds/network route).
        assert "Do NOT push" in prompt
        assert "trusted process pushes" in prompt


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
        assert "no commits pushed" in outcome.summary_text


# ── trusted-side push: §H cycle a write-back design ────────────────
#
# The agent never has origin mounted or reachable (no repo_dir mount, §H
# cycle a) — it can only commit inside the clone. run() (trusted, host-side)
# performs the actual `git push` after call_claude_agent returns, guarded to
# gripe_<id> branch names.


class TestPushBranchTrusted:
    def test_refuses_non_gripe_branch_name(self, tmp_path: Path) -> None:
        from precis.workers.job_types.fix_gripe import _push_branch_trusted

        with pytest.raises(RuntimeError, match="gripe_"):
            _push_branch_trusted(tmp_path, "main")

    def test_refuses_gripe_branch_with_unexpected_shape(self, tmp_path: Path) -> None:
        from precis.workers.job_types.fix_gripe import _push_branch_trusted

        # Only the exact gripe_<digits> shape run() constructs is accepted —
        # not a lookalike that could smuggle extra refspec/shell content.
        with pytest.raises(RuntimeError, match="gripe_"):
            _push_branch_trusted(tmp_path, "gripe_42_evil")

    def test_no_subprocess_when_branch_name_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        from precis.workers.job_types.fix_gripe import _push_branch_trusted

        called: list[object] = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append((a, k)))
        with pytest.raises(RuntimeError):
            _push_branch_trusted(tmp_path, "not-a-gripe-branch")
        assert called == []


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
