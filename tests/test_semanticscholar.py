"""Tests for the Semantic Scholar handler (#186).

Covers the response-formatting helper and the canonical-key + slug
shape. Network is mocked via httpx (``_s2_get_json`` monkeypatched) so
tests stay offline — no live S2 calls, per test convention.

The held/stub/NEW corpus-diff + exclude= tests (docs/backlog/
discovery-exclude-by-container.md) are DB-backed (real ``store``/``hub``
fixtures): the diff walks real ``refs``/``ref_identifiers`` rows and the
exclude= container walk reads a real draft, so those need the live
store — only the outbound S2 HTTP call is mocked.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.semanticscholar import SemanticScholarHandler, _format_paper
from precis.store import Store


def test_format_paper_full_record() -> None:
    """A typical S2 response row renders with every field we project."""
    paper = {
        "title": "Carbon nanotube field-effect transistors",
        "year": 2003,
        "authors": [{"name": "A. Javey"}, {"name": "J. Guo"}],
        "venue": "Nature",
        "externalIds": {"DOI": "10.1038/nature01797", "ArXiv": "0307108"},
        "citationCount": 1742,
        "openAccessPdf": {"url": "https://example.com/paper.pdf"},
        "abstract": "We report on ballistic CNT-FETs operating near…",
    }
    out = _format_paper(paper)
    assert "## Carbon nanotube field-effect transistors (2003)" in out
    assert "A. Javey, J. Guo" in out
    assert "Nature" in out
    assert "1742" in out
    assert "10.1038/nature01797" in out
    assert "https://doi.org/10.1038/nature01797" in out
    assert "0307108" in out
    assert "https://arxiv.org/abs/0307108" in out
    assert "https://example.com/paper.pdf" in out
    assert "ballistic CNT-FETs" in out


def test_format_paper_minimal_record_no_extras() -> None:
    """A bare hit (no abstract / no externalIds / no venue) renders
    cleanly without raising — we just lose the absent fields."""
    paper = {"title": "Anon work", "year": 2001, "authors": []}
    out = _format_paper(paper)
    assert "## Anon work (2001)" in out
    # No section markers for missing fields.
    assert "DOI" not in out
    assert "arXiv" not in out
    assert "Venue" not in out


def test_format_paper_truncates_long_author_lists() -> None:
    """Six authors then ``et al. (N authors)`` to keep the block tight."""
    paper = {
        "title": "Many-author paper",
        "year": 2024,
        "authors": [{"name": f"Author{i}"} for i in range(15)],
    }
    out = _format_paper(paper)
    assert "Author5" in out
    assert "et al. (15 authors)" in out


def test_format_paper_untitled_fallback() -> None:
    """``(untitled)`` placeholder so the heading still renders."""
    paper = {"year": 2020}
    out = _format_paper(paper)
    assert "## (untitled) (2020)" in out


# ---- Canonical key + slug ------------------------------------------


@pytest.fixture
def handler() -> object:
    """A stub handler instance — bypassing ``__init__`` since we only
    exercise the pure-function methods on ``CacheBackedHandler``."""
    from precis.handlers.semanticscholar import SemanticScholarHandler

    return SemanticScholarHandler.__new__(SemanticScholarHandler)


def test_canonical_key_lowercases_and_collapses_whitespace(handler) -> None:
    assert (
        handler._canonical_key("  Carbon  Nanotube  TRANSISTORS ")
        == "carbon nanotube transistors"
    )


def test_canonical_key_rejects_empty_query(handler) -> None:
    from precis.errors import BadInput

    with pytest.raises(BadInput):
        handler._canonical_key("")
    with pytest.raises(BadInput):
        handler._canonical_key("   ")


def test_slug_from_query(handler) -> None:
    """The slug is a kebab from the canonical key, with a fallback for
    queries that slugify to empty."""
    slug = handler._slug_for("carbon nanotube transistors")
    assert "carbon" in slug
    assert "transistors" in slug


def test_recover_key_from_cache_meta(handler) -> None:
    """A cached ref can re-fetch from its meta-stored query string."""
    from types import SimpleNamespace

    ref = SimpleNamespace()
    cache = SimpleNamespace(meta={"query": "graphene heterojunctions"})
    assert handler._recover_key(ref, cache) == "graphene heterojunctions"


def test_provider_is_registered_slug() -> None:
    """The handler must stamp a provider that exists in the providers
    table. Semantic Scholar is registered under the slug ``s2`` — the
    literal ``semanticscholar`` is NOT a row, so stamping it FK-violated
    on every cache write (gripe #39242)."""
    from precis.handlers.semanticscholar import SemanticScholarHandler

    assert SemanticScholarHandler.provider == "s2"


# ---- Citation-graph navigation (refs: / cites:) --------------------


def test_canonical_key_passes_nav_prefix_through(handler) -> None:
    """``refs:`` / ``cites:`` survive canonicalisation as distinct cache
    keys; the identifier is lower-cased (safe for DOI / arXiv / S2)."""
    assert handler._canonical_key("refs:10.1038/Nature12373") == (
        "refs:10.1038/nature12373"
    )
    assert handler._canonical_key("  CITES: 10.x/Y ") == "cites:10.x/y"


def test_canonical_key_nav_prefix_requires_identifier(handler) -> None:
    from precis.errors import BadInput

    with pytest.raises(BadInput):
        handler._canonical_key("refs:")
    with pytest.raises(BadInput):
        handler._canonical_key("cites:   ")


def test_parse_nav_key(handler) -> None:
    assert handler._parse_nav_key("refs:10.x/y") == ("refs", "10.x/y")
    assert handler._parse_nav_key("cites:abc123") == ("cites", "abc123")
    # A plain search key is not a nav key.
    assert handler._parse_nav_key("carbon nanotubes") is None


def test_s2_path_id_maps_bare_and_prefixed_ids(handler) -> None:
    # Bare DOI / arXiv get auto-prefixed for the S2 path.
    assert handler._s2_path_id("10.1038/nature12373") == "DOI:10.1038/nature12373"
    assert handler._s2_path_id("2401.00001") == "ARXIV:2401.00001"
    assert handler._s2_path_id("2401.00001v2") == "ARXIV:2401.00001v2"
    # Explicit prefixes normalise; s2: drops to the bare hash.
    assert handler._s2_path_id("doi:10.x/y") == "DOI:10.x/y"
    assert handler._s2_path_id("arxiv:2401.00001") == "ARXIV:2401.00001"
    assert handler._s2_path_id("s2:abcdef0123") == "abcdef0123"
    assert handler._s2_path_id("CorpusId:215416146") == "CorpusId:215416146"
    # An unrecognised shape is assumed to be a raw S2 hash, passed through.
    assert handler._s2_path_id("deadbeefcafe") == "deadbeefcafe"


def _refs_payload() -> dict:
    """A minimal ``/paper/{id}/references`` response — neighbour nested
    under ``citedPaper`` (the shape the endpoint actually returns)."""
    return {
        "data": [
            {
                "citedPaper": {
                    "title": "Ballistic carbon nanotube transistors",
                    "year": 1998,
                    "authors": [{"name": "S. Tans"}],
                    "externalIds": {"DOI": "10.1038/29954"},
                    "citationCount": 4200,
                }
            },
            {"citedPaper": None},  # S2 returns nulls for unresolved refs
        ]
    }


def test_fetch_graph_references(handler, monkeypatch) -> None:
    """``refs:`` hits the references endpoint, lifts ``citedPaper``,
    drops null rows, and renders one block per neighbour."""
    captured: dict = {}

    def fake_get(url, params):
        captured["url"] = url
        captured["params"] = params
        return _refs_payload()

    monkeypatch.setattr(handler, "_s2_get_json", fake_get)
    result = handler._fetch("refs:10.1038/nature12373")

    assert captured["url"].endswith("/paper/DOI:10.1038/nature12373/references")
    assert len(result.body_blocks) == 1  # the null row is dropped
    assert "Ballistic carbon nanotube transistors" in result.body_blocks[0].text
    assert "10.1038/29954" in result.body_blocks[0].text  # DOI to feed a stub
    assert result.meta["nav"] == "refs"
    assert result.meta["result_count"] == 1


def test_fetch_graph_citations_endpoint(handler, monkeypatch) -> None:
    """``cites:`` hits the citations endpoint and reads ``citingPaper``."""
    captured: dict = {}

    def fake_get(url, params):
        captured["url"] = url
        return {"data": [{"citingPaper": {"title": "Later work", "year": 2020}}]}

    monkeypatch.setattr(handler, "_s2_get_json", fake_get)
    result = handler._fetch("cites:2401.00001")

    assert captured["url"].endswith("/paper/ARXIV:2401.00001/citations")
    assert "Later work" in result.body_blocks[0].text
    assert result.meta["nav"] == "cites"


def test_fetch_graph_empty_is_not_an_error(handler, monkeypatch) -> None:
    """A paper with no recorded references yields a friendly empty body,
    not a raise — the agent learns the graph is bare here."""
    monkeypatch.setattr(handler, "_s2_get_json", lambda url, params: {"data": []})
    result = handler._fetch("refs:10.x/y")
    assert result.meta["result_count"] == 0
    assert "No references found" in result.body_blocks[0].text


# ---------------------------------------------------------------------------
# Corpus-diff (held/stub/NEW) + exclude= on a plain topic search —
# docs/backlog/discovery-exclude-by-container.md. DB-backed: real
# store/hub fixtures (the diff walks real refs/ref_identifiers rows, and
# exclude= reads a real draft); only the outbound S2 HTTP call
# (``_s2_get_json``) is mocked, so no live network.
# ---------------------------------------------------------------------------


@pytest.fixture
def s2handler(hub: Hub, store: Store) -> SemanticScholarHandler:
    # ``kinds`` is a code-driven registry (upserted at ``boot()`` time from
    # every registered KindSpec, not migration-seeded — see
    # ``store/_kinds_ops.py``); these tests build the handler directly
    # rather than booting the whole Hub, so seed the row by hand.
    store.upsert_kinds([SemanticScholarHandler.spec])
    return SemanticScholarHandler(hub=hub)


def _mk_paper_with_doi(store: Store, *, slug: str, doi: str, held: bool) -> int:
    """A paper ref carrying ``doi`` as an alias, held (a PDF on file) or a
    bare stub (``pdf_sha256 IS NULL``)."""
    ref = store.insert_ref(kind="paper", slug=slug, title=f"Title {slug}", meta={})
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
            "VALUES (%s, 'doi', %s, 'manual')",
            (ref.id, doi),
        )
        if held:
            sha = f"{ref.id:064d}"
            conn.execute(
                "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
                "size_bytes, storage_path) VALUES (%s, %s, 1, 100, '/tmp/held') "
                "ON CONFLICT (pdf_sha256) DO NOTHING",
                (sha, sha),
            )
            conn.execute(
                "UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s", (sha, ref.id)
            )
    return ref.id


