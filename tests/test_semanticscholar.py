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
# author:<id> complete=True — the whole bibliography as one worklist table
# (gr346833). The one-page read hid an established author's back
# catalogue (S2 ranks recent + highly-cited) and offered no way past it.
# ---------------------------------------------------------------------------


def _author_paper(i: int, *, year: int, doi: str | None = None, cited: int = 0) -> dict:
    return {
        "paperId": f"p{i}",
        "title": f"Work {i}",
        "year": year,
        "venue": "J. Test",
        "citationCount": cited,
        "externalIds": {"DOI": doi} if doi else {},
        "authors": [{"name": "M. Pryce"}],
    }


class _PagedS2:
    """Fake ``/author/{id}/papers`` honouring limit/offset + ``next``."""

    def __init__(self, papers: list[dict]) -> None:
        self.papers = papers
        self.calls: list[dict] = []

    def __call__(self, url: str, params: dict) -> dict:
        self.calls.append(dict(params))
        assert url.endswith("/papers")
        off = int(params.get("offset", 0))
        lim = int(params["limit"])
        page = self.papers[off : off + lim]
        out: dict = {"offset": off, "data": page}
        if off + lim < len(self.papers):
            out["next"] = off + lim
        return out


def test_complete_walks_every_page_and_renders_one_table(
    store: Store, s2handler: SemanticScholarHandler, monkeypatch
) -> None:
    held_id = _mk_paper_with_doi(
        store, slug="motor", doi="10.1021/ja8037245", held=True
    )
    stub_id = _mk_paper_with_doi(store, slug="early", doi="10.1000/early", held=False)
    papers = [_author_paper(i, year=2024 - i // 10) for i in range(123)]
    # The 2008 motor paper sits deep in the list — exactly what the top-50
    # page dropped.
    papers[110] = _author_paper(110, year=2008, doi="10.1021/ja8037245", cited=190)
    papers[120] = _author_paper(120, year=2005, doi="10.1000/early")
    fake = _PagedS2(papers)
    monkeypatch.setattr(s2handler, "_s2_get_json", fake)

    resp = s2handler.get(id="author:8890863", complete=True)
    body = resp.body
    # ceil(123/50) = 3 requests, offsets 0/50/100.
    assert [c.get("offset", 0) for c in fake.calls] == [0, 50, 100]
    assert "123 works" in body and "3 S2 request(s)" in body
    assert "{year\tcited\ttitle\tvenue\tdoi\tcorpus}" in body
    assert f"2008\t190\tWork 110\tJ. Test\t10.1021/ja8037245\theld: pa{held_id}" in body
    assert f"2005\t0\tWork 120\tJ. Test\t10.1000/early\tstub: pa{stub_id}" in body
    assert "121 NEW, 1 stub, 1 held" in body
    # Grouped NEW → stub → held: the missing ones read off in one block.
    assert body.index("\tNEW") < body.index("\tstub: ") < body.index("\theld: ")
    assert "put(kind='paper', doi=" in body
    assert "NOT complete" not in body


def test_complete_stops_at_the_page_ceiling_and_says_so(
    store: Store, s2handler: SemanticScholarHandler, monkeypatch
) -> None:
    from precis.handlers import semanticscholar as mod

    monkeypatch.setattr(mod, "_AUTHOR_PAPERS_MAX_PAGES", 2)
    fake = _PagedS2([_author_paper(i, year=2020) for i in range(130)])
    monkeypatch.setattr(s2handler, "_s2_get_json", fake)
    resp = s2handler.get(id="author:1", complete=True)
    assert len(fake.calls) == 2
    assert "100 works" in resp.body
    assert "NOT complete" in resp.body


def test_complete_is_its_own_cache_row_and_rediffs_on_hit(
    store: Store, s2handler: SemanticScholarHandler, monkeypatch
) -> None:
    """The top-50 page and the complete walk are different artifacts, so
    they cache under different keys; a second complete read is a cache
    hit (no refetch) whose corpus column reflects a paper minted since."""
    fake = _PagedS2([_author_paper(i, year=2020, doi=f"10.1000/w{i}") for i in range(3)])
    monkeypatch.setattr(s2handler, "_s2_get_json", fake)
    one_page = s2handler.get(id="author:7")
    assert "3 shown" in one_page.body
    assert "args={'complete': True}" not in one_page.body  # under the cap

    full = s2handler.get(id="author:7", complete=True)
    assert "3 works" in full.body and "3 NEW" in full.body
    n_calls = len(fake.calls)

    pid = _mk_paper_with_doi(store, slug="w1", doi="10.1000/w1", held=False)
    again = s2handler.get(id="author:7", complete=True)
    assert len(fake.calls) == n_calls  # cache hit
    assert f"stub: pa{pid}" in again.body and "2 NEW, 1 stub" in again.body


def test_one_page_capped_read_points_at_complete(
    store: Store, s2handler: SemanticScholarHandler, monkeypatch
) -> None:
    fake = _PagedS2([_author_paper(i, year=2020) for i in range(50)])
    monkeypatch.setattr(s2handler, "_s2_get_json", fake)
    resp = s2handler.get(id="author:9")
    assert "capped" in resp.body
    assert "id='author:9', args={'complete': True}" in resp.body
    assert len(fake.calls) == 1


def test_complete_rejected_off_the_author_path(
    s2handler: SemanticScholarHandler,
) -> None:
    from precis.errors import BadInput

    with pytest.raises(BadInput):
        s2handler.get(id="refs:10.1/x", complete=True)
    with pytest.raises(BadInput):
        s2handler.get(id="molecular motors", complete=True)
