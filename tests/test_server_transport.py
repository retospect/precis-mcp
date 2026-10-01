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
        "fd": None,
    }


def test_main_network_transport_passes_an_inherited_fd_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(server, "_log_version_banner", lambda: None)
    monkeypatch.setattr(server, "_init_runtime", lambda: object())
    monkeypatch.setattr(server, "_warm_embedder_background", lambda runtime: None)
    monkeypatch.setattr(
        server, "_run_network_transport", lambda **kw: captured.update(kw)
    )

    server.main(transport="streamable-http", token="tok", fd=7)

    assert captured["fd"] == 7


def test_run_network_transport_serves_on_the_inherited_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``fd`` the server must serve on that listening socket, not bind
    host:port itself — the supervisor keeps the port bound across restarts."""
    import socket

    import uvicorn

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    seen: list[Any] = []

    async def fake_serve(self: Any, sockets: Any = None) -> None:
        seen.append(sockets)

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    try:
        server._run_network_transport(
            transport="streamable-http",
            host="127.0.0.1",
            port=1,
            token="tok",
            fd=listener.fileno(),
        )
        assert len(seen) == 1
        (sock,) = seen[0]
        assert sock.getsockname() == listener.getsockname()
        sock.detach()  # same fd as the listener; let only the listener close it
    finally:
        listener.close()


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
    """A permanently-unavailable embedder is bounded, and says so.

    Two halves of gr457326: the retry is not unbounded (a down embedder
    must not leave a thread looping for the process's life), and the
    outcome is recorded for `precis-status` instead of living only in a
    background thread's traceback.

    The outcome is "warm with gaps", not COLD: since gr459088 a
    retryable error that outlasts the per-batch budget skips that batch
    rather than aborting the pass, so even a fully-down embedder reports
    what it skipped. COLD is now reserved for a non-retryable error —
    see `test_warm_md_index_does_not_retry_a_deterministic_error`.
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
    assert "gaps" in state
    assert "skipped" in state
    assert "0 new" in state


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


def test_cold_md_search_rearms_the_warm_pass(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A search against an incomplete cache starts another warm pass.

    gr457326 Do-next 2b. The boot pass is one shot against whatever the
    embedder is doing at boot, and on the shared session server the
    checkout watchdog restarts the process every few minutes during a
    qland burst — so the pass can spend its whole budget in the worst
    window and then stay dead. Measured 2026-10-01T00:19Z: the pass gave
    up while the embedder was busy and `inflight` was 0 three minutes
    later, with nothing able to go back for it.
    """
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.embedder import EmbedderUnavailable, MockEmbedder
    from precis.md_index import vectors as vectors_mod
    from precis.runtime import PrecisRuntime

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(server, "_MD_WARMUP_BACKOFF_S", 0.001)
    monkeypatch.setattr(server, "_MD_WARMUP_BATCH_ATTEMPTS", 1)
    monkeypatch.setattr(server, "_MD_WARMUP_REARM_COOLDOWN_S", 0.0)
    monkeypatch.setattr(vectors_mod, "_WARMUP_STATE", None)
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text("# Hello\n\nSome body text.\n", encoding="utf-8")

    class _DownThenUp(MockEmbedder):
        def __init__(self) -> None:
            super().__init__()
            self.down = True

        def embed(self, texts: list[str]) -> Any:
            if self.down:
                raise EmbedderUnavailable("at capacity", last_status=429)
            return super().embed(texts)

    embedder = _DownThenUp()
    rt = PrecisRuntime(
        config=PrecisConfig(), hub=boot(embedder=embedder, md_roots=f"r:{root}")
    )
    handler = rt.hub.handler_for("md")
    assert handler is not None and handler.vector_cache is not None

    server._warm_md_index_background(rt)
    _join_warmup_threads()
    assert len(handler.vector_cache) == 0
    assert "gaps" in (vectors_mod.warmup_state() or "")

    # The embedder recovers. A search is what notices.
    embedder.down = False
    handler.search(q="body")
    _join_warmup_threads()

    assert len(handler.vector_cache) > 0
    assert "warm" in (vectors_mod.warmup_state() or "")


def test_md_search_does_not_rearm_a_warm_cache(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fully-warm cache must not start a pass per search."""
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.embedder import MockEmbedder
    from precis.md_index import vectors as vectors_mod
    from precis.runtime import PrecisRuntime

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(server, "_MD_WARMUP_REARM_COOLDOWN_S", 0.0)
    monkeypatch.setattr(vectors_mod, "_WARMUP_STATE", None)
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text("# Hello\n\nSome body text.\n", encoding="utf-8")

    rt = PrecisRuntime(
        config=PrecisConfig(), hub=boot(embedder=MockEmbedder(), md_roots=f"r:{root}")
    )
    handler = rt.hub.handler_for("md")
    assert handler is not None

    server._warm_md_index_background(rt)
    _join_warmup_threads()
    assert len(handler.vector_cache) > 0

    starts: list[int] = []
    assert handler.rearm_warmup is not None

    def _count() -> bool:
        starts.append(1)
        return True

    handler.rearm_warmup = _count
    handler.search(q="body")
    assert starts == []  # nothing cold, nothing nudged


def test_rearm_is_non_blocking_and_single_flight(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook declines while a pass runs, and never waits on one.

    A search thread that blocked on an embed would hand the request path
    exactly the stall the background pass exists to avoid.
    """
    import threading

    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.embedder import MockEmbedder
    from precis.md_index import vectors as vectors_mod
    from precis.runtime import PrecisRuntime

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(server, "_MD_WARMUP_REARM_COOLDOWN_S", 0.0)
    monkeypatch.setattr(vectors_mod, "_WARMUP_STATE", None)
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text("# Hello\n\nSome body text.\n", encoding="utf-8")

    release = threading.Event()

    class _Blocks(MockEmbedder):
        def embed(self, texts: list[str]) -> Any:
            release.wait(timeout=10)
            return super().embed(texts)

    rt = PrecisRuntime(
        config=PrecisConfig(), hub=boot(embedder=_Blocks(), md_roots=f"r:{root}")
    )
    handler = rt.hub.handler_for("md")
    assert handler is not None

    server._warm_md_index_background(rt)
    assert handler.rearm_warmup is not None

    # The boot pass is parked inside embed(); a second arm must decline
    # rather than queue behind it or spawn a duplicate.
    assert handler.rearm_warmup() is False
    release.set()
    _join_warmup_threads()
    assert len(handler.vector_cache) > 0


def test_rearm_cooldown_backs_off_while_the_embedder_stays_down(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consecutive failures double the cooldown; a success resets it.

    Found by dogfooding the re-arm on 2026-10-01: a pass against a down
    embedder costs ~2 min of retries, and on a flat 60s floor every
    searching session on the machine can start one a minute. The
    embedder it is hammering sheds rather than queues (gr458940), so a
    flat floor turns the warm pass into an amplifier of the 429 storm
    that is already starving it.
    """
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.embedder import EmbedderUnavailable, MockEmbedder
    from precis.md_index import vectors as vectors_mod
    from precis.runtime import PrecisRuntime

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(server, "_MD_WARMUP_BACKOFF_S", 0.001)
    monkeypatch.setattr(server, "_MD_WARMUP_BATCH_ATTEMPTS", 1)
    monkeypatch.setattr(server, "_MD_WARMUP_REARM_COOLDOWN_S", 10.0)
    monkeypatch.setattr(vectors_mod, "_WARMUP_STATE", None)
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text("# Hello\n\nSome body text.\n", encoding="utf-8")

    clock = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])

    class _Down(MockEmbedder):
        def __init__(self) -> None:
            super().__init__()
            self.down = True

        def embed(self, texts: list[str]) -> Any:
            if self.down:
                raise EmbedderUnavailable("at capacity", last_status=429)
            return super().embed(texts)

    embedder = _Down()
    rt = PrecisRuntime(
        config=PrecisConfig(), hub=boot(embedder=embedder, md_roots=f"r:{root}")
    )
    handler = rt.hub.handler_for("md")
    assert handler is not None

    server._warm_md_index_background(rt)  # failure 1
    _join_warmup_threads()
    rearm = handler.rearm_warmup
    assert rearm is not None

    # One failure -> 20s. At +15s still cooling, at +25s allowed.
    clock[0] += 15
    assert rearm() is False
    clock[0] += 10
    assert rearm() is True  # failure 2
    _join_warmup_threads()

    # Two failures -> 40s. +25s is no longer enough.
    clock[0] += 25
    assert rearm() is False
    clock[0] += 20
    assert rearm() is True  # failure 3
    _join_warmup_threads()

    # A success resets the ladder back to the base cooldown. Three
    # failures would have put it at 80s, so a 15s gap being allowed is
    # the reset, and is what distinguishes it from a stuck ladder.
    embedder.down = False
    clock[0] += 100
    assert rearm() is True
    _join_warmup_threads()
    assert "warm" in (vectors_mod.warmup_state() or "")
    assert len(handler.vector_cache) > 0
    clock[0] += 5
    assert rearm() is False  # inside the 10s base
    clock[0] += 10
    assert rearm() is True  # past it — the ladder really did reset


def test_rearm_cooldown_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The doubling has a ceiling, so a long outage cannot park the
    re-arm past the point of usefulness."""
    assert server._MD_WARMUP_REARM_COOLDOWN_CAP_S >= (
        server._MD_WARMUP_REARM_COOLDOWN_S * 2
    )
    assert server._MD_WARMUP_REARM_COOLDOWN_CAP_S <= 3600.0


def test_one_unembeddable_batch_does_not_discard_the_rest(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch that cannot succeed costs one batch, not the whole pass.

    gr459088: batch 3 failed, burned its retries, and the pass aborted —
    discarding 1193 batches it had never attempted, so a pass netted 32
    vectors of 76k. At ~600 passes and a 900s cooldown ceiling that is
    days of wall time for work the embedder was perfectly able to do.
    """
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.embedder import EmbedderUnavailable, MockEmbedder
    from precis.md_index import vectors as vectors_mod
    from precis.runtime import PrecisRuntime

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(server, "_MD_WARMUP_BACKOFF_S", 0.001)
    monkeypatch.setattr(server, "_MD_WARMUP_BATCH_ATTEMPTS", 2)
    monkeypatch.setattr(server, "_MD_WARMUP_BATCH_SIZE", 1)
    monkeypatch.setattr(server, "_MD_WARMUP_BATCH_CHARS", None)
    monkeypatch.setattr(vectors_mod, "_WARMUP_STATE", None)
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text(
        "# One\n\nAlpha.\n\n# Two\n\nBeta.\n\n# Three\n\nPOISON here.\n\n"
        "# Four\n\nDelta.\n\n# Five\n\nEpsilon.\n",
        encoding="utf-8",
    )

    class _OneBadBatch(MockEmbedder):
        def embed(self, texts: list[str]) -> Any:
            if any("POISON" in text for text in texts):
                raise EmbedderUnavailable("too big", last_status=429)
            return super().embed(texts)

    rt = PrecisRuntime(
        config=PrecisConfig(), hub=boot(embedder=_OneBadBatch(), md_roots=f"r:{root}")
    )
    handler = rt.hub.handler_for("md")
    assert handler is not None and handler.vector_cache is not None

    total_blocks = sum(
        len([b for _, b in handler.cache.get(r).all_blocks()])
        for r in handler.roots.values()
    )
    server._warm_md_index_background(rt)
    _join_warmup_threads()

    # Everything except the poisoned batch landed.
    assert len(handler.vector_cache) == total_blocks - 1
    state = vectors_mod.warmup_state() or ""
    assert "gaps" in state and "1 batch(es) skipped" in state


def test_batch_chars_caps_a_batch_by_length_not_count(tmp_path: Any) -> None:
    """Characters are the axis that predicts the deadline.

    Blocks in this repo run 3..22756 chars, so a count cap of 64 is
    anywhere between a trivial request and one that cannot finish in
    15s — which is why the count-capped first fix kept failing on
    whichever batch collected the long blocks.
    """
    from precis.md_index.vectors import _plan_batches

    class _B:
        def __init__(self, text: str) -> None:
            self.text = text

    by_sha = {
        "a": _B("x" * 100),
        "b": _B("x" * 100),
        "c": _B("x" * 900),
        "d": _B("x" * 10),
    }
    shas = ["a", "b", "c", "d"]

    # 250-char cap: a+b fit, c alone exceeds it, d opens a new batch.
    assert _plan_batches(shas, by_sha, batch_size=None, batch_chars=250) == [
        ["a", "b"],
        ["c"],
        ["d"],
    ]
    # A single oversized block still goes out alone rather than being
    # dropped here — skipping it is the caller's decision.
    assert _plan_batches(["c"], by_sha, batch_size=None, batch_chars=10) == [["c"]]
    # Count cap still works, and the two caps compose (whichever first).
    assert _plan_batches(shas, by_sha, batch_size=3, batch_chars=None) == [
        ["a", "b", "c"],
        ["d"],
    ]
    assert _plan_batches(shas, by_sha, batch_size=3, batch_chars=250) == [
        ["a", "b"],
        ["c"],
        ["d"],
    ]
    # No caps: one batch, the request path's behaviour.
    assert _plan_batches(shas, by_sha, batch_size=None, batch_chars=None) == [shas]
    assert _plan_batches([], by_sha, batch_size=None, batch_chars=None) == []


def test_warmup_state_is_visible_while_the_pass_is_still_running(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`md_vector_warmup` must exist during the pass, not only after it.

    gr459088 comment 7: `record_warmup_state` was only called on a batch
    error or at the terminal state, so a cleanly-warming process
    rendered no row at all — which is the one hour the field is for, and
    it made "warming normally" and "never started" look identical.
    Caught 63s into a healthy pass on ae7bdb6a.
    """
    import threading

    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.embedder import MockEmbedder
    from precis.handlers import skill as skill_mod
    from precis.md_index import vectors as vectors_mod
    from precis.runtime import PrecisRuntime

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(server, "_MD_WARMUP_BATCH_SIZE", 1)
    monkeypatch.setattr(server, "_MD_WARMUP_BATCH_CHARS", None)
    monkeypatch.setattr(vectors_mod, "_WARMUP_STATE", None)
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text(
        "# One\n\nAlpha.\n\n# Two\n\nBeta.\n\n# Three\n\nGamma.\n\n# Four\n\nDelta.\n",
        encoding="utf-8",
    )

    seen_mid_pass: list[str] = []
    gate = threading.Event()

    class _Watched(MockEmbedder):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def embed(self, texts: list[str]) -> Any:
            self.calls += 1
            if self.calls == 3:
                # Two batches are cached by now; the row must already
                # say so rather than waiting for the pass to end.
                rows = dict(skill_mod._collect_runtime_info())
                seen_mid_pass.append(rows.get("md_vector_warmup", "<<missing>>"))
                gate.set()
            return super().embed(texts)

    rt = PrecisRuntime(
        config=PrecisConfig(), hub=boot(embedder=_Watched(), md_roots=f"r:{root}")
    )
    server._warm_md_index_background(rt)
    assert gate.wait(timeout=10), "pass never reached the third batch"
    _join_warmup_threads()

    assert seen_mid_pass, "no sample taken"
    state = seen_mid_pass[0]
    assert "warming" in state, state
    assert "batch 2/" in state, state  # two batches done when batch 3 began
    # And the terminal state still replaces it.
    assert "warm (" in (vectors_mod.warmup_state() or "")


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
