"""Handle-coded fetch caches: wikipedia / semanticscholar / chem (gr702974).

Their reads persist a refs row + chunks, so they carry universal handles
(``wp`` / ``ss`` / ``ch``): a note can cite them, the handle routes by ref
id (a wikipedia slug is query-derived, so a slug round-trip would read as a
fresh query), and ``view='links'`` shows the inbound edges.
"""

from __future__ import annotations

import ast
import re

import pytest

from precis.handlers._cache_base import _cite_pointer
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.store.types import ChunkInsert, Ref
from precis.utils import handle_registry
from precis.utils.mentions import LINKIFY_KINDS, resolve_link_targets
from precis_web.routes.refs import _CACHE_BACKED_KINDS, _REFS_BROWSABLE_KINDS
from tests._refs import insert_gripe

_KINDS = {"wikipedia": "wp", "semanticscholar": "ss", "chem": "ch"}


@pytest.mark.parametrize(("kind", "code"), sorted(_KINDS.items()))
def test_kind_is_handle_coded_linkable_and_browsable(kind: str, code: str) -> None:
    assert handle_registry.format_handle(kind, 7) == f"{code}7"
    assert kind not in handle_registry.CODELESS_KINDS
    assert kind in LINKIFY_KINDS
    assert kind in _REFS_BROWSABLE_KINDS
    # The detail page must never fire a fetch for a browsable cache kind.
    assert kind in _CACHE_BACKED_KINDS


def test_cite_pointer_prints_the_handle() -> None:
    line = _cite_pointer("wikipedia", 999)
    assert line is not None
    assert "`[wp999]`" in line


def _seed_wikipedia(store: Store, *, query: str = "crispr gene editing") -> Ref:
    ref, _cache = store.put_cache_entry(
        kind="wikipedia",
        slug="crispr-gene-editing",
        title="CRISPR gene editing",
        body_blocks=[
            ChunkInsert(ord=0, text="CRISPR gene editing is a genetic method."),
            ChunkInsert(ord=1, text="It was adapted from a bacterial immune system."),
        ],
        provider="wikipedia",
        request_hash="h-" + query,
        ttl_seconds=None,
        cache_meta={"query": query},
    )
    return ref


@pytest.fixture
def no_fetch(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = runtime_with_store.hub.handler_for("wikipedia")
    assert handler is not None

    def _boom(key: str) -> None:
        raise AssertionError(f"a stored-row read fired a fetch for {key!r}")

    monkeypatch.setattr(handler, "_fetch", _boom)


@pytest.mark.usefixtures("no_fetch")
def test_handle_get_reads_the_stored_ref_without_fetching(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref = _seed_wikipedia(store)
    out = runtime_with_store.dispatch("get", {"id": f"wp{ref.id}"})
    assert "bacterial immune system" in out
    assert f"cite as `[wp{ref.id}]`" in out
    # The handle's chunk selector routes by ref id too.
    out = runtime_with_store.dispatch("get", {"id": f"wp{ref.id}~1"})
    assert "bacterial immune system" in out
    assert "genetic method" not in out


@pytest.mark.usefixtures("no_fetch")
def test_bare_ref_id_reads_before_the_query_path(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref = _seed_wikipedia(store)
    out = runtime_with_store.dispatch("get", {"kind": "wikipedia", "id": str(ref.id)})
    assert "genetic method" in out


@pytest.mark.usefixtures("no_fetch")
def test_links_view_shows_inbound_mentions(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref = _seed_wikipedia(store)
    note = store.insert_ref(kind="memory", slug=None, title="gene editing note")
    # A note's ``[wp<id>]`` and ``wikipedia:<id>`` both resolve to this ref.
    for token in (f"[wp{ref.id}]", f"wikipedia:{ref.id}"):
        targets = resolve_link_targets(store, f"background in {token}")
        assert [t.dst_ref_id for t in targets] == [ref.id], token
    store.add_link(src_ref_id=note.id, dst_ref_id=ref.id)
    out = runtime_with_store.dispatch("get", {"id": f"wp{ref.id}", "view": "links"})
    assert f"me{note.id}" in out
    out = runtime_with_store.dispatch(
        "get", {"kind": "wikipedia", "id": ref.slug, "view": "links"}
    )
    assert f"me{note.id}" in out


def test_links_view_on_uncached_ref_never_fetches(
    runtime_with_store: PrecisRuntime,
) -> None:
    out = runtime_with_store.dispatch(
        "get", {"kind": "chem", "id": "aspirin", "view": "links"}
    )
    assert "NotFound" in out
    assert "never fetches" in out


@pytest.mark.usefixtures("no_fetch")
def test_links_pagination_and_filters_through_dispatch(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """Catch dispatch rejecting the shared cache links continuation arguments."""
    ref = _seed_wikipedia(store)
    notes = [
        store.insert_ref(kind="memory", slug=None, title=f"source note {i}")
        for i in range(3)
    ]
    for note in notes:
        store.add_link(src_ref_id=note.id, dst_ref_id=ref.id, relation="cites")
    other = insert_gripe(store, "excluded kind")
    store.add_link(src_ref_id=other.id, dst_ref_id=ref.id, relation="cites")
    store.add_link(src_ref_id=notes[0].id, dst_ref_id=ref.id, relation="supports")

    args = {"limit": 1, "rel": "cites", "of_kind": "memory"}
    for i, note in enumerate(notes):
        # Direct dispatch spells the args= dict as ``__extras__`` (the MCP
        # wrapper is what renames the wire ``args`` key).
        out = runtime_with_store.dispatch(
            "get", {"id": f"wp{ref.id}", "view": "links", "__extras__": args}
        )
        assert f"me{note.id}" in out
        assert all(f"me{n.id}" not in out for n in notes if n.id != note.id)
        assert f"gr{other.id}" not in out
        assert "supported by" not in out
        assert f"shown {i + 1} of 3" in out
        if i < len(notes) - 1:
            continuation = re.search(r"args=(\{[^\n]+\})\)", out)
            assert continuation is not None, out
            args = ast.literal_eval(continuation.group(1))
        else:
            assert "complete" in out
