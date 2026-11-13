"""Field-scoped paper lookup — ``search(kind='paper', title=…/author=…)``.

The block-hit path buries an exact-title query below content-dense
bodies and, for a bare author name, surfaces *other* papers' bibliography
lines instead of the paper itself (the combined card dilutes the byline).
``title=`` / ``author=`` match ``refs.title`` (trigram + FTS) /
``refs.authors`` (jsonb) directly and return paper *records* — the
handle + a one-line citation + a cite path — with held copies first.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import precis.handlers.paper as paper_mod
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.paper import PaperHandler
from precis.store import Store, Tag


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cohort tests close read-paper passes via ``put`` — stub the
    metadata lookups the mint side shares, like test_paper_unread."""
    monkeypatch.setattr(
        paper_mod, "_lookup_acquire_metadata", lambda *_: {"_resolved": True}
    )
    monkeypatch.setattr(paper_mod, "_lookup_doi_registry", lambda *_: None)


def _seed_paper(
    store: Store,
    *,
    slug: str,
    title: str,
    authors: list[str],
    year: int | None = None,
) -> int:
    ref = store.insert_ref(
        kind="paper",
        slug=slug,
        title=title,
        authors=[{"name": n} for n in authors],
        year=year,
    )
    return ref.id


def _seed_paper_entries(
    store: Store,
    *,
    slug: str,
    title: str,
    authors: list[dict[str, str]],
    year: int | None = None,
) -> int:
    ref = store.insert_ref(
        kind="paper",
        slug=slug,
        title=title,
        authors=authors,
        year=year,
    )
    return ref.id


def _handler(store: Store) -> PaperHandler:
    return PaperHandler(hub=Hub(store=store))


# ── store level ───────────────────────────────────────────────────


def test_find_papers_by_title_exact(store: Store) -> None:
    rid = _seed_paper(
        store,
        slug="attn",
        title="Attention Is All You Need",
        authors=["Ashish Vaswani", "Noam Shazeer"],
        year=2017,
    )
    _seed_paper(store, slug="other", title="A study of graphene oxide", authors=["X"])
    assert store.find_papers_by_title(kind="paper", q="attention is all you need") == [
        rid
    ]


def test_find_papers_by_title_partial(store: Store) -> None:
    """A few distinctive title words (FTS leg) still land the paper."""
    rid = _seed_paper(
        store,
        slug="attn",
        title="Attention Is All You Need",
        authors=["Ashish Vaswani"],
    )
    assert rid in store.find_papers_by_title(kind="paper", q="attention need")


def test_find_papers_by_author_surname(store: Store) -> None:
    """A bare surname matches the structured byline (substring)."""
    rid = _seed_paper(
        store,
        slug="attn",
        title="Attention Is All You Need",
        authors=["Ashish Vaswani", "Noam Shazeer"],
    )
    _seed_paper(store, slug="other", title="Unrelated", authors=["Jane Doe"])
    assert store.find_papers_by_author(kind="paper", q="Vaswani") == [rid]


def test_find_papers_by_author_no_match(store: Store) -> None:
    _seed_paper(store, slug="attn", title="T", authors=["Ashish Vaswani"])
    assert store.find_papers_by_author(kind="paper", q="Hinton") == []


def test_find_papers_by_author_given_family_shape(store: Store) -> None:
    """A canonical {given,family}-only byline (no 'name' key) is invisible
    to a bare ``elem->>'name'`` match — S0 fix: matched on the display
    form + the reversed "family, given" form (precis.utils.authors module docstring)."""
    rid = _seed_paper_entries(
        store,
        slug="luo-ge",
        title="Some canonical-byline paper",
        authors=[
            {"given": "Ronggang", "family": "Luo"},
            {"given": "Shengbo", "family": "Ge"},
        ],
    )
    _seed_paper(store, slug="other2", title="Unrelated", authors=["Jane Doe"])
    assert store.find_papers_by_author(kind="paper", q="Luo") == [rid]
    assert store.find_papers_by_author(kind="paper", q="Ronggang Luo") == [rid]
    assert store.find_papers_by_author(kind="paper", q="Luo, Ronggang") == [rid]


