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
import threading
import time
from typing import Any

import anyio
import pytest

from precis import server


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


# ── concurrency audit: PaginationCache ───────────────────────────────


def test_pagination_cache_thread_safe_under_concurrent_split_pop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit item (c): ``PaginationCache`` already guards ``self._entries``
    with its own ``threading.Lock`` (verified by inspection); this
    exercises it under genuine concurrent worker-thread ``split``/``pop``
    calls (rather than just trusting the read) — every call must round-
    trip cleanly and the cache must end up empty.

    ``PRECIS_MAX_BODY_BYTES`` is pinned small so ``big_body`` overflows
    on the *first* split but its tail comfortably fits the cap on the
    second — one ``split`` + one ``pop`` per call fully drains it. A
    body sized to force ``pop``'s own recursive re-split (its tail
    still too big) would leave a second-generation cursor behind per
    call, which is a real behaviour of the cache (each ``more()`` page
    is one-cursor-at-a-time by design) and not what this test is
    checking — it's checking thread-safety, not pagination depth.
    """
    from precis._pagination import PaginationCache

    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "2000")
    cache = PaginationCache(max_cursors=256)
    big_body = "y" * 3000  # no delimiters: exercises the hard-split fallback

    def _split_and_pop(_i: int) -> bool:
        _head, cursor = cache.split(big_body)
        if cursor is None:
            return True  # fit in one page — nothing to pop
        tail = cache.pop(cursor)
        return tail is not None

    async def _run() -> list[bool]:
        return await asyncio.gather(
            *(anyio.to_thread.run_sync(_split_and_pop, i) for i in range(20))
        )

    results = asyncio.run(_run())
    assert all(results), results
    assert len(cache) == 0
