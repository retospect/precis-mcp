"""``diagnose_gripe`` job_type — the read-only root-cause diagnosis pass.

The claude subprocess is stubbed via monkeypatching
``diagnose_gripe._spawn_claude`` (mirrors ``test_fix_gripe.py``'s pattern of
stubbing ``fix_gripe._spawn_claude``) so the real clone -> prompt ->
write-back path runs against real git + the real test DB, without a real
``claude -p`` call. Repo resolution reuses ``fix_gripe``'s env
(``PRECIS_FIX_WORK_DIR`` / ``PRECIS_FIX_REPO_DIR``) — see the module
docstring for why.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from precis.store import Store
from precis.utils.claude_agent import AgentResult
from precis.workers.job_types import diagnose_gripe as dg
from precis.workers.job_types import get_job_type, known_job_types

pytestmark = pytest.mark.db


def _open_gripe(store: Store, text: str) -> int:
    """File a gripe (ref + gripe_body chunk + STATUS:open) via the same
    SECURITY DEFINER path GripeHandler._create uses; returns its id."""
    with store.pool.connection() as conn:
        row = conn.execute("SELECT public.file_gripe_readonly(%s)", (text,)).fetchone()
        assert row is not None
        conn.commit()
        return int(row[0])


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True
    )
    (repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "init",
        ],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    return repo


def _diagnosis_reply(confidence: str = "0.92") -> str:
    return (
        "DIAGNOSIS:\n"
        "Root cause: the embedder client isn't retried on a transient 503.\n"
        "Evidence: src/precis/workers/embed.py::EmbedHandler.process\n"
        "Proposed fix: wrap the call in the existing retry helper.\n"
        f"Confidence: {confidence}\n"
    )


class _FakeCtx:
    def __init__(self, store: Store, ref_id: int, params: dict):
        self.store = store
        self.ref_id = ref_id
        self.title = "diagnose"
        self.meta = {"params": params}
        self.chunks: list[tuple[str, str]] = []
        self.status: str | None = None
        self.failure: str | None = None
        self.meta_sets: dict = {}

    def set_status(self, s: str) -> None:
        self.status = s

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))

    def set_meta(self, **kw: object) -> None:
        self.meta_sets.update(kw)

    def record_failure(self, msg: str, **_kw: object) -> None:
        self.failure = msg

    def is_cancel_requested(self) -> bool:
        return False


@pytest.fixture
def work_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Env wired so ``load_config_from_env`` / the gr179498 gate both pass —
    a repo to clone from, and the unsandboxed ack (no container in tests)."""
    repo = _make_repo(tmp_path)
    wd = tmp_path / "work"
    monkeypatch.setenv("PRECIS_FIX_WORK_DIR", str(wd))
    monkeypatch.setenv("PRECIS_FIX_REPO_DIR", str(repo))
    monkeypatch.delenv("PRECIS_FIX_REPOS", raising=False)
    monkeypatch.setenv("PRECIS_FIX_GRIPE_UNSANDBOXED_ACK", "1")
    monkeypatch.delenv("PRECIS_DIAGNOSE_AUTOPROMOTE", raising=False)
    return wd


# ── registry ─────────────────────────────────────────────────────


def test_registered_with_dispatch() -> None:
    spec = get_job_type("diagnose_gripe")
    assert spec is not None and spec.dispatch is not None
    assert spec.compatible_executors == frozenset({"claude_inproc"})
    assert "claude_bin" in spec.requires and "git" in spec.requires
    assert "diagnose_gripe" in known_job_types()


# ── happy path ───────────────────────────────────────────────────


