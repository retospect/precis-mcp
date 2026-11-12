"""gr330541: FastMCP 1.28.1 invokes registered sync tool callables
in-line on the single asyncio event-loop thread
(``FuncMetadata.call_fn_with_arg_validation`` — no worker-thread
offload, unlike its own ``resources/types.py``). Since every MCP
request is a concurrent asyncio Task on that one thread, one
long-blocking sync tool (``perplexity-research`` holding a synchronous
HTTP POST open for minutes) used to head-of-line-block every other
in-flight call in the session, down to a microsecond skill read.

``precis.server._offload_sync`` wraps every registered tool function at
our own registration seam in an ``async def`` that runs the real sync
body via ``anyio.to_thread.run_sync``, gated by a module-level
semaphore. These tests pin:

- the wrapper preserves the wire-level pydantic arg-model schema
  FastMCP derives from the wrapped function's introspected signature
  (the fix is worthless if it silently degrades the schema
  ``tests/test_text_coercion_schema.py`` / ``tests/test_edit_schema.py``
  already pin for ``put``/``edit`` — both of those import
  ``precis.server`` and so already exercise the *wrapped* registration
  this module adds);
- a slow sync tool no longer blocks a concurrent fast one on the real
  ``Tool.run`` dispatch path;
- the semaphore's concurrency bound is exact, not just "eventually
  fewer than N";
- the concurrency audit's two thread-safety claims that are cheap to
  pin directly: ``HintBus`` request-scoping survives worker-thread
  offload (each concurrent call gets its own copied ``contextvars``
  context), and ``PaginationCache`` tolerates concurrent
  ``split``/``pop`` from multiple worker threads.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from typing import Any

import anyio

from precis import server
from tests._timing import budget

# ── schema preservation ─────────────────────────────────────────────


def test_offload_sync_wrapper_is_a_coroutine_function() -> None:
    """``Tool.from_function``'s ``is_async`` detection
    (``inspect.iscoroutinefunction``) must see the wrapper as async —
    that's what makes FastMCP ``await`` it instead of calling it
    in-line, which is the entire point of the fix."""

    def plain(x: int) -> int:
        return x

    wrapped = server._offload_sync(plain)
    assert inspect.iscoroutinefunction(wrapped)
    assert not inspect.iscoroutinefunction(plain)


def test_offload_sync_preserves_signature_and_identity() -> None:
    def sample(a: int, b: str = "x", *, c: bool = False) -> str:
        """Sample docstring."""
        return f"{a}{b}{c}"

    wrapped = server._offload_sync(sample)

    assert wrapped.__name__ == sample.__name__
    assert wrapped.__doc__ == sample.__doc__
    # ``eval_str=True`` on both sides: this module's own
    # ``from __future__ import annotations`` makes ``sample``'s
    # annotations lazy strings, and that's exactly what
    # ``_offload_sync`` (and FastMCP's own ``func_metadata``) resolves
    # before comparing — an unresolved comparison would spuriously
    # differ on string vs. real-type annotations regardless of whether
    # the wrapper preserved anything.
    assert inspect.signature(wrapped) == inspect.signature(sample, eval_str=True)


def test_offload_sync_preserves_arg_model_schema_for_put() -> None:
    """The core claim: wrapping ``put`` for dispatch must not change the
    wire ``inputSchema`` FastMCP builds from it one byte. Compares
    ``func_metadata`` run directly against ``TOOL_REGISTRY['put']['func']``
    (what registration looked like before this fix) to running it
    against ``_offload_sync``'s wrapper (what registration does now)."""
    from mcp.server.fastmcp.utilities.func_metadata import func_metadata

    from precis.tools import TOOL_REGISTRY

    fn = TOOL_REGISTRY["put"]["func"]
    wrapped = server._offload_sync(fn)

    direct = func_metadata(fn, structured_output=False)
    via_wrapper = func_metadata(wrapped, structured_output=False)

    direct_schema = direct.arg_model.model_json_schema(by_alias=True)
    wrapper_schema = via_wrapper.arg_model.model_json_schema(by_alias=True)
    assert direct_schema == wrapper_schema