def _s2_hit(*, title: str, doi: str | None = None, year: int = 2021) -> dict:
    return {
        "title": title,
        "year": year,
        "authors": [{"name": "Some Author"}],
        "externalIds": {"DOI": doi} if doi else {},
        "abstract": f"Abstract for {title}.",
    }


def test_topic_search_flags_held_stub_and_new(
    store: Store, s2handler: SemanticScholarHandler, monkeypatch
) -> None:
    held_id = _mk_paper_with_doi(
        store, slug="held-paper", doi="10.1234/held-paper", held=True
    )
    stub_id = _mk_paper_with_doi(
        store, slug="stub-paper", doi="10.1234/stub-paper", held=False
    )

    hits = [
        _s2_hit(title="Held Paper", doi="10.1234/held-paper"),
        _s2_hit(title="Stub Paper", doi="10.1234/stub-paper"),
        _s2_hit(title="Brand New Paper", doi="10.1234/brand-new"),
    ]
    monkeypatch.setattr(
        s2handler, "_s2_get_json", lambda url, params: {"data": hits, "total": 3}
    )
    resp = s2handler.get(id="widget catalysis")
    body = resp.body
    assert f"held: pa{held_id}" in body
    assert f"stub: pa{stub_id}" in body
    assert "_Corpus:_ NEW" in body
    assert "1 NEW, 1 held, 1 stub" in body
    # Accept-path nudge only shows up when a NEW hit is present.
    assert "put(kind='paper', doi=" in body


