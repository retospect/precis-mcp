"""gr693011: an undeclared wire kwarg refuses loudly, never drops silently.

``link(..., dry_run=True)`` used to vanish inside FastMCP's arg model
(``extra='ignore'``) and the edge wrote for real. Both profiles now
keep extras through validation and *bind* the call against the verb's
fixed signature before invoking:

- typed profile — :func:`precis.server._strict_wire_verb` wraps every
  non-slim verb; a bind failure renders as ``[error:BadInput]``;
- command profile — ``precis(command=...)`` binds the parsed kwargs the
  same way, so the refusal is byte-identical. Binding first also means
  a ``TypeError`` raised *inside* the verb propagates as the fault it
  is instead of being misreported as BadInput.

Every refusal renders through ``_verb_typeerror_result`` →
``tools.core._get_runtime()``, which would lazily build the real
configured runtime when none is mounted — so each test mounts a
stateless (or test-store) runtime onto ``tools_core._runtime`` and
restores the previous value afterwards.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from mcp.types import CallToolResult

from precis import server
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.tools import TOOL_REGISTRY
from precis.tools import core as tools_core


@pytest.fixture
def mounted_runtime(runtime: PrecisRuntime) -> Iterator[PrecisRuntime]:
    """Stateless runtime on the tool path; refusal rendering must never
    fall through to ``_get_runtime``'s lazy real-runtime build."""
    saved = tools_core._runtime
    tools_core._runtime = runtime
    try:
        yield runtime
    finally:
        tools_core._runtime = saved


@pytest.fixture
def wire_runtime(runtime_with_store: PrecisRuntime) -> Iterator[PrecisRuntime]:
    """Test-store runtime on the tool path. The ``precis`` command tool
    is only registered under ``PRECIS_MCP_PROFILE=command``, so the wire
    call goes through ``server.precis`` directly; mounting here keeps
    the refusal's render (and any regressed real write) on the test DB."""
    saved = tools_core._runtime
    tools_core._runtime = runtime_with_store
    try:
        yield runtime_with_store
    finally:
        tools_core._runtime = saved


def _body(out: Any) -> str:
    if isinstance(out, CallToolResult):
        return "".join(getattr(b, "text", "") for b in out.content)
    return str(out)


def _sentinel(exc: BaseException) -> Any:
    """A registry stand-in with the verb's fixed signature that must
    never be reached."""

    def fn(
        kind: str | None = None,
        id: str | int | list | None = None,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        element: str | None = None,
        meta: dict[str, Any] | None = None,
        reason: str | None = None,
        note: str | None = None,
        ctx: Any = None,
    ) -> str:
        raise exc

    return fn


def _swap_verb(monkeypatch: pytest.MonkeyPatch, verb: str, exc: BaseException) -> None:
    fn = _sentinel(exc)
    monkeypatch.setitem(TOOL_REGISTRY, verb, {**TOOL_REGISTRY[verb], "func": fn})


def test_unknown_kwarg_is_refused_before_the_verb_runs(
    mounted_runtime: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``zzz_bogus=`` never reaches dispatch: the bind fails first and
    the envelope names the bad kwarg."""
    _swap_verb(monkeypatch, "link", AssertionError("link dispatch reached"))
    out = server.precis(command="link(kind='todo', id=1, target='todo:2', zzz_bogus=1)")
    body = _body(out)
    assert "[error:BadInput]" in body, body
    assert "zzz_bogus" in body


def test_link_dry_run_over_the_wire_is_refused(
    wire_runtime: PrecisRuntime, store: Store
) -> None:
    """gr693011's exact call on the command-profile surface: ``dry_run=``
    is not a link kwarg — refuse, write no edge."""
    src = store.insert_ref(kind="todo", slug=None, title="zzq dry-run src")
    dst = store.insert_ref(kind="todo", slug=None, title="zzq dry-run dst")
    out = server.precis(
        command=(
            f"link(kind='todo', id={src.id}, "
            f"target='todo:{dst.id}', rel='plan-of', dry_run=True)"
        )
    )
    body = _body(out)
    assert "[error:BadInput]" in body, body
    assert "dry_run" in body
    assert store.links_for(src.id) == []


def test_internal_typeerror_is_not_misreported_as_badinput(
    mounted_runtime: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bind-first (review td701893): a ``TypeError`` raised *inside* the
    verb is a real fault and must propagate, not render as BadInput."""
    _swap_verb(monkeypatch, "link", TypeError("boom inside link"))
    with pytest.raises(TypeError, match="boom inside link"):
        server.precis(command="link(kind='todo', id=1, target='todo:2')")


def test_strict_wrapper_passes_through_internal_typeerror() -> None:
    """The typed profile's wrapper binds too: same propagation rule."""

    def verb(kind: str | None = None) -> str:
        raise TypeError("boom inside verb")

    strict = server._strict_wire_verb("link", verb)
    with pytest.raises(TypeError, match="boom inside verb"):
        strict(kind="todo")


def test_strict_wrapper_refuses_unknown_kwarg_with_alias_hint(
    mounted_runtime: PrecisRuntime,
) -> None:
    """Bind failure still renders the BadInput envelope (and the
    ``to=`` → ``target=`` hint survives the wrapper, gr338670)."""

    def verb(
        kind: str | None = None,
        id: str | int | None = None,
        target: str | None = None,
        ctx: Any = None,
    ) -> str:
        raise AssertionError("verb reached")

    strict = server._strict_wire_verb("link", verb)
    with pytest.raises(AssertionError):
        strict(kind="todo", id=1)  # valid kwargs reach the verb
    out = strict(kind="todo", id=1, to="todo:2")
    body = _body(out)
    assert "[error:BadInput]" in body, body
    assert "target=" in body
