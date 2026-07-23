"""``handlers/_links_render.py`` — the shared F8 "Links:" extraction.

Covers the citation-chunk-grounding "paper link-blindness fix"
(docs/design/citation-chunk-grounding.md): the compact links table
used to live only on ``NumericRefHandler`` (memory/todo/gripe/finding/
…); it's now a free function every ``Handler``-direct kind
(paper/draft/structure/cad/pcb/plan/pres/patent) can call from its own
``view='links'``.
"""

from __future__ import annotations

from precis.dispatch import Hub
from precis.handlers._links_render import render_links_section, render_links_view
from precis.handlers.cad import CadHandler
from precis.handlers.paper import PaperHandler
from precis.handlers.patent import PatentHandler
from precis.store import Store


def _mk_ref(store: Store, kind: str, title: str) -> int:
    return store.insert_ref(kind=kind, slug=None, title=title).id


# ---------------------------------------------------------------------------
# render_links_section — the pure free-function extraction
# ---------------------------------------------------------------------------


class TestRenderLinksSection:
    def test_no_links_is_empty_string(self, store: Store) -> None:
        a = _mk_ref(store, "memory", "lonely memory")
        ref = store.get_ref(kind="memory", id=a)
        assert ref is not None
        assert render_links_section(store, ref) == ""

    def test_outbound_link_renders_marker_and_target(self, store: Store) -> None:
        a = _mk_ref(store, "memory", "source memory")
        b = _mk_ref(store, "memory", "target memory about photosynthesis")
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to")
        ref = store.get_ref(kind="memory", id=a)
        assert ref is not None
        section = render_links_section(store, ref)
        assert "Links:" in section
        assert "--" in section  # default related-to marker
        assert "photosynthesis" in section  # teaser from target title

    def test_inbound_cites_renders_passive_form(self, store: Store) -> None:
        citer = store.insert_ref(
            kind="paper", slug="citer2020", title="citing paper"
        ).id
        cited = store.insert_ref(kind="paper", slug="cited2020", title="cited paper").id
        store.add_link(src_ref_id=citer, dst_ref_id=cited, relation="cites")
        ref = store.get_ref(kind="paper", id=cited)
        assert ref is not None
        section = render_links_section(store, ref)
        assert "cited by" in section


# ---------------------------------------------------------------------------
# render_links_view — the Response wrapper Handler-direct kinds use
# ---------------------------------------------------------------------------


class TestRenderLinksView:
    def test_empty_view_offers_a_recipe(self, store: Store) -> None:
        a = _mk_ref(store, "memory", "lonely")
        ref = store.get_ref(kind="memory", id=a)
        assert ref is not None
        resp = render_links_view(store, ref, sense="memory")
        assert "(no links)" in resp.body
        assert "link(kind='memory'" in resp.body

    def test_populated_view_has_header_and_table(self, store: Store) -> None:
        a = store.insert_ref(kind="paper", slug="papera2020", title="paper A").id
        b = store.insert_ref(kind="paper", slug="paperb2020", title="paper B").id
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="cites")
        ref = store.get_ref(kind="paper", id=a)
        assert ref is not None
        resp = render_links_view(store, ref, sense="paper")
        assert resp.body.startswith(f"# paper {a} - links")
        assert "Links:" in resp.body


# ---------------------------------------------------------------------------
# NumericRefHandler — pure-refactor behaviour preservation
# ---------------------------------------------------------------------------


def test_numeric_ref_handler_still_appends_links_section_on_get(hub: Hub) -> None:
    from precis.handlers.memory import MemoryHandler

    handler = MemoryHandler(hub=hub)
    a = hub.store.insert_ref(kind="memory", slug=None, title="A note").id
    b = hub.store.insert_ref(kind="memory", slug=None, title="Another note").id
    hub.store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to")
    resp = handler.get(id=a)
    assert "Links:" in resp.body
    assert "Another note" in resp.body


# ---------------------------------------------------------------------------
# PaperHandler.get(view='links') — the primary flagged blocker
# ---------------------------------------------------------------------------


class TestPaperLinksView:
    def test_paper_links_view_registered(self, hub: Hub) -> None:
        handler = PaperHandler(hub=hub)
        assert "links" in handler.accepted_views()

    def test_paper_links_view_shows_inbound_and_outbound(self, hub: Hub) -> None:
        store = hub.store
        y = store.insert_ref(kind="paper", slug="y2020cited", title="Cited Paper Y").id
        x = store.insert_ref(kind="paper", slug="x2021citer", title="Citing Paper X").id
        store.add_link(src_ref_id=x, dst_ref_id=y, relation="cites")
        store.add_link(
            src_ref_id=y, dst_ref_id=x, relation="related-to", meta={"note": "similar"}
        )
        handler = PaperHandler(hub=hub)
        resp = handler.get(id="y2020cited", view="links")
        assert "cited by" in resp.body  # inbound cites → passive form
        assert "Citing Paper X" in resp.body

    def test_paper_links_view_empty_still_renders(self, hub: Hub) -> None:
        store = hub.store
        store.insert_ref(kind="paper", slug="lonely2020", title="Lonely Paper")
        handler = PaperHandler(hub=hub)
        resp = handler.get(id="lonely2020", view="links")
        assert "(no links)" in resp.body


# ---------------------------------------------------------------------------
# One representative non-paper Handler-direct kind (cad) + patent
# ---------------------------------------------------------------------------


def test_cad_links_view(hub: Hub) -> None:
    store = hub.store
    a = store.insert_ref(kind="cad", slug="bracket", title="bracket design").id
    b = store.insert_ref(kind="memory", slug=None, title="a design note").id
    store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to")
    handler = CadHandler(hub=hub)
    resp = handler.get(id="bracket", view="links")
    assert "a design note" in resp.body


def test_patent_links_view_registered_even_without_credentials() -> None:
    assert "links" in PatentHandler.spec.views
