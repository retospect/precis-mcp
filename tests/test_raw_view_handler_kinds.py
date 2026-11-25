"""Stored raw records on kinds outside the numeric-ref base."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.cad import CadHandler
from precis.handlers.cfp import CfpHandler
from precis.handlers.datasheet import DatasheetHandler
from precis.handlers.draft import DraftHandler
from precis.handlers.news import NewsHandler
from precis.handlers.paper import PaperHandler
from precis.handlers.pcb import PcbHandler
from precis.handlers.structure import StructureHandler
from precis.store import Store, Tag


@pytest.mark.parametrize(
    "handler_type",
    [
        pytest.param(PaperHandler, id="paper"),
        pytest.param(DraftHandler, id="draft"),
        pytest.param(CadHandler, id="cad"),
        pytest.param(StructureHandler, id="structure"),
        pytest.param(PcbHandler, id="pcb"),
        pytest.param(NewsHandler, id="news"),
        pytest.param(DatasheetHandler, id="datasheet"),
        pytest.param(CfpHandler, id="cfp"),
    ],
)
def test_raw_renders_stored_record(
    hub: Hub,
    store: Store,
    handler_type: type[
        PaperHandler
        | DraftHandler
        | CadHandler
        | StructureHandler
        | PcbHandler
        | NewsHandler
    ],
) -> None:
    handler = handler_type(hub=hub)
    kind = handler.spec.kind
    slug = "raw-view-fixture"
    title = "Raw view fixture"
    if kind == "news":
        ref, _ = store.put_cache_entry(
            kind=kind,
            slug=slug,
            title=title,
            provider="news",
            request_hash="raw-view-fixture",
            ttl_seconds=None,
            body_blocks=[],
            ref_meta={"probe": "x"},
        )
    else:
        ref = store.insert_ref(kind=kind, slug=slug, title=title, meta={"probe": "x"})
    store.add_tag(ref.id, Tag.open("raw-fixture-tag"))

    resp = handler.get(id=slug, view="raw")

    assert "raw" in handler.spec.views
    assert resp.result_count == 1
    assert resp.body.startswith(f"# raw {kind} {ref.public_id}\n")
    assert f"\ntitle:\n{title}\n" in resp.body
    assert "raw-fixture-tag" in resp.body
    assert '"probe": "x"' in resp.body


def test_paper_raw_elides_large_meta_value(hub: Hub, store: Store) -> None:
    value = "x" * 2001
    ref = store.insert_ref(
        kind="paper",
        slug="raw-elision-fixture",
        title="Elision fixture",
        meta={"large": value, "probe": "x"},
    )
    handler = PaperHandler(hub=hub)

    body = handler.get(id=ref.public_id, view="raw").body
    assert "2003 chars elided" in body
    assert '"probe": "x"' in body
    assert value not in body
    key_body = handler.get(
        id=ref.public_id, view="raw", args={"meta_key": "large"}
    ).body
    assert key_body == f"# raw paper {ref.public_id} — meta.large\n{value}"


def test_news_raw_meta_key_serves_elided_value(hub: Hub, store: Store) -> None:
    """The elision note's ``args={'meta_key': …}`` door works on news too —
    dispatch flattens it to the ``meta_key`` kwarg ``CacheBackedHandler.get``
    declares (review td789008)."""
    value = "x" * 2001
    handler = NewsHandler(hub=hub)
    ref, _ = store.put_cache_entry(
        kind="news",
        slug="raw-meta-key-fixture",
        title="Meta key fixture",
        provider="news",
        request_hash="raw-meta-key-fixture",
        ttl_seconds=None,
        body_blocks=[],
        ref_meta={"large": value, "probe": "x"},
    )

    body = handler.get(id="raw-meta-key-fixture", view="raw").body
    assert "chars elided" in body
    assert value not in body
    key_body = handler.get(id="raw-meta-key-fixture", view="raw", meta_key="large").body
    assert key_body == f"# raw news {ref.public_id} — meta.large\n{value}"


def test_structure_raw_refuses_external_source(hub: Hub, store: Store) -> None:
    """``view='raw'`` is stored-only like ``log``/``completion``: a
    ``source=`` hydration request refuses instead of silently serving the
    stored record (review td789008)."""
    handler = StructureHandler(hub=hub)
    with pytest.raises(BadInput, match="cannot hydrate external sources"):
        handler.get(id="any-slug", view="raw", source="catalysis-hub")
