"""Stateless boots record ``store required`` verdicts for store-backed kinds.

gr779330: ``precis eval "get(kind='memory', id='me779065')"`` on a host
with no store used to answer ``[error:NotFound] unknown kind: memory``
with a precis-get-help pointer — the kind exists in the build; the store
is what is missing. ``boot(store=None)`` now records a deferred
``store required`` verdict for every kind in the store-backed boot
section, so dispatch raises ``Unsupported`` naming the missing store
and the asa ping can recognise the degraded signature.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from precis.config import PrecisConfig
from precis.dispatch import _STORE_DEFERRED_KINDS, boot
from precis.kind_gate import Loadability, parse_disabled, parse_disabled_reasons
from precis.runtime import PrecisRuntime
from precis.server import _kinds_unavailable_line
from precis.store import Store

#: Kinds the stateless section of ``boot`` always records a verdict for,
#: loaded or not. Kept in the test (not imported from dispatch) so the
#: equality ratchet stays an independent cross-check of the boot layout.
_STATELESS_KINDS = frozenset({"calc", "time", "handle", "provenance", "python", "md"})


def _assert_verdicts(hub_loadabilities: dict[str, Loadability]) -> None:
    for kind in _STORE_DEFERRED_KINDS:
        verdict = hub_loadabilities.get(kind)
        assert verdict is not None, f"no verdict recorded for {kind!r}"
        assert verdict.loaded is False, kind
        assert verdict.reason == "store required", (
            f"{kind}: {verdict.reason!r} — the deferred verdict must name "
            "the store, not a downstream gate"
        )


def test_tuple_is_sorted_unique_and_disjoint() -> None:
    assert list(_STORE_DEFERRED_KINDS) == sorted(_STORE_DEFERRED_KINDS)
    assert len(set(_STORE_DEFERRED_KINDS)) == len(_STORE_DEFERRED_KINDS)
    assert set(_STORE_DEFERRED_KINDS).isdisjoint(_STATELESS_KINDS)


def test_stateless_boot_records_store_required_verdicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Compare core verdicts from both boot paths. Store-backed boot records
    # verdicts even when optional dependencies keep a kind from loading.
    monkeypatch.setattr("precis.dispatch._load_plugins", Mock())
    store = Mock(spec=Store)
    store.embedding_dim.return_value = 3
    backed = boot(store=store, boot_writes=False)
    hub = boot(store=None)
    _assert_verdicts(hub.loadabilities)
    assert set(hub.kinds).isdisjoint(_STORE_DEFERRED_KINDS)
    assert set(hub.loadabilities) == set(backed.loadabilities)


def test_stateless_boot_preserves_operator_prohibitions() -> None:
    disabled = "memory:use the approved deployment,skill"
    hub = boot(
        store=None,
        kinds_disabled=parse_disabled(disabled),
        kinds_disabled_reasons=parse_disabled_reasons(disabled),
    )
    runtime = PrecisRuntime(config=PrecisConfig(), hub=hub)
    assert hub.loadabilities["skill"].reason == "prohibited"
    out = runtime.dispatch("get", {"kind": "memory", "id": "me779065"})
    assert "[error:Unsupported]" in out
    assert "use the approved deployment" in out
    assert "PRECIS_DATABASE_URL" not in out
    banner = _kinds_unavailable_line(runtime)
    assert "memory (use the approved deployment)" in banner
    assert "store required" not in banner


def test_stateless_dispatch_get_names_the_missing_store(
    runtime_stateless: PrecisRuntime,
) -> None:
    out = runtime_stateless.dispatch("get", {"kind": "memory", "id": "me779065"})
    assert "[error:Unsupported]" in out, out
    assert "store required" in out, out
    assert "unknown kind" not in out, out
    assert "PRECIS_DATABASE_URL" in out, out


def test_stateless_dispatch_search_names_the_missing_store(
    runtime_stateless: PrecisRuntime,
) -> None:
    out = runtime_stateless.dispatch("search", {"kind": "skill", "q": "memory"})
    assert "[error:Unsupported]" in out, out
    assert "store required" in out, out
    assert "unknown kind" not in out, out