def test_topic_search_no_new_hits_no_accept_nudge(
    store: Store, s2handler: SemanticScholarHandler, monkeypatch
) -> None:
    held_id = _mk_paper_with_doi(
        store, slug="only-held", doi="10.1234/only-held", held=True
    )
    hits = [_s2_hit(title="Only Held", doi="10.1234/only-held")]
    monkeypatch.setattr(
        s2handler, "_s2_get_json", lambda url, params: {"data": hits, "total": 1}
    )
    resp = s2handler.get(id="only held query")
    assert f"held: pa{held_id}" in resp.body
    assert "put(kind='paper', doi=" not in resp.body


def test_topic_search_exclude_drops_cited_hit(
    store: Store, hub: Hub, s2handler: SemanticScholarHandler, monkeypatch
) -> None:
    """A hit matching a paper cited by an excluded draft is dropped from
    the render entirely — the exclude= container semantics apply to the
    S2 surface the same way they do to ``search(kind='paper')``."""
    from precis.handlers.draft import DraftHandler
    from precis.handlers.todo import TodoHandler
    from precis.utils import handle_registry

    cited_id = _mk_paper_with_doi(
        store, slug="cited-in-draft", doi="10.1234/cited-in-draft", held=True
    )
    proj = TodoHandler(hub=hub).put(text="proj", meta={"rotation_root": True})
    proj_id = int(proj.body.split("id=")[1].split()[0].rstrip(",.()"))
    draft = DraftHandler(hub=hub)
    draft.put(id="s2-exclude-draft", title="Cites it", project=proj_id)
    handle = handle_registry.format_handle("paper", cited_id)
    draft.put(
        id="s2-exclude-draft",
        chunk_kind="paragraph",
        text=f"builds on [{handle}]",
        at={"last": True},
    )
    draft_ref = store.get_ref(kind="draft", id="s2-exclude-draft")
    assert draft_ref is not None

    hits = [
        _s2_hit(title="Cited In Draft", doi="10.1234/cited-in-draft"),
        _s2_hit(title="Unrelated Paper", doi="10.1234/unrelated"),
    ]
    monkeypatch.setattr(
        s2handler, "_s2_get_json", lambda url, params: {"data": hits, "total": 2}
    )

    resp = s2handler.get(id="topic query", exclude=[f"dr{draft_ref.id}"])
    assert f"pa{cited_id}" not in resp.body
    assert "Cited In Draft" not in resp.body
    assert "Unrelated Paper" in resp.body
    assert "1 excluded" in resp.body