def test_find_papers_by_author_name_shape_still_matches(store: Store) -> None:
    """Legacy {name}-shape byline still matches as before (regression
    guard on the S0 rewrite)."""
    rid = _seed_paper(
        store,
        slug="attn3",
        title="A name-shape byline paper",
        authors=["Ashish Vaswani", "Noam Shazeer"],
    )
    assert store.find_papers_by_author(kind="paper", q="Vaswani") == [rid]


# ── handler level ─────────────────────────────────────────────────


def test_handler_title_returns_record_row(store: Store) -> None:
    rid = _seed_paper(
        store,
        slug="attn",
        title="Attention Is All You Need",
        authors=["Ashish Vaswani", "Noam Shazeer"],
        year=2017,
    )
    out = _handler(store).search(title="attention is all you need")
    assert f"pa{rid}" in out.body
    assert "Vaswani" in out.body  # citation line
    assert "2017" in out.body
    assert "view='bibtex'" in out.body  # cite affordance


def test_handler_author_returns_record_row(store: Store) -> None:
    rid = _seed_paper(
        store,
        slug="attn",
        title="Attention Is All You Need",
        authors=["Ashish Vaswani"],
        year=2017,
    )
    out = _handler(store).search(author="Vaswani")
    assert f"pa{rid}" in out.body
    assert "Attention Is All You Need" in out.body


def test_handler_title_miss_suggests_stub(store: Store) -> None:
    out = _handler(store).search(title="a paper we do not hold")
    assert "no paper matches" in out.body
    assert "put(kind='paper'" in out.body  # request-it affordance


def test_handler_title_and_author_together_rejected(store: Store) -> None:
    with pytest.raises(BadInput):
        _handler(store).search(title="x", author="y")


def test_handler_empty_byline_falls_through_to_q_guard(store: Store) -> None:
    """Blank title/author is treated as absent — the normal q= guard fires."""
    with pytest.raises(BadInput):
        _handler(store).search(title="   ")


def test_handler_title_has_more_offers_page_size_not_page(store: Store) -> None:
    """gr634204: the verb refuses ``page=`` — a byline lookup with more
    matches than the page holds offers a page_size raise, never a
    "next page" call that errors."""
    for i in range(11):
        _seed_paper(
            store,
            slug=f"vol-{i}",
            title=f"High throughput catalyst screening volume {i}",
            authors=["A. Researcher"],
        )
    out = _handler(store).search(title="catalyst screening", page_size=10)
    assert "page=" not in out.body
    assert "page_size=20" in out.body


def test_handler_title_page_size_doubling_clamps_at_cap(store: Store) -> None:
    """The doubling hint must clamp at the 100-row search cap
    (``tools/core.py`` refuses anything above it): a ``page_size`` in
    51..99 advertises ``page_size=100``, never an over-cap value, and
    at the cap itself no raise is offered."""
    for i in range(52):
        _seed_paper(
            store,
            slug=f"cap-{i}",
            title=f"High throughput catalyst screening volume {i}",
            authors=["A. Researcher"],
        )
    out = _handler(store).search(title="catalyst screening", page_size=51)
    assert "page_size=100" in out.body
    assert "page_size=102" not in out.body
    at_cap = _handler(store).search(title="catalyst screening", page_size=100)
    assert "raise page_size" not in at_cap.body


# ── ranking: text first, not merely a held PDF (gr453860) ─────────


def _give_pdf(store: Store, ref_id: int, sha: str) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, size_bytes, "
            "storage_path) VALUES (%s, %s, 1, 1, '') ON CONFLICT DO NOTHING",
            (sha, sha),
        )
        conn.execute("UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s", (sha, ref_id))


def _give_body(store: Store, ref_id: int) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, 0, 'paragraph', 'Body text.')",
            (ref_id,),
        )


