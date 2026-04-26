"""Runtime dispatcher — verb routing, error rendering, hint integration."""

from __future__ import annotations

from precis.hints import Hint, HintBus
from precis.runtime import PrecisRuntime


def test_calc_through_dispatch(runtime: PrecisRuntime) -> None:
    out = runtime.dispatch("get", {"kind": "calc", "id": "2+3*4"})
    assert "14" in out


def test_unknown_verb(runtime: PrecisRuntime) -> None:
    out = runtime.dispatch("frobnicate", {})
    assert "[error:BadInput]" in out
    assert "options:" in out


def test_missing_kind(runtime: PrecisRuntime) -> None:
    out = runtime.dispatch("get", {})
    assert "[error:BadInput]" in out
    assert "missing kind" in out


def test_unknown_kind(runtime: PrecisRuntime) -> None:
    out = runtime.dispatch("get", {"kind": "nope"})
    assert "[error:NotFound]" in out
    assert "next:" in out


def test_unsupported_verb_for_kind(runtime: PrecisRuntime) -> None:
    out = runtime.dispatch("put", {"kind": "calc", "mode": "replace", "text": "x"})
    assert "[error:Unsupported]" in out
    assert "calc does not support put" in out


def test_calc_bad_input_renders(runtime: PrecisRuntime) -> None:
    out = runtime.dispatch("get", {"kind": "calc", "id": "@@@"})
    assert "[error:BadInput]" in out
    assert "next:" in out


def test_hints_appended_to_response(runtime: PrecisRuntime) -> None:
    """Verify hints emitted during a verb call land in the rendered output."""

    # Wrap calc.get to emit a hint mid-call
    original = runtime.registry.get("calc").get

    def wrapped(**kw):  # type: ignore[no-untyped-def]
        runtime.hints.emit(Hint("calc tip", topic="test.tip"))
        return original(**kw)

    runtime.registry.get("calc").get = wrapped  # type: ignore[method-assign]
    try:
        out = runtime.dispatch("get", {"kind": "calc", "id": "1+1"})
    finally:
        runtime.registry.get("calc").get = original  # type: ignore[method-assign]

    assert "2" in out
    assert "[tip] calc tip" in out


def test_search_without_kind_returns_phase1_stub(
    runtime: PrecisRuntime,
) -> None:
    out = runtime.dispatch("search", {"q": "anything"})
    assert "not yet implemented" in out


def test_build_runtime_no_database() -> None:
    """Without PRECIS_DATABASE_URL set, build_runtime returns a
    stateless runtime (calc only, no store)."""
    import os

    from precis.runtime import build_runtime

    # Ensure the env var is unset for this test
    original = os.environ.pop("PRECIS_DATABASE_URL", None)
    try:
        rt = build_runtime()
        assert "calc" in rt.registry
        assert "memory" not in rt.registry
        assert rt.store is None
        assert isinstance(rt.hints, HintBus)
    finally:
        if original is not None:
            os.environ["PRECIS_DATABASE_URL"] = original
