"""``handlers/_links_render.py`` — the shared F8 "Links:" extraction.

Covers the citation-chunk-grounding "paper link-blindness fix":
the compact links table
used to live only on ``NumericRefHandler`` (memory/todo/gripe/finding/
…); it's now a free function every ``Handler``-direct kind
(paper/draft/structure/cad/pcb/plan/pres/patent) can call from its own
``view='links'``.
"""

from __future__ import annotations

import re

import pytest

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

    def test_sub_kb_endpoint_row_still_names_a_size(self, store: Store) -> None:
        """gr568219: a memory under 1 kB used to render a blank size cell
        next to sized gripe rows — every row now reports decimal kB
        (gr573411: ``353B`` → ``0.4kB``; only the ~1kB default blanks)."""
        from precis.store.types import ChunkInsert

        a = _mk_ref(store, "memory", "subject note")
        b = _mk_ref(store, "memory", "tiny target note")
        body = "x" * 353
        store.chunks.insert_chunks(
            b,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "memory_body"})],
        )
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to")
        ref = store.get_ref(kind="memory", id=a)
        assert ref is not None
        section = render_links_section(store, ref)
        assert "size [kB] (default 1kB)" in section
        assert "0.4kB" in section

    def test_default_kb_endpoint_row_blanks_the_cell(self, store: Store) -> None:
        """gr573411: a body rounding to the 1kB default renders no size
        cell — the header's ``(default 1kB)`` carries the meaning."""
        from precis.store.types import ChunkInsert

        a = _mk_ref(store, "memory", "subject note")
        b = _mk_ref(store, "memory", "one-kilobyte target note")
        store.chunks.insert_chunks(
            b,
            [ChunkInsert(ord=0, text="y" * 1006, meta={"chunk_kind": "memory_body"})],
        )
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to")
        ref = store.get_ref(kind="memory", id=a)
        assert ref is not None
        section = render_links_section(store, ref)
        assert "size [kB] (default 1kB)" in section
        assert section.count("1kB") == 1  # only the header's default note

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


# ---------------------------------------------------------------------------
# gr627629 — claim-graph priority, own-side ``via``, exact cursor paging
# ---------------------------------------------------------------------------


def test_claim_edges_outrank_citations(store: Store) -> None:
    """gr627629 slice 1: a citation flood must not bury the claim-graph
    edges a paper's link rollup exists to surface — with 30 inbound
    cites and 2 outbound corroborates at ``limit=20``, both corroborates
    rows make the capped preview."""
    a = store.insert_ref(kind="paper", slug="focus2020x", title="focus paper").id
    # Citation flood first (lower link ids) — id order alone would put
    # the corroborates rows last, past the cap.
    for i in range(30):
        citer = store.insert_ref(
            kind="paper", slug=f"citer{i}2020x", title=f"citing paper {i}"
        ).id
        store.add_link(src_ref_id=citer, dst_ref_id=a, relation="cites")
    fin1 = store.insert_ref(kind="finding", slug=None, title="claim one").id
    fin2 = store.insert_ref(kind="finding", slug=None, title="claim two").id
    store.add_link(src_ref_id=a, dst_ref_id=fin1, relation="corroborates")
    store.add_link(src_ref_id=a, dst_ref_id=fin2, relation="corroborates")
    ref = store.get_ref(kind="paper", id=a)
    assert ref is not None

    section = render_links_section(store, ref, priority=True, limit=20)
    assert "claim one" in section
    assert "claim two" in section
    # the claim rows lead the table; at most 18 citation rows remain
    assert section.index("claim one") < section.index("citing paper")
    # ``related-to`` still sorts after even an unlisted relation
    rel = store.insert_ref(kind="paper", slug="rel2020x", title="related paper").id
    store.add_link(src_ref_id=a, dst_ref_id=rel, relation="related-to")
    tail = render_links_section(store, ref, priority=True, limit=33)
    assert tail.index("related paper") > tail.index("citing paper")