def test_find_papers_by_title_ranks_body_over_bodiless_pdf(store: Store) -> None:
    """A bodiless PDF-carrying paper (bytes, no text) must not outrank a
    paper an agent can read: the markup-ingested copy with no PDF."""
    bodiless = _seed_paper(
        store,
        slug="bl",
        title="Graphene oxide membranes for desalination",
        authors=["A"],
    )
    _give_pdf(store, bodiless, "b" * 64)
    with_body = _seed_paper(
        store,
        slug="wb",
        title="Graphene oxide membranes for desalination",
        authors=["B"],
    )
    _give_body(store, with_body)
    got = store.find_papers_by_title(kind="paper", q="graphene oxide membranes")
    assert got[:2] == [with_body, bodiless]


def test_find_papers_by_author_ranks_body_over_bodiless_pdf(store: Store) -> None:
    bodiless = _seed_paper(store, slug="bl2", title="Paper one", authors=["Kim Nguyen"])
    _give_pdf(store, bodiless, "c" * 64)
    with_body = _seed_paper(
        store, slug="wb2", title="Paper two", authors=["Kim Nguyen"]
    )
    _give_body(store, with_body)
    assert store.find_papers_by_author(kind="paper", q="Nguyen") == [
        with_body,
        bodiless,
    ]


# ── cohort composition (gr653528 review) ──────────────────────────
#
# The byline door used to pass only ``min_quartile`` through — every
# other ref-level filter (unread, ingest/id bounds, tags, the
# dispatch-resolved include/exclude cohorts, serves/stamp/link cohorts,
# has_fulltext, record_type, ids/exclude, year/created windows) dropped
# silently, and the see-more hint echoed none of it. The door now
# materializes the cohort via ``filtered_ref_clauses`` and the
# continuation re-issues every filter verbatim.


def _read_note(handler: PaperHandler, slug: str) -> None:
    """Close a read-paper pass on ``slug`` the skill's way."""
    handler.put(id=slug, text="read-paper: setup me1; hubs fi2")


def _set_chunk_created(store: Store, ref_id: int, ts: datetime) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET created_at = %s WHERE ref_id = %s",
            (ts, ref_id),
        )


def _cohort_pair(store: Store, *, title: str = "Cohort probe study") -> tuple[int, int]:
    a = _seed_paper(store, slug="coh-a", title=title, authors=["Cohort Author"])
    b = _seed_paper(store, slug="coh-b", title=title, authors=["Cohort Author"])
    return a, b


def test_byline_honours_unread(hub: Hub, store: Store) -> None:
    a, b = _cohort_pair(store)
    _read_note(PaperHandler(hub=hub), "coh-b")
    out = PaperHandler(hub=hub).search(title="cohort probe study", unread=True)
    assert f"pa{a}" in out.body
    assert f"pa{b}" not in out.body
    out = PaperHandler(hub=hub).search(title="cohort probe study", unread=False)
    assert f"pa{a}" not in out.body
    assert f"pa{b}" in out.body


def test_byline_honours_unread_on_author(hub: Hub, store: Store) -> None:
    a, b = _cohort_pair(store)
    _read_note(PaperHandler(hub=hub), "coh-b")
    out = PaperHandler(hub=hub).search(author="cohort author", unread=True)
    assert f"pa{a}" in out.body
    assert f"pa{b}" not in out.body


def test_byline_honours_ingested_window(hub: Hub, store: Store) -> None:
    a, b = _cohort_pair(store)
    _give_body(store, a)
    _give_body(store, b)
    _set_chunk_created(store, b, datetime(2020, 1, 1, tzinfo=UTC))
    out = PaperHandler(hub=hub).search(
        title="cohort probe study", ingested_after="2025-01-01"
    )
    assert f"pa{a}" in out.body
    assert f"pa{b}" not in out.body


def test_byline_honours_id_bounds(hub: Hub, store: Store) -> None:
    a, b = _cohort_pair(store)
    out = PaperHandler(hub=hub).search(title="cohort probe study", id_max=a)
    assert f"pa{a}" in out.body
    assert f"pa{b}" not in out.body
    out = PaperHandler(hub=hub).search(title="cohort probe study", id_min=b)
    assert f"pa{a}" not in out.body
    assert f"pa{b}" in out.body


