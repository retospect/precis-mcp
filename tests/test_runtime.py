"""Runtime dispatcher — verb routing, error rendering, hint integration."""

from __future__ import annotations

import pytest

from precis.hints import Hint, HintBus
from precis.runtime import PrecisRuntime


@pytest.mark.asyncio
async def test_calc_through_dispatch(runtime: PrecisRuntime) -> None:
    out = await runtime.dispatch("get", {"kind": "calc", "id": "2+3*4"})
    assert "14" in out


@pytest.mark.asyncio
async def test_unknown_verb(runtime: PrecisRuntime) -> None:
    out = await runtime.dispatch("frobnicate", {})
    assert "[error:BadInput]" in out
    assert "options:" in out


@pytest.mark.asyncio
async def test_missing_kind(runtime: PrecisRuntime) -> None:
    out = await runtime.dispatch("get", {})
    assert "[error:BadInput]" in out
    assert "missing kind" in out


@pytest.mark.asyncio
async def test_unknown_kind(runtime: PrecisRuntime) -> None:
    out = await runtime.dispatch("get", {"kind": "nope"})
    assert "[error:NotFound]" in out
    assert "next:" in out


@pytest.mark.asyncio
async def test_unsupported_verb_for_kind(runtime: PrecisRuntime) -> None:
    out = await runtime.dispatch(
        "put", {"kind": "calc", "mode": "replace", "text": "x"}
    )
    assert "[error:Unsupported]" in out
    assert "calc does not support put" in out


@pytest.mark.asyncio
async def test_calc_bad_input_renders(runtime: PrecisRuntime) -> None:
    out = await runtime.dispatch("get", {"kind": "calc", "id": "@@@"})
    assert "[error:BadInput]" in out
    assert "next:" in out


@pytest.mark.asyncio
async def test_hints_appended_to_response(runtime: PrecisRuntime) -> None:
    """Verify hints emitted during a verb call land in the rendered output."""

    # Wrap calc.get to emit a hint mid-call
    original = runtime.registry.get("calc").get

    async def wrapped(**kw):  # type: ignore[no-untyped-def]
        runtime.hints.emit(Hint("calc tip", topic="test.tip"))
        return await original(**kw)

    runtime.registry.get("calc").get = wrapped  # type: ignore[method-assign]
    try:
        out = await runtime.dispatch("get", {"kind": "calc", "id": "1+1"})
    finally:
        runtime.registry.get("calc").get = original  # type: ignore[method-assign]

    assert "2" in out
    assert "[tip] calc tip" in out


@pytest.mark.asyncio
async def test_search_without_kind_returns_phase1_stub(
    runtime: PrecisRuntime,
) -> None:
    out = await runtime.dispatch("search", {"q": "anything"})
    assert "not yet implemented" in out


def test_build_runtime() -> None:
    from precis.runtime import build_runtime

    rt = build_runtime()
    assert "calc" in rt.registry
    assert isinstance(rt.hints, HintBus)