def test_topic_search_no_hits_renders_no_results_message(
    store: Store, s2handler: SemanticScholarHandler, monkeypatch
) -> None:
    monkeypatch.setattr(s2handler, "_s2_get_json", lambda url, params: {"data": []})
    resp = s2handler.get(id="nothing matches this at all")
    assert "No Semantic Scholar results" in resp.body


def test_topic_search_cache_hit_rediffs_on_state_change(
    store: Store, hub: Hub, s2handler: SemanticScholarHandler, monkeypatch
) -> None:
    """The corpus-diff (and exclude=) MUST re-run at render time on every
    call, including a cache hit — a raw S2 hit is cached once, but the
    held corpus and the caller's exclude= are both call-time-varying, so
    a stale bake-in-at-fetch would silently regress "re-diff on every
    call" into "diff once, 30 days stale." Pin the two ways state can
    move between two calls of the SAME cached query:

    1. A hit that was ``NEW`` on call 1 becomes ``stub: pa…`` on call 2
       after a matching paper is minted in between — with NO second S2
       fetch (``_s2_get_json`` raises if called twice).
    2. The SAME cache hit drops a paper from the render when call 2
       passes an ``exclude=`` call 1 didn't.
    """
    from precis.handlers.draft import DraftHandler
    from precis.handlers.todo import TodoHandler
    from precis.utils import handle_registry

    call_count = 0

    def fake_get(url: str, params: dict) -> dict:
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            raise AssertionError(
                "a second S2 fetch means the render is baking flags in at "
                "fetch time instead of re-diffing per call"
            )
        return {
            "data": [
                _s2_hit(title="Rediff Target", doi="10.1234/rediff-target"),
                _s2_hit(title="Always Unrelated", doi="10.1234/always-unrelated"),
            ],
            "total": 2,
        }

    monkeypatch.setattr(s2handler, "_s2_get_json", fake_get)

    # Call 1: nothing in the corpus matches yet — both hits render NEW.
    first = s2handler.get(id="rediff query")
    assert call_count == 1
    assert "_Corpus:_ NEW" in first.body
    assert "Rediff Target" in first.body
    assert "held:" not in first.body
    assert "stub:" not in first.body

    # Corpus state changes: a paper matching the first hit's DOI shows up.
    rediff_id = _mk_paper_with_doi(
        store, slug="rediff-target", doi="10.1234/rediff-target", held=False
    )

    # Call 2: SAME cached query (still within TTL) — no second S2 fetch —
    # but the render now flags the newly-minted stub.
    second = s2handler.get(id="rediff query")
    assert call_count == 1, "expected a cache hit, not a re-fetch"
    assert f"stub: pa{rediff_id}" in second.body
    assert "Always Unrelated" in second.body

    # Now also exercise exclude= varying across cache-hit calls: exclude
    # the paper that just started matching, via a draft that cites it.
    proj = TodoHandler(hub=hub).put(text="proj", meta={"rotation_root": True})
    proj_id = int(proj.body.split("id=")[1].split()[0].rstrip(",.()"))
    draft = DraftHandler(hub=hub)
    draft.put(id="rediff-exclude-draft", title="Cites rediff target", project=proj_id)
    handle = handle_registry.format_handle("paper", rediff_id)
    draft.put(
        id="rediff-exclude-draft",
        chunk_kind="paragraph",
        text=f"builds on [{handle}]",
        at={"last": True},
    )
    draft_ref = store.get_ref(kind="draft", id="rediff-exclude-draft")
    assert draft_ref is not None

    third = s2handler.get(id="rediff query", exclude=[f"dr{draft_ref.id}"])
    assert call_count == 1, "expected a cache hit, not a re-fetch"
    assert f"pa{rediff_id}" not in third.body
    assert "Rediff Target" not in third.body
    assert "Always Unrelated" in third.body