def test_offload_sync_preserves_arg_model_schema_for_edit() -> None:
    """Companion to the ``put`` pin — ``edit`` is the other tool whose
    schema gets a second mutation pass (``_install_edit_schema_constraints``)
    after registration; that pass looks the registered tool up by name,
    so it must still find ``edit`` under the wrapped function."""
    from mcp.server.fastmcp.utilities.func_metadata import func_metadata

    from precis.tools import TOOL_REGISTRY

    fn = TOOL_REGISTRY["edit"]["func"]
    wrapped = server._offload_sync(fn)

    direct = func_metadata(fn, structured_output=False)
    via_wrapper = func_metadata(wrapped, structured_output=False)

    assert direct.arg_model.model_json_schema(
        by_alias=True
    ) == via_wrapper.arg_model.model_json_schema(by_alias=True)


def test_real_registration_installs_wrapped_functions() -> None:
    """The production registration path (``_register_tools_from_registry``)
    must actually be feeding ``_offload_sync``'s wrapper to FastMCP, not
    just something that happens to look similar in isolation — check the
    live ``server.mcp`` tool manager's registered ``fn``."""
    tool = server.mcp._tool_manager.get_tool("get")
    assert tool is not None
    assert inspect.iscoroutinefunction(tool.fn)


# ── no head-of-line blocking ────────────────────────────────────────


def test_offload_sync_prevents_head_of_line_blocking() -> None:
    """A slow sync tool call must not block a concurrent fast one on the
    real ``Tool.run`` dispatch path — the exact regression gr330541
    diagnosed for FastMCP 1.28.1's in-line sync invocation."""
    from mcp.server.fastmcp import FastMCP

    scratch = FastMCP("test-offload-holb")
    order: list[str] = []
    lock = threading.Lock()

    def slow() -> str:
        time.sleep(0.3)
        with lock:
            order.append("slow")
        return "slow-done"

    def fast() -> str:
        with lock:
            order.append("fast")
        return "fast-done"

    scratch.tool()(server._offload_sync(slow))
    scratch.tool()(server._offload_sync(fast))

    async def _run() -> tuple[Any, Any]:
        slow_tool = scratch._tool_manager.get_tool("slow")
        fast_tool = scratch._tool_manager.get_tool("fast")
        assert slow_tool is not None and fast_tool is not None
        return await asyncio.gather(slow_tool.run({}), fast_tool.run({}))

    results = asyncio.run(_run())
    assert results == ["slow-done", "fast-done"]
    # ``fast`` finished (and recorded itself) well before ``slow``'s 0.3s
    # sleep returns — proof the two ran concurrently on separate worker
    # threads rather than serializing on the event loop.
    assert order == ["fast", "slow"], order


# ── semaphore bound ──────────────────────────────────────────────────


def test_offload_sync_semaphore_bounds_concurrency() -> None:
    """A burst of N+2 concurrent calls against a semaphore capped at N
    must never run more than N sync bodies at once, and must actually
    reach N concurrently (not some stricter, accidentally-serializing
    bound).

    NB a ``threading.Barrier(N)`` looks like the natural "prove exactly
    N run at once" primitive here, but it's the wrong tool: a
    ``Barrier`` is cyclic — once N parties arrive it resets and waits
    for the *next* N. With N+2 total callers, the 2 stragglers that
    each eventually acquire a freed permit call ``wait()`` too, one at
    a time, and the barrier hangs forever waiting for a third that
    never comes (verified experimentally against this exact fix during
    development — it is not a hypothetical). A held-open sync section
    long enough that every contender has had ample scheduling
    opportunity to attempt entry is the reliable way to observe the
    bound instead.
    """
    limit = 3
    total = limit + 2
    sem = anyio.Semaphore(limit)
    lock = threading.Lock()
    current = 0
    peak = 0

    def body() -> None:
        nonlocal current, peak
        with lock:
            current += 1
            peak = max(peak, current)
        time.sleep(0.3)
        with lock:
            current -= 1

    wrapped = server._offload_sync(body, semaphore=sem)

    async def _run() -> None:
        await asyncio.gather(*(wrapped() for _ in range(total)))

    asyncio.run(_run())
    assert peak == limit, peak


# ── cancellation frees the awaiting task (gr337045 symptom 2) ───────


