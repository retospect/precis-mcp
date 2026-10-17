"""M3 (gr494831): every unsupported (verb, kind) pair refuses with
``Unsupported`` plus a next step and the supported-verb options.

Enumerated from ``KindSpec.supports(verb)`` over the live hub, so a new
kind or a flipped ``supports_*`` flag is covered without editing this file.
"""

from __future__ import annotations

import pytest

from precis.runtime import PrecisRuntime
from precis.runtime.dispatch import _VERBS


@pytest.fixture
def runtime(runtime_with_store: PrecisRuntime) -> PrecisRuntime:
    """Store-backed: the stateless runtime drops every store kind and, with
    them, the options a not-found kind lists."""
    return runtime_with_store


def _unsupported_pairs(runtime: PrecisRuntime) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for kind in sorted(runtime.hub.kinds):
        handler = runtime.hub.handler_for(kind)
        if handler is None:
            continue
        out += [(v, kind) for v in _VERBS if not handler.spec.supports(v)]
    return out


def test_matrix_is_not_empty(runtime: PrecisRuntime) -> None:
    assert len(_unsupported_pairs(runtime)) > 50


def test_every_unsupported_pair_refuses_with_a_hint(runtime: PrecisRuntime) -> None:
    bad: list[str] = []
    for verb, kind in _unsupported_pairs(runtime):
        out = runtime.dispatch(verb, {"kind": kind, "id": "x"})
        head = out.splitlines()[0] if out else ""
        if not head.startswith("[error:Unsupported]"):
            bad.append(f"{verb}/{kind}: head={head[:70]!r}")
            continue
        if "next:" not in out:
            bad.append(f"{verb}/{kind}: no next:")
        if "options:" not in out:
            bad.append(f"{verb}/{kind}: no options:")
    assert not bad, "unsupported pairs without Unsupported+hint:\n" + "\n".join(bad)


@pytest.mark.parametrize("verb", list(_VERBS))
def test_unknown_kind_is_not_found_with_options(
    runtime: PrecisRuntime, verb: str
) -> None:
    out = runtime.dispatch(verb, {"kind": "zz_no_such_kind", "id": "x"})
    assert out.startswith("[error:NotFound]")
    assert "options:" in out and "next:" in out