# ---------------------------------------------------------------------------
# Author-papers view (``author:<id>``) — default single page vs.
# ``args={'complete': True}``'s full paginated walk (gr346833: the 50-work
# cap silently hid a 190-citation JACS paper the corpus already held).
# ---------------------------------------------------------------------------


def _author_work(i: int, **over: object) -> dict:
    base: dict = {
        "title": f"Work {i}",
        "year": 2000 + (i % 20),
        "citationCount": i,
        "paperId": f"p{i}",
        "externalIds": {},
    }
    base.update(over)
    return base


def test_canonical_key_author_complete_gets_a_distinct_suffix(handler) -> None:
    """``complete=True`` (threaded via ``_pending_complete``, mirroring
    ``_pending_exclude``) folds into the canonical key so the walked
    listing caches separately from the capped default page."""
    handler._pending_complete = False
    assert handler._canonical_key("author:1741101") == "author:1741101"
    handler._pending_complete = True
    try:
        assert handler._canonical_key("author:1741101") == "author:1741101:complete"
    finally:
        handler._pending_complete = False


def test_format_author_papers_table_sorts_truncates_and_maps_corpus() -> None:
    from precis.handlers.semanticscholar import _format_author_papers_table

    long_title = "A" * 90
    papers = [
        {"title": long_title, "year": 2001, "citationCount": 2, "paperId": "low"},
        {
            "title": "High cite work",
            "year": 2010,
            "citationCount": 190,
            "paperId": "high",
        },
        {"title": "No cite count", "year": 2020, "paperId": "none"},
    ]
    flags = [("stub: pa5", 5), ("held: pa9", 9), ("NEW", None)]
    table = _format_author_papers_table(papers, flags)
    rows = table.splitlines()[2:]  # drop the header + separator rows

    assert rows[0].split("|")[1].strip() == "2010"  # highest cites sorts first
    assert "held pa9" in rows[0]
    assert "—" in rows[-1]  # NEW (no citation count) sorts last

    trunc_row = next(r for r in rows if "stub pa5" in r)
    assert "…" in trunc_row
    assert long_title not in trunc_row


def test_fetch_author_papers_default_shows_exact_capped_hint(
    store: Store, s2handler: SemanticScholarHandler, monkeypatch
) -> None:
    """The default (no ``complete=``) page replaces the old vague
    "capped at 50" with the exact line the gripe specifies, carrying
    the API-reported total when available."""
    papers = [_author_work(i) for i in range(50)]
    monkeypatch.setattr(
        s2handler, "_s2_get_json", lambda url, params: {"data": papers, "total": 120}
    )
    result = s2handler._fetch("author:1741101")
    body = result.body_blocks[0].text
    assert "showing 50 of 120 — args={'complete': True} for all" in body
    assert result.meta["result_count"] == 50
    assert result.meta["capped"] is True
    assert result.meta["complete"] is False


def test_fetch_author_papers_default_under_limit_has_no_capped_hint(
    store: Store, s2handler: SemanticScholarHandler, monkeypatch
) -> None:
    """An author with fewer than the page size has no "showing X of Y"
    footer — the page already holds everything."""
    papers = [_author_work(i) for i in range(3)]
    monkeypatch.setattr(s2handler, "_s2_get_json", lambda url, params: {"data": papers})
    result = s2handler._fetch("author:1741101")
    body = result.body_blocks[0].text
    assert "showing" not in body
    assert result.meta["capped"] is False