def test_chunk_pinned_row_names_own_side_via(store: Store) -> None:
    """gr627629 slice 2: the shared Links table names the *focus's own*
    chunk a grounded edge pins on this side (``via pc<id> ~<ord>``); a
    ref-level edge renders no via."""
    from precis.store.types import ChunkInsert

    paper = store.insert_ref(kind="paper", slug="viasrc2020", title="evidence paper").id
    store.chunks.insert_chunks(
        paper, [ChunkInsert(ord=0, text="the carrying passage", meta={})]
    )
    fin = store.insert_ref(kind="finding", slug=None, title="a claim").id
    store.add_link(src_ref_id=paper, src_pos=0, dst_ref_id=fin, relation="corroborates")
    plain = store.insert_ref(kind="memory", slug=None, title="plain note").id
    store.add_link(src_ref_id=paper, dst_ref_id=plain, relation="related-to")
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s", (paper,)
        ).fetchone()
        assert row is not None
        pc_id = int(row[0])

    ref = store.get_ref(kind="paper", id=paper)
    assert ref is not None
    section = render_links_section(store, ref)
    assert f"via pc{pc_id} ~0" in section
    plain_row = next(line for line in section.splitlines() if "plain note" in line)
    assert "via" not in plain_row
    # the standalone view renders the same rows
    resp = render_links_view(store, ref, sense="paper")
    assert f"via pc{pc_id} ~0" in resp.body


def test_links_view_cursor_pages_without_gaps(store: Store) -> None:
    """gr627629 slice 4: ``args={'cursor': '<bucket>:<link_id>'}`` resumes
    exactly where the page ended — following ``next:`` to ``complete``
    covers every link once, in a deterministic order."""
    a = store.insert_ref(kind="paper", slug="paged2020", title="paged paper").id
    for i in range(250):
        target = store.insert_ref(
            kind="paper", slug=f"t{i}2020p", title=f"target {i}"
        ).id
        store.add_link(src_ref_id=a, dst_ref_id=target, relation="related-to")
    ref = store.get_ref(kind="paper", id=a)
    assert ref is not None

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        resp = render_links_view(store, ref, sense="paper", cursor=cursor)
        pages += 1
        seen += re.findall(r"target (\d+)", resp.body)
        m = re.search(r"'cursor': '([^']+)'", resp.body)
        if "— complete" in resp.body:
            assert m is None
            break
        assert "— next:" in resp.body
        assert m is not None, resp.body
        cursor = m.group(1)
        assert pages < 10  # a looping cursor fails fast, not on timeout
    assert pages == 3  # 100 + 100 + 50 at the default page size
    assert len(seen) == len(set(seen)) == 250
    assert sorted(int(s) for s in seen) == list(range(250))

    # a smaller limit pages the same set without gaps, and the footer
    # echoes it back into the next call
    small: list[str] = []
    cursor = None
    while True:
        resp = render_links_view(store, ref, sense="paper", limit=7, cursor=cursor)
        small += re.findall(r"target (\d+)", resp.body)
        m = re.search(r"'cursor': '([^']+)'", resp.body)
        if "— complete" in resp.body:
            break
        assert "'limit': 7" in resp.body
        assert m is not None
        cursor = m.group(1)
    assert len(small) == len(set(small)) == 250


def test_links_view_footer_reports_partial_and_complete(store: Store) -> None:
    """A partial page never implies full coverage: ``shown S of T``
    with the exact continuation on page one, ``complete`` on the last."""
    a = store.insert_ref(kind="paper", slug="foot2020", title="foot paper").id
    for i in range(3):
        target = store.insert_ref(
            kind="paper", slug=f"ft{i}2020", title=f"foot target {i}"
        ).id
        store.add_link(src_ref_id=a, dst_ref_id=target, relation="related-to")
    ref = store.get_ref(kind="paper", id=a)
    assert ref is not None

    page1 = render_links_view(store, ref, sense="paper", limit=2)
    assert "shown 2 of 3 — next:" in page1.body
    assert "view='links'" in page1.body
    cursor = re.search(r"'cursor': '([^']+)'", page1.body)
    assert cursor is not None
    page2 = render_links_view(
        store, ref, sense="paper", limit=2, cursor=cursor.group(1)
    )
    assert "shown 3 of 3 — complete" in page2.body
    assert "foot target 2" in page2.body
    assert "foot target 0" not in page2.body