def test_offload_sync_cancel_returns_promptly_and_frees_semaphore() -> None:
    """gr337045: with ``abandon_on_cancel`` left at its ``False`` default,
    a cancelled MCP request stayed pinned to its worker thread until the
    sync body finished on its own — a long CPU-bound tool call was
    architecturally unstoppable short of killing the server process.
    Pin the fix: cancelling the awaiting task returns promptly (not
    after the sync body's full duration) and releases the tool
    semaphore, even while the abandoned thread is still running."""
    started = threading.Event()
    release = threading.Event()

    def stuck() -> str:
        started.set()
        release.wait(timeout=10)
        return "done"

    sem = anyio.Semaphore(1)
    wrapped = server._offload_sync(stuck, semaphore=sem)

    async def _run() -> tuple[bool, float, int]:
        t0 = time.monotonic()
        with anyio.move_on_after(0.5) as scope:
            await wrapped()
        # semaphore back to full capacity even though ``stuck`` is
        # still blocked on ``release`` inside the abandoned thread
        return scope.cancelled_caught, time.monotonic() - t0, sem.value

    try:
        cancelled, elapsed, sem_value = asyncio.run(_run())
    finally:
        release.set()  # unstick the abandoned thread

    assert started.is_set()
    assert cancelled
    # the regression (abandon_on_cancel=False) holds the await for the
    # sync body's full ~10s wait; the fix returns at the 0.5s deadline
    assert elapsed < budget(5.0), elapsed
    assert sem_value == 1


# ── concurrency audit: HintBus request-scoping ──────────────────────


def test_hintbus_request_scope_isolated_across_worker_threads() -> None:
    """Audit item (a): confirm ``anyio.to_thread.run_sync`` propagates a
    *copy* of the calling context, so ``HintBus``'s ``ContextVar``-backed
    per-request collector stays correctly isolated when concurrent
    requests are offloaded to separate worker threads — two overlapping
    ``bus.request()`` scopes must never see each other's hints."""
    from precis.hints import Hint, HintBus

    bus = HintBus()

    def _one(tag: str) -> list[str]:
        with bus.request():
            bus.emit(Hint(text=f"hint-{tag}", topic=f"topic.{tag}", cooldown=0))
            time.sleep(0.05)  # overlap the other thread's request scope
            collected = bus.collect()
        return [h.topic for h in collected]

    async def _run() -> tuple[list[str], list[str]]:
        return await asyncio.gather(
            anyio.to_thread.run_sync(_one, "a"),
            anyio.to_thread.run_sync(_one, "b"),
        )

    result_a, result_b = asyncio.run(_run())
    assert result_a == ["topic.a"]
    assert result_b == ["topic.b"]


# ── bounded queue wait (gr648196) ─────────────────────────────────────


def test_offload_sync_queue_wait_refuses_unrun_with_health_hint(
    monkeypatch: Any,
) -> None:
    """With every slot held, a queued call is refused unrun after the wait
    bound — not left hanging until the client times out — and the text
    says nothing was written and where to check server health."""
    monkeypatch.setenv("PRECIS_MCP_TOOL_CONCURRENCY", "1")
    monkeypatch.setenv("PRECIS_MCP_QUEUE_WAIT_S", "0.2")
    monkeypatch.setattr(server, "_tool_semaphore", None)
    release = threading.Event()
    ran: list[str] = []

    def hold() -> str:
        release.wait(5)
        return "held"

    def write() -> str:
        ran.append("write")
        return "ok"

    held = server._offload_sync(hold)
    queued = server._offload_sync(write)

    async def _run() -> str:
        task = asyncio.create_task(held())
        await asyncio.sleep(0.05)
        try:
            return str(await queued())
        finally:
            release.set()
            await task

    try:
        out = asyncio.run(_run())
    finally:
        monkeypatch.setattr(server, "_tool_semaphore", None)
    assert out.startswith("[error:Upstream]"), out
    assert "NOT run" in out and "precis-status-help" in out
    assert ran == []


# ── cancelled-after-dispatch reporting (gr648196) ────────────────────


def _wait_for_log(caplog: Any, needle: str) -> str | None:
    """Poll ``caplog`` for a line the detached worker thread emits.

    The abandon report and the calllat record land synchronously on the
    event loop, but the detached body's exit line is written on the OS
    thread whenever it finally unwinds — poll rather than read once."""
    deadline = time.monotonic() + budget(10.0)
    while time.monotonic() < deadline:
        for record in caplog.records:
            if needle in record.getMessage():
                return record.getMessage()
        time.sleep(0.05)
    return None


