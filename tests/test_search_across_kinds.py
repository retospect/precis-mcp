"""Cross-kind chunk search — the source-search primitive.

``Store.search_chunks_across_kinds`` searches the chunks of a *set* of
kinds at once (semantic + lexical, RRF-fused), collapses to one best
chunk per ref, bounds by ``refs.created_at``, and orders by relevance
or recency. These tests pin the cross-kind reach, the per-ref collapse,
the date window, and the recency sort — all with the deterministic
``MockEmbedder`` so vectors are reproducible.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from precis.embedder import MockEmbedder
from precis.runtime import PrecisRuntime
from precis.store import ChunkInsert, Store, Tag


def _seed(store: Store, kind: str, slug: str, blocks: list[str], emb: MockEmbedder):
    ref = store.insert_ref(kind=kind, slug=slug, title=slug)
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(ord=i, text=t, embedding=emb.embed_one(t))
            for i, t in enumerate(blocks)
        ],
    )
    return ref.id


def test_reaches_multiple_kinds_and_collapses_per_ref(store: Store) -> None:
    emb = MockEmbedder(dim=store.embedding_dim())
    pid = _seed(
        store,
        "paper",
        "paper-mof",
        ["MOF adsorbents capture carbon dioxide.", "MOF pore size tuning for CO2."],
        emb,
    )
    wid = _seed(store, "web", "web-mof", ["A blog on MOF carbon dioxide capture."], emb)
    # A third kind that should NOT appear when we scope to paper+web.
    _seed(store, "pres", "pres-mof", ["slide: MOF carbon dioxide idea."], emb)

    hits = store.chunks.search_chunks_across_kinds(
        kinds=["paper", "web"],
        q="MOF carbon dioxide capture",
        query_vec=emb.embed_one("MOF carbon dioxide capture"),
        max_distance=None,
    )
    got_refs = {ref.id for _, ref, _ in hits}
    assert pid in got_refs
    assert wid in got_refs
    assert all(ref.kind in ("paper", "web") for _, ref, _ in hits)
    # Per-ref collapse: the paper has two matching chunks but contributes
    # exactly one row (its best chunk).
    assert sum(1 for _, ref, _ in hits if ref.id == pid) == 1


def test_kind_scope_excludes_unlisted_kinds(store: Store) -> None:
    emb = MockEmbedder(dim=store.embedding_dim())
    pid = _seed(store, "paper", "p1", ["nitrate reduction on copper"], emb)
    _seed(store, "web", "w1", ["nitrate reduction on copper"], emb)

    hits = store.chunks.search_chunks_across_kinds(
        kinds=["paper"],
        q="nitrate copper",
        query_vec=emb.embed_one("nitrate copper"),
        max_distance=None,
    )
    assert {ref.id for _, ref, _ in hits} == {pid}


def test_recency_sort_orders_newest_first(store: Store) -> None:
    emb = MockEmbedder(dim=store.embedding_dim())
    text = "graphene field-effect transistor mobility"
    older = _seed(store, "paper", "older", [text], emb)
    newer = _seed(store, "web", "newer", [text], emb)  # inserted later → newer

    hits = store.chunks.search_chunks_across_kinds(
        kinds=["paper", "web"],
        q="graphene transistor mobility",
        query_vec=emb.embed_one("graphene transistor mobility"),
        sort="recency",
        max_distance=None,
    )
    ordered = [ref.id for _, ref, _ in hits]
    assert ordered[:2] == [newer, older]


def test_oldest_sort_orders_oldest_first(store: Store) -> None:
    emb = MockEmbedder(dim=store.embedding_dim())
    text = "graphene field-effect transistor mobility"
    older = _seed(store, "paper", "o-older", [text], emb)
    newer = _seed(store, "web", "o-newer", [text], emb)  # inserted later → newer

    hits = store.chunks.search_chunks_across_kinds(
        kinds=["paper", "web"],
        q="graphene transistor mobility",
        query_vec=emb.embed_one("graphene transistor mobility"),
        sort="oldest",
        max_distance=None,
    )
    ordered = [ref.id for _, ref, _ in hits]
    assert ordered[:2] == [older, newer]  # mirror of the recency test


def test_date_window_bounds_results(store: Store) -> None:
    emb = MockEmbedder(dim=store.embedding_dim())
    _seed(store, "paper", "d1", ["perovskite solar cell efficiency"], emb)
    qv = emb.embed_one("perovskite solar")

    # Far-past floor keeps everything; far-future floor drops everything.
    assert store.chunks.search_chunks_across_kinds(
        kinds=["paper"],
        q="perovskite solar",
        query_vec=qv,
        max_distance=None,
        since=datetime(2000, 1, 1, tzinfo=UTC),
    )
    assert not store.chunks.search_chunks_across_kinds(
        kinds=["paper"],
        q="perovskite solar",
        query_vec=qv,
        max_distance=None,
        since=datetime(2999, 1, 1, tzinfo=UTC),
    )
    # Far-past ceiling drops everything (created_at is "now").
    assert not store.chunks.search_chunks_across_kinds(
        kinds=["paper"],
        q="perovskite solar",
        query_vec=qv,
        max_distance=None,
        until=datetime(2000, 1, 1, tzinfo=UTC),
    )


def test_lexical_only_when_no_vector(store: Store) -> None:
    """No query_vec → lexical leg alone still answers (embedder-down path)."""
    emb = MockEmbedder(dim=store.embedding_dim())
    pid = _seed(store, "paper", "lex", ["thermoelectric figure of merit ZT"], emb)
    hits = store.chunks.search_chunks_across_kinds(
        kinds=["paper"], q="thermoelectric ZT"
    )
    assert pid in {ref.id for _, ref, _ in hits}


def test_empty_kinds_returns_empty(store: Store) -> None:
    assert store.chunks.search_chunks_across_kinds(kinds=[], q="anything") == []


def test_offset_pages_past_the_limit_window(store: Store) -> None:
    """Slice-3 pagination: ``offset`` returns the next window with no
    overlap and no gap against the first page, for both sort orders."""
    emb = MockEmbedder(dim=store.embedding_dim())
    ids = [
        _seed(store, "paper", f"pg-{i}", [f"xenon isotope separation run {i}"], emb)
        for i in range(5)
    ]
    qv = emb.embed_one("xenon isotope separation")

    page1 = store.chunks.search_chunks_across_kinds(
        kinds=["paper"],
        q="xenon isotope separation",
        query_vec=qv,
        max_distance=None,
        limit=2,
        offset=0,
    )
    page2 = store.chunks.search_chunks_across_kinds(
        kinds=["paper"],
        q="xenon isotope separation",
        query_vec=qv,
        max_distance=None,
        limit=2,
        offset=2,
    )
    assert len(page1) == 2
    assert len(page2) == 2
    p1_ids = {ref.id for _, ref, _ in page1}
    p2_ids = {ref.id for _, ref, _ in page2}
    assert p1_ids.isdisjoint(p2_ids)
    assert p1_ids | p2_ids <= set(ids)

    # Recency sort pages the same relevance-qualified pool by date.
    r1 = store.chunks.search_chunks_across_kinds(
        kinds=["paper"],
        q="xenon isotope separation",
        query_vec=qv,
        max_distance=None,
        sort="recency",
        limit=2,
        offset=0,
    )
    r2 = store.chunks.search_chunks_across_kinds(
        kinds=["paper"],
        q="xenon isotope separation",
        query_vec=qv,
        max_distance=None,
        sort="recency",
        limit=2,
        offset=2,
    )
    r1_ids = {ref.id for _, ref, _ in r1}
    r2_ids = {ref.id for _, ref, _ in r2}
    assert r1_ids.isdisjoint(r2_ids)


def test_recent_refs_newest_first_and_kind_scoped(store: Store) -> None:
    a = store.insert_ref(kind="paper", slug="rr-a", title="A")
    b = store.insert_ref(kind="web", slug="rr-b", title="B")  # inserted later
    c = store.insert_ref(kind="memory", slug=None, title="C")  # unlisted kind

    got = store.recent_refs(["paper", "web"], limit=10)
    ids = [r.id for r in got]
    assert ids[:2] == [b.id, a.id]  # newest first
    assert c.id not in ids
    assert store.recent_refs([], limit=10) == []


def test_recent_refs_tag_filter(store: Store) -> None:
    a = store.insert_ref(kind="paper", slug="rt-a", title="A")
    store.insert_ref(kind="paper", slug="rt-b", title="B")  # untagged, excluded
    store.add_tag(a.id, Tag.open("keepme"))

    got = store.recent_refs(["paper"], tags=["keepme"])
    assert [r.id for r in got] == [a.id]


def test_suggest_tags_substring(store: Store) -> None:
    a = store.insert_ref(kind="paper", slug="sg-a", title="A")
    store.add_tag(a.id, Tag.open("topic-co2-capture"))

    hits = store.suggest_tags("co2")
    assert ("OPEN", "topic-co2-capture") in [(ns, val) for ns, val, _ in hits]
    assert store.suggest_tags("") == []
    assert store.suggest_tags("zzznomatch") == []


def test_recent_refs_stub_filter(store: Store) -> None:
    stub = store.insert_ref(kind="paper", slug="hp-stub", title="stub")
    full = store.insert_ref(kind="paper", slug="hp-full", title="full")
    # refs.pdf_sha256 is a char(64) FK into pdfs — seed a real row first.
    sha = f"{full.id:064d}"
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
            "size_bytes, storage_path) VALUES (%s, %s, 1, 100, '/tmp/held') "
            "ON CONFLICT (pdf_sha256) DO NOTHING",
            (sha, sha),
        )
        conn.execute(
            "UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s", (sha, full.id)
        )

    stubs = [r.id for r in store.recent_refs(["paper"], has_pdf=False)]
    assert stub.id in stubs and full.id not in stubs
    held = [r.id for r in store.recent_refs(["paper"], has_pdf=True)]
    assert full.id in held and stub.id not in held


def test_recent_refs_offset_pages(store: Store) -> None:
    a = store.insert_ref(kind="paper", slug="ro-a", title="A")
    b = store.insert_ref(kind="paper", slug="ro-b", title="B")  # newer
    c = store.insert_ref(kind="paper", slug="ro-c", title="C")  # newest

    page1 = store.recent_refs(["paper"], limit=2, offset=0)
    page2 = store.recent_refs(["paper"], limit=2, offset=2)
    assert [r.id for r in page1] == [c.id, b.id]
    assert [r.id for r in page2] == [a.id]


def test_recent_refs_folder_facet(store: Store) -> None:
    folder = store.insert_ref(kind="folder", slug=None, title="Fld")
    inside = store.insert_ref(kind="draft", slug="rf-inside", title="Inside")
    store.set_parent(inside.id, folder.id)
    outside = store.insert_ref(kind="draft", slug="rf-outside", title="Outside")

    got = [r.id for r in store.recent_refs(["draft"], parent_id=folder.id)]
    assert inside.id in got
    assert outside.id not in got

    # unfiled_only is the mirror image: the top-level view hides anything
    # filed into a folder, keeping only the unfiled ref.
    unfiled = [r.id for r in store.recent_refs(["draft"], unfiled_only=True)]
    assert outside.id in unfiled
    assert inside.id not in unfiled


def test_recent_refs_edited_bubbles_up(store: Store) -> None:
    # Ordered by updated_at, not created_at — editing an older ref lifts it
    # back above a newer, untouched one (a re-worked draft returns to the top).
    old = store.insert_ref(kind="draft", slug="rb-old", title="Old")
    new = store.insert_ref(kind="draft", slug="rb-new", title="New")  # newer

    assert [r.id for r in store.recent_refs(["draft"])] == [new.id, old.id]

    store.replace_ref_text(old.id, "Old (edited)")  # bumps updated_at
    assert [r.id for r in store.recent_refs(["draft"])] == [old.id, new.id]


def test_list_folders(store: Store) -> None:
    root = store.insert_ref(kind="folder", slug=None, title="Root")
    child = store.insert_ref(kind="folder", slug=None, title="Child")
    store.set_parent(child.id, root.id)

    edges = {ref_id: (title, parent) for ref_id, title, parent in store.list_folders()}
    assert edges[root.id] == ("Root", None)
    assert edges[child.id] == ("Child", root.id)


def test_paper_identifiers(store: Store) -> None:
    a = store.insert_ref(kind="paper", slug="pi-a", title="A")
    b = store.insert_ref(kind="paper", slug="pi-b", title="B")  # no identifier
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES ('doi', %s, %s, 'test')",
            ("10.1038/nature01797", a.id),
        )
    got = store.paper_identifiers([a.id, b.id])
    assert got == {a.id: "10.1038/nature01797"}
    assert store.paper_identifiers([]) == {}


def test_ref_tags_bulk(store: Store) -> None:
    a = store.insert_ref(kind="paper", slug="tb-a", title="A")
    b = store.insert_ref(kind="paper", slug="tb-b", title="B")  # untagged
    store.add_tag(a.id, Tag.open("topic-x"))
    store.add_tag(a.id, Tag.closed("PRIO", "high"))

    got = store.ref_tags_bulk([a.id, b.id])
    assert set(got[a.id]) == {("OPEN", "topic-x"), ("PRIO", "high")}
    assert b.id not in got  # untagged refs are simply absent
    assert store.ref_tags_bulk([]) == {}


def test_refs_with_body_chunks(store: Store) -> None:
    emb = MockEmbedder(dim=store.embedding_dim())
    ingested = _seed(store, "paper", "has-chunks", ["some body text"], emb)
    stub = store.insert_ref(kind="paper", slug="no-chunks", title="stub")

    got = store.refs_with_body_chunks([ingested, stub.id])
    assert got == {ingested}
    assert store.refs_with_body_chunks([]) == set()


# ── gr340057: search(mode=...) fanned out through NumericRefHandler ────
#
# The LLM-facing cross-kind dispatch (``search(kind='a,b', mode=...)``)
# calls each kind's ``search_hits(mode=...)``. Before the fix, every kind
# built on ``NumericRefHandler`` (job, todo, memory, gripe, ...) let
# ``mode`` fall into ``**_kw`` and silently ran the default hybrid stream
# regardless of what the caller asked for.


def test_cross_kind_semantic_mode_excludes_lexical_only_memory_hit(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """``memory`` is a body-chunk NumericRefHandler kind
    (``search_body_chunks=True``): ``mode='semantic'`` now runs a true
    ``SEMANTIC_DISTANCE_FLOOR`` cut with no lexical leg at all, so a
    lexical-only match must be dropped outright — not merely re-ranked."""
    tag = uuid4().hex[:8]
    e = MockEmbedder(dim=store.embedding_dim())
    marker = f"gr340057uniq{tag}"
    query = f"{marker} exact phrase carried straight into the semantic leg"

    paper = store.insert_ref(kind="paper", slug=f"mode-{tag}-paper", title="P")
    store.chunks.insert_chunks(
        paper.id, [ChunkInsert(ord=0, text=query, embedding=e.embed_one(query))]
    )
    far_text = f"{marker} — a wholly unrelated sentence about gardening topics"
    mem_title = f"farmarker{tag}"
    mem = store.insert_ref(kind="memory", slug=None, title=mem_title, meta={})
    store.chunks.insert_chunks(
        mem.id, [ChunkInsert(ord=0, text=far_text, embedding=e.embed_one(far_text))]
    )

    out_semantic = runtime_with_store.dispatch(
        "search", {"kind": "paper,memory", "q": query, "mode": "semantic"}
    )
    assert "[error:" not in out_semantic
    assert query in out_semantic  # the close paper chunk shows
    # The lexical-only memory hit is cut outright, not merely re-ranked —
    # the compact cross-kind table renders title, not full chunk text, so
    # the marker-tagged title (not far_text itself) is the honest probe.
    assert mem_title not in out_semantic

    # mode='lexical' on the shared marker: memory's hit answers via pure
    # FTS (no embedder round-trip needed for the lexical leg either).
    out_lexical = runtime_with_store.dispatch(
        "search", {"kind": "paper,memory", "q": marker, "mode": "lexical"}
    )
    assert "[error:" not in out_lexical
    assert mem_title in out_lexical


def test_cross_kind_mode_is_forwarded_to_ref_level_numeric_ref_kind(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """``job`` is a ref-level NumericRefHandler kind
    (``search_body_chunks=False``). Integration-level smoke check that
    ``mode=`` survives the cross-kind fan-out end to end without erroring
    and the title-lexical hit still answers; the direct pin that
    ``fused_ref_hits`` actually *receives* ``mode`` (previously swallowed
    by ``search_hits``'s ``**_kw``) is
    ``tests/test_numeric_ref_search_mode.py::
    test_ref_level_search_hits_forwards_explicit_mode``."""
    tag = uuid4().hex[:8]
    marker = f"gr340057job{tag}"
    store.insert_ref(kind="job", slug=None, title=f"{marker} plain title match")

    out = runtime_with_store.dispatch(
        "search", {"kind": "job,memory", "q": marker, "mode": "lexical"}
    )
    assert "[error:" not in out
    assert marker in out


# ── gr537833: drafts join the cross-kind fan-out; title/headings lexical ──


def _seed_draft(
    store: Store, slug: str, title: str, blocks: list[str], emb: MockEmbedder
) -> int:
    ref = store.insert_ref(kind="draft", slug=slug, title=title)
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(ord=i, text=t, embedding=emb.embed_one(t))
            for i, t in enumerate(blocks)
        ],
    )
    return ref.id


def test_cross_kind_returns_a_matching_draft(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """``kind='draft'`` was absent from the fan-out's kind list — the same
    q that found it single-kind returned nothing cross-kind (gr537833)."""
    emb = MockEmbedder(dim=store.embedding_dim())
    marker = f"gr537833{uuid4().hex[:8]}"
    _seed_draft(
        store,
        "xkind-plan",
        f"the {marker} plan",
        [f"staffing note for {marker}"],
        emb,
    )

    out = runtime_with_store.dispatch(
        "search", {"kind": "draft,paper", "q": marker, "mode": "lexical"}
    )
    assert "[error:" not in out
    assert marker in out

    # The wildcard (and a kindless search) take the same fan-out.
    out = runtime_with_store.dispatch(
        "search", {"kind": "*", "q": marker, "mode": "lexical"}
    )
    assert "[error:" not in out
    assert marker in out


def test_draft_search_heading_word_finds_its_draft(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    emb = MockEmbedder(dim=store.embedding_dim())
    marker = f"hedwark{uuid4().hex[:8]}"
    _seed_draft(
        store,
        "heading-plan",
        "a plan",
        [f"Root {marker} technical plan", "ordinary body prose"],
        emb,
    )
    out = runtime_with_store.dispatch("search", {"kind": "draft", "q": marker})
    assert "heading-plan" in out


def test_draft_search_title_word_finds_its_draft(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """``refs.title`` is not a chunk — only the title lexical leg can see
    it. A title word absent from every chunk still finds the draft."""
    emb = MockEmbedder(dim=store.embedding_dim())
    marker = f"titlemark{uuid4().hex[:8]}"
    _seed_draft(
        store,
        "title-plan",
        f"The {marker} initiative",
        ["body prose that never names the title word"],
        emb,
    )
    out = runtime_with_store.dispatch(
        "search", {"kind": "draft", "q": marker, "mode": "lexical"}
    )
    assert "title-plan" in out
    assert "[title]" in out


def test_draft_search_title_leg_skips_later_pages_and_headings_only(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """refs.title has no paging: page 2 must not repeat the page-1 title
    hit, and ``headings_only`` searches headings, not titles."""
    emb = MockEmbedder(dim=store.embedding_dim())
    marker = f"pgtitle{uuid4().hex[:8]}"
    _seed_draft(
        store,
        "pg-title-plan",
        f"The {marker} initiative",
        ["body prose that never names the title word"],
        emb,
    )
    args = {"kind": "draft", "q": marker, "mode": "lexical"}
    page1 = runtime_with_store.dispatch("search", args)
    assert "[title]" in page1
    page2 = runtime_with_store.dispatch("search", {**args, "page": 2})
    assert "[title]" not in page2
    headings = runtime_with_store.dispatch("search", {**args, "headings_only": True})
    assert "[title]" not in headings


def test_draft_search_hits_capped_at_page_size(store: Store) -> None:
    """Title hits are prepended to chunk hits; the combined stream must
    still honour ``page_size`` (was up to 2x)."""
    from precis.dispatch import Hub
    from precis.handlers.draft import DraftHandler

    emb = MockEmbedder(dim=store.embedding_dim())
    marker = f"capmark{uuid4().hex[:8]}"
    for i in range(3):
        _seed_draft(
            store,
            f"cap-plan-{i}",
            f"The {marker} plan {i}",
            [f"chunk about {marker} number {i}"],
            emb,
        )
    handler = DraftHandler(hub=Hub(store=store, embedder=emb))
    hits = handler.search_hits(q=marker, page_size=2, mode="lexical")
    assert len(hits) == 2


# ── gr608577: source search pages via args={'page': N} ──────────────


def _seed_paper_window(store: Store, n: int, emb: MockEmbedder) -> list[int]:
    return [
        _seed(
            store,
            "paper",
            f"pw-{i}-{uuid4().hex[:6]}",
            [f"zephyr tide gauge reading {i}"],
            emb,
        )
        for i in range(n)
    ]


def test_source_search_args_page_walks_the_window(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """``args={'page': N}`` pages the since=/sort= source search —
    disjoint windows, no gap (gr608577)."""
    emb = MockEmbedder(dim=store.embedding_dim())
    _seed_paper_window(store, 5, emb)
    base = {
        "kind": "paper",
        "q": "zephyr tide gauge",
        "since": "2000-01-01",
        "page_size": 2,
    }
    p1 = runtime_with_store.dispatch("search", base)
    assert "[error:" not in p1
    assert "'page': 2" in p1  # the continuation names args={'page': 2}

    p2 = runtime_with_store.dispatch("search", {**base, "__extras__": {"page": 2}})
    assert "[error:" not in p2
    p3 = runtime_with_store.dispatch("search", {**base, "__extras__": {"page": 3}})
    assert "[error:" not in p3

    def shown_ids(body: str) -> set[int]:
        return {int(i) for i in re.findall(r"pw-(\d+)-", body)}

    g1, g2, g3 = (shown_ids(p) for p in (p1, p2, p3))
    assert [len(g) for g in (g1, g2, g3)] == [2, 2, 1]
    assert g1.isdisjoint(g2) and g1.isdisjoint(g3) and g2.isdisjoint(g3)
    assert g1 | g2 | g3 == {0, 1, 2, 3, 4}


def test_source_search_page_validation(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    emb = MockEmbedder(dim=store.embedding_dim())
    _seed_paper_window(store, 1, emb)
    base = {"kind": "paper", "q": "zephyr tide", "since": "2000-01-01"}
    out = runtime_with_store.dispatch("search", {**base, "__extras__": {"page": "two"}})
    assert "[error:BadInput]" in out
    assert "page" in out


def _source_window_ids(body: str) -> set[int]:
    return {int(i) for i in re.findall(r"pw-(\d+)-", body)}


def _advertised_window(body: str) -> tuple[str, int] | None:
    """The first ``args={'page'|'offset': N}`` a body names, else None."""
    m = re.search(r"args=\{[^}]*'(page|offset)': (\d+)", body)
    return (m.group(1), int(m.group(2))) if m else None


def test_source_search_offset_window_and_validation(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """``args={'offset': N}`` places the same window ``page`` would at a
    multiple of ``page_size`` — and refuses a bad value or a ``page=``
    conflict (td610130)."""
    emb = MockEmbedder(dim=store.embedding_dim())
    _seed_paper_window(store, 5, emb)
    base = {
        "kind": "paper",
        "q": "zephyr tide gauge",
        "since": "2000-01-01",
        "page_size": 2,
    }
    by_page = runtime_with_store.dispatch("search", {**base, "__extras__": {"page": 2}})
    by_offset = runtime_with_store.dispatch(
        "search", {**base, "__extras__": {"offset": 2}}
    )
    assert _source_window_ids(by_offset) == _source_window_ids(by_page)
    # An offset window continues by offset, not page.
    assert "'offset': 4" in by_offset

    out = runtime_with_store.dispatch("search", {**base, "__extras__": {"offset": -1}})
    assert "[error:BadInput]" in out
    assert "offset" in out
    out = runtime_with_store.dispatch(
        "search", {**base, "page": 2, "__extras__": {"offset": 2}}
    )
    assert "[error:BadInput]" in out


def test_source_search_fit_names_exact_resume(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An over-cap page cuts at a row boundary and names the resume call
    that continues the same ordering — ``args={'offset': M}`` at the
    exact underlying result offset (gr608577; td610130 — page*size
    arithmetic could not express a mid-page cut)."""
    emb = MockEmbedder(dim=store.embedding_dim())
    _seed_paper_window(store, 30, emb)
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "1200")
    call: dict[str, Any] = {
        "kind": "paper",
        "q": "zephyr tide gauge",
        "since": "2000-01-01",
        "page_size": 30,
    }
    seen: set[int] = set()
    for _ in range(8):
        body, is_error = runtime_with_store.dispatch_with_status("search", call)
        assert is_error is False, body
        ids = _source_window_ids(body)
        assert ids.isdisjoint(seen), f"rows shown twice: {sorted(ids & seen)}"
        seen |= ids
        window = _advertised_window(body)
        if window is None:
            break
        call = {**call, "__extras__": {window[0]: window[1]}}
    assert seen == set(range(30))