def test_links_view_byte_cap_fits_at_row_boundary_and_keeps_cursor(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review td681248 (gr627629 slice 4): the row cap alone did not keep
    a default page under ``PRECIS_MAX_BODY_BYTES`` — 100 rows of wide
    (CJK) titles and reasons overrun it, and the generic line-fit cut
    the ``next:`` cursor footer, restoring the search hint. The page's
    ``budget_fit`` cuts at a whole-row boundary instead and keeps the
    exact continuation, so following it still covers every link once."""
    from precis._result_budget import fit_result_body

    cap = 24576
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", str(cap))
    a = store.insert_ref(kind="paper", slug="wide2020", title="wide paper").id
    for i in range(60):
        target = store.insert_ref(
            kind="paper",
            slug=f"wide{i}2020",
            title=f"{i} " + chr(0x984C) * 60,  # 3-byte CJK teaser per row
        ).id
        store.add_link(
            src_ref_id=a,
            dst_ref_id=target,
            relation="related-to",
            meta={"reason": chr(0x7E01) * 79 + str(i)},  # 80-char CJK why
        )
    ref = store.get_ref(kind="paper", id=a)
    assert ref is not None

    def fitted(resp) -> str:
        # The dispatch chokepoint's exact call shape.
        return fit_result_body(
            resp.body,
            verb="get",
            kind="paper",
            context_limited=False,
            targeted_hint=resp.result_budget_hint,
            read="single",
            structured_fit=resp.budget_fit,
            suffix="",
        )

    first = render_links_view(store, ref, sense="paper")
    assert len(first.body.encode("utf-8")) > cap  # the fit really runs
    assert first.budget_fit is not None

    body = fitted(first)
    assert len(body.encode("utf-8")) <= cap
    # the byte cap produced an exact continuation, not the generic
    # truncated-head + search-hint tail
    assert "— next:" in body
    assert "'cursor':" in body
    assert "… truncated" not in body
    assert "narrow q" not in body

    seen: list[int] = []
    cursor: str | None = None
    pages = 0
    while True:
        resp = render_links_view(store, ref, sense="paper", cursor=cursor)
        body = fitted(resp)
        assert len(body.encode("utf-8")) <= cap
        seen += [int(s) for s in re.findall("(\\d+) " + chr(0x984C), body)]
        m = re.search(r"'cursor': '([^']+)'", body)
        if "— complete" in body:
            assert m is None
            break
        assert "— next:" in body
        assert m is not None, body
        cursor = m.group(1)
        pages += 1
        assert pages < 60  # a looping cursor fails fast, not on timeout
    assert len(seen) == len(set(seen)) == 60
    assert sorted(seen) == list(range(60))


def test_cad_links_view_warnings_keep_budget_fit(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review td681248 (gr627629): the cad links arm appends its
    coverage / stale-analysis warnings by rebuilding the Response —
    which used to discard the links view's ``budget_fit``, so a wide
    page fell back to the generic line-fit and lost its ``next:``
    cursor. The fitter is now preserved with the warnings' bytes
    reserved: a fitted page still cuts on a whole row, keeps the exact
    continuation, and re-appends the warnings past the cut."""
    from precis._result_budget import fit_result_body

    cap = 24576
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", str(cap))
    store = hub.live_store
    a = store.insert_ref(kind="cad", slug="warnrig", title="warning rig").id
    for i in range(60):
        target = store.insert_ref(
            kind="memory", slug=None, title=f"{i} " + chr(0x984C) * 60
        ).id
        store.add_link(
            src_ref_id=a,
            dst_ref_id=target,
            relation="related-to",
            meta={"reason": chr(0x7E01) * 79 + str(i)},
        )
    fin = store.insert_ref(kind="finding", slug=None, title="an analysis").id
    store.add_link(
        src_ref_id=a,
        dst_ref_id=fin,
        relation="analyzed-by",
        meta={"sha": "not-the-current-sha", "at": "2026-01-01"},
    )
    handler = CadHandler(hub=hub)

    resp = handler.get(id="warnrig", view="links")
    assert "⚠ STALE analyses" in resp.body
    assert resp.budget_fit is not None  # the regression: it used to be dropped
    assert len(resp.body.encode("utf-8")) > cap

    body = fit_result_body(
        resp.body,
        verb="get",
        kind="cad",
        context_limited=False,
        targeted_hint=resp.result_budget_hint,
        read="single",
        structured_fit=resp.budget_fit,
        suffix="",
    )
    assert len(body.encode("utf-8")) <= cap
    # the byte cap produced an exact continuation, not the generic
    # truncated-head + search-hint tail — and the warning rides past it
    assert "— next:" in body
    assert "'cursor':" in body
    assert "… truncated" not in body
    assert "narrow q" not in body
    assert body.index("⚠ STALE analyses") > body.index("— next:")
    assert body.rstrip().endswith("design has changed since)")
