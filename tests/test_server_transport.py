"""``precis serve``'s optional network transport (§H cycle b, deliverable 3
— the sandbox_run ``precis_access:read`` callback): the bearer-token
gate that guards ``sse``/``streamable-http``, and ``main()``'s
transport branching. ``stdio`` — every existing caller — must stay
byte-identical; the pure token-check function and the ASGI middleware
wrapping it are unit-tested directly, mirroring
``test_edit_schema.py``'s precedent for importing ``precis.server``
narrowly (not through the full MCP tool-dispatch surface).
"""

from __future__ import annotations

from typing import Any

import pytest

from precis import server

# ── pure token check ────────────────────────────────────────────────


def test_check_bearer_token_accepts_exact_match() -> None:
    assert server._check_bearer_token("Bearer tok123", "tok123") is True


def test_check_bearer_token_rejects_missing_header() -> None:
    assert server._check_bearer_token(None, "tok123") is False


def test_check_bearer_token_rejects_wrong_token() -> None:
    assert server._check_bearer_token("Bearer wrong", "tok123") is False


def test_check_bearer_token_rejects_missing_bearer_prefix() -> None:
    assert server._check_bearer_token("tok123", "tok123") is False


def test_check_bearer_token_rejects_empty_expected_mismatch() -> None:
    # An empty header never matches even a would-be-empty expected token —
    # the caller (main()) already refuses to start with no token at all.
    assert server._check_bearer_token("", "tok123") is False


def test_check_bearer_token_uses_constant_time_compare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Finding 4: must delegate to secrets.compare_digest (constant-time),
    # not a plain ``==`` that short-circuits on the first mismatch — spy
    # on the real implementation to prove it's actually invoked, not just
    # that the boolean result happens to match.
    calls: list[tuple[str, str]] = []
    real_compare_digest = server.secrets.compare_digest

    def spy(a: str, b: str) -> bool:
        calls.append((a, b))
        return bool(real_compare_digest(a, b))

    monkeypatch.setattr(server.secrets, "compare_digest", spy)
    assert server._check_bearer_token("Bearer tok123", "tok123") is True
    assert calls == [("Bearer tok123", "Bearer tok123")]


# ── ASGI middleware (Starlette TestClient) ─────────────────────────


def _tiny_app() -> Any:
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    async def _ok(request: Any) -> Any:
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/probe", _ok)])


def test_install_token_auth_rejects_missing_token() -> None:
    from starlette.testclient import TestClient

    app = _tiny_app()
    server._install_token_auth(app, token="s3cr3t")
    client = TestClient(app)
    resp = client.get("/probe")
    assert resp.status_code == 401


