"""asa_bot → ``precis serve`` run attribution (gr540852).

The gripe: every ``asa-bot/0.1.0`` row in ``tool_calls`` carried
``source``/``agentlog_id`` NULL — the daemon's long-lived serve
subprocess froze its env at spawn and the wire had no per-call run
channel at all. The fix threads the run through
``CallToolRequest.params._meta.precis`` (server binds it as a
:class:`TickContext` in ``tools/core._dispatch``) and pins
``PRECIS_CURRENT_SOURCE=asa_bot`` in the spawn env.

The e2e test drives the REAL client path: ``PrecisClient`` spawning a
``python -m precis serve`` subprocess over stdio JSON-RPC against the
session's test DB — not an in-process fake — because the defect lived
exactly at that wire boundary.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any
from uuid import uuid4

import pytest

from asa_bot.precis_client import (
    PrecisClient,
    _run_meta,
    bind_run,
    run_context,
)


def test_run_meta_explicit_and_bound() -> None:
    """``call_tool``'s ``_meta``: explicit kwargs win, the ``bind_run`` /
    ``run_context`` binding fills the rest, empty stays absent."""

    assert _run_meta() == {}
    assert _run_meta(agentlog_id=5) == {"precis": {"agentlog_id": 5}}
    assert _run_meta(agentlog_id=" 42 ", source=" asa_bot ") == {
        "precis": {"agentlog_id": 42, "source": "asa_bot"}
    }
    # Unparseable ids are dropped, not raised through.
    assert _run_meta(agentlog_id="junk") == {}

    bind_run(agentlog_id=7, source="daemon")
    try:
        assert _run_meta() == {"precis": {"agentlog_id": 7, "source": "daemon"}}
        # An explicit kwarg overrides the binding.
        assert _run_meta(agentlog_id=9)["precis"]["agentlog_id"] == 9
        with run_context(agentlog_id=13):
            assert _run_meta()["precis"]["agentlog_id"] == 13
        # run_context resets on exit — the outer bind stands.
        assert _run_meta()["precis"]["agentlog_id"] == 7
    finally:
        bind_run()

    # And the wire shape: call_tool puts it on params._meta.
    seen: list[dict[str, Any]] = []

    class _Stub(PrecisClient):
        async def request(self, method: str, params: dict[str, Any]) -> Any:
            seen.append(params)
            return {"content": [{"type": "text", "text": "ok"}]}

    client = _Stub.__new__(_Stub)
    asyncio.run(client.call_tool("get", {"kind": "gripe", "id": [1]}, agentlog_id=4))
    assert seen[0]["_meta"] == {"precis": {"agentlog_id": 4}}
    asyncio.run(client.call_tool("get", {"kind": "gripe", "id": [1]}))
    assert "_meta" not in seen[1]


def test_spawn_env_carries_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spawned serve gets ``PRECIS_CURRENT_SOURCE=asa_bot`` — the
    daemon's static producing-job stamp — unless the env already pins it."""
    from asa_bot.config import PrecisConfig

    client = PrecisClient(PrecisConfig(command=["true"]))
    captured: dict[str, Any] = {}

    class _Proc:
        pid = 1
        returncode = 0

    async def fake_spawn(*a: Any, env: dict[str, str] | None = None, **kw: Any) -> Any:
        captured.update(env or {})
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.delenv("PRECIS_CURRENT_SOURCE", raising=False)
    asyncio.run(client._spawn())
    assert captured["PRECIS_CURRENT_SOURCE"] == "asa_bot"

    monkeypatch.setenv("PRECIS_CURRENT_SOURCE", "pinned")
    captured.clear()
    asyncio.run(client._spawn())
    assert captured["PRECIS_CURRENT_SOURCE"] == "pinned"


class _FakeRC:
    """``RequestContext`` stand-in for the transport gate.

    mcp 1.30's ``RequestContext.request`` is the *transport* request
    context — ``None`` on stdio, the Starlette request on HTTP — and
    ``.meta`` is the parsed ``params._meta``. Tests fake both directly.
    """

    def __init__(self, meta: Any, *, request: Any = None) -> None:
        self.request = request
        self.meta = meta


class _FakeCtx:
    """FastMCP ``Context`` stand-in — only the attributes
    ``tools/core``'s ctx readers touch."""

    def __init__(self, meta: Any, *, request: Any = None) -> None:
        self.request_context = _FakeRC(meta, request=request)
        self.session = None