def test_byline_honours_include_scope(hub: Hub, store: Store) -> None:
    a, b = _cohort_pair(store)
    out = PaperHandler(hub=hub).search(title="cohort probe study", include_ref_ids=[a])
    assert f"pa{a}" in out.body
    assert f"pa{b}" not in out.body


def test_byline_empty_include_scope_is_zero_hits(hub: Hub, store: Store) -> None:
    _cohort_pair(store)
    out = PaperHandler(hub=hub).search(title="cohort probe study", include_ref_ids=[])
    assert "no paper matches" in out.body
    assert "filtered cohort" in out.body


def test_byline_honours_exclude_scope(hub: Hub, store: Store) -> None:
    a, b = _cohort_pair(store)
    out = PaperHandler(hub=hub).search(title="cohort probe study", exclude_ref_ids=[b])
    assert f"pa{a}" in out.body
    assert f"pa{b}" not in out.body
    out = PaperHandler(hub=hub).search(title="cohort probe study", exclude=["coh-b"])
    assert f"pa{a}" in out.body
    assert f"pa{b}" not in out.body


def test_byline_honours_tags(hub: Hub, store: Store) -> None:
    a, b = _cohort_pair(store)
    store.add_tag(a, Tag.open("area-cohort"), set_by="agent")
    out = PaperHandler(hub=hub).search(title="cohort probe study", tags=["area-cohort"])
    assert f"pa{a}" in out.body
    assert f"pa{b}" not in out.body


def test_byline_drops_junk_inside_materialized_cohort(hub: Hub, store: Store) -> None:
    """The default ``JUNK:junk`` verdict must hold inside the
    materialized cohort too — a filtered byline call doesn't opt a junked
    paper back in."""
    a, b = _cohort_pair(store)
    store.add_tag(b, Tag.closed("JUNK", "junk"))
    out = PaperHandler(hub=hub).search(title="cohort probe study", unread=True)
    assert f"pa{a}" in out.body
    assert f"pa{b}" not in out.body


def test_byline_hit_shape_args_refuse(hub: Hub, store: Store) -> None:
    """Ranked-hit knobs the record listing can't express refuse rather
    than drop silently."""
    h = PaperHandler(hub=hub)
    with pytest.raises(BadInput, match="not applied"):
        h.search(title="x", q="y")
    with pytest.raises(BadInput, match="not applied"):
        h.search(title="x", scope="pa1")
    with pytest.raises(BadInput, match="not applied"):
        h.search(title="x", unique_per="paper")
    with pytest.raises(BadInput, match="not applied"):
        h.search(author="x", mode="semantic")
    with pytest.raises(BadInput, match="not applied"):
        h.search(title="x", days=7)


def test_byline_see_more_echoes_every_filter(hub: Hub, store: Store) -> None:
    """The continuation re-issues the whole cohort spec — a caller that
    follows the hint verbatim must land the same filtered set, wider."""
    ids = []
    for i in range(11):
        ids.append(
            _seed_paper(
                store,
                slug=f"sweep-{i}",
                title="Sweep cohort title probe",
                authors=["Sweep Author"],
            )
        )
    h = PaperHandler(hub=hub)
    out = h.search(title="sweep cohort title probe", unread=True, page_size=10)
    assert "page_size=20" in out.body
    assert "'unread': True" in out.body
    wider = h.search(title="sweep cohort title probe", unread=True, page_size=20)
    for rid in ids:
        assert f"pa{rid}" in wider.body


def test_byline_see_more_names_unspellable_scope(hub: Hub, store: Store) -> None:
    """A dispatch-resolved cohort (cited=/uncited=/hubbed=) can't respell
    on the ``next:`` call — the hint says to re-add the scope."""
    ids = []
    for i in range(11):
        ids.append(
            _seed_paper(
                store,
                slug=f"sc-{i}",
                title="Scoped cohort title probe",
                authors=["Scoped Author"],
            )
        )
    out = PaperHandler(hub=hub).search(
        title="scoped cohort title probe",
        include_ref_ids=ids,
        page_size=10,
    )
    assert "re-add your cited=/uncited=/hubbed= scope" in out.body