def test_install_token_auth_rejects_wrong_token() -> None:
    from starlette.testclient import TestClient

    app = _tiny_app()
    server._install_token_auth(app, token="s3cr3t")
    client = TestClient(app)
    resp = client.get("/probe", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_install_token_auth_accepts_correct_token() -> None:
    from starlette.testclient import TestClient

    app = _tiny_app()
    server._install_token_auth(app, token="s3cr3t")
    client = TestClient(app)
    resp = client.get("/probe", headers={"Authorization": "Bearer s3cr3t"})
    assert resp.status_code == 200
    assert resp.text == "ok"


# ── main()'s transport branching (stdio stays byte-identical) ──────


def test_main_stdio_default_calls_mcp_run_stdio_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No transport/host/port/token ever consulted on the stdio path —
    exactly the pre-existing ``mcp.run(transport="stdio")`` call."""
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(server, "_log_version_banner", lambda: None)
    monkeypatch.setattr(server, "_init_runtime", lambda: object())
    monkeypatch.setattr(server, "_warm_embedder_background", lambda runtime: None)
    monkeypatch.setattr(server.mcp, "run", lambda transport: calls.append((transport,)))
    monkeypatch.setattr(
        server,
        "_run_network_transport",
        lambda **kw: pytest.fail("must not be called on the stdio path"),
    )

    server.main()

    assert calls == [("stdio",)]


def test_main_network_transport_requires_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "_log_version_banner", lambda: None)
    monkeypatch.setattr(server, "_init_runtime", lambda: object())
    monkeypatch.setattr(server, "_warm_embedder_background", lambda runtime: None)
    monkeypatch.delenv("PRECIS_MCP_TOKEN", raising=False)

    with pytest.raises(ValueError, match="requires --token"):
        server.main(transport="streamable-http")


def test_main_network_transport_dispatches_with_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(server, "_log_version_banner", lambda: None)
    monkeypatch.setattr(server, "_init_runtime", lambda: object())
    monkeypatch.setattr(server, "_warm_embedder_background", lambda runtime: None)
    monkeypatch.setattr(
        server, "_run_network_transport", lambda **kw: captured.update(kw)
    )

    server.main(transport="sse", host="0.0.0.0", port=9999, token="tok")

    assert captured == {
        "transport": "sse",
        "host": "0.0.0.0",
        "port": 9999,
        "token": "tok",
    }


def test_main_network_transport_falls_back_to_env_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(server, "_log_version_banner", lambda: None)
    monkeypatch.setattr(server, "_init_runtime", lambda: object())
    monkeypatch.setattr(server, "_warm_embedder_background", lambda runtime: None)
    monkeypatch.setattr(
        server, "_run_network_transport", lambda **kw: captured.update(kw)
    )
    monkeypatch.setenv("PRECIS_MCP_TOKEN", "from-env")

    server.main(transport="streamable-http")

    assert captured["token"] == "from-env"


def test_main_rejects_unknown_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_log_version_banner", lambda: None)
    monkeypatch.setattr(server, "_init_runtime", lambda: object())
    monkeypatch.setattr(server, "_warm_embedder_background", lambda runtime: None)

    with pytest.raises(ValueError, match="unknown --transport"):
        server.main(transport="carrier-pigeon", token="tok")


# ── md index background warmup (see precis.md_index package docstring) ──


def _join_warmup_threads() -> None:
    """Wait for any live ``precis-md-index-warmup`` daemon thread.

    The function under test only *starts* a background thread and
    returns immediately (mirrors ``_warm_embedder_background``); tests
    need the work actually done before asserting on its effects.
    """
    import threading

    for t in threading.enumerate():
        if t.name == "precis-md-index-warmup":
            t.join(timeout=5)


def test_warm_md_index_background_noop_on_bare_object() -> None:
    """A runtime double with no ``hub`` attribute (matches the
    ``_init_runtime`` monkeypatch other tests in this module use)
    must not raise — mirrors ``_warm_embedder_background``'s
    defensive ``getattr`` style."""
    server._warm_md_index_background(object())  # type: ignore[arg-type]


def test_warm_md_index_background_noop_when_md_not_registered() -> None:
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.runtime import PrecisRuntime

    rt = PrecisRuntime(config=PrecisConfig(), hub=boot())
    server._warm_md_index_background(rt)
    _join_warmup_threads()


def test_warm_md_index_background_noop_without_embedder(tmp_path: Any) -> None:
    """Storeless boot has no embedder; ``vector_cache`` is ``None`` on
    the handler and the warmup must no-op rather than error."""
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.runtime import PrecisRuntime

    rt = PrecisRuntime(config=PrecisConfig(), hub=boot(md_roots=f"r:{tmp_path}"))
    server._warm_md_index_background(rt)
    _join_warmup_threads()


def test_warm_md_index_background_embeds_missing_and_flushes(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With an embedder wired, warmup embeds every block missing from
    the vector cache and persists the cache to disk (flush)."""
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.embedder import MockEmbedder
    from precis.runtime import PrecisRuntime

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text("# Hello\n\nSome body text.\n", encoding="utf-8")

    rt = PrecisRuntime(
        config=PrecisConfig(), hub=boot(embedder=MockEmbedder(), md_roots=f"r:{root}")
    )
    handler = rt.hub.handler_for("md")
    assert handler is not None
    assert handler.vector_cache is not None
    assert len(handler.vector_cache) == 0

    server._warm_md_index_background(rt)
    _join_warmup_threads()

    assert len(handler.vector_cache) > 0
    assert handler.vector_cache.npz_path.is_file()
    assert handler.vector_cache.manifest_path.is_file()


def test_warm_md_index_retries_a_transient_failure_and_records_warm(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr457326: a single timed-out batch must not strand the cache.

    The pass runs exactly once per process, so before this there was no
    "next successful pass" — one blip left md search lexical-only for the
    process's whole lifetime, which on the shared session server is every
    session on the machine.
    """
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.embedder import EmbedderUnavailable, MockEmbedder
    from precis.md_index import vectors as vectors_mod
    from precis.runtime import PrecisRuntime

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(server, "_MD_WARMUP_BACKOFF_S", 0.001)
    monkeypatch.setattr(vectors_mod, "_WARMUP_STATE", None)
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text("# Hello\n\nSome body text.\n", encoding="utf-8")

    class _FailsOnce(MockEmbedder):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def embed(self, texts: list[str]) -> Any:
            self.attempts += 1
            if self.attempts == 1:
                # What a bare TimeoutError becomes by the time it leaves
                # RemoteEmbedder._call — the retryable classification.
                raise EmbedderUnavailable("timed out", last_status=None)
            return super().embed(texts)

    rt = PrecisRuntime(
        config=PrecisConfig(), hub=boot(embedder=_FailsOnce(), md_roots=f"r:{root}")
    )
    handler = rt.hub.handler_for("md")
    assert handler is not None and handler.vector_cache is not None

    server._warm_md_index_background(rt)
    _join_warmup_threads()

    assert len(handler.vector_cache) > 0
    assert "warm" in (vectors_mod.warmup_state() or "")


def test_warm_md_index_gives_up_and_records_a_cold_cache(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A permanently-failing embedder is bounded, and says so.

    Two halves of gr457326: the retry is not unbounded (a down embedder
    must not leave a thread looping for the process's life), and the
    resulting cold cache is recorded for `precis-status` instead of
    living only in a background thread's traceback.
    """
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.embedder import EmbedderUnavailable, MockEmbedder
    from precis.md_index import vectors as vectors_mod
    from precis.runtime import PrecisRuntime

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(server, "_MD_WARMUP_BACKOFF_S", 0.001)
    monkeypatch.setattr(server, "_MD_WARMUP_BATCH_ATTEMPTS", 2)
    monkeypatch.setattr(vectors_mod, "_WARMUP_STATE", None)
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text("# Hello\n\nSome body text.\n", encoding="utf-8")

    class _AlwaysFails(MockEmbedder):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def embed(self, texts: list[str]) -> Any:
            self.attempts += 1
            raise EmbedderUnavailable("at capacity", last_status=429)

    embedder = _AlwaysFails()
    rt = PrecisRuntime(
        config=PrecisConfig(), hub=boot(embedder=embedder, md_roots=f"r:{root}")
    )

    server._warm_md_index_background(rt)
    _join_warmup_threads()

    assert embedder.attempts == 2  # bounded, not looping forever
    state = vectors_mod.warmup_state() or ""
    assert "COLD" in state
    assert "EmbedderUnavailable" in state
    assert "lexical-only" in state


def test_warm_md_index_retries_the_failing_batch_not_the_whole_pass(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pass must not restart from batch 0 when a later batch fails.

    The shape gr457326's first fix got wrong, measured on the shared
    server 2026-09-30: with the retry at the pass level, a failure
    unwinds everything and the next attempt re-embeds from the start,
    so at ~315 batches a first-batch failure means no batch ever lands.
    Here batch 2 fails once; the assertion is that batches 0 and 1 are
    embedded exactly once each — i.e. the retry resumed in place.
    """
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.embedder import EmbedderUnavailable, MockEmbedder
    from precis.md_index import vectors as vectors_mod
    from precis.runtime import PrecisRuntime

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(server, "_MD_WARMUP_BACKOFF_S", 0.001)
    monkeypatch.setattr(server, "_MD_WARMUP_BATCH_SIZE", 1)
    monkeypatch.setattr(vectors_mod, "_WARMUP_STATE", None)
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text(
        "# One\n\nAlpha body.\n\n# Two\n\nBeta body.\n\n# Three\n\nGamma body.\n",
        encoding="utf-8",
    )

    class _ThirdBatchFailsOnce(MockEmbedder):
        def __init__(self) -> None:
            super().__init__()
            self.batches: list[list[str]] = []
            self.failed = False

        def embed(self, texts: list[str]) -> Any:
            self.batches.append(list(texts))
            if len(self.batches) == 3 and not self.failed:
                self.failed = True
                raise EmbedderUnavailable("at capacity", last_status=429)
            return super().embed(texts)

    embedder = _ThirdBatchFailsOnce()
    rt = PrecisRuntime(
        config=PrecisConfig(), hub=boot(embedder=embedder, md_roots=f"r:{root}")
    )
    handler = rt.hub.handler_for("md")
    assert handler is not None and handler.vector_cache is not None

    server._warm_md_index_background(rt)
    _join_warmup_threads()

    assert "warm" in (vectors_mod.warmup_state() or "")
    # The first two batches went out once each — no restart from zero.
    first_two = embedder.batches[:2]
    for sent in first_two:
        assert embedder.batches.count(sent) == 1
    # The failing batch is the only one sent twice.
    assert embedder.batches[2] == embedder.batches[3]


def test_warm_md_index_honours_the_services_retry_after_hint(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 429 carrying `retry_after_s` overrides the backoff ladder.

    The admission queue knows when it will have room; guessing 30s at a
    429 that clears in 0.6s is what burned the retry budget in the
    measured failure.
    """
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.embedder import EmbedderUnavailable, MockEmbedder
    from precis.md_index import vectors as vectors_mod
    from precis.runtime import PrecisRuntime

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(server, "_MD_WARMUP_BACKOFF_S", 99.0)  # never used
    monkeypatch.setattr(vectors_mod, "_WARMUP_STATE", None)
    slept: list[float] = []
    monkeypatch.setattr(server.time, "sleep", slept.append)
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text("# Hello\n\nSome body text.\n", encoding="utf-8")

    class _BusyOnce(MockEmbedder):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def embed(self, texts: list[str]) -> Any:
            self.calls += 1
            if self.calls == 1:
                raise EmbedderUnavailable(
                    "at capacity", retry_after_s=0.25, last_status=429
                )
            return super().embed(texts)

    rt = PrecisRuntime(
        config=PrecisConfig(), hub=boot(embedder=_BusyOnce(), md_roots=f"r:{root}")
    )
    server._warm_md_index_background(rt)
    _join_warmup_threads()

    assert slept == [0.25]  # the hint, not the 99s ladder
    assert "warm" in (vectors_mod.warmup_state() or "")


def test_warm_md_index_does_not_retry_a_deterministic_error(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only `EmbedderUnavailable` is retryable.

    A dim mismatch or a short vector list is deterministic — retrying it
    six times per batch across hundreds of batches would turn a config
    error into a hang, so it fails the pass on the first occurrence.
    """
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.embedder import MockEmbedder
    from precis.md_index import vectors as vectors_mod
    from precis.runtime import PrecisRuntime

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(server, "_MD_WARMUP_BACKOFF_S", 0.001)
    monkeypatch.setattr(vectors_mod, "_WARMUP_STATE", None)
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text("# Hello\n\nSome body text.\n", encoding="utf-8")

    class _Misconfigured(MockEmbedder):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def embed(self, texts: list[str]) -> Any:
            self.calls += 1
            raise ValueError("dim mismatch: 512 != 1024")

    embedder = _Misconfigured()
    rt = PrecisRuntime(
        config=PrecisConfig(), hub=boot(embedder=embedder, md_roots=f"r:{root}")
    )
    server._warm_md_index_background(rt)
    _join_warmup_threads()

    assert embedder.calls == 1  # no retry at all
    state = vectors_mod.warmup_state() or ""
    assert "COLD" in state
    assert "ValueError" in state


def test_cold_md_warmup_is_visible_in_precis_status_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cold cache reaches the surface built to answer "why is search
    worse" — it used to exist only as a log line nobody reads."""
    from precis.handlers import skill as skill_mod
    from precis.md_index import vectors as vectors_mod

    monkeypatch.setattr(vectors_mod, "_WARMUP_STATE", None)
    assert "md_vector_warmup" not in dict(skill_mod._collect_runtime_info())

    monkeypatch.setattr(
        vectors_mod, "_WARMUP_STATE", "COLD after 4 attempt(s): TimeoutError"
    )
    rows = dict(skill_mod._collect_runtime_info())
    assert "COLD" in rows["md_vector_warmup"]


# ── md vector cache flush at shutdown ────────────────────────────────


def test_shutdown_runtime_flushes_md_vector_cache(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.embedder import MockEmbedder
    from precis.runtime import PrecisRuntime

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text("# Hello\n\nSome body text.\n", encoding="utf-8")

    rt = PrecisRuntime(
        config=PrecisConfig(), hub=boot(embedder=MockEmbedder(), md_roots=f"r:{root}")
    )
    handler = rt.hub.handler_for("md")
    assert handler is not None and handler.vector_cache is not None
    blocks = [b for _, b in handler.cache.get(root.resolve()).all_blocks()]
    handler.vector_cache.embed_missing(blocks, handler.embedder)
    assert len(handler.vector_cache) > 0
    assert not handler.vector_cache.npz_path.is_file()

    monkeypatch.setattr(server, "_runtime", rt)
    server._shutdown_runtime()

    assert handler.vector_cache.npz_path.is_file()
    assert handler.vector_cache.manifest_path.is_file()
    assert server._runtime is None