def test_fetch_author_papers_complete_walks_pages_ranks_and_flags_corpus(
    store: Store, s2handler: SemanticScholarHandler, monkeypatch
) -> None:
    """``complete=True`` walks BOTH pages of a 120-work author, renders
    one ranked (citations desc) table, and resolves the corpus column
    against real held/stub refs."""
    held_id = _mk_paper_with_doi(
        store, slug="author-held", doi="10.1234/author-held", held=True
    )
    stub_id = _mk_paper_with_doi(
        store, slug="author-stub", doi="10.1234/author-stub", held=False
    )

    page1 = [_author_work(i) for i in range(100)]
    page1[0] = _author_work(
        0, citationCount=190, externalIds={"DOI": "10.1234/author-held"}
    )
    page1[1] = _author_work(
        1, citationCount=5, externalIds={"DOI": "10.1234/author-stub"}
    )
    page2 = [_author_work(i) for i in range(100, 120)]

    calls: list[dict] = []

    def fake_backoff(url: str, params: dict) -> tuple[dict, int]:
        calls.append(dict(params))
        offset = params["offset"]
        if offset == 0:
            return {"data": page1, "next": 100}, 1
        if offset == 100:
            return {"data": page2}, 1
        raise AssertionError(f"unexpected offset {offset}")

    monkeypatch.setattr(s2handler, "_s2_get_json_backoff", fake_backoff)
    result = s2handler._fetch("author:1741101:complete")
    body = "\n\n".join(b.text for b in result.body_blocks)

    assert len(calls) == 2
    assert "120 works (complete, 2 requests)" in body
    assert result.meta["result_count"] == 120
    assert result.meta["complete"] is True
    assert result.meta["s2_requests"] == 2
    assert result.meta["s2_requests_retried"] == 0

    lines = body.splitlines()
    table_start = lines.index("| year | cites | corpus | title | s2 id |")
    first_row = lines[table_start + 2]
    assert "190" in first_row and f"held pa{held_id}" in first_row
    assert f"stub pa{stub_id}" in body


def test_fetch_author_papers_complete_caps_title_fallback_at_200(
    store: Store, s2handler: SemanticScholarHandler, monkeypatch
) -> None:
    """300 identifier-less works in complete mode: only the first 200
    actually pay a title-similarity lookup; the rest render ``?`` in the
    corpus column with a footnote explaining why (reviewer finding 4 on
    83c0abca)."""

    def _idless_work(i: int) -> dict:
        return {
            "title": f"Untitled Work Number {i}",
            "year": 2000 + (i % 20),
            "citationCount": i,
            "paperId": f"q{i}",
            "externalIds": {},
        }

    pages = [[_idless_work(i) for i in range(n, n + 100)] for n in (0, 100, 200)]

    def fake_backoff(url: str, params: dict) -> tuple[dict, int]:
        offset = params["offset"]
        idx = offset // 100
        result: dict = {"data": pages[idx]}
        if idx + 1 < len(pages):
            result["next"] = offset + 100
        return result, 1

    monkeypatch.setattr(s2handler, "_s2_get_json_backoff", fake_backoff)

    lookups: list[str] = []

    def fake_title_lookup(
        *, kind: str, q: str, limit: int, min_similarity: float
    ) -> list:
        lookups.append(q)
        return []

    monkeypatch.setattr(store, "find_refs_by_title_similarity", fake_title_lookup)

    result = s2handler._fetch("author:1741101:complete")
    body = "\n\n".join(b.text for b in result.body_blocks)

    assert len(lookups) == 200
    assert result.meta["result_count"] == 300
    assert body.count("| ? |") == 100
    assert "not resolvable by id" in body
    assert "capped at 200" in body


def test_fetch_author_papers_complete_stops_at_hard_cap(
    store: Store, s2handler: SemanticScholarHandler, monkeypatch
) -> None:
    """A tail that never exhausts stops at the hard cap, with an exact
    "stopped at N" note rather than fetching forever."""
    monkeypatch.setattr("precis.handlers.semanticscholar._AUTHOR_COMPLETE_HARD_CAP", 5)

    def fake_backoff(url: str, params: dict) -> tuple[dict, int]:
        offset = params["offset"]
        return (
            {
                "data": [_author_work(offset + j) for j in range(3)],
                "next": offset + 3,
            },
            1,
        )

    monkeypatch.setattr(s2handler, "_s2_get_json_backoff", fake_backoff)
    result = s2handler._fetch("author:1741101:complete")
    assert result.meta["result_count"] == 5
    assert result.meta["stopped_at_cap"] is True
    assert "stopped at 5 works" in result.body_blocks[0].text