def test_dispatch_writes_diagnosis_comment_and_parses_confidence(
    store: Store, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gid = _open_gripe(store, "search 500s on a percent sign")

    def _fake_spawn(*, model: str, clone_dir: Path, prompt: str, timeout_s: float):
        assert clone_dir.exists()  # the clone landed before the "agent" ran
        assert "search 500s on a percent sign" in prompt
        assert "READ-ONLY" in prompt
        return AgentResult(
            final_text=_diagnosis_reply("0.92"),
            cost_usd=0.01,
            duration_s=0.1,
            turns_used=1,
        )

    monkeypatch.setattr(dg, "_spawn_claude", _fake_spawn)

    ctx = _FakeCtx(store, 999, {"gripe_id": gid})
    dg._dispatch(ctx, dg.SPEC)

    assert ctx.status == "succeeded" and ctx.failure is None
    assert ctx.meta_sets.get("confidence") == 0.92

    blocks = store.chunks.list_chunks_for_ref(gid)
    comments = [b for b in blocks if b.chunk_kind == "gripe_comment"]
    assert len(comments) == 1
    assert comments[0].text.startswith("DIAGNOSIS (auto, job 999):")
    assert "Root cause" in comments[0].text

    # STATUS is untouched — diagnose_gripe never flips it.
    tags = store.tags_for(gid)
    assert any("STATUS:open" in str(t) for t in tags)

    # The clone is disposable — gone once the run finishes.
    assert not (work_dir / "diagnose_clones" / f"gripe_{gid}").exists()


# ── failure paths ────────────────────────────────────────────────


def test_dispatch_fails_on_malformed_params(store: Store) -> None:
    ctx = _FakeCtx(store, 1, {})  # no gripe_id
    dg._dispatch(ctx, dg.SPEC)
    assert ctx.status is None
    assert ctx.failure is not None and "malformed params" in ctx.failure


def test_dispatch_fails_on_missing_gripe(store: Store) -> None:
    ctx = _FakeCtx(store, 1, {"gripe_id": 999_999_999})
    dg._dispatch(ctx, dg.SPEC)
    assert ctx.status is None
    assert ctx.failure is not None and "not found" in ctx.failure


def test_dispatch_fails_on_empty_diagnosis(
    store: Store, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gid = _open_gripe(store, "bug with a blank reply")
    monkeypatch.setattr(
        dg,
        "_spawn_claude",
        lambda **_kw: AgentResult(
            final_text="   ", cost_usd=0.0, duration_s=0.1, turns_used=1
        ),
    )
    ctx = _FakeCtx(store, 2, {"gripe_id": gid})
    dg._dispatch(ctx, dg.SPEC)
    assert ctx.status is None
    assert ctx.failure is not None and "empty diagnosis" in ctx.failure
    # No gripe_comment was appended — only the original body chunk.
    blocks = store.chunks.list_chunks_for_ref(gid)
    assert all(b.chunk_kind != "gripe_comment" for b in blocks)


def test_dispatch_fails_when_clone_fails(
    store: Store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gid = _open_gripe(store, "repo dir points nowhere")
    monkeypatch.setenv("PRECIS_FIX_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("PRECIS_FIX_REPO_DIR", str(tmp_path / "does-not-exist"))
    monkeypatch.delenv("PRECIS_FIX_REPOS", raising=False)
    monkeypatch.setenv("PRECIS_FIX_GRIPE_UNSANDBOXED_ACK", "1")

    ctx = _FakeCtx(store, 3, {"gripe_id": gid})
    dg._dispatch(ctx, dg.SPEC)
    assert ctx.status is None
    assert ctx.failure is not None and "clone failed" in ctx.failure


# ── safety gates (reused from fix_gripe) ────────────────────────


def test_dispatch_skips_under_openai_backend(
    store: Store, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.utils.llm.router import Backend

    monkeypatch.setattr(dg, "resolve_backend", lambda: Backend.OPENAI)
    gid = _open_gripe(store, "backend flip skip")
    ctx = _FakeCtx(store, 4, {"gripe_id": gid})
    dg._dispatch(ctx, dg.SPEC)
    assert ctx.status == "cancelled"
    assert ctx.failure is None


def test_dispatch_skips_when_no_container_and_no_ack(
    store: Store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("PRECIS_FIX_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("PRECIS_FIX_REPO_DIR", str(repo))
    monkeypatch.delenv("PRECIS_FIX_REPOS", raising=False)
    monkeypatch.delenv("PRECIS_FIX_GRIPE_UNSANDBOXED_ACK", raising=False)
    monkeypatch.delenv("PRECIS_AGENT_CONTAINER", raising=False)

    gid = _open_gripe(store, "gr179498 fail-closed")
    ctx = _FakeCtx(store, 5, {"gripe_id": gid})
    dg._dispatch(ctx, dg.SPEC)
    assert ctx.status == "cancelled"
    assert ctx.failure is None


# ── autopromote bridge (dark) ────────────────────────────────────


def test_autopromote_tags_gripe_when_confidence_high_and_env_set(
    store: Store, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gid = _open_gripe(store, "autopromote candidate")
    monkeypatch.setenv("PRECIS_DIAGNOSE_AUTOPROMOTE", "1")
    monkeypatch.setattr(
        dg,
        "_spawn_claude",
        lambda **_kw: AgentResult(
            final_text=_diagnosis_reply("0.85"),
            cost_usd=0.0,
            duration_s=0.1,
            turns_used=1,
        ),
    )
    ctx = _FakeCtx(store, 6, {"gripe_id": gid})
    dg._dispatch(ctx, dg.SPEC)
    assert ctx.status == "succeeded"
    tags = store.tags_for(gid)
    assert any("auto-fix" in str(t) for t in tags)


def test_autopromote_skipped_when_env_unset(
    store: Store, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gid = _open_gripe(store, "no autopromote without the env flag")
    monkeypatch.delenv("PRECIS_DIAGNOSE_AUTOPROMOTE", raising=False)
    monkeypatch.setattr(
        dg,
        "_spawn_claude",
        lambda **_kw: AgentResult(
            final_text=_diagnosis_reply("0.95"),
            cost_usd=0.0,
            duration_s=0.1,
            turns_used=1,
        ),
    )
    ctx = _FakeCtx(store, 7, {"gripe_id": gid})
    dg._dispatch(ctx, dg.SPEC)
    assert ctx.status == "succeeded"
    tags = store.tags_for(gid)
    assert not any("auto-fix" in str(t) for t in tags)


def test_autopromote_skipped_when_confidence_below_threshold(
    store: Store, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gid = _open_gripe(store, "low confidence diagnosis")
    monkeypatch.setenv("PRECIS_DIAGNOSE_AUTOPROMOTE", "1")
    monkeypatch.setattr(
        dg,
        "_spawn_claude",
        lambda **_kw: AgentResult(
            final_text=_diagnosis_reply("0.50"),
            cost_usd=0.0,
            duration_s=0.1,
            turns_used=1,
        ),
    )
    ctx = _FakeCtx(store, 8, {"gripe_id": gid})
    dg._dispatch(ctx, dg.SPEC)
    assert ctx.status == "succeeded"
    tags = store.tags_for(gid)
    assert not any("auto-fix" in str(t) for t in tags)


# ── pure helpers ─────────────────────────────────────────────────


class TestParseConfidence:
    def test_extracts_value(self) -> None:
        assert dg._parse_confidence("blah\nConfidence: 0.73\n") == 0.73

    def test_case_insensitive_label(self) -> None:
        assert dg._parse_confidence("confidence: 0.4") == 0.4

    def test_missing_returns_none(self) -> None:
        assert dg._parse_confidence("no confidence line here") is None

    def test_out_of_range_returns_none(self) -> None:
        assert dg._parse_confidence("Confidence: 1.5") is None

    def test_boundary_values(self) -> None:
        assert dg._parse_confidence("Confidence: 0") == 0.0
        assert dg._parse_confidence("Confidence: 1") == 1.0


class TestComposePrompt:
    @staticmethod
    def _block(text: str):
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class _B:
            text: str

        return _B(text)

    def test_shape(self) -> None:
        prompt = dg._compose_prompt(
            ref_title="bug",
            blocks=[self._block("the body"), self._block("extra detail")],
        )
        assert "BODY: the body" in prompt
        assert "COMMENT 1: extra detail" in prompt
        assert "DIAGNOSIS:" in prompt
        assert "Confidence:" in prompt
        assert "READ-ONLY" in prompt
        assert "do NOT edit, commit, branch, or push" in prompt


# ── Auth: subscription OAuth token first, metered API key as fallback ──


class TestSpawnAuth:
    """``--bare`` auth is *strictly* ``ANTHROPIC_API_KEY`` (``claude --help``),
    and upstream ``call_claude_agent`` derives the container's secret-by-key
    channel from the same flag (``"api" if bare else agent_run_mode()``). So
    ``bare`` has to track which credential we actually have, and dropping it
    for an OAuth run must not silently switch on the CLAUDE.md / ``.claude``
    hook discovery that ``--bare`` was suppressing."""

    @staticmethod
    def _clone_with_project_config(tmp_path: Path) -> Path:
        clone = tmp_path / "clone"
        (clone / ".claude").mkdir(parents=True)
        (clone / ".claude" / "settings.json").write_text(
            '{"hooks": {}}', encoding="utf-8"
        )
        (clone / "CLAUDE.md").write_text("# project brief", encoding="utf-8")
        (clone / "AGENTS.md").write_text("# conventions", encoding="utf-8")
        (clone / "src.py").write_text("x = 1\n", encoding="utf-8")
        return clone

    @staticmethod
    def _capture(monkeypatch: pytest.MonkeyPatch) -> dict:
        """Stub the agent chokepoint; return the kwargs it was called with."""
        seen: dict = {}

        def _fake(prompt: str, **kw: object) -> AgentResult:
            seen.update(kw)
            seen["prompt"] = prompt
            return AgentResult(
                final_text="ok", cost_usd=0.0, duration_s=0.0, turns_used=1
            )

        monkeypatch.setattr(
            "precis.utils.claude_agent.call_claude_agent", _fake, raising=True
        )
        # Deterministic: no vault reads in either direction.
        monkeypatch.setattr(
            "precis.secrets.get_secret", lambda name, **kw: None, raising=True
        )
        return seen

    def test_oauth_token_drops_bare_and_strips_project_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone = self._clone_with_project_config(tmp_path)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-TEST")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        seen = self._capture(monkeypatch)

        dg._spawn_claude(model="m", clone_dir=clone, prompt="p", timeout_s=1.0)

        assert seen["bare"] is False
        env = seen["env_base"]
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-TEST"
        # The billed path is scrubbed so the CLI cannot choose it.
        assert "ANTHROPIC_API_KEY" not in env
        # ...and the ambient project config --bare used to suppress is gone.
        assert not (clone / "CLAUDE.md").exists()
        assert not (clone / "AGENTS.md").exists()
        assert not (clone / ".claude").exists()
        # Only the auto-loaded config goes; the code under diagnosis stays.
        assert (clone / "src.py").exists()

    def test_api_key_only_keeps_bare_and_leaves_the_clone_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No token: fall back to the metered key, keep ``--bare`` — which
        suppresses the discovery itself, so nothing needs stripping."""
        clone = self._clone_with_project_config(tmp_path)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-TEST")
        seen = self._capture(monkeypatch)

        dg._spawn_claude(model="m", clone_dir=clone, prompt="p", timeout_s=1.0)

        assert seen["bare"] is True
        assert seen["env_base"]["ANTHROPIC_API_KEY"] == "sk-ant-api03-TEST"
        assert (clone / "CLAUDE.md").exists()
        assert (clone / ".claude").exists()

    def test_no_credential_at_all_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone = self._clone_with_project_config(tmp_path)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        self._capture(monkeypatch)

        with pytest.raises(RuntimeError, match="no usable credential"):
            dg._spawn_claude(model="m", clone_dir=clone, prompt="p", timeout_s=1.0)