def test_source_search_fit_pinned_card_not_a_result(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pinned identity card fills row 1 of the head window but
    consumes no *result* slot — a budget cut resumes at the first
    unshown result, not one row later (td610130: counting the card as
    a consumed result skipped an unseen row)."""
    emb = MockEmbedder(dim=store.embedding_dim())
    ref_ids = _seed_paper_window(store, 30, emb)
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "1200")
    call: dict[str, Any] = {
        "kind": "paper",
        # ``q`` naming the record handle pins its card — its ref leaves
        # the store query (the pin *is* its row, td609888), so no window
        # can render it twice. The ``queries=`` leg supplies the results
        # the page renders beside it.
        "q": f"pa{ref_ids[0]}",
        "queries": ["zephyr tide gauge"],
        "since": "2000-01-01",
        "sort": "oldest",
        "page_size": 30,
    }
    seen: set[int] = set()
    first = True
    for _ in range(8):
        body, is_error = runtime_with_store.dispatch_with_status("search", call)
        assert is_error is False, body
        ids = _source_window_ids(body)
        assert ids.isdisjoint(seen), f"rows shown twice: {sorted(ids & seen)}"
        seen |= ids
        if first:
            first = False
            assert "… truncated: showing" in body
            # The pinned card renders as row 1 but consumes no result
            # slot: with the pin row the page shows n_rows rendered rows
            # for n_rows-1 consumed results, so the resume lands at
            # n_rows-1 — not n_rows (a skip) nor n_rows-2 (a re-show).
            n_rows = len(re.findall(r"\n## \d+\.", body))
            window = _advertised_window(body)
            assert window == ("offset", n_rows - 1)
        window = _advertised_window(body)
        if window is None:
            break
        call = {**call, "__extras__": {window[0]: window[1]}}
    assert seen == set(range(30))


# ── gr608733: source search applies args={'serves': 'quest:<id>'} ────


def test_source_search_serves_filters_to_serving_refs(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """``args={'serves': 'quest:<id>'}`` on a since=/sort= source search
    restricts hits to the quest's inbound ``serves`` edges — the same
    cohort the paper path's ``serves=`` resolves (gr608733: the nested
    spelling used to die at the unapplied-args gate)."""
    emb = MockEmbedder(dim=store.embedding_dim())
    serving = _seed(store, "paper", "sv-inside", ["xenon serves cohort"], emb)
    _seed(store, "paper", "sv-outside", ["xenon serves cohort"], emb)
    quest = store.insert_ref(kind="quest", slug=None, title="q")
    store.add_link(src_ref_id=serving, dst_ref_id=quest.id, relation="serves")

    body, is_error = runtime_with_store.dispatch_with_status(
        "search",
        {
            "kind": "paper",
            "q": "xenon serves cohort",
            "since": "2000-01-01",
            "__extras__": {"serves": f"quest:{quest.id}"},
        },
    )
    assert not is_error, body
    assert "sv-inside" in body
    assert "sv-outside" not in body
    assert f"serves=qu{quest.id}" in body


def test_source_search_serves_empty_cohort_is_zero_hits(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """A quest nothing serves yields zero hits — the include channel
    holds an empty set, it never falls through unfiltered."""
    emb = MockEmbedder(dim=store.embedding_dim())
    _seed(store, "paper", "sv-unserved", ["xenon serves cohort"], emb)
    quest = store.insert_ref(kind="quest", slug=None, title="q")

    body, is_error = runtime_with_store.dispatch_with_status(
        "search",
        {
            "kind": "paper",
            "q": "xenon serves cohort",
            "since": "2000-01-01",
            "__extras__": {"serves": f"quest:{quest.id}"},
        },
    )
    assert not is_error, body
    assert "sv-unserved" not in body
    assert "restricted to 0 serving refs" in body


def test_source_search_serves_replays_caller_spelling(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """The resolved ref-id list never leaks into a continuation hint —
    ``next:`` re-renders the caller's ``serves=`` spelling like the
    cite-facet replays do."""
    emb = MockEmbedder(dim=store.embedding_dim())
    quest = store.insert_ref(kind="quest", slug=None, title="q")
    for i in range(3):
        rid = _seed(store, "paper", f"sv-page-{i}", ["xenon serves cohort"], emb)
        store.add_link(src_ref_id=rid, dst_ref_id=quest.id, relation="serves")

    body, is_error = runtime_with_store.dispatch_with_status(
        "search",
        {
            "kind": "paper",
            "q": "xenon serves cohort",
            "since": "2000-01-01",
            "page_size": 2,
            "__extras__": {"serves": f"quest:{quest.id}"},
        },
    )
    assert not is_error, body
    assert "'serves': 'quest:" in body
    assert "include_ref_ids" not in body


def test_source_search_serves_bad_target_refuses(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """A ``serves=`` token that is no quest reference fails the same way
    the paper path fails it — never degrades to an unfiltered search."""
    emb = MockEmbedder(dim=store.embedding_dim())
    _seed(store, "paper", "sv-bad", ["xenon serves cohort"], emb)
    body, is_error = runtime_with_store.dispatch_with_status(
        "search",
        {
            "kind": "paper",
            "q": "xenon serves cohort",
            "since": "2000-01-01",
            "__extras__": {"serves": "bogus"},
        },
    )
    assert is_error
    assert "serves= takes a quest reference" in body


def test_source_search_serves_missing_quest_refuses(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    body, is_error = runtime_with_store.dispatch_with_status(
        "search",
        {
            "kind": "paper",
            "q": "xenon serves cohort",
            "since": "2000-01-01",
            "__extras__": {"serves": "quest:99999999"},
        },
    )
    assert is_error
    assert "quest id=99999999 not found" in body


# ── td628495 review: queryless serves= is a filtered listing ─────────


def test_source_search_serves_without_q_lists_the_cohort(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """A queryless ``paper + since + serves`` call used to die with
    "requires q=" before ``serves`` ever resolved (every test above
    still passes ``q``). With a named cohort the queryless call is a
    filtered listing of the serving *refs* — so a serving paper with no
    body chunks (invisible to any chunk search) lists too."""
    emb = MockEmbedder(dim=store.embedding_dim())
    serving = _seed(store, "paper", "svq-inside", ["xenon serves cohort"], emb)
    _seed(store, "paper", "svq-outside", ["xenon serves cohort"], emb)
    chunkless = store.insert_ref(
        kind="paper", slug="svq-chunkless", title="svq-chunkless"
    )
    quest = store.insert_ref(kind="quest", slug=None, title="q")
    store.add_link(src_ref_id=serving, dst_ref_id=quest.id, relation="serves")
    store.add_link(src_ref_id=chunkless.id, dst_ref_id=quest.id, relation="serves")

    body, is_error = runtime_with_store.dispatch_with_status(
        "search",
        {
            "kind": "paper",
            "since": "2000-01-01",
            "__extras__": {"serves": f"quest:{quest.id}"},
        },
    )
    assert not is_error, body
    assert "svq-inside" in body
    assert "svq-chunkless" in body
    assert "svq-outside" not in body
    assert f"serves=qu{quest.id}" in body


def test_source_search_queryless_without_cohort_still_refuses(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """A bare date window over the whole corpus is a browse, not a
    listing — with no ``serves=``/facet cohort the refusal stands. The
    refusal's ``next=`` calls must spell ``since`` inside ``args={...}``
    (gr608575): the slim wrapper deprecates the top-level spelling, so a
    top-level hint aims the caller back at the note the trigger label
    just named."""
    body, is_error = runtime_with_store.dispatch_with_status(
        "search", {"kind": "paper", "since": "2000-01-01"}
    )
    assert is_error
    assert "requires q=" in body
    assert "sort= / args={'since'/'until'}" in body
    assert "since='2026-01-01'" not in body
    assert "'since': '2026-01-01'" in body


def test_source_search_serves_listing_pages_and_replays(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """The listing keeps the source-search continuation contract: the
    next-page call replays ``serves=`` in the caller's spelling (no
    resolved ref-id list leaks) and resumes the same ordering."""
    quest = store.insert_ref(kind="quest", slug=None, title="q")
    for i in range(3):
        rid = store.insert_ref(
            kind="paper", slug=f"svq-page-{i}", title=f"svq-page-{i}"
        ).id
        store.add_link(src_ref_id=rid, dst_ref_id=quest.id, relation="serves")
    base = {
        "kind": "paper",
        "since": "2000-01-01",
        "page_size": 2,
        "__extras__": {"serves": f"quest:{quest.id}"},
    }

    p1, is_error = runtime_with_store.dispatch_with_status("search", base)
    assert not is_error, p1
    assert "'serves': 'quest:" in p1
    assert "include_ref_ids" not in p1

    p2, is_error = runtime_with_store.dispatch_with_status(
        "search",
        {**base, "__extras__": {"serves": f"quest:{quest.id}", "page": 2}},
    )
    assert not is_error, p2
    ids1 = {int(i) for i in re.findall(r"svq-page-(\d)", p1)}
    ids2 = {int(i) for i in re.findall(r"svq-page-(\d)", p2)}
    assert ids1.isdisjoint(ids2)
    assert ids1 | ids2 == {0, 1, 2}