def test_fetch_author_papers_complete_exact_cap_with_no_next_has_no_more_remain(
    store: Store, s2handler: SemanticScholarHandler, monkeypatch
) -> None:
    """Landing exactly on the hard cap on a page that ALSO reports no
    further ``next`` is a normal exhaustion, not a truncation — no
    "more remain" note (reviewer finding 1 on 83c0abca)."""
    monkeypatch.setattr("precis.handlers.semanticscholar._AUTHOR_COMPLETE_HARD_CAP", 6)

    def fake_backoff(url: str, params: dict) -> tuple[dict, int]:
        offset = params["offset"]
        if offset == 0:
            return {"data": [_author_work(i) for i in range(3)], "next": 3}, 1
        # Final page lands exactly on the cap (3 + 3 == 6) with no next.
        return {"data": [_author_work(i) for i in range(3, 6)]}, 1

    monkeypatch.setattr(s2handler, "_s2_get_json_backoff", fake_backoff)
    result = s2handler._fetch("author:1741101:complete")
    body = result.body_blocks[0].text
    assert result.meta["result_count"] == 6
    assert result.meta["stopped_at_cap"] is False
    assert "more remain" not in body
    assert "stopped at" not in body


def test_walk_author_papers_dedupes_a_replayed_page(
    store: Store, s2handler: SemanticScholarHandler, monkeypatch
) -> None:
    """A page replayed verbatim (same ``paperId``s, at an advancing
    offset) contributes zero new rows, doesn't count toward the cap, and
    ends the walk instead of looping forever (reviewer finding 2)."""
    page1 = [_author_work(i) for i in range(5)]
    calls = {"n": 0}

    def fake_backoff(url: str, params: dict) -> tuple[dict, int]:
        calls["n"] += 1
        offset = params["offset"]
        if offset == 0:
            return {"data": page1, "next": 5}, 1
        # Page 2 replays page 1's exact paperIds at a new (advancing)
        # offset — every row is a duplicate.
        return {"data": page1, "next": 10}, 1

    monkeypatch.setattr(s2handler, "_s2_get_json_backoff", fake_backoff)
    result = s2handler._fetch("author:1741101:complete")
    body = result.body_blocks[0].text
    assert calls["n"] == 2  # walk stops right after the all-duplicate page
    assert result.meta["result_count"] == 5  # no duplicate rows counted
    assert result.meta["stopped_at_cap"] is False
    assert "stopped at" not in body


def test_walk_author_papers_stops_on_non_advancing_offset(
    store: Store, s2handler: SemanticScholarHandler, monkeypatch, caplog
) -> None:
    """A ``next`` that doesn't advance past the offset just fetched (a
    malformed/buggy API response) logs one warning and stops the walk
    rather than looping forever re-requesting the same page (reviewer
    finding 2)."""

    def fake_backoff(url: str, params: dict) -> tuple[dict, int]:
        offset = params["offset"]
        # `next` never advances past 0 no matter what offset was asked.
        return {"data": [_author_work(offset)], "next": 0}, 1

    monkeypatch.setattr(s2handler, "_s2_get_json_backoff", fake_backoff)
    with caplog.at_level("WARNING", logger="precis.handlers.semanticscholar"):
        result = s2handler._fetch("author:1741101:complete")
    assert result.meta["result_count"] == 1
    assert any("non-advancing offset" in r.message for r in caplog.records)