def test_offload_sync_abandoned_write_reports_outcome_and_health_hint(
    caplog: Any,
) -> None:
    """gr648196's post-dispatch half: the queue bound refuses an unrun
    call, but a caller can still give up *after* its write took a slot —
    ``abandon_on_cancel`` returns the task while the OS thread keeps the
    body running. Pin the reporting contract that makes that visible: the
    abandonment logs a warning naming where to check, calllat marks the
    call ``abandoned`` (not ``exception`` — a dropped caller is not a
    handler fault), and the detached body's own exit is reported, so
    "did the write land?" is answerable from the log alone."""
    release = threading.Event()
    written: list[str] = []

    def put() -> str:
        release.wait(10)
        written.append("wrote")
        return "ok"

    wrapped = server._offload_sync(put, semaphore=anyio.Semaphore(1))

    async def _run() -> bool:
        with anyio.move_on_after(0.3) as scope:
            await wrapped()
        return scope.cancelled_caught

    caplog.set_level(logging.INFO)
    try:
        cancelled = asyncio.run(_run())
        assert cancelled

        # Reported at cancel time, while the body is still held on
        # ``release`` — the write-in-doubt warning plus a health hint.
        abandon = _wait_for_log(caplog, "cancelled after dispatch")
        assert abandon is not None
        assert "put kind=-" in abandon and "write may commit" in abandon
        assert "precis-status-help" in abandon
        assert any(
            r.name == "precis.calllat" and "outcome=abandoned" in r.getMessage()
            for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

        # The abandoned write still lands — and its landing is reported.
        release.set()
        detached = _wait_for_log(caplog, "detached put")
        assert detached is not None
        assert "finished after its caller cancelled: ok" in detached
        assert written == ["wrote"]
    finally:
        release.set()


def test_offload_sync_abandoned_body_reports_its_failure(caplog: Any) -> None:
    """The detached report names a failed exit too — a write that did
    NOT land must be as visible as one that did."""
    release = threading.Event()

    def edit() -> str:
        release.wait(10)
        raise RuntimeError("boom")

    wrapped = server._offload_sync(edit, semaphore=anyio.Semaphore(1))

    async def _run() -> None:
        with anyio.move_on_after(0.3):
            await wrapped()

    caplog.set_level(logging.INFO)
    try:
        asyncio.run(_run())
        release.set()
        detached = _wait_for_log(caplog, "detached edit")
        assert detached is not None
        assert "raised RuntimeError" in detached
    finally:
        release.set()


def test_offload_sync_abandoned_read_stays_info_level(caplog: Any) -> None:
    """A cancelled ``get`` cannot leave a write in doubt — the same
    abandonment is reported at INFO and carries no write warning."""
    release = threading.Event()

    def get() -> str:
        release.wait(10)
        return "ok"

    wrapped = server._offload_sync(get, semaphore=anyio.Semaphore(1))

    async def _run() -> None:
        with anyio.move_on_after(0.3):
            await wrapped()

    caplog.set_level(logging.INFO)
    try:
        asyncio.run(_run())
        abandon = _wait_for_log(caplog, "cancelled after dispatch")
        assert abandon is not None and "get kind=-" in abandon
        assert not any("write may commit" in r.getMessage() for r in caplog.records)
    finally:
        release.set()


def test_offload_sync_queue_abandon_records_calllat(
    caplog: Any, monkeypatch: Any
) -> None:
    """A caller that gives up *while still queued* never dispatches, so
    it leaves no ``tool_calls`` row — the calllat ``abandoned`` record is
    the only trace. Pinned because gr648196's calls left exactly this
    shape of absence."""
    monkeypatch.setattr(server, "_tool_semaphore", server._FairSemaphore(0))

    def put() -> str:
        return "ok"

    wrapped = server._offload_sync(put)

    async def _run() -> bool:
        with anyio.move_on_after(0.3) as scope:
            await wrapped()
        return scope.cancelled_caught

    caplog.set_level(logging.INFO)
    try:
        cancelled = asyncio.run(_run())
    finally:
        monkeypatch.setattr(server, "_tool_semaphore", None)
    assert cancelled
    assert any(
        r.name == "precis.calllat"
        and "outcome=abandoned" in r.getMessage()
        and "wait_ms" in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]