def _wire_ctx(meta: Any, *, request: Any = None) -> Any:
    """A ctx the run-context reader takes: ``request=None`` reads as the
    stdio transport, a non-None ``request`` as HTTP."""
    return _FakeCtx(meta, request=request)


def test_ctx_run_context_forged_id_dropped_and_logged(
    store: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A wire ``agentlog_id`` that isn't a live ``kind='agentlog'`` ref is
    dropped with a warning — never stamped; so is a ``source`` outside
    the wire-producer vocabulary (gr540852 review)."""
    import logging

    from precis import agentlog
    from precis.tools.core import _WIRE_AGENTLOG_OK, _ctx_run_context

    _WIRE_AGENTLOG_OK.clear()
    caplog.set_level(logging.WARNING, logger="precis.tools.core")

    live = agentlog.open_log(store, source="asa_bot", title="live run")
    with store.tx() as conn:
        not_a_log = store.insert_ref(
            kind="memory", slug=None, title="not a log", meta={}, conn=conn
        )
    closed = agentlog.open_log(store, source="asa_bot", title="closed run")
    agentlog.finalize_log(store, log_id=closed, status="ok")

    nonexistent = 10**12
    for bad_id in (nonexistent, int(not_a_log.id), closed):
        assert (
            _ctx_run_context(
                _wire_ctx({"precis": {"agentlog_id": bad_id}}), store=store
            )
            is None
        )
    assert caplog.text.count("names no open agentlog ref") == 3

    caplog.clear()
    assert (
        _ctx_run_context(_wire_ctx({"precis": {"source": "operator"}}), store=store)
        is None
    )
    assert "not a wire producer" in caplog.text

    # And the valid pair still binds.
    ctx = _ctx_run_context(
        _wire_ctx({"precis": {"agentlog_id": live, "source": "asa_bot"}}),
        store=store,
    )
    assert ctx is not None
    assert ctx.agentlog_id == live and ctx.source == "asa_bot"


def test_ctx_run_context_http_transport_ignored(store: Any) -> None:
    """``_meta.precis`` on a non-stdio transport is ignored entirely —
    an HTTP client must not forge run attribution (gr540852 review)."""
    from precis.tools.core import _ctx_run_context

    ctx = _wire_ctx(
        {"precis": {"agentlog_id": 1, "source": "asa_bot"}}, request=object()
    )
    assert _ctx_run_context(ctx, store=store) is None


def test_dispatch_merges_wire_ctx_onto_ambient_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wire TickContext merges onto the ambient one — ``disabled_kinds``,
    ``parent_todo`` and friends keep their values; only the declared keys
    override (gr540852 review)."""
    from precis.tools import core
    from precis.utils.inproc_context import TickContext, current, tick_context

    seen: dict[str, Any] = {}

    class _RT:
        store = None

        def dispatch_with_status(
            self, verb: str, payload: dict[str, Any], fold: bool = False
        ) -> tuple[str, bool]:
            seen["ctx"] = current()
            return ("ok-body", False)

        def render_error(self, e: Exception) -> str:
            return str(e)

    monkeypatch.setattr(core, "_get_runtime", lambda: _RT())
    ambient = TickContext(
        parent_todo=5,
        agentlog_id=11,
        model="m",
        disabled_kinds=(("tex", "hint"),),
    )
    ctx = _wire_ctx({"precis": {"source": "asa_bot"}})
    with tick_context(ambient):
        core._dispatch("get", {"kind": "gripe", "id": [1]}, ctx)

    bound = seen["ctx"]
    assert bound.disabled_kinds == (("tex", "hint"),)
    assert bound.parent_todo == 5 and bound.model == "m"
    # Wire declared only ``source``: it wins; the undeclared agentlog_id
    # falls through to the ambient tick.
    assert bound.source == "asa_bot"
    assert bound.agentlog_id == 11


def test_asa_client_stamps_tool_calls_row(store: Any) -> None:
    """End-to-end: PrecisClient → real ``precis serve`` subprocess →
    ``tool_calls`` carries ``source='asa_bot'`` and the minted
    ``agentlog_id`` — plus ``client='asa-bot/0.1.0'``.

    Asserts all three stamp paths on one server:
      * bound (``bind_run``) call → agentlog_id + source via ``_meta``;
      * explicit-kwarg call → same, overriding the binding;
      * unbound call → ``source`` from the spawn env, no agentlog_id.
    Forged ids (a nonexistent id, a non-agentlog ref id) stamp NULL
    (gr540852 review).
    """
    from precis import agentlog

    dsn = store.dsn
    assert dsn, "store fixture must expose the session clone's DSN"
    tag = uuid4().hex[:8]
    log_id = agentlog.open_log(
        store, source="asa_bot", title=f"asa turn conv-test-{tag}"
    )
    other_log_id = agentlog.open_log(
        store, source="asa_bot", title=f"asa turn conv-test-{tag}-b"
    )
    with store.tx() as conn:
        not_a_log = store.insert_ref(
            kind="memory", slug=None, title="not a log", meta={}, conn=conn
        )

    from pathlib import Path

    # Force the subprocess onto THIS tree's precis: the test image also
    # carries an installed wheel, and an old serve lacks the _meta reader
    # this test exists to prove.
    import precis as _precis_pkg
    from asa_bot.config import PrecisConfig

    src_root = str(Path(_precis_pkg.__file__).resolve().parent.parent)
    cfg = PrecisConfig(
        command=[sys.executable, "-m", "precis", "serve"],
        database_url=dsn,
        # cfg.env beats setdefault — an ambient PRECIS_DATABASE_URL in the
        # test container's env must not redirect this serve at another DB.
        env={"PRECIS_DATABASE_URL": dsn, "PYTHONPATH": src_root},
        request_timeout_seconds=60.0,
    )
    client = PrecisClient(cfg)

    async def drive() -> list[str]:
        results: list[str] = []
        await client.start()
        try:
            bind_run(agentlog_id=log_id)
            try:
                results.append(
                    await client.call_tool(
                        "put",
                        {
                            "kind": "memory",
                            "text": f"body-{tag}",
                            "title": f"stamp-bound-{tag}",
                        },
                    )
                )
                # Explicit kwargs ride the same channel and can differ.
                results.append(
                    await client.call_tool(
                        "put",
                        {
                            "kind": "memory",
                            "text": f"body-{tag}",
                            "title": f"stamp-explicit-{tag}",
                        },
                        agentlog_id=other_log_id,
                        source="asa_bot",
                    )
                )
                # Forged ids: nonexistent and non-agentlog — the serve
                # drops them, so the rows stamp NULL agentlog_id.
                results.append(
                    await client.call_tool(
                        "put",
                        {
                            "kind": "memory",
                            "text": f"body-{tag}",
                            "title": f"stamp-forged-nonexistent-{tag}",
                        },
                        agentlog_id=10**12,
                        source="asa_bot",
                    )
                )
                results.append(
                    await client.call_tool(
                        "put",
                        {
                            "kind": "memory",
                            "text": f"body-{tag}",
                            "title": f"stamp-forged-ref-{tag}",
                        },
                        agentlog_id=int(not_a_log.id),
                        source="asa_bot",
                    )
                )
            finally:
                bind_run()
            # Unbound call: spawn env still stamps source, no agentlog.
            results.append(
                await client.call_tool(
                    "put",
                    {
                        "kind": "memory",
                        "text": f"body-{tag}",
                        "title": f"stamp-unbound-{tag}",
                    },
                )
            )
        finally:
            await client.stop()
        return results

    with store.pool.connection() as conn:
        marker = int(
            conn.execute("SELECT coalesce(max(call_id), 0) FROM tool_calls").fetchone()[
                0
            ]
        )
    results = asyncio.run(drive())
    assert not any(r.startswith("[error") for r in results), results

    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT agentlog_id, source, client FROM tool_calls "
            "WHERE call_id > %s AND verb = 'put' ORDER BY call_id",
            (marker,),
        ).fetchall()
    # bound → minted log id; explicit → the other id; both forged ids →
    # NULL (dropped by the serve's validation); unbound → NULL id,
    # source from the spawn env; all five asa-bot/0.1.0.
    assert rows == [
        (log_id, "asa_bot", "asa-bot/0.1.0"),
        (other_log_id, "asa_bot", "asa-bot/0.1.0"),
        (None, "asa_bot", "asa-bot/0.1.0"),
        (None, "asa_bot", "asa-bot/0.1.0"),
        (None, "asa_bot", "asa-bot/0.1.0"),
    ], rows