def test_walk_author_papers_backoff_retries_a_429_mid_walk(
    store: Store, s2handler: SemanticScholarHandler, monkeypatch
) -> None:
    """A 429 hit on the SECOND page (mid-walk) is retried with
    exponential backoff instead of aborting the whole walk."""
    sleeps: list[float] = []
    monkeypatch.setattr("precis.handlers.semanticscholar.time.sleep", sleeps.append)

    class _FakeResp:
        def __init__(self, status_code: int, payload: dict | None = None) -> None:
            self.status_code = status_code
            self._payload = payload
            self.text = "rate limited" if status_code == 429 else ""

        def json(self) -> dict:
            assert self._payload is not None
            return self._payload

    calls = {"n": 0}

    def fake_raw(url: str, params: dict) -> _FakeResp:
        calls["n"] += 1
        offset = params["offset"]
        if offset == 0:
            return _FakeResp(200, {"data": [_author_work(0)], "next": 1})
        # Page 2: first attempt 429s, the retry succeeds and exhausts.
        if calls["n"] == 2:
            return _FakeResp(429)
        return _FakeResp(200, {"data": [_author_work(1)]})

    monkeypatch.setattr(s2handler, "_s2_raw_get", fake_raw)
    result = s2handler._fetch("author:1741101:complete")
    body = "\n\n".join(b.text for b in result.body_blocks)

    assert calls["n"] == 3
    assert sleeps == [1.0]
    assert result.meta["result_count"] == 2
    # 3 actual HTTP calls total (page 1, page 2's 429, page 2's retry) —
    # 1 of those 3 was a retry (gr356740: the render names the request
    # cost of a complete=True walk).
    assert result.meta["s2_requests"] == 3
    assert result.meta["s2_requests_retried"] == 1
    assert "2 works (complete, 3 requests, 1 retried)." in body
    assert body.count("works (complete,") == 1


def test_s2_get_json_backoff_raises_after_5_consecutive_429s(
    s2handler: SemanticScholarHandler, monkeypatch
) -> None:
    """5 consecutive 429s exhaust the retry budget: 4 sleeps (between
    attempts 1-4 and 2-5), then an ``Upstream`` with the exact
    "retries exhausted" message — the previous trailing raise for this
    was unreachable (reviewer finding 3 on 83c0abca)."""
    from precis.errors import Upstream

    sleeps: list[float] = []
    monkeypatch.setattr("precis.handlers.semanticscholar.time.sleep", sleeps.append)

    class _FakeResp:
        status_code = 429
        text = "rate limited"

        def json(self) -> dict:
            raise AssertionError("a 429 must never be JSON-parsed")

    monkeypatch.setattr(s2handler, "_s2_raw_get", lambda url, params: _FakeResp())

    with pytest.raises(Upstream, match="retries exhausted"):
        s2handler._s2_get_json_backoff("http://example/author/x/papers", {})

    assert sleeps == [1.0, 2.0, 4.0, 8.0]


def test_get_complete_kwarg_threads_to_a_distinct_cache_row(
    store: Store, s2handler: SemanticScholarHandler, monkeypatch
) -> None:
    """``get(..., complete=True)`` — the python-kwarg landing spot for
    ``args={'complete': True}`` (dispatch.py flattens ``args=`` into top-
    level kwargs against the handler's own explicit signature) — caches
    under a key distinct from the default page, so each fetch path runs
    exactly once even when both are called for the same author."""
    calls = {"default": 0, "complete": 0}

    def fake_default(url: str, params: dict) -> dict:
        calls["default"] += 1
        return {"data": [_author_work(0)], "total": 1}

    def fake_complete(url: str, params: dict) -> tuple[dict, int]:
        calls["complete"] += 1
        return {"data": [_author_work(1, citationCount=9)]}, 1

    monkeypatch.setattr(s2handler, "_s2_get_json", fake_default)
    monkeypatch.setattr(s2handler, "_s2_get_json_backoff", fake_complete)

    default_resp = s2handler.get(id="author:9999999")
    complete_resp = s2handler.get(id="author:9999999", complete=True)

    assert calls == {"default": 1, "complete": 1}
    assert "1 works (complete, 1 request)." in complete_resp.body
    assert default_resp.body != complete_resp.body


def test_get_author_complete_summary_line_renders_once_with_request_count(
    store: Store, s2handler: SemanticScholarHandler, monkeypatch
) -> None:
    """Full ``get()`` round trip (store + ``_render()``'s chunk-rejoin),
    not just ``_fetch()`` — a big enough table pushes the combined body
    past ``chunk_target_chars`` and into the base class's auto-splitter,
    which is where the summary line used to print twice (gr356740): a
    single "note\\n\\ntable" blob got auto-split with the short note
    carried forward as an undetected overlap tail. Also covers the
    request-count addition the same gripe asked for."""
    papers = [_author_work(i) for i in range(150)]

    def fake_backoff(url: str, params: dict) -> tuple[dict, int]:
        return {"data": papers}, 1

    monkeypatch.setattr(s2handler, "_s2_get_json_backoff", fake_backoff)

    resp = s2handler.get(id="author:1741101", complete=True)

    assert resp.body.count("150 works (complete, 1 request).") == 1
    assert resp.body.count("Work 0") == 1
    assert resp.body.count("Work 149") == 1
