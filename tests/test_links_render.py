"""``handlers/_links_render.py`` — the shared F8 "Links:" extraction.

Covers the citation-chunk-grounding "paper link-blindness fix":
the compact links table
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

    def test_chunk_level_endpoints_render_pc_and_dc_handles(self, store: Store) -> None:
        """A chunk-grounded edge renders the *chunk* handle (``pc<id>`` /
        ``dc<id>``), not the coarse record handle — the granular address
        that lets the citation tree resolve to the supporting passage.
        The ``how to get`` column is chunk-scoped too: ``slug~ord`` for a
        paper, the ``dc<id>`` handle for a draft."""
        from precis.store.types import ChunkInsert

        fin = store.insert_ref(kind="finding", slug=None, title="a canonical claim")
        # paper chunk (ord 0) --corroborates--> finding
        paper = store.insert_ref(kind="paper", slug="wu2022a", title="Rotaxane paper")
        store.chunks.insert_chunks(
            paper.id, [ChunkInsert(ord=0, text="the supporting passage", meta={})]
        )
        store.add_link(
            src_ref_id=paper.id, src_pos=0, dst_ref_id=fin.id, relation="corroborates"
        )
        # draft chunk (ord 0) --cites--> finding
        dref = store.insert_ref(kind="draft", slug="nano-computer", title="A draft")
        store.chunks.insert_chunks(
            dref.id, [ChunkInsert(ord=0, text="cites the claim", meta={})]
        )
        store.add_link(
            src_ref_id=dref.id, src_pos=0, dst_ref_id=fin.id, relation="cites"
        )

        with store.pool.connection() as conn:
            pc_row = conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s", (paper.id,)
            ).fetchone()
            assert pc_row is not None
            pc_id = int(pc_row[0])
            dc_row = conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s", (dref.id,)
            ).fetchone()
            assert dc_row is not None
            dc_id = int(dc_row[0])

        ref = store.get_ref(kind="finding", id=fin.id)
        assert ref is not None
        section = render_links_section(store, ref)
        # related-to column: the granular chunk handles, NOT pa<id>/dr<id>
        assert f"pc{pc_id}" in section
        assert f"dc{dc_id}" in section
        assert f"pa{paper.id}" not in section
        assert f"dr{dref.id}" not in section
        # how-to-get column: chunk-scoped retrieval per kind
        assert "id='wu2022a~0'" in section  # paper → slug~ord
        assert f"id='dc{dc_id}'" in section  # draft → dc<id> handle


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
    a = hub.live_store.insert_ref(kind="memory", slug=None, title="A note").id
    b = hub.live_store.insert_ref(kind="memory", slug=None, title="Another note").id
    hub.live_store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to")
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
        store = hub.live_store
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
        store = hub.live_store
        store.insert_ref(kind="paper", slug="lonely2020", title="Lonely Paper")
        handler = PaperHandler(hub=hub)
        resp = handler.get(id="lonely2020", view="links")
        assert "(no links)" in resp.body


# ---------------------------------------------------------------------------
# One representative non-paper Handler-direct kind (cad) + patent
# ---------------------------------------------------------------------------


def test_cad_links_view(hub: Hub) -> None:
    store = hub.live_store
    a = store.insert_ref(kind="cad", slug="bracket", title="bracket design").id
    b = store.insert_ref(kind="memory", slug=None, title="a design note").id
    store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to")
    handler = CadHandler(hub=hub)
    resp = handler.get(id="bracket", view="links")
    assert "a design note" in resp.body


def test_patent_links_view_registered_even_without_credentials() -> None:
    assert "links" in PatentHandler.spec.views


# ---------------------------------------------------------------------------
# render_links_section(priority=, limit=) — Change B
# ---------------------------------------------------------------------------


class TestRenderLinksSectionCapAndPriority:
    def test_default_no_kwargs_is_byte_identical_to_before(self, store: Store) -> None:
        """Regression: the numeric-ref callsite (and render_links_view)
        pass no kwargs — the new priority/limit machinery must be
        fully guarded off in that case, sorting by link.id exactly as
        it did before this signature grew."""
        a = store.insert_ref(kind="paper", slug="subj2020", title="subject paper").id
        b = store.insert_ref(kind="paper", slug="rel2020", title="related paper").id
        c = store.insert_ref(kind="paper", slug="citer2020", title="citing paper").id
        # related-to link created first (lower link id); cites second
        # (higher link id) — priority order would flip these, id order
        # (the default) must not.
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to")
        store.add_link(src_ref_id=c, dst_ref_id=a, relation="cites")
        ref = store.get_ref(kind="paper", id=a)
        assert ref is not None

        no_kwargs = render_links_section(store, ref)
        explicit_off = render_links_section(store, ref, limit=None, priority=False)
        assert no_kwargs == explicit_off
        # Default (id) order: related-to row (link 1) before cites row
        # (link 2).
        assert no_kwargs.index("related paper") < no_kwargs.index("citing paper")
        assert "Links:" in no_kwargs
        assert "Links (" not in no_kwargs  # no truncation header

    def test_priority_true_sorts_evidential_before_related_to(
        self, store: Store
    ) -> None:
        a = store.insert_ref(kind="paper", slug="subj2020b", title="subject paper").id
        b = store.insert_ref(kind="paper", slug="rel2020b", title="related paper").id
        c = store.insert_ref(kind="paper", slug="citer2020b", title="citing paper").id
        # Same insert order as the id-order test above (related-to
        # first / lower id) — priority=True must flip it.
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to")
        store.add_link(src_ref_id=c, dst_ref_id=a, relation="cites")
        ref = store.get_ref(kind="paper", id=a)
        assert ref is not None

        section = render_links_section(store, ref, priority=True)
        assert section.index("citing paper") < section.index("related paper")

    def test_limit_truncates_and_emits_overflow_line(self, store: Store) -> None:
        a = store.insert_ref(kind="paper", slug="subj2020c", title="subject paper").id
        for i in range(15):
            target = store.insert_ref(
                kind="paper", slug=f"target{i}2020c", title=f"target paper {i}"
            ).id
            store.add_link(src_ref_id=a, dst_ref_id=target, relation="related-to")
        ref = store.get_ref(kind="paper", id=a)
        assert ref is not None

        section = render_links_section(store, ref, limit=12, priority=True)
        assert "Links (12 of 15):" in section
        assert "\n+3 more · get(kind='paper', id='pa" in section
        assert "view='links')" in section

    def test_limit_not_exceeded_keeps_plain_header(self, store: Store) -> None:
        a = store.insert_ref(kind="paper", slug="subj2020d", title="subject paper").id
        b = store.insert_ref(kind="paper", slug="rel2020d", title="related paper").id
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to")
        ref = store.get_ref(kind="paper", id=a)
        assert ref is not None

        section = render_links_section(store, ref, limit=12, priority=True)
        assert "Links:" in section
        assert "more ·" not in section

    def test_truncation_is_deterministic_not_reshuffled(self, store: Store) -> None:
        """gr311679: repeated calls over the same over-cap link set must
        render byte-identical output — truncation slices a stable sort,
        it must never resettle to a different top-N on a re-render."""
        a = store.insert_ref(kind="paper", slug="subj2020e", title="subject paper").id
        for i in range(25):
            target = store.insert_ref(
                kind="paper", slug=f"target{i}2020e", title=f"target paper {i}"
            ).id
            relation = "cites" if i % 5 == 0 else "related-to"
            store.add_link(src_ref_id=a, dst_ref_id=target, relation=relation)
        ref = store.get_ref(kind="paper", id=a)
        assert ref is not None

        first = render_links_section(store, ref, limit=12, priority=True)
        second = render_links_section(store, ref, limit=12, priority=True)
        assert first == second


# ---------------------------------------------------------------------------
# DEFAULT_LINK_ROW_CAP — gr311679: named module constant, shared cap
# ---------------------------------------------------------------------------


class TestDefaultLinkRowCap:
    def test_synthetic_scale_stays_bounded_and_deterministic(
        self, store: Store
    ) -> None:
        """gr311679 dossier evidence: an unbounded links section grows
        linearly with link count (78KB @ N=1231 real, 671KB @ N=10k
        synthetic). At a synthetic scale well past DEFAULT_LINK_ROW_CAP,
        the rendered section must stay capped, carry the honest
        remainder count, and be stable across repeat renders."""
        from precis.handlers._links_render import DEFAULT_LINK_ROW_CAP

        a = store.insert_ref(kind="paper", slug="subj2020f", title="subject paper").id
        n = 500
        for i in range(n):
            target = store.insert_ref(
                kind="paper", slug=f"target{i}2020f", title=f"target paper {i}"
            ).id
            store.add_link(src_ref_id=a, dst_ref_id=target, relation="related-to")
        ref = store.get_ref(kind="paper", id=a)
        assert ref is not None

        section = render_links_section(store, ref, limit=DEFAULT_LINK_ROW_CAP)
        assert f"Links ({DEFAULT_LINK_ROW_CAP} of {n}):" in section
        assert f"\n+{n - DEFAULT_LINK_ROW_CAP} more ·" in section
        assert "view='links')" in section
        rendered_titles = sum(1 for i in range(n) if f"target paper {i}" in section)
        assert rendered_titles == DEFAULT_LINK_ROW_CAP
        assert render_links_section(store, ref, limit=DEFAULT_LINK_ROW_CAP) == section
