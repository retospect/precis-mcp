"""Deterministic paper-hygiene heals — card drift, superseded chains, links."""

from __future__ import annotations

from typing import Any

from precis.identity import PLACEHOLDER_TITLE
from precis.ingest.paper_hygiene import (
    close_resolved_title_fix_todos,
    collapse_superseded_chains,
    flag_spliced_issue_papers,
    heal_drifted_cards,
    is_filename_like_title,
    migrate_dangling_paper_links,
    raise_junk_title_papers,
    requeue_papers_for_enrich,
    requeue_placeholder_title_papers,
)
from precis.store import Store
from precis.store.types import Tag


def _paper(store: Store, *, slug: str, title: str) -> int:
    return store.insert_ref(kind="paper", slug=slug, title=title).id


def _card(store: Store, ref_id: int, text: str) -> None:
    with store.pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, -1, 'card_combined', %s)",
                (ref_id, text),
            )


def _card_text(store: Store, ref_id: int) -> str:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT text FROM chunks WHERE ref_id=%s AND chunk_kind='card_combined'",
            (ref_id,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _legacy_authors(store: Store, ref_id: int, authors: list[dict[str, str]]) -> None:
    """Seed a pre-0168 ``refs.authors`` jsonb directly — ``insert_ref``
    now junk-guards + projects at write time, so a junk entry can only
    exist as legacy data."""
    from psycopg.types.json import Jsonb

    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET authors = %s::jsonb WHERE ref_id = %s",
            (Jsonb(authors), ref_id),
        )


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    ref = store.fetch_refs_by_ids([ref_id], include_deleted=True).get(ref_id)
    assert ref is not None
    return ref.meta or {}


# ── card drift ────────────────────────────────────────────────────


def test_heal_rebuilds_stale_card(store: Store) -> None:
    rid = _paper(store, slug="fixed20", title="A Properly Recovered Paper Title")
    _card(store, rid, "wang.dvi\n\nsome stale junk from the old import")

    healed = heal_drifted_cards(store, dry_run=False)
    assert rid in healed
    assert _card_text(store, rid).startswith("A Properly Recovered Paper Title")


def test_heal_skips_card_that_matches_modulo_punctuation(store: Store) -> None:
    """An en-dash / markup difference is not drift — leave it alone."""
    rid = _paper(store, slug="ok20", title="Non-Watson-Crick Interactions in DNA")
    # Card carries the same title with different punctuation/markup.
    _card(store, rid, "Non–Watson–Crick Interactions in DNA\n\nA. Author")

    assert heal_drifted_cards(store, dry_run=False) == []


def test_heal_dry_run_writes_nothing(store: Store) -> None:
    rid = _paper(store, slug="dry20", title="Another Real Title For The Paper")
    _card(store, rid, "cgibbs.dvi\n\nstale")
    assert heal_drifted_cards(store, dry_run=True) == [rid]
    assert _card_text(store, rid).startswith("cgibbs.dvi")  # untouched


# ── superseded-chain collapse ─────────────────────────────────────


def test_collapse_points_chain_at_terminal_survivor(store: Store) -> None:
    final = _paper(store, slug="final20", title="Survivor Paper")
    mid = _paper(store, slug="mid20", title="Middle Stub")
    head = _paper(store, slug="head20", title="Head Stub")
    with store.tx() as conn:
        store.stamp_ref_meta(mid, {"superseded_by": final}, conn=conn)
        store.stamp_ref_meta(head, {"superseded_by": mid}, conn=conn)
        store.retire_ref(mid, conn=conn)
        store.retire_ref(head, conn=conn)

    fixed = collapse_superseded_chains(store, dry_run=False)
    assert (head, final) in fixed
    assert _meta(store, head)["superseded_by"] == final


# ── dangling links ────────────────────────────────────────────────


def test_migrate_repoints_dangling_link_to_survivor(store: Store) -> None:
    survivor = _paper(store, slug="surv20", title="The Held Survivor")
    dead = _paper(store, slug="dead20", title="Retired Duplicate")
    citer = _paper(store, slug="citer20", title="A Citing Paper")
    with store.tx() as conn:
        store.add_link(
            src_ref_id=citer,
            dst_ref_id=dead,
            relation="related-to",
            set_by="system",
            conn=conn,
        )
        store.stamp_ref_meta(dead, {"superseded_by": survivor}, conn=conn)
        store.retire_ref(dead, conn=conn)

    acted = migrate_dangling_paper_links(store, dry_run=False)
    assert len(acted) == 1
    with store.pool.connection() as conn:
        dst = conn.execute(
            "SELECT dst_ref_id FROM links WHERE src_ref_id=%s AND relation='related-to'",
            (citer,),
        ).fetchone()
    assert dst is not None and int(dst[0]) == survivor


def test_migrate_leaves_supersedes_edge_alone(store: Store) -> None:
    """The supersedes audit edge legitimately points at the dead ref."""
    survivor = _paper(store, slug="surv21", title="Survivor Two")
    dead = _paper(store, slug="dead21", title="Retired Two")
    with store.tx() as conn:
        store.add_link(
            src_ref_id=survivor,
            dst_ref_id=dead,
            relation="supersedes",
            set_by="system",
            conn=conn,
        )
        store.stamp_ref_meta(dead, {"superseded_by": survivor}, conn=conn)
        store.retire_ref(dead, conn=conn)

    assert migrate_dangling_paper_links(store, dry_run=False) == []
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT dst_ref_id FROM links WHERE relation='supersedes' AND src_ref_id=%s",
            (survivor,),
        ).fetchone()
    assert row is not None and int(row[0]) == dead  # untouched


# ── stranded OA fetches ───────────────────────────────────────────


def _fetch_event(
    store: Store,
    ref_id: int,
    event: str,
    *,
    hours_ago: float,
    source: str = "fetcher:s2",
) -> None:
    with store.pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO ref_events (ref_id, source, event, ts) "
                "VALUES (%s, %s, %s, now() - make_interval(hours => %s))",
                (ref_id, source, event, hours_ago),
            )


def _fetcher_event_count(store: Store, ref_id: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM ref_events "
            "WHERE ref_id=%s AND source LIKE 'fetcher:%%'",
            (ref_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def test_requeue_clears_backoff_on_stranded_fetch(store: Store) -> None:
    """A stub with an old fetch_ok but no PDF is re-queued: fetcher events
    deleted (backoff reset) and a one-shot guard stamped."""
    from precis.ingest.paper_hygiene import requeue_stranded_fetches

    rid = _paper(store, slug="stranded18", title="Stranded Fetch Paper")
    # Several failed legs plus the black-holed fetch_ok, all >48h old.
    _fetch_event(store, rid, "no_oa_version", hours_ago=100)
    _fetch_event(store, rid, "fetch_ok", hours_ago=96)

    out = requeue_stranded_fetches(store, dry_run=False)
    assert rid in out
    assert _fetcher_event_count(store, rid) == 0  # backoff reset
    marker = _meta(store, rid).get("oa_requeued")
    assert marker and marker["prior_attempts"] == 2


def test_requeue_is_one_shot_guarded(store: Store) -> None:
    """A stub already carrying the oa_requeued marker is never swept again."""
    from precis.ingest.paper_hygiene import requeue_stranded_fetches

    rid = _paper(store, slug="guarded18", title="Already Requeued Paper")
    _fetch_event(store, rid, "fetch_ok", hours_ago=96)
    assert requeue_stranded_fetches(store, dry_run=False) == [rid]
    # A fresh black-holed fetch_ok arrives after the re-queue…
    _fetch_event(store, rid, "fetch_ok", hours_ago=96)
    # …but the marker blocks a second re-queue (so it can't spin).
    assert requeue_stranded_fetches(store, dry_run=False) == []
    assert _fetcher_event_count(store, rid) == 1  # left intact this time


def test_requeue_skips_recent_fetch_ok(store: Store) -> None:
    """A just-downloaded PDF still mid-ingest (fetch_ok < threshold) is left
    alone — no premature re-queue."""
    from precis.ingest.paper_hygiene import requeue_stranded_fetches

    rid = _paper(store, slug="recent18", title="Recently Fetched Paper")
    _fetch_event(store, rid, "fetch_ok", hours_ago=1)
    assert requeue_stranded_fetches(store, dry_run=False) == []
    assert _fetcher_event_count(store, rid) == 1


def test_requeue_skips_held_paper(store: Store) -> None:
    """A paper that actually landed a PDF is not a stranded stub."""
    from precis.ingest.paper_hygiene import requeue_stranded_fetches

    rid = _paper(store, slug="held18", title="Successfully Ingested Paper")
    _fetch_event(store, rid, "fetch_ok", hours_ago=96)
    sha = f"{rid:064d}"
    with store.pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
                "size_bytes, storage_path) VALUES (%s, %s, 1, 100, '/tmp/held') "
                "ON CONFLICT (pdf_sha256) DO NOTHING",
                (sha, sha),
            )
            conn.execute("UPDATE refs SET pdf_sha256=%s WHERE ref_id=%s", (sha, rid))
    assert requeue_stranded_fetches(store, dry_run=False) == []
    assert _fetcher_event_count(store, rid) == 1  # untouched


def test_requeue_dry_run_writes_nothing(store: Store) -> None:
    from precis.ingest.paper_hygiene import requeue_stranded_fetches

    rid = _paper(store, slug="drystr18", title="Dry Run Stranded Paper")
    _fetch_event(store, rid, "fetch_ok", hours_ago=96)
    assert requeue_stranded_fetches(store, dry_run=True) == [rid]
    assert _fetcher_event_count(store, rid) == 1  # untouched
    assert "oa_requeued" not in _meta(store, rid)


# ── front-matter-only Elsevier previews ───────────────────────────


def _body_chunk(store: Store, ref_id: int, ord_: int, text: str) -> None:
    with store.pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, %s, 'paragraph', %s)",
                (ref_id, ord_, text),
            )


def _stamp_pdf(store: Store, ref_id: int, sha: str) -> None:
    with store.pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
                "size_bytes, storage_path) VALUES (%s, %s, 1, 100, '/tmp/x') "
                "ON CONFLICT (pdf_sha256) DO NOTHING",
                (sha, sha),
            )
            conn.execute("UPDATE refs SET pdf_sha256=%s WHERE ref_id=%s", (sha, ref_id))


_FOOTER_TEXT = (
    "Contents lists available at ScienceDirect\n\n"
    "journal homepage: www.elsevier.com/locate/xyz"
)


def _seed_front_matter_paper(
    store: Store,
    *,
    slug: str,
    n_body_chunks: int = 8,
    include_footer: bool = True,
    include_references: bool = False,
    elsevier_xml_fetch_ok: bool = False,
) -> int:
    """Seed a live paper shaped like an Elsevier front-matter-only preview."""
    rid = _paper(store, slug=slug, title=f"Paper {slug}")
    sha = f"{rid:064d}"
    _stamp_pdf(store, rid, sha)
    _fetch_event(store, rid, "fetch_ok", hours_ago=1, source="fetcher:elsevier")
    if elsevier_xml_fetch_ok:
        _fetch_event(store, rid, "fetch_ok", hours_ago=1, source="fetcher:elsevier_xml")
    for i in range(n_body_chunks):
        text = f"Body paragraph {i}."
        if i == n_body_chunks - 1 and include_footer:
            text = _FOOTER_TEXT
        _body_chunk(store, rid, i, text)
    if include_references:
        _body_chunk(store, rid, n_body_chunks, "References\n[1] Some citation.")
    return rid


class TestRequeueFrontMatterOnlyPapers:
    def test_detects_and_pins_front_matter_only_paper(self, store: Store) -> None:
        from precis.ingest.paper_hygiene import requeue_front_matter_only_papers

        rid = _seed_front_matter_paper(store, slug="fmo1")

        out = requeue_front_matter_only_papers(store, dry_run=False)
        assert out == [rid]

        marker = _meta(store, rid).get("markup_refetch")
        assert marker and marker["body_chunks"] == 8
        assert marker["reason"] == "front-matter-only body"
        assert _fetcher_event_count(store, rid) == 0  # backoff cleared

        with store.pool.connection() as conn:
            breadcrumb = conn.execute(
                "SELECT event, payload FROM ref_events "
                "WHERE ref_id=%s AND source='paper_reconcile'",
                (rid,),
            ).fetchone()
        assert breadcrumb is not None
        assert breadcrumb[0] == "markup_refetch_queued"
        assert breadcrumb[1]["body_chunks"] == 8

    def test_dry_run_writes_nothing(self, store: Store) -> None:
        from precis.ingest.paper_hygiene import requeue_front_matter_only_papers

        rid = _seed_front_matter_paper(store, slug="fmo-dry1")

        out = requeue_front_matter_only_papers(store, dry_run=True)
        assert out == [rid]
        assert "markup_refetch" not in _meta(store, rid)
        assert _fetcher_event_count(store, rid) == 1  # untouched

    def test_skips_paper_with_elsevier_xml_fetch_ok(self, store: Store) -> None:
        from precis.ingest.paper_hygiene import requeue_front_matter_only_papers

        _seed_front_matter_paper(store, slug="fmo-xml1", elsevier_xml_fetch_ok=True)
        assert requeue_front_matter_only_papers(store, dry_run=False) == []

    def test_skips_paper_with_references_chunk(self, store: Store) -> None:
        from precis.ingest.paper_hygiene import requeue_front_matter_only_papers

        _seed_front_matter_paper(store, slug="fmo-refs1", include_references=True)
        assert requeue_front_matter_only_papers(store, dry_run=False) == []

    def test_skips_paper_over_chunk_cap(self, store: Store) -> None:
        from precis.ingest.paper_hygiene import requeue_front_matter_only_papers

        _seed_front_matter_paper(store, slug="fmo-big1", n_body_chunks=20)
        assert requeue_front_matter_only_papers(store, dry_run=False) == []

    def test_skips_already_pinned_paper(self, store: Store) -> None:
        from precis.ingest.paper_hygiene import requeue_front_matter_only_papers

        rid = _seed_front_matter_paper(store, slug="fmo-pinned1")
        with store.tx() as conn:
            store.stamp_ref_meta(
                rid, {"markup_refetch": {"at": "2026-01-01T00:00:00+00:00"}}, conn=conn
            )
        assert requeue_front_matter_only_papers(store, dry_run=False) == []


# ── metadata hygiene stats ───────────────────────────────────────


def test_metadata_hygiene_stats_structured_vs_flat_split(store: Store) -> None:
    from precis.ingest.paper_hygiene import metadata_hygiene_stats

    _paper(store, slug="unauthored-h1", title="No Authors At All")
    store.insert_ref(
        kind="paper",
        slug="structured-h1",
        title="Fully Structured Authors",
        authors=[
            {"given": "Ada", "family": "Lovelace"},
            {"given": "Alan", "family": "Turing"},
        ],
    )
    store.insert_ref(
        kind="paper",
        slug="flat-h1",
        title="Flat Author Byline",
        authors=[{"name": "Grace Hopper"}],
    )
    store.insert_ref(
        kind="paper",
        slug="mixed-h1",
        title="One Structured One Flat",
        authors=[{"given": "Rosalind", "family": "Franklin"}, {"name": "J. Watson"}],
    )

    stats = metadata_hygiene_stats(store)
    assert stats.total_papers == 4
    assert stats.authored_papers == 3
    assert stats.structured_authors_papers == 1
    assert stats.structured_authors_pct == round(100 / 3, 1)


def test_metadata_hygiene_stats_entry_type_and_journal_coverage(store: Store) -> None:
    from precis.ingest.paper_hygiene import metadata_hygiene_stats

    store.insert_ref(
        kind="paper",
        slug="full-meta-h1",
        title="Paper With Full Meta",
        meta={"entry_type": "journal-article", "journal": "Nature"},
    )
    _paper(store, slug="bare-meta-h1", title="Paper With No Meta")

    stats = metadata_hygiene_stats(store)
    assert stats.total_papers == 2
    assert stats.entry_type_papers == 1
    assert stats.journal_papers == 1
    assert stats.entry_type_pct == 50.0
    assert stats.journal_pct == 50.0


def test_metadata_hygiene_stats_heuristic_source_and_junk_authors(
    store: Store,
) -> None:
    from precis.ingest.paper_hygiene import metadata_hygiene_stats

    store.insert_ref(
        kind="paper",
        slug="heuristic-h1",
        title="Heuristically Split Paper",
        authors=[{"name": "Doe, Jane"}],
        meta={"authors_source": "heuristic"},
    )
    store.insert_ref(
        kind="paper",
        slug="crossref-h1",
        title="Crossref Resolved Paper",
        authors=[{"given": "Marie", "family": "Curie"}],
        meta={"authors_source": "crossref"},
    )
    junk = store.insert_ref(
        kind="paper",
        slug="junk-h1",
        title="Paper With A Junk Author Entry",
    )
    _legacy_authors(
        store, junk.id, [{"name": "REFERENCES"}, {"name": "not-a-name@example.com"}]
    )

    stats = metadata_hygiene_stats(store)
    assert stats.heuristic_source_papers == 1
    assert stats.junk_author_entries == 2
    assert stats.junk_sample_bounded is False


def test_metadata_hygiene_stats_junk_sample_is_bounded(store: Store) -> None:
    from precis.ingest.paper_hygiene import metadata_hygiene_stats

    for i in range(3):
        ref = store.insert_ref(
            kind="paper",
            slug=f"bound-h{i}",
            title=f"Bounded Sample Paper {i}",
        )
        _legacy_authors(store, ref.id, [{"name": "REFERENCES"}])

    stats = metadata_hygiene_stats(store, junk_sample_limit=2)
    assert stats.junk_sample_papers == 2
    assert stats.junk_sample_bounded is True
    assert stats.junk_author_entries == 2  # only the sampled two counted


def test_metadata_hygiene_stats_is_read_only(store: Store) -> None:
    from precis.ingest.paper_hygiene import metadata_hygiene_stats

    rid = _paper(store, slug="readonly-h1", title="Untouched Paper")
    before = _meta(store, rid)
    metadata_hygiene_stats(store)
    assert _meta(store, rid) == before


# ── is_filename_like_title ───────────────────────────────────────


def test_is_filename_like_title_catches_word_stamp_and_extensions() -> None:
    assert is_filename_like_title("Microsoft Word - manuscript_v3.docx")
    assert is_filename_like_title("Microsoft Word - final draft")
    assert is_filename_like_title("some_manuscript_final.docx")
    assert is_filename_like_title("scan_20240102.pdf")
    assert is_filename_like_title("layout_master.indd")


def test_is_filename_like_title_catches_placeholders_pii_and_bare_doi() -> None:
    assert is_filename_like_title("Untitled")
    assert is_filename_like_title("untitled document")
    assert is_filename_like_title("No Job Name")
    assert is_filename_like_title("PII: S0021-9797(20)31234-5")
    assert is_filename_like_title("doi:10.1016/j.something.2020.01.001")


def test_is_filename_like_title_leaves_real_titles_alone() -> None:
    assert not is_filename_like_title("Attention Is All You Need")
    assert not is_filename_like_title(
        "A Study on Word Embeddings for Document Classification"
    )
    assert not is_filename_like_title("The DOI System and Its Discontents")
    assert not is_filename_like_title("")
    assert not is_filename_like_title("   ")


def test_metadata_hygiene_stats_counts_filename_like_titles(store: Store) -> None:
    from precis.ingest.paper_hygiene import metadata_hygiene_stats

    _paper(store, slug="junk-title-h1", title="Microsoft Word - draft.docx")
    _paper(store, slug="junk-title-h2", title="Untitled")
    _paper(store, slug="clean-title-h1", title="A Perfectly Good Title")

    stats = metadata_hygiene_stats(store)
    assert stats.filename_like_titles == 2
    assert stats.title_sample_bounded is False


def test_metadata_hygiene_stats_title_sample_is_bounded(store: Store) -> None:
    from precis.ingest.paper_hygiene import metadata_hygiene_stats

    for i in range(3):
        _paper(store, slug=f"bound-title-h{i}", title="Untitled")

    stats = metadata_hygiene_stats(store, junk_sample_limit=2)
    assert stats.title_sample_papers == 2
    assert stats.title_sample_bounded is True
    assert stats.filename_like_titles == 2  # only the sampled two counted


class TestRequeuePlaceholderTitlePapers:
    """Re-arming is the whole heal: clear the enrichment pass's
    idempotency stamp so it reconsiders a row it already gave up on."""

    def _stub(
        self,
        store: Store,
        *,
        slug: str,
        title: str,
        doi: str | None = "10.1234/a",
        resolved: bool = True,
    ) -> int:
        meta: dict[str, Any] = {"authors_resolved_at": "2026-09-01T00:00:00+00:00"}
        ref = store.insert_ref(
            kind="paper", slug=slug, title=title, meta=meta if resolved else {}
        )
        if doi:
            store.set_ref_identifier(ref.id, "doi", doi, source="manual")
        return ref.id

    def test_clears_the_stamp_so_the_pass_reclaims_it(self, store: Store) -> None:
        rid = self._stub(store, slug="t1", title=PLACEHOLDER_TITLE)
        assert requeue_placeholder_title_papers(store, dry_run=False) == [rid]
        ref = store.fetch_refs_by_ids([rid])[rid]
        assert "authors_resolved_at" not in (ref.meta or {})

    def test_dry_run_selects_without_writing(self, store: Store) -> None:
        rid = self._stub(store, slug="t2", title=PLACEHOLDER_TITLE)
        assert requeue_placeholder_title_papers(store, dry_run=True) == [rid]
        ref = store.fetch_refs_by_ids([rid])[rid]
        assert "authors_resolved_at" in (ref.meta or {})

    def test_skips_titled_papers_and_doi_less_placeholders(self, store: Store) -> None:
        self._stub(store, slug="t3", title="A Real Title")
        self._stub(store, slug="t4", title=PLACEHOLDER_TITLE, doi=None)
        # Never visited by the pass - it will claim this one anyway.
        self._stub(
            store, slug="t5", title=PLACEHOLDER_TITLE, doi="10.1234/b", resolved=False
        )
        assert requeue_placeholder_title_papers(store, dry_run=True) == []

    def test_second_run_is_a_no_op(self, store: Store) -> None:
        self._stub(store, slug="t6", title=PLACEHOLDER_TITLE)
        assert len(requeue_placeholder_title_papers(store, dry_run=False)) == 1
        assert requeue_placeholder_title_papers(store, dry_run=False) == []


class TestRequeuePapersForEnrich:
    """``precis enrich-rearm``: clear the stamp on the NAMED papers only."""

    def _paper(
        self,
        store: Store,
        *,
        slug: str,
        doi: str | None = "10.1234/a",
        stamped: bool = True,
        kind: str = "paper",
    ) -> int:
        meta: dict[str, Any] = (
            {"authors_resolved_at": "2026-09-01T00:00:00+00:00", "volume": "3"}
            if stamped
            else {}
        )
        ref = store.insert_ref(kind=kind, slug=slug, title="T", meta=meta)
        if doi:
            store.set_ref_identifier(ref.id, "doi", doi, source="manual")
        return ref.id

    def test_dry_run_selects_without_writing(self, store: Store) -> None:
        rid = self._paper(store, slug="e1")
        assert requeue_papers_for_enrich(store, [rid], dry_run=True) == [rid]
        assert "authors_resolved_at" in (store.fetch_refs_by_ids([rid])[rid].meta or {})

    def test_apply_clears_only_the_stamp_for_named_refs(self, store: Store) -> None:
        a = self._paper(store, slug="e2a", doi="10.1234/2a")
        b = self._paper(store, slug="e2b", doi="10.1234/2b")  # not named
        assert requeue_papers_for_enrich(store, [a, a], dry_run=False) == [a]
        meta_a = store.fetch_refs_by_ids([a])[a].meta or {}
        meta_b = store.fetch_refs_by_ids([b])[b].meta or {}
        assert "authors_resolved_at" not in meta_a
        assert meta_a["volume"] == "3"  # nothing else touched
        assert "authors_resolved_at" in meta_b

    def test_skips_doi_less_unstamped_and_non_papers(self, store: Store) -> None:
        no_doi = self._paper(store, slug="e3a", doi=None)
        unstamped = self._paper(store, slug="e3b", doi="10.1234/3b", stamped=False)
        other = self._paper(store, slug="e3c", doi="10.1234/3c", kind="patent")
        named = [no_doi, unstamped, other, 999999999]
        assert requeue_papers_for_enrich(store, named, dry_run=True) == []

    def test_empty_and_second_run(self, store: Store) -> None:
        assert requeue_papers_for_enrich(store, [], dry_run=False) == []
        rid = self._paper(store, slug="e4")
        assert requeue_papers_for_enrich(store, [rid], dry_run=False) == [rid]
        assert requeue_papers_for_enrich(store, [rid], dry_run=False) == []


class TestEnrichRearmCli:
    def _run(self, store: Store, monkeypatch: Any, argv: list[str]) -> None:
        import argparse
        from types import SimpleNamespace

        from precis.cli import enrich_rearm

        monkeypatch.setattr(
            "precis.runtime.build_runtime", lambda cfg: SimpleNamespace(store=store)
        )
        parser = argparse.ArgumentParser()
        enrich_rearm.add_parser(parser.add_subparsers())
        args = parser.parse_args(["enrich-rearm", *argv, "--database-url", "x://y"])
        enrich_rearm.run(args)

    def _stamped(self, store: Store, slug: str) -> int:
        ref = store.insert_ref(
            kind="paper",
            slug=slug,
            title="T",
            meta={"authors_resolved_at": "2026-09-01T00:00:00+00:00"},
        )
        store.set_ref_identifier(ref.id, "doi", f"10.1234/{slug}", source="manual")
        return ref.id

    def test_dry_run_prints_and_does_not_write(
        self, store: Store, monkeypatch: Any, capsys: Any
    ) -> None:
        rid = self._stamped(store, "cli1")
        self._run(store, monkeypatch, ["--refs", f"{rid}, 999999999"])
        out = capsys.readouterr()
        assert f"ref_id={rid}" in out.out
        assert "DRY-RUN" in out.err and "would re-arm 1" in out.err
        assert "authors_resolved_at" in (store.fetch_refs_by_ids([rid])[rid].meta or {})

    def test_apply_clears_the_stamp(
        self, store: Store, monkeypatch: Any, capsys: Any
    ) -> None:
        rid = self._stamped(store, "cli2")
        self._run(store, monkeypatch, ["--refs", str(rid), "--apply"])
        assert f"ref_id={rid}" in capsys.readouterr().out
        meta = store.fetch_refs_by_ids([rid])[rid].meta or {}
        assert "authors_resolved_at" not in meta

    def test_bad_refs_exits_2(self, store: Store, monkeypatch: Any) -> None:
        import pytest

        with pytest.raises(SystemExit) as exc:
            self._run(store, monkeypatch, ["--refs", "12,abc"])
        assert exc.value.code == 2


# ── bodiless PDFs (gr453860) ──────────────────────────────────────


def _bodiless_paper(
    store: Store, *, slug: str, storage_path: str, title: str | None = None
) -> tuple[int, str]:
    """A live paper that holds a PDF (``pdf_sha256`` set, ``pdfs`` row at
    ``storage_path``) and carries only its ``ord < 0`` card — the bodiless
    shape the heal targets. ``title=None`` (the default) writes the real
    title ``f"Paper {slug}"`` — the identity gate judges it (gr517423:
    a blank default hid the gate for every test). Pass ``title=""`` to
    opt out explicitly (``unchecked``), or a real title to exercise the
    gate (then the pdf text must carry its words or a doi)."""
    rid = _paper(store, slug=slug, title=title or f"Paper {slug}")
    if title == "":
        with store.pool.connection() as conn:
            conn.execute("UPDATE refs SET title = '' WHERE ref_id = %s", (rid,))
            conn.commit()
    sha = f"{rid:064d}"
    with store.pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
                "size_bytes, storage_path) VALUES (%s, %s, 1, 100, %s)",
                (sha, sha, storage_path),
            )
            conn.execute("UPDATE refs SET pdf_sha256=%s WHERE ref_id=%s", (sha, rid))
    _card(store, rid, f"Paper {slug}\n\ncard text")
    return rid, sha


def _write_pdf(path: Any, *, text: str | None) -> None:
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    if text:
        page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()


def _heal_events(store: Store, ref_id: int) -> list[tuple[str, dict[str, Any]]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT event, payload FROM ref_events "
            "WHERE ref_id=%s AND source='heal:bodiless' ORDER BY event_id",
            (ref_id,),
        ).fetchall()
    return [(str(r[0]), dict(r[1] or {})) for r in rows]


def _tags(store: Store, ref_id: int) -> set[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s",
            (ref_id,),
        ).fetchall()
    return {str(r[0]) for r in rows}


def _body_count(store: Store, ref_id: int) -> int:
    return store.chunks.count_chunks(ref_id)


def _fake_body(
    n_chunks: int,
    *,
    meta: dict[str, Any] | None = None,
    spread: dict[str, Any] | None = None,
) -> Any:
    from precis.ingest.db_writer import ChunkToWrite
    from precis.ingest.pipeline import BodyExtraction

    chunks = [
        ChunkToWrite(ord=i, chunk_kind="paragraph", text=f"Body paragraph {i}.")
        for i in range(n_chunks)
    ]
    return BodyExtraction(
        chunks=chunks,
        blocks=[],
        meta=dict(meta or {}),
        page_first=1 if chunks else None,
        page_last=1 if chunks else None,
        page_count=1 if chunks else 0,
        spread=spread,
    )


def _spread_events(store: Store, ref_id: int) -> list[tuple[str, dict[str, Any]]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT event, payload FROM ref_events "
            "WHERE ref_id=%s AND source='ingest:spread' ORDER BY event_id",
            (ref_id,),
        ).fetchall()
    return [(str(r[0]), dict(r[1] or {})) for r in rows]


class TestHealBodilessPdfs:
    def test_preview_verdict_is_journalled_once_and_never_extracted(
        self, store: Store, tmp_path: Any
    ) -> None:
        from precis.ingest.paper_hygiene import heal_bodiless_pdfs

        pdf = tmp_path / "prev.pdf"
        _write_pdf(pdf, text="Contents lists available at ScienceDirect")
        rid, sha = _bodiless_paper(store, slug="bl-prev", storage_path=str(pdf))
        _fetch_event(store, rid, "fetch_ok", hours_ago=2, source="fetcher:elsevier")

        calls: list[Any] = []

        def never(path: Any, cand: Any) -> Any:
            calls.append(path)
            return _fake_body(5)

        out = heal_bodiless_pdfs(
            store, corpus_dirs=(tmp_path,), dry_run=False, extractor=never
        )
        assert [(o.ref_id, o.outcome) for o in out] == [(rid, "preview")]
        assert calls == []
        assert _body_count(store, rid) == 0
        events = _heal_events(store, rid)
        assert len(events) == 1 and events[0][0] == "preview"
        assert events[0][1]["pdf_sha256"] == sha
        assert "elsevier" in events[0][1]["reason"]
        # Judged: the next pass does not see it again.
        assert heal_bodiless_pdfs(store, corpus_dirs=(tmp_path,), dry_run=False) == []

    def test_missing_file_when_no_node_holds_it(
        self, store: Store, tmp_path: Any
    ) -> None:
        from precis.ingest.paper_hygiene import heal_bodiless_pdfs

        rid, sha = _bodiless_paper(
            store, slug="bl-miss", storage_path=str(tmp_path / "gone.pdf")
        )
        out = heal_bodiless_pdfs(store, corpus_dirs=(tmp_path,), dry_run=False)
        assert [(o.ref_id, o.outcome) for o in out] == [(rid, "missing_file")]
        events = _heal_events(store, rid)
        assert events[0][0] == "missing_file"
        assert "gone.pdf" in events[0][1]["reason"]

    def test_deferred_not_judged_when_another_node_holds_it(
        self, store: Store, tmp_path: Any
    ) -> None:
        from precis.ingest.paper_hygiene import heal_bodiless_pdfs

        rid, sha = _bodiless_paper(
            store,
            slug="bl-else",
            storage_path="/opt/nas/botshome/papers/corpus/x/y.pdf",
        )
        store.record_pdf_location(
            sha, "other-node", "/nas/botshome/papers/corpus/x/y.pdf"
        )
        out = heal_bodiless_pdfs(store, corpus_dirs=(tmp_path,), dry_run=False)
        assert [(o.ref_id, o.outcome) for o in out] == [(rid, "deferred")]
        assert _heal_events(store, rid) == []
        # Still a candidate for the node that holds the file.
        again = heal_bodiless_pdfs(store, corpus_dirs=(tmp_path,), dry_run=True)
        assert [o.ref_id for o in again] == [rid]

    def test_unreadable_corrupt_file(self, store: Store, tmp_path: Any) -> None:
        from precis.ingest.paper_hygiene import heal_bodiless_pdfs

        bad = tmp_path / "bad.pdf"
        bad.write_bytes(b"%PDF-1.4 this is not really a pdf")
        rid, _ = _bodiless_paper(store, slug="bl-bad", storage_path=str(bad))
        out = heal_bodiless_pdfs(store, corpus_dirs=(tmp_path,), dry_run=False)
        assert [(o.ref_id, o.outcome) for o in out] == [(rid, "unreadable")]
        assert out[0].reason.startswith("corrupt:")
        assert _heal_events(store, rid)[0][1]["reason"].startswith("corrupt:")

    def test_unreadable_scanned_no_text_layer(
        self, store: Store, tmp_path: Any
    ) -> None:
        from precis.ingest.paper_hygiene import heal_bodiless_pdfs

        scan = tmp_path / "scan.pdf"
        _write_pdf(scan, text=None)
        rid, _ = _bodiless_paper(store, slug="bl-scan", storage_path=str(scan))

        def never(path: Any, cand: Any) -> Any:
            raise AssertionError("extractor must not run on a text-less PDF")

        out = heal_bodiless_pdfs(
            store, corpus_dirs=(tmp_path,), dry_run=False, extractor=never
        )
        assert [(o.ref_id, o.outcome) for o in out] == [(rid, "unreadable")]
        assert out[0].reason.startswith("scanned:")
        assert "body:needs_ocr" in _tags(store, rid)

    def test_extracts_readable_pdf_and_journals_chunks(
        self, store: Store, tmp_path: Any
    ) -> None:
        from precis.ingest.paper_hygiene import heal_bodiless_pdfs

        pdf = tmp_path / "ok.pdf"
        _write_pdf(pdf, text="A real body of text on the page.")
        rid, sha = _bodiless_paper(store, slug="bl-ok", storage_path=str(pdf))
        seen: list[tuple[Any, str]] = []

        def fake(path: Any, cand: Any) -> Any:
            seen.append((path, cand.paper_id))
            return _fake_body(3, meta={"glyph_health": {"suspected": True}})

        out = heal_bodiless_pdfs(
            store, corpus_dirs=(tmp_path,), dry_run=False, extractor=fake
        )
        assert [(o.ref_id, o.outcome, o.chunks_written) for o in out] == [
            (rid, "extracted", 3)
        ]
        assert seen and seen[0][0] == pdf
        assert _body_count(store, rid) == 3
        events = _heal_events(store, rid)
        assert events[0][0] == "extracted"
        assert events[0][1]["chunks"] == 3 and events[0][1]["pdf_sha256"] == sha
        # Body-owned meta landed through the ordinary stub-upgrade path.
        assert _meta(store, rid)["glyph_health"] == {"suspected": True}
        with store.pool.connection() as conn:
            ch = conn.execute(
                "SELECT 1 FROM ref_identifiers WHERE ref_id=%s AND id_kind='content_hash'",
                (rid,),
            ).fetchone()
        assert ch is not None
        # Healed: no longer a candidate.
        assert heal_bodiless_pdfs(store, corpus_dirs=(tmp_path,), dry_run=True) == []

    def test_extractor_without_body_is_unreadable(
        self, store: Store, tmp_path: Any
    ) -> None:
        from precis.ingest.paper_hygiene import heal_bodiless_pdfs

        pdf = tmp_path / "empty.pdf"
        _write_pdf(pdf, text="Some text the probe sees but Marker drops.")
        rid, _ = _bodiless_paper(
            store, slug="bl-nobody", storage_path=str(pdf), title=""
        )

        def nothing(path: Any, cand: Any) -> Any:
            return _fake_body(
                0,
                meta={"extract_used_fallback": True, "extract_fallback_reason": "boom"},
            )

        # Pass 1: retry marker only (transient Marker failure is possible).
        out = heal_bodiless_pdfs(
            store, corpus_dirs=(tmp_path,), dry_run=False, extractor=nothing
        )
        assert [(o.ref_id, o.outcome) for o in out] == [(rid, "extract_retry")]
        assert not _tags(store, rid)
        # Pass 2: still empty -> terminal, flagged for OCR.
        out = heal_bodiless_pdfs(
            store, corpus_dirs=(tmp_path,), dry_run=False, extractor=nothing
        )
        assert [(o.ref_id, o.outcome) for o in out] == [(rid, "unreadable")]
        assert "no body chunks" in out[0].reason and "boom" in out[0].reason
        assert out[0].needs_ocr
        assert "body:needs_ocr" in _tags(store, rid)
        assert [e[0] for e in _heal_events(store, rid)] == [
            "extract_retry",
            "unreadable",
        ]
        assert _heal_events(store, rid)[1][1]["route"] == "ocr"
        # Pass 3: bounded, nothing more happens.
        assert (
            heal_bodiless_pdfs(
                store, corpus_dirs=(tmp_path,), dry_run=False, extractor=nothing
            )
            == []
        )
        assert _body_count(store, rid) == 0

    def test_empty_then_body_on_retry_heals(self, store: Store, tmp_path: Any) -> None:
        from precis.ingest.paper_hygiene import heal_bodiless_pdfs

        pdf = tmp_path / "flaky.pdf"
        _write_pdf(pdf, text="Some text the probe sees.")
        rid, _ = _bodiless_paper(
            store, slug="bl-flaky", storage_path=str(pdf), title=""
        )
        heal_bodiless_pdfs(
            store,
            corpus_dirs=(tmp_path,),
            dry_run=False,
            extractor=lambda p, cand: _fake_body(0),
        )
        out = heal_bodiless_pdfs(
            store,
            corpus_dirs=(tmp_path,),
            dry_run=False,
            extractor=lambda p, cand: _fake_body(3),
        )
        assert [(o.ref_id, o.outcome) for o in out] == [(rid, "extracted")]
        assert not _tags(store, rid)

    def test_dry_run_probes_but_writes_nothing(
        self, store: Store, tmp_path: Any
    ) -> None:
        from precis.ingest.paper_hygiene import heal_bodiless_pdfs

        pdf = tmp_path / "dry.pdf"
        _write_pdf(pdf, text="Readable text.")
        rid_ok, _ = _bodiless_paper(
            store, slug="bl-dry-ok", storage_path=str(pdf), title=""
        )
        rid_prev, _ = _bodiless_paper(store, slug="bl-dry-prev", storage_path=str(pdf))
        _fetch_event(
            store, rid_prev, "fetch_ok", hours_ago=2, source="fetcher:elsevier"
        )

        def never(path: Any, cand: Any) -> Any:
            raise AssertionError("dry run must not extract")

        out = heal_bodiless_pdfs(store, corpus_dirs=(tmp_path,), extractor=never)
        assert {o.ref_id: o.outcome for o in out} == {
            rid_ok: "readable",
            rid_prev: "preview",
        }
        assert _heal_events(store, rid_ok) == [] and _heal_events(store, rid_prev) == []
        assert _body_count(store, rid_ok) == 0

    def test_extract_limit_defers_the_rest(self, store: Store, tmp_path: Any) -> None:
        from precis.ingest.paper_hygiene import heal_bodiless_pdfs

        pdf = tmp_path / "cap.pdf"
        _write_pdf(pdf, text="Readable text.")
        rid_a, _ = _bodiless_paper(
            store, slug="bl-cap-a", storage_path=str(pdf), title=""
        )
        rid_b, _ = _bodiless_paper(
            store, slug="bl-cap-b", storage_path=str(pdf), title=""
        )

        out = heal_bodiless_pdfs(
            store,
            corpus_dirs=(tmp_path,),
            dry_run=False,
            extract_limit=1,
            extractor=lambda p, cand: _fake_body(2),
        )
        assert [(o.ref_id, o.outcome) for o in out] == [
            (rid_a, "extracted"),
            (rid_b, "deferred"),
        ]
        assert _heal_events(store, rid_b) == []
        # The deferred one is first in line next pass.
        again = heal_bodiless_pdfs(
            store,
            corpus_dirs=(tmp_path,),
            dry_run=False,
            extractor=lambda p, cand: _fake_body(2),
        )
        assert [(o.ref_id, o.outcome) for o in again] == [(rid_b, "extracted")]

    def test_new_sha_after_verdict_is_judged_afresh(
        self, store: Store, tmp_path: Any
    ) -> None:
        from precis.ingest.paper_hygiene import heal_bodiless_pdfs

        rid, _ = _bodiless_paper(
            store, slug="bl-resha", storage_path=str(tmp_path / "x.pdf")
        )
        out = heal_bodiless_pdfs(store, corpus_dirs=(tmp_path,), dry_run=False)
        assert out[0].outcome == "missing_file"
        assert heal_bodiless_pdfs(store, corpus_dirs=(tmp_path,), dry_run=False) == []

        # A re-fetch lands a different file for the same ref.
        new_sha = "f" * 64
        _stamp_pdf(store, rid, new_sha)
        again = heal_bodiless_pdfs(store, corpus_dirs=(tmp_path,), dry_run=False)
        assert [(o.ref_id, o.pdf_sha256) for o in again] == [(rid, new_sha)]
        assert [e[0] for e in _heal_events(store, rid)] == [
            "missing_file",
            "missing_file",
        ]

    def test_bodied_and_retired_papers_are_not_candidates(
        self, store: Store, tmp_path: Any
    ) -> None:
        from precis.ingest.paper_hygiene import heal_bodiless_pdfs

        rid_body, _ = _bodiless_paper(store, slug="bl-has", storage_path="/nope.pdf")
        _body_chunk(store, rid_body, 0, "A body paragraph.")
        rid_dead, _ = _bodiless_paper(store, slug="bl-dead", storage_path="/nope.pdf")
        with store.tx() as conn:
            store.retire_ref(rid_dead, conn=conn)
        assert heal_bodiless_pdfs(store, corpus_dirs=(tmp_path,), dry_run=True) == []

    def test_extract_per_pass_env(self, monkeypatch: Any) -> None:
        from precis.ingest.paper_hygiene import bodiless_extract_per_pass

        monkeypatch.delenv("PRECIS_BODILESS_HEAL_EXTRACT_PER_PASS", raising=False)
        assert bodiless_extract_per_pass() == 5
        monkeypatch.setenv("PRECIS_BODILESS_HEAL_EXTRACT_PER_PASS", "0")
        assert bodiless_extract_per_pass() == 0
        monkeypatch.setenv("PRECIS_BODILESS_HEAL_EXTRACT_PER_PASS", "junk")
        assert bodiless_extract_per_pass() == 5

    def test_extractor_gets_ref_title_and_doi_and_spread_is_journalled(
        self, store: Store, tmp_path: Any
    ) -> None:
        """gr463414: the ref's own identity is the article-spread target and
        a trimmed spread lands an ``ingest:spread`` event, untagged."""
        from precis.ingest.paper_hygiene import heal_bodiless_pdfs

        pdf = tmp_path / "spread.pdf"
        _write_pdf(pdf, text="Paper bl-spread\nTwo letters share this page.")
        rid, _ = _bodiless_paper(
            store, slug="bl-spread", storage_path=str(pdf), title="Paper bl-spread"
        )
        store.set_ref_identifier(rid, "doi", "10.1038/24521", source="manual")
        seen: list[Any] = []

        def fake(path: Any, cand: Any) -> Any:
            seen.append(cand)
            return _fake_body(
                2,
                spread={
                    "verdict": "trimmed",
                    "reason": "title",
                    "articles": 2,
                    "kept": {
                        "blocks": [3, 8],
                        "pages": [2, 3],
                        "head": "Paper bl-spread",
                        "score": 0.9,
                    },
                    "cut": [
                        {
                            "blocks": [0, 3],
                            "pages": [1, 2],
                            "head": "Neighbour letter",
                            "score": 0.1,
                        }
                    ],
                    "residual": False,
                },
            )

        out = heal_bodiless_pdfs(
            store, corpus_dirs=(tmp_path,), dry_run=False, extractor=fake
        )
        assert [(o.ref_id, o.outcome) for o in out] == [(rid, "extracted")]
        (cand,) = seen
        assert cand.title == "Paper bl-spread"
        assert cand.doi == "10.1038/24521"
        [(event, payload)] = _spread_events(store, rid)
        assert event == "trimmed"
        assert payload["verdict"] == "trimmed" and payload["articles"] == 2
        assert payload["kept"]["blocks"] == [3, 8]
        assert payload["cut"][0]["head"] == "Neighbour letter"
        assert payload["file"] == "spread.pdf"
        # A clean trim needs no multi-article tag.
        assert "multi-article" not in _tags(store, rid)
        assert _body_count(store, rid) == 2

    def test_ambiguous_spread_is_journalled_and_tagged(
        self, store: Store, tmp_path: Any
    ) -> None:
        """An ambiguous/residual spread keeps the whole body, journals the
        verdict and tags the paper ``multi-article``, like precis_add."""
        from precis.ingest.paper_hygiene import heal_bodiless_pdfs

        pdf = tmp_path / "ambig.pdf"
        _write_pdf(pdf, text="Two letters share this page.")
        rid, _ = _bodiless_paper(
            store, slug="bl-ambig", storage_path=str(pdf), title=""
        )

        def fake(path: Any, cand: Any) -> Any:
            return _fake_body(
                4,
                spread={
                    "verdict": "ambiguous",
                    "reason": "no confident match",
                    "articles": 2,
                    "found": [
                        {"blocks": [0, 4], "pages": [1, 1], "head": "A", "score": 0.4},
                        {"blocks": [4, 8], "pages": [2, 2], "head": "B", "score": 0.4},
                    ],
                },
            )

        out = heal_bodiless_pdfs(
            store, corpus_dirs=(tmp_path,), dry_run=False, extractor=fake
        )
        assert [(o.ref_id, o.outcome) for o in out] == [(rid, "extracted")]
        [(event, payload)] = _spread_events(store, rid)
        assert event == "ambiguous" and payload["reason"] == "no confident match"
        assert "multi-article" in _tags(store, rid)
        assert _body_count(store, rid) == 4

    def test_filename_like_title_heals_unchecked(
        self, store: Store, tmp_path: Any
    ) -> None:
        """gr517423 r3: a filename-like title carries no identity signal —
        the gate reads ``unchecked`` and the ref heals, not strands."""
        from precis.ingest.paper_hygiene import heal_bodiless_pdfs

        pdf = tmp_path / "ok.pdf"
        _write_pdf(pdf, text="A real body of text on the page.")
        rid, _ = _bodiless_paper(
            store,
            slug="bl-fname",
            storage_path=str(pdf),
            title="s41586-020-2649-2.pdf",
        )
        out = heal_bodiless_pdfs(
            store,
            corpus_dirs=(tmp_path,),
            dry_run=False,
            extractor=lambda p, cand: _fake_body(3),
        )
        assert [(o.ref_id, o.outcome) for o in out] == [(rid, "extracted")]
        assert _body_count(store, rid) == 3

    def test_identity_mismatch_rearms_on_repaired_title(
        self, store: Store, tmp_path: Any
    ) -> None:
        """gr517423 r3: a mismatch verdict is keyed on the judged title —
        while the title stands the sha stays excluded, but a repaired
        title re-arms the heal for a later pass."""
        from precis.ingest.paper_hygiene import heal_bodiless_pdfs

        pdf = tmp_path / "rearm.pdf"
        _write_pdf(pdf, text="Paper bl-rearm\nA real body of text.")
        rid, sha = _bodiless_paper(
            store,
            slug="bl-rearm",
            storage_path=str(pdf),
            title="Totally Unrelated Study of Field Mice",
        )
        out = heal_bodiless_pdfs(
            store,
            corpus_dirs=(tmp_path,),
            dry_run=False,
            extractor=lambda p, cand: _fake_body(3),
        )
        assert [(o.ref_id, o.outcome) for o in out] == [(rid, "identity_mismatch")]
        events = _heal_events(store, rid)
        assert events[0][0] == "identity_mismatch"
        assert events[0][1]["pdf_sha256"] == sha
        assert events[0][1]["title"] == "Totally Unrelated Study of Field Mice"
        assert _body_count(store, rid) == 0
        # Same title: the recorded verdict keeps the sha excluded.
        assert heal_bodiless_pdfs(store, corpus_dirs=(tmp_path,), dry_run=False) == []
        # The title-repair pass lands a real title — the (sha, title)
        # key no longer matches, so the next pass judges the file afresh.
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET title = 'Paper bl-rearm' WHERE ref_id = %s",
                (rid,),
            )
            conn.commit()
        out = heal_bodiless_pdfs(
            store,
            corpus_dirs=(tmp_path,),
            dry_run=False,
            extractor=lambda p, cand: _fake_body(3),
        )
        assert [(o.ref_id, o.outcome) for o in out] == [(rid, "extracted")]
        assert _body_count(store, rid) == 3


class TestRaiseJunkTitlePapers:
    """gr477964: unfixable junk-titled papers are raised to a human queue."""

    def _old(
        self, store: Store, *, slug: str, title: str, doi: str | None = None
    ) -> int:
        rid = store.insert_ref(kind="paper", slug=slug, title=title).id
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET created_at = now() - interval '30 days' "
                "WHERE ref_id = %s",
                (rid,),
            )
        if doi:
            store.set_ref_identifier(rid, "doi", doi, source="manual")
        return rid

    def _todos(self, store: Store, paper_id: int) -> list[Any]:
        with store.pool.connection() as conn:
            return conn.execute(
                "SELECT ref_id, title FROM refs WHERE kind='todo' "
                "AND meta->>'paper_ref_id' = %s",
                (str(paper_id),),
            ).fetchall()

    def _stamp(self, store: Store, paper_id: int) -> None:
        """Mark the paper terminally concluded by the repair cascade —
        the ``title_repair_v`` stamp is the gate for owner-queue filing
        (gr508499)."""
        from precis.ingest.paper_hygiene import _TITLE_REPAIR_VERSION

        store.stamp_ref_meta(paper_id, {"title_repair_v": _TITLE_REPAIR_VERSION})

    def test_unstamped_junk_paper_waits_for_repair(self, store: Store) -> None:
        """gr508499: a junk title the cascade hasn't concluded on is NOT
        filed — the todo claims "automation could not repair it", which is
        only true once ``title_repair_v`` is stamped."""
        self._old(store, slug="anon24z", title="__")
        assert raise_junk_title_papers(store, dry_run=True) == []

    def test_files_one_todo_for_underscore_title(self, store: Store) -> None:
        rid = self._old(store, slug="anon24d", title="__")
        self._stamp(store, rid)
        assert raise_junk_title_papers(store, dry_run=False) == [rid]
        (todo,) = self._todos(store, rid)
        assert "anon24d" in todo[1] and "'__'" in todo[1]
        rendered = {str(t) for t in store.tags_for(todo[0])}
        assert any("paper-title-fix" in r for r in rendered), rendered
        # gr508499: owner_login NULL → the deployment-owner default
        # (``load_config().owner``, ``"reto"``), not the old "owner"
        # placeholder nobody claimed.
        assert "waiting-for:reto" in rendered, rendered
        assert any("open" in r for r in rendered), rendered

    def test_not_refiled_while_open(self, store: Store) -> None:
        rid = self._old(store, slug="anon24f", title="-")
        self._stamp(store, rid)
        raise_junk_title_papers(store, dry_run=False)
        assert raise_junk_title_papers(store, dry_run=False) == []
        assert len(self._todos(store, rid)) == 1

    def _close(self, store: Store, paper_id: int, status: str) -> None:
        from precis.store.types import Tag

        for todo_id, _ in self._todos(store, paper_id):
            store.add_tag(
                todo_id,
                Tag.closed("STATUS", status),
                set_by="system",
                replace_prefix=True,
            )

    def test_refiled_once_when_closed_done_but_still_junk(self, store: Store) -> None:
        rid = self._old(store, slug="anon24h", title="__")
        self._stamp(store, rid)
        raise_junk_title_papers(store, dry_run=False)
        self._close(store, rid, "done")
        assert raise_junk_title_papers(store, dry_run=False) == [rid]
        assert raise_junk_title_papers(store, dry_run=False) == []
        assert len(self._todos(store, rid)) == 2

    def test_not_refiled_when_closed_wontfix(self, store: Store) -> None:
        rid = self._old(store, slug="anon24i", title="__")
        self._stamp(store, rid)
        raise_junk_title_papers(store, dry_run=False)
        self._close(store, rid, "wontfix")
        assert raise_junk_title_papers(store, dry_run=False) == []
        assert len(self._todos(store, rid)) == 1

    def test_dry_run_writes_nothing(self, store: Store) -> None:
        rid = self._old(store, slug="anon24g", title="...")
        self._stamp(store, rid)
        assert raise_junk_title_papers(store, dry_run=True) == [rid]
        assert self._todos(store, rid) == []

    def test_closes_todo_once_title_is_fixed(self, store: Store) -> None:
        """gr658659: enrichment fixed the title after the todo was filed."""
        rid = self._old(store, slug="anon24s", title="__")
        self._stamp(store, rid)
        raise_junk_title_papers(store, dry_run=False)
        assert close_resolved_title_fix_todos(store, dry_run=False) == []
        store.update_paper_fields(rid, title="A Real Title")
        assert close_resolved_title_fix_todos(store, dry_run=True) == [rid]
        assert close_resolved_title_fix_todos(store, dry_run=False) == [rid]
        (todo,) = self._todos(store, rid)
        assert any("done" in str(t) for t in store.tags_for(todo[0]))
        assert close_resolved_title_fix_todos(store, dry_run=False) == []

    def test_wontdo_todo_is_never_auto_closed_or_counted(self, store: Store) -> None:
        """review td659675: a won't-do/auto-timeout todo can never be
        auto-closed (``_close_title_fix_todos`` touches open todos only),
        so the shared terminal predicate must exclude it from the SELECT
        before ``limit`` — else it is selected every pass, reported as
        closed, and starves genuinely open todos."""
        wontdo = self._old(store, slug="wontdo26a", title="__")
        self._stamp(store, wontdo)
        open_p = self._old(store, slug="open26a", title="__")
        self._stamp(store, open_p)
        assert raise_junk_title_papers(store, dry_run=False) == [wontdo, open_p]
        self._close(store, wontdo, "won't-do")
        for rid in (wontdo, open_p):
            store.update_paper_fields(rid, title="A Real Title")
        # The won't-do todo sorts first (lower paper ref_id) yet must not
        # consume the limit: the open paper is the one closed.
        assert close_resolved_title_fix_todos(store, dry_run=True, limit=1) == [open_p]
        assert close_resolved_title_fix_todos(store, dry_run=False) == [open_p]
        (todo,) = self._todos(store, open_p)
        assert any("done" in str(t) for t in store.tags_for(todo[0]))
        (wtodo,) = self._todos(store, wontdo)
        wtags = {str(t) for t in store.tags_for(wtodo[0])}
        assert any("won't-do" in t for t in wtags), wtags
        assert not any("done" in t for t in wtags), wtags
        # A second pass is a no-op: nothing left open, nothing re-counted.
        assert close_resolved_title_fix_todos(store, dry_run=False) == []

    def test_skips_fixable_and_young_papers(self, store: Store) -> None:
        good = self._old(store, slug="good24", title="A Real Title")
        stub = self._old(store, slug="stub24", title=PLACEHOLDER_TITLE, doi="10.1234/z")
        young = store.insert_ref(kind="paper", slug="young24", title="__").id
        # The stamp gate (gr508499) doesn't override the other skips: real
        # titles, enrich-owned placeholders and under-grace papers still
        # file nothing.
        for rid in (good, stub, young):
            self._stamp(store, rid)
        assert raise_junk_title_papers(store, dry_run=True) == []

    def test_non_article_entry_type_is_never_flagged(self, store: Store) -> None:
        """gr515227: the junk rules are article-title rules — a dissertation
        / proceedings volume / component whose title looks like junk
        ("__", a section name, an export filename) keeps its title; no
        todo is filed. An unset entry_type still files (un-enriched
        papers are presumed articles)."""
        dis = self._old(store, slug="diss25", title="__")
        store.update_ref(dis, meta_patch={"entry_type": "dissertation"})
        art = self._old(store, slug="art25", title="__")
        no_type = self._old(store, slug="notype25", title="__")
        for rid in (dis, art, no_type):
            self._stamp(store, rid)
        store.update_ref(art, meta_patch={"entry_type": "journal-article"})
        assert raise_junk_title_papers(store, dry_run=True) == [art, no_type]
        assert self._todos(store, dis) == []


# ── partial bodies: references / SI only (gr464259) ──────────────


def _kind_chunk(
    store: Store, ref_id: int, ord_: int, text: str, kind: str = "paragraph"
) -> None:
    with store.pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, %s, %s, %s)",
                (ref_id, ord_, kind, text),
            )


def _partial_paper(store: Store, slug: str, *, si: bool = False) -> int:
    rid = _paper(store, slug=slug, title=f"Paper {slug}")
    _stamp_pdf(store, rid, f"{rid:064d}")
    if si:
        _kind_chunk(store, rid, 0, "Supporting Information for the rotary motors")
        for i in range(1, 10):
            _kind_chunk(store, rid, i, f"General experimental NMR data {i}.")
    else:
        _kind_chunk(store, rid, 0, "Abstract of the structure.")
        for i in range(1, 12):
            _kind_chunk(store, rid, i, f"[{i}] Some citation.", kind="references")
    return rid


def _partial_events(store: Store, rid: int) -> list[tuple[str, dict[str, Any]]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT event, payload FROM ref_events WHERE ref_id=%s "
            "AND source='heal:partial_body' ORDER BY event_id",
            (rid,),
        ).fetchall()
    return [(str(r[0]), dict(r[1] or {})) for r in rows]


def _age_event(store: Store, rid: int, event: str, hours: float) -> None:
    with store.pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "UPDATE ref_events SET ts = now() - make_interval(hours => %s) "
                "WHERE ref_id=%s AND event=%s",
                (hours, rid, event),
            )


class TestClassifyPartialBody:
    def test_references_dominant(self) -> None:
        from precis.ingest.paper_hygiene import classify_partial_body

        chunks = [("paragraph", "Abstract.", None)] + [
            ("references", f"[{i}] x", None) for i in range(27)
        ]
        v = classify_partial_body(chunks)
        assert v is not None and v.kind == "references" and v.fraction >= 0.9

    def test_si_document(self) -> None:
        from precis.ingest.paper_hygiene import classify_partial_body

        chunks = [("paragraph", "Supporting Information", None)] + [
            ("paragraph", f"NMR data {i}", None) for i in range(17)
        ]
        v = classify_partial_body(chunks)
        assert v is not None and v.kind == "si"

    def test_real_article_is_not_partial(self) -> None:
        from precis.ingest.paper_hygiene import classify_partial_body

        chunks = (
            [("paragraph", "# Introduction\nWe study.", None)]
            + [("paragraph", f"Result {i}.", None) for i in range(10)]
            + [("references", f"[{i}] x", None) for i in range(10)]
        )
        assert classify_partial_body(chunks) is None

    def test_si_with_main_heading_is_not_si_doc(self) -> None:
        from precis.ingest.paper_hygiene import classify_partial_body

        chunks = [
            ("paragraph", "Supporting Information", None),
            ("paragraph", "# Results\nThe rate is 3 MHz.", None),
        ] + [("paragraph", f"Body {i}", None) for i in range(10)]
        assert classify_partial_body(chunks) is None


class TestHealPartialBody:
    def test_queues_one_refetch_then_terminal_flag_and_never_again(
        self, store: Store
    ) -> None:
        from precis.ingest.paper_hygiene import heal_partial_body_papers

        rid = _partial_paper(store, "pb-refs")
        _fetch_event(store, rid, "fetch_ok", hours_ago=48)

        # Dry run writes nothing.
        dry = heal_partial_body_papers(store, dry_run=True)
        assert [(o.ref_id, o.outcome) for o in dry] == [(rid, "refetch_queued")]
        assert _partial_events(store, rid) == []

        out = heal_partial_body_papers(store, dry_run=False)
        assert [(o.ref_id, o.outcome) for o in out] == [(rid, "refetch_queued")]
        assert [e[0] for e in _partial_events(store, rid)] == ["refetch_queued"]
        meta = _meta(store, rid)
        assert "markup_refetch" in meta and meta["prio_by"] == "acquire"
        assert _fetcher_event_count(store, rid) == 0  # backoff cleared

        # Second pass, re-fetch still in flight: no second queueing.
        again = heal_partial_body_papers(store, dry_run=False)
        assert [(o.ref_id, o.outcome) for o in again] == [(rid, "pending")]
        assert [e[0] for e in _partial_events(store, rid)] == ["refetch_queued"]

        # The re-fetch ran (byte-identical: same sha, pin never consumed).
        _age_event(store, rid, "refetch_queued", 30)
        _fetch_event(store, rid, "fetch_ok", hours_ago=7)
        done = heal_partial_body_papers(store, dry_run=False)
        assert [(o.ref_id, o.outcome, o.reason) for o in done] == [
            (rid, "terminal", "same_file")
        ]
        assert "body:partial" in _tags(store, rid)
        assert "markup_refetch" not in _meta(store, rid)
        assert [e[0] for e in _partial_events(store, rid)] == [
            "refetch_queued",
            "terminal",
        ]

        # Cap: terminal is final, later passes do nothing and never re-queue.
        assert heal_partial_body_papers(store, dry_run=False) == []
        assert "markup_refetch" not in _meta(store, rid)
        assert len(_partial_events(store, rid)) == 2

    def test_new_file_still_partial_is_terminal(self, store: Store) -> None:
        from precis.ingest.paper_hygiene import heal_partial_body_papers

        rid = _partial_paper(store, "pb-new", si=True)
        heal_partial_body_papers(store, dry_run=False)
        _stamp_pdf(store, rid, "e" * 64)  # re-fetch landed a different file
        _age_event(store, rid, "refetch_queued", 30)
        _fetch_event(store, rid, "fetch_ok", hours_ago=7)
        out = heal_partial_body_papers(store, dry_run=False)
        assert [(o.outcome, o.reason) for o in out] == [("terminal", "still_partial")]
        assert "body:partial" in _tags(store, rid)

    def test_no_fetch_within_timeout_is_terminal(self, store: Store) -> None:
        from precis.ingest.paper_hygiene import heal_partial_body_papers

        rid = _partial_paper(store, "pb-timeout")
        heal_partial_body_papers(store, dry_run=False)
        _age_event(store, rid, "refetch_queued", 24 * 8)
        out = heal_partial_body_papers(store, dry_run=False)
        assert [(o.outcome, o.reason) for o in out] == [
            ("terminal", "refetch_unavailable")
        ]
        assert "body:partial" in _tags(store, rid)

    def test_healed_paper_is_left_alone(self, store: Store) -> None:
        from precis.ingest.paper_hygiene import heal_partial_body_papers

        rid = _partial_paper(store, "pb-healed")
        heal_partial_body_papers(store, dry_run=False)
        for i in range(20, 60):
            _kind_chunk(store, rid, i, f"Main text paragraph {i}.")
        _fetch_event(store, rid, "fetch_ok", hours_ago=7)
        assert heal_partial_body_papers(store, dry_run=False) == []
        assert "body:partial" not in _tags(store, rid)


# ── junk-title repair (gr500372) ──────────────────────────────────


def test_export_name_query() -> None:
    from precis.ingest.pdf_sidecar import export_name_query

    assert (
        export_name_query("Microsoft Word - Acceso restringido.doc")
        == "Acceso restringido"
    )
    assert export_name_query("Dispatches.qxp") == "Dispatches"
    assert (
        export_name_query("Microsoft Word - CREATIVITY-by-Mihaly.docx")
        == "CREATIVITY-by-Mihaly"
    )
    # Filename + page-range strings carry no title signal at all.
    assert export_name_query("S401_9d 152..155") == ""
    assert export_name_query("j405_8c 681..685") == ""
    # Not export-shaped → never a query (a PII, a section heading).
    assert export_name_query("PII: S0021-9797(20)31234-5") == ""
    assert export_name_query("In this issue") == ""
    assert export_name_query("A Real Paper Title") == ""


class TestRepairJunkTitlePapers:
    """gr500372: the repair cascade runs before the owner queue."""

    def _old(
        self,
        store: Store,
        *,
        slug: str,
        title: str,
        doi: str | None = None,
        cite_keys: list[str] | None = None,
    ) -> int:
        rid = store.insert_ref(kind="paper", slug=slug, title=title).id
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET created_at = now() - interval '30 days' "
                "WHERE ref_id = %s",
                (rid,),
            )
        if doi:
            store.set_ref_identifier(rid, "doi", doi, source="manual")
        if cite_keys:
            store.insert_ref_identifiers(
                rid, [("cite_key", ck, "manual") for ck in cite_keys]
            )
        return rid

    def _todos(self, store: Store, paper_id: int) -> list[Any]:
        with store.pool.connection() as conn:
            return conn.execute(
                "SELECT ref_id, title FROM refs WHERE kind='todo' "
                "AND meta->>'paper_ref_id' = %s",
                (str(paper_id),),
            ).fetchall()

    def _todo_status(self, store: Store, todo_id: int) -> str | None:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT tg.value FROM ref_tags rt JOIN tags tg USING (tag_id) "
                "WHERE rt.ref_id = %s AND tg.namespace = 'STATUS'",
                (todo_id,),
            ).fetchone()
        return str(row[0]) if row else None

    def _no_net(self) -> dict[str, Any]:
        def refuse(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("network resolver should not be called")

        return {"crossref_fn": refuse, "openalex_fn": refuse, "s2_fn": refuse}

    def test_markup_title_is_not_junk(self, store: Store) -> None:
        """gr515227: markup stays in the stored title — its plain view is
        a real title, so neither the repair cascade nor the raise leg
        touches the paper."""
        from precis.ingest.paper_hygiene import repair_junk_title_papers

        rid = self._old(
            store,
            slug="ma09",
            title="Stone-Wales defects in graphene and other planar"
            "<mml:math><mml:mrow><mml:mi>s</mml:mi><mml:msup><mml:mi>p</mml:mi>"
            "<mml:mn>2</mml:mn></mml:msup></mml:mrow></mml:math>"
            "-bonded materials",
        )
        assert repair_junk_title_papers(store, dry_run=False, **self._no_net()) == []
        ref = store.fetch_refs_by_ids([rid])[rid]
        assert "<mml:math>" in ref.title  # stored raw, untouched
        assert raise_junk_title_papers(store, dry_run=True) == []

    def test_repair_closes_open_todo(self, store: Store) -> None:
        from precis.ingest.paper_hygiene import (
            TITLE_FIX_SOURCE,
            repair_junk_title_papers,
        )

        rid = self._old(
            store,
            slug="ravel05",
            title="Microsoft Word - whatever.doc",
            doi="10.1234/real.1",
        )
        # Filed directly: since gr508499 the raise leg only files on
        # cascade-stamped papers, and a stamped paper is excluded from
        # repair — so this test seeds the todo shape the leg closes.
        with store.tx() as conn:
            store.insert_ref(
                kind="todo",
                slug=None,
                title="Fix title/metadata of paper:paX",
                meta={"source": TITLE_FIX_SOURCE, "paper_ref_id": rid},
                conn=conn,
            )
        (todo,) = self._todos(store, rid)
        out = repair_junk_title_papers(
            store,
            dry_run=False,
            crossref_fn=_fake_crossref(
                {"title": "The Real Registry Title", "doi": "10.1234/real.1"}
            ),
            openalex_fn=self._no_net()["openalex_fn"],
            s2_fn=self._no_net()["s2_fn"],
        )
        assert [o.action for o in out] == ["crossref"]
        assert self._todo_status(store, todo[0]) == "done"

    def test_real_titles_untouched(self, store: Store) -> None:
        from precis.ingest.paper_hygiene import repair_junk_title_papers

        self._old(store, slug="good99", title="A Perfectly Real Paper Title")
        assert repair_junk_title_papers(store, dry_run=False) == []

    def test_crossref_leg_repairs_export_name(self, store: Store) -> None:
        from precis.ingest.paper_hygiene import repair_junk_title_papers

        rid = self._old(
            store,
            slug="worddoc",
            title="Microsoft Word - whatever.doc",
            doi="10.1234/real.1",
        )
        crossref = _fake_crossref(
            {"title": "The Real Registry Title", "doi": "10.1234/real.1"}
        )
        out = repair_junk_title_papers(
            store,
            dry_run=False,
            crossref_fn=crossref,
            openalex_fn=self._no_net()["openalex_fn"],
            s2_fn=self._no_net()["s2_fn"],
        )
        assert [(o.ref_id, o.action) for o in out] == [(rid, "crossref")]
        assert store.fetch_refs_by_ids([rid])[rid].title == "The Real Registry Title"

    def test_title_leg_merges_into_doi_owner(self, store: Store) -> None:
        """gr658635: a hit whose DOI a live paper holds is a duplicate."""
        from precis.ingest.paper_hygiene import repair_junk_title_papers

        owner = self._old(
            store,
            slug="hanson96",
            title="Methanotrophic bacteria and their ecology in soils",
            doi="10.1234/dup.1",
        )
        with store.pool.connection() as conn:
            conn.execute("UPDATE refs SET year = 1996 WHERE ref_id = %s", (owner,))
        stub = self._old(store, slug="stub01", title="S401_9d 152..155")
        with store.pool.connection() as conn:
            conn.execute("UPDATE refs SET year = 1996 WHERE ref_id = %s", (stub,))
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, 0, 'paragraph', %s)",
                (stub, "Methanotrophic bacteria and their ecology in soils\n\nBody."),
            )
        hit = {
            "title": "Methanotrophic bacteria and their ecology in soils",
            "doi": "10.1234/dup.1",
            "year": 1996,
        }
        out = repair_junk_title_papers(
            store,
            dry_run=False,
            crossref_fn=self._no_net()["crossref_fn"],
            openalex_fn=self._no_net()["openalex_fn"],
            s2_fn=lambda title, key: hit,
        )
        assert [(o.ref_id, o.action, o.detail) for o in out] == [
            (stub, "merged", f"into pa{owner}")
        ]
        loser = store.fetch_refs_by_ids([stub])[stub]
        assert loser.retired_at is not None
        assert store.follow_supersede(stub) == owner

    def test_title_leg_doi_owner_mismatch_fails_naming_owner(
        self, store: Store
    ) -> None:
        """gr658635: a DOI owner whose title disagrees is not merged."""
        from precis.ingest.paper_hygiene import repair_junk_title_papers

        owner = self._old(
            store,
            slug="other99",
            title="Quantum dots in photovoltaic tandem architectures",
            doi="10.1234/dup.2",
        )
        stub = self._old(store, slug="stub02", title="S401_9d 152..155")
        with store.pool.connection() as conn:
            conn.execute("UPDATE refs SET year = 1996 WHERE ref_id = %s", (stub,))
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, 0, 'paragraph', %s)",
                (stub, "Methanotrophic bacteria and their ecology in soils\n\nBody."),
            )
        hit = {
            "title": "Methanotrophic bacteria and their ecology in soils",
            "doi": "10.1234/dup.2",
            "year": 1996,
        }
        out = repair_junk_title_papers(
            store,
            dry_run=False,
            crossref_fn=self._no_net()["crossref_fn"],
            openalex_fn=self._no_net()["openalex_fn"],
            s2_fn=lambda title, key: hit,
        )
        assert [(o.ref_id, o.action) for o in out] == [(stub, "failed")]
        assert f"doi-held-by-pa{owner}" in out[0].detail
        assert store.fetch_refs_by_ids([stub])[stub].retired_at is None
        assert store.fetch_refs_by_ids([owner])[owner].retired_at is None

    def test_title_leg_doi_owner_body_guard_parks_not_merges(
        self, store: Store
    ) -> None:
        """gr658635/td659670: the DOI-owner merge only consumes a hit that
        survived the resolver's body guards — a stub whose own chunk 0
        shares no content tokens with the recovered title is a different
        work, not a duplicate: it parks, it is NOT folded into the owner."""
        from precis.ingest.paper_hygiene import repair_junk_title_papers

        owner = self._old(
            store,
            slug="hanson97",
            title="Methanotrophic bacteria and their ecology in soils",
            doi="10.1234/dup.3",
        )
        stub = self._old(store, slug="stub03", title="S401_9d 152..155")
        with store.pool.connection() as conn:
            conn.execute("UPDATE refs SET year = 1996 WHERE ref_id = %s", (stub,))
            # Chunk 0 is a different paper's front matter — the
            # zero-overlap guard's signature. The hit title sits in the
            # next chunk so the title-search leg still finds it.
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, 0, 'paragraph', %s)",
                (
                    stub,
                    "Quantum spin Hall insulator state in mercury "
                    "telluride wells measured through microwave "
                    "impedance spectroscopy",
                ),
            )
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, 1, 'paragraph', %s)",
                (
                    stub,
                    "Methanotrophic bacteria and their ecology in soils\n\nBody.",
                ),
            )
        hit = {
            "title": "Methanotrophic bacteria and their ecology in soils",
            "doi": "10.1234/dup.3",
            "year": 1996,
        }
        out = repair_junk_title_papers(
            store,
            dry_run=False,
            crossref_fn=self._no_net()["crossref_fn"],
            openalex_fn=self._no_net()["openalex_fn"],
            s2_fn=lambda title, key: hit,
        )
        assert [(o.ref_id, o.action, o.detail) for o in out] == [
            (stub, "parked", "registry-title-zero-overlap")
        ]
        assert store.fetch_refs_by_ids([stub])[stub].retired_at is None
        assert store.fetch_refs_by_ids([owner])[owner].retired_at is None

    def test_title_leg_merges_when_owner_year_unknown(self, store: Store) -> None:
        """gr658635: merge_into gate parity — a NULL owner year passes at
        near-identical sim (``_MERGE_UNKNOWN_YEAR_SIM``), like the door."""
        from precis.ingest.paper_hygiene import repair_junk_title_papers

        owner = self._old(
            store,
            slug="hanson98",
            title="Methanotrophic bacteria and their ecology in soils",
            doi="10.1234/dup.4",
        )
        # Owner deliberately carries no year — the 0.95-sim waiver lets the
        # merge through anyway (the DOI twin is the corroboration).
        stub = self._old(store, slug="stub04", title="S401_9d 152..155")
        with store.pool.connection() as conn:
            conn.execute("UPDATE refs SET year = 1996 WHERE ref_id = %s", (stub,))
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, 0, 'paragraph', %s)",
                (stub, "Methanotrophic bacteria and their ecology in soils\n\nBody."),
            )
        hit = {
            "title": "Methanotrophic bacteria and their ecology in soils",
            "doi": "10.1234/dup.4",
            "year": 1996,
        }
        out = repair_junk_title_papers(
            store,
            dry_run=False,
            crossref_fn=self._no_net()["crossref_fn"],
            openalex_fn=self._no_net()["openalex_fn"],
            s2_fn=lambda title, key: hit,
        )
        assert [(o.ref_id, o.action, o.detail) for o in out] == [
            (stub, "merged", f"into pa{owner}")
        ]
        assert store.follow_supersede(stub) == owner

    def test_v1_stamped_duplicate_is_retried_and_merged(self, store: Store) -> None:
        """gr658635/td659670: a stub the pre-merge cascade already failed
        (``title_repair_v=1``, ``paper-title-fix`` todo filed) is exactly
        the backlog the merge leg was built for — the version bump must
        re-admit it, merge it and close its todo."""
        from precis.ingest.paper_hygiene import (
            _TITLE_REPAIR_VERSION,
            TITLE_FIX_SOURCE,
            repair_junk_title_papers,
        )

        assert _TITLE_REPAIR_VERSION > 1
        owner = self._old(
            store,
            slug="hanson00",
            title="Methanotrophic bacteria and their ecology in soils",
            doi="10.1234/dup.5",
        )
        with store.pool.connection() as conn:
            conn.execute("UPDATE refs SET year = 1996 WHERE ref_id = %s", (owner,))
        stub = self._old(store, slug="stub05", title="S401_9d 152..155")
        with store.pool.connection() as conn:
            conn.execute("UPDATE refs SET year = 1996 WHERE ref_id = %s", (stub,))
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, 0, 'paragraph', %s)",
                (stub, "Methanotrophic bacteria and their ecology in soils\n\nBody."),
            )
            # Stamped by the v1 (pre-merge) cascade and already raised to
            # the owner queue — the gripe's exact starting state.
            store.insert_ref(
                kind="todo",
                slug=None,
                title="Fix title/metadata of paper:paX",
                meta={"source": TITLE_FIX_SOURCE, "paper_ref_id": stub},
                conn=conn,
            )
        store.stamp_ref_meta(stub, {"title_repair_v": 1})
        (todo,) = self._todos(store, stub)
        hit = {
            "title": "Methanotrophic bacteria and their ecology in soils",
            "doi": "10.1234/dup.5",
            "year": 1996,
        }
        out = repair_junk_title_papers(
            store,
            dry_run=False,
            crossref_fn=self._no_net()["crossref_fn"],
            openalex_fn=self._no_net()["openalex_fn"],
            s2_fn=lambda title, key: hit,
        )
        assert [(o.ref_id, o.action, o.detail) for o in out] == [
            (stub, "merged", f"into pa{owner}")
        ]
        assert store.follow_supersede(stub) == owner
        assert self._todo_status(store, todo[0]) == "done"

    def test_title_leg_doi_owner_cleared_doi_blocks_merge(self, store: Store) -> None:
        """gr658635/td659670 review: a stub whose doi scheme a human
        cleared (``meta.cleared_identifiers.doi``, gr515246) is never
        folded into the recovered DOI's owner — the merge honours the
        same suppression ``apply_resolution`` does. The stub stays live
        and its paper-title-fix todo stays open."""
        from precis.ingest.paper_hygiene import (
            TITLE_FIX_SOURCE,
            repair_junk_title_papers,
        )

        owner = self._old(
            store,
            slug="hanson96c",
            title="Methanotrophic bacteria and their ecology in soils",
            doi="10.1234/dup.6",
        )
        with store.pool.connection() as conn:
            conn.execute("UPDATE refs SET year = 1996 WHERE ref_id = %s", (owner,))
        stub = self._old(store, slug="stub06", title="S401_9d 152..155")
        # The stub once carried a wrong DOI; a human cleared it — the
        # marker now suppresses every machine DOI write, so the recovered
        # DOI must not retire the stub either.
        store.set_ref_identifier(stub, "doi", "10.1234/wrong.1", source="manual")
        store.clear_ref_identifier(stub, "doi", source="web-edit")
        with store.pool.connection() as conn:
            conn.execute("UPDATE refs SET year = 1996 WHERE ref_id = %s", (stub,))
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, 0, 'paragraph', %s)",
                (stub, "Methanotrophic bacteria and their ecology in soils\n\nBody."),
            )
            store.insert_ref(
                kind="todo",
                slug=None,
                title="Fix title/metadata of paper:paX",
                meta={"source": TITLE_FIX_SOURCE, "paper_ref_id": stub},
                conn=conn,
            )
        (todo,) = self._todos(store, stub)
        hit = {
            "title": "Methanotrophic bacteria and their ecology in soils",
            "doi": "10.1234/dup.6",
            "year": 1996,
        }
        out = repair_junk_title_papers(
            store,
            dry_run=False,
            crossref_fn=self._no_net()["crossref_fn"],
            openalex_fn=self._no_net()["openalex_fn"],
            s2_fn=lambda title, key: hit,
        )
        assert [(o.ref_id, o.action) for o in out] == [(stub, "failed")]
        assert f"doi-human-suppressed:pa{owner}" in out[0].detail
        assert store.fetch_refs_by_ids([stub])[stub].retired_at is None
        assert self._todo_status(store, todo[0]) is None
        # The owner keeps its DOI — the stub's cleared mark never rode
        # across (``_merge_cleared_markers`` would delete the row).
        assert store.find_ref_by_identifier("doi", "10.1234/dup.6") == owner

    def test_title_leg_doi_owner_dropped_pair_blocks_merge(self, store: Store) -> None:
        """gr658635/td659670 review: the per-value sibling — a stub whose
        operator dropped exactly the recovered (doi, value) pair
        (``meta.dropped_identifiers``, gr517591) is not folded into that
        DOI's owner either."""
        from precis.ingest.paper_hygiene import repair_junk_title_papers

        owner = self._old(
            store,
            slug="hanson96d",
            title="Methanotrophic bacteria and their ecology in soils",
            doi="10.1234/dup.7",
        )
        with store.pool.connection() as conn:
            conn.execute("UPDATE refs SET year = 1996 WHERE ref_id = %s", (owner,))
        stub = self._old(store, slug="stub07", title="S401_9d 152..155")
        # The stub once held this very DOI; a human dropped just that
        # value (the scheme stays open for a correct one) before the
        # owner claimed it.
        store.stamp_ref_meta(
            stub,
            {
                "dropped_identifiers": {
                    "doi": {
                        "10.1234/dup.7": {
                            "at": "2026-01-01T00:00:00+00:00",
                            "by": "drop_identifier",
                        }
                    }
                }
            },
        )
        with store.pool.connection() as conn:
            conn.execute("UPDATE refs SET year = 1996 WHERE ref_id = %s", (stub,))
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, 0, 'paragraph', %s)",
                (stub, "Methanotrophic bacteria and their ecology in soils\n\nBody."),
            )
        hit = {
            "title": "Methanotrophic bacteria and their ecology in soils",
            "doi": "10.1234/dup.7",
            "year": 1996,
        }
        out = repair_junk_title_papers(
            store,
            dry_run=False,
            crossref_fn=self._no_net()["crossref_fn"],
            openalex_fn=self._no_net()["openalex_fn"],
            s2_fn=lambda title, key: hit,
        )
        assert [(o.ref_id, o.action) for o in out] == [(stub, "failed")]
        assert f"doi-human-suppressed:pa{owner}" in out[0].detail
        assert store.fetch_refs_by_ids([stub])[stub].retired_at is None
        assert store.find_ref_by_identifier("doi", "10.1234/dup.7") == owner

    def test_title_leg_merge_locks_owner_before_stub(self, store: Store) -> None:
        """gr658635/td659670 review: the fold takes its row locks in
        ascending id order (owner, then stub) — ``merge_duplicate``'s
        order. A concurrent tx holding the older owner and then reaching
        for the stub must not deadlock against the repair pass: while the
        pass waits on the owner it holds no lock on the stub."""
        import threading
        import time

        from precis.ingest.paper_hygiene import repair_junk_title_papers

        owner = self._old(
            store,
            slug="hanson96e",
            title="Methanotrophic bacteria and their ecology in soils",
            doi="10.1234/dup.8",
        )
        stub = self._old(store, slug="stub08", title="S401_9d 152..155")
        assert owner < stub
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET year = 1996 WHERE ref_id = ANY(%s)", ([owner, stub],)
            )
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, 0, 'paragraph', %s)",
                (stub, "Methanotrophic bacteria and their ecology in soils\n\nBody."),
            )
        hit = {
            "title": "Methanotrophic bacteria and their ecology in soils",
            "doi": "10.1234/dup.8",
            "year": 1996,
        }
        out: list[Any] = []
        errors: list[BaseException] = []

        def repair() -> None:
            try:
                out.extend(
                    repair_junk_title_papers(
                        store,
                        dry_run=False,
                        crossref_fn=self._no_net()["crossref_fn"],
                        openalex_fn=self._no_net()["openalex_fn"],
                        s2_fn=lambda title, key: hit,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        with store.pool.connection() as conn:
            conn.execute(
                "SELECT 1 FROM refs WHERE ref_id = %s FOR NO KEY UPDATE", (owner,)
            )
            worker = threading.Thread(target=repair)
            worker.start()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and worker.is_alive():
                row = conn.execute(
                    "SELECT count(*) FROM pg_locks WHERE NOT granted"
                ).fetchone()
                if row is not None and row[0] > 0:
                    break
                time.sleep(0.05)
            assert worker.is_alive(), "repair pass did not wait on the owner lock"
            # The pass is parked on the owner: the stub must be free.
            conn.execute("SET LOCAL lock_timeout = '5s'")
            conn.execute(
                "SELECT 1 FROM refs WHERE ref_id = %s FOR NO KEY UPDATE", (stub,)
            )
            conn.commit()
        worker.join(30)
        assert not worker.is_alive()
        assert errors == []
        assert [(o.ref_id, o.action, o.detail) for o in out] == [
            (stub, "merged", f"into pa{owner}")
        ]
        assert store.follow_supersede(stub) == owner

    def test_v1_stamped_failure_restamps_before_refiling(self, store: Store) -> None:
        """gr658635: the bump must not strand the owner queue — a
        v1-stamped paper that still can't be repaired is retried once,
        restamped at the current version and only then re-filed."""
        from precis.ingest.paper_hygiene import (
            _TITLE_REPAIR_VERSION,
            repair_junk_title_papers,
        )

        rid = self._old(store, slug="nope98", title="S999_1a 1..3")
        store.stamp_ref_meta(rid, {"title_repair_v": 1})
        # Below the current gate: the raise leg waits for the retry.
        assert raise_junk_title_papers(store, dry_run=True) == []
        out = repair_junk_title_papers(
            store,
            dry_run=False,
            crossref_fn=_no_calling(),
            openalex_fn=_no_calling(),
            s2_fn=lambda *_a, **_k: None,
        )
        assert [o.action for o in out] == ["failed"]
        assert _meta(store, rid).get("title_repair_v") == _TITLE_REPAIR_VERSION
        assert raise_junk_title_papers(store, dry_run=True) == [rid]

    def test_openalex_leg_when_crossref_misses(self, store: Store) -> None:
        from precis.ingest.paper_hygiene import repair_junk_title_papers

        rid = self._old(
            store,
            slug="oamiss",
            title="nnano.2006.77 feature.indd",
            doi="10.1234/oa.1",
        )
        out = repair_junk_title_papers(
            store,
            dry_run=False,
            crossref_fn=_fake_crossref(None),
            openalex_fn=lambda doi, e: {
                "title": "OpenAlex Recovered Title",
                "publication_year": 2006,
            },
            s2_fn=self._no_net()["s2_fn"],
        )
        assert [(o.ref_id, o.action) for o in out] == [(rid, "openalex")]
        ref = store.fetch_refs_by_ids([rid])[rid]
        assert ref.title == "OpenAlex Recovered Title"
        assert ref.year == 2006

    def test_title_search_leg_recovers_doi_less(self, store: Store) -> None:
        from precis.ingest.paper_hygiene import repair_junk_title_papers

        rid = self._old(store, slug="filep", title="S401_9d 152..155")
        with store.pool.connection() as conn:
            conn.execute("UPDATE refs SET year = 1999 WHERE ref_id = %s", (rid,))
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, 0, 'paragraph', %s)",
                (rid, "Recovered Page One Title\n\nJ. Doe\n\nBody text."),
            )
        out = repair_junk_title_papers(
            store,
            dry_run=False,
            crossref_fn=self._no_net()["crossref_fn"],
            openalex_fn=self._no_net()["openalex_fn"],
            s2_fn=lambda title, key: {
                "title": "Recovered Page One Title",
                "doi": "10.1234/found",
                "year": 1999,
            },
        )
        assert [(o.ref_id, o.action) for o in out] == [(rid, "title")]
        ref = store.fetch_refs_by_ids([rid])[rid]
        assert ref.title == "Recovered Page One Title"

    def test_failed_repair_stamps_and_skips_next_pass(self, store: Store) -> None:
        from precis.ingest.paper_hygiene import (
            _TITLE_REPAIR_VERSION,
            repair_junk_title_papers,
        )

        rid = self._old(store, slug="nope99", title="S999_1a 1..3")
        out = repair_junk_title_papers(
            store,
            dry_run=False,
            crossref_fn=_no_calling(),
            openalex_fn=_no_calling(),
            s2_fn=lambda *_a, **_k: None,
        )
        assert [o.action for o in out] == ["failed"]
        assert _meta(store, rid).get("title_repair_v") == _TITLE_REPAIR_VERSION
        # Second pass: stamped → not re-attempted.
        assert repair_junk_title_papers(store, dry_run=False) == []
        # The stamp is exactly what hands the paper to the owner queue
        # (gr508499) — unstamped candidates file nothing.
        assert raise_junk_title_papers(store, dry_run=True) == [rid]

    def test_repair_dry_run_writes_nothing(self, store: Store) -> None:
        from precis.ingest.paper_hygiene import repair_junk_title_papers

        rid = self._old(
            store,
            slug="drymk",
            title="Microsoft Word - whatever.doc",
            doi="10.1234/real.1",
        )
        out = repair_junk_title_papers(
            store,
            dry_run=True,
            crossref_fn=_fake_crossref(
                {"title": "The Real Registry Title", "doi": "10.1234/real.1"}
            ),
            openalex_fn=self._no_net()["openalex_fn"],
            s2_fn=self._no_net()["s2_fn"],
        )
        assert [o.action for o in out] == ["crossref"]
        assert (
            store.fetch_refs_by_ids([rid])[rid].title == "Microsoft Word - whatever.doc"
        )

    def test_one_todo_per_paper_not_per_cite_key(self, store: Store) -> None:
        from precis.ingest.paper_hygiene import _TITLE_REPAIR_VERSION

        rid = self._old(
            store,
            slug="tri24",
            title="__",
            cite_keys=["ma09", "ma2009stone", "jiema2009stone"],
        )
        store.stamp_ref_meta(rid, {"title_repair_v": _TITLE_REPAIR_VERSION})
        assert raise_junk_title_papers(store, dry_run=False) == [rid]
        assert len(self._todos(store, rid)) == 1

    def test_parked_outcome_stamps_for_the_owner_queue(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """gr508499: parked is terminal at this cascade version — it must
        stamp like a failed leg, else the same paper re-burns a registry
        lookup every pass and never reaches the owner queue."""
        from precis.ingest import metadata_resolve
        from precis.ingest.metadata_resolve import Resolution
        from precis.ingest.paper_hygiene import (
            _TITLE_REPAIR_VERSION,
            repair_junk_title_papers,
        )

        rid = self._old(
            store,
            slug="park24",
            title="Microsoft Word - mismatch.doc",
            doi="10.1234/parked.1",
        )
        monkeypatch.setattr(
            metadata_resolve,
            "_resolve_by_doi",
            lambda *a, **k: Resolution(rid, "parked", "doi", "edition-mismatch-year"),
        )
        out = repair_junk_title_papers(store, dry_run=False, **self._no_net())
        assert [o.action for o in out] == ["parked"]
        assert _meta(store, rid).get("title_repair_v") == _TITLE_REPAIR_VERSION
        assert raise_junk_title_papers(store, dry_run=True) == [rid]

    def test_repair_summary_logs_even_with_no_candidates(
        self, store: Store, caplog: Any
    ) -> None:
        """gr508499: a zero-attempt pass still logs — silence reads as
        "the leg never ran", which is how this went undiagnosed."""
        import logging

        from precis.ingest.paper_hygiene import repair_junk_title_papers

        self._old(store, slug="ok24x", title="A Perfectly Fine Real Title")
        with caplog.at_level(logging.INFO, logger="precis.ingest.paper_hygiene"):
            assert repair_junk_title_papers(store, dry_run=False) == []
        assert any("junk-title" in r.message for r in caplog.records)


def _no_calling() -> Any:
    def refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("resolver should not be called")

    return refuse


def _fake_crossref(result: dict[str, Any] | None) -> Any:
    def fn(doi: str, mailto: str) -> dict[str, Any] | None:
        return result

    return fn


# ── body-split routing: OCR-concatenated multi-article bodies (gr658650) ──

_TREMATODE_KWS = ["potamopyrgus", "trematode", "microphallus", "snail", "infection"]
_GECKO_KWS = ["gecko", "seta", "adhesive", "foot-hair", "van der waals"]


def _kw_body(
    store: Store, ref_id: int, specs: list[list[str] | None], *, kind: str = "paragraph"
) -> None:
    """Body chunks carrying KeyBERT keyword labels, in ``ord`` order."""
    with store.pool.connection() as conn:
        with conn.transaction():
            for i, kws in enumerate(specs):
                conn.execute(
                    "INSERT INTO chunks (ref_id, ord, chunk_kind, text, keywords) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (ref_id, i, kind, f"body text {i}", kws),
                )


def _concat_specs(left: int = 7, right: int = 30) -> list[list[str] | None]:
    out: list[list[str] | None] = [_TREMATODE_KWS for _ in range(left)]
    out.extend(_GECKO_KWS for _ in range(right))
    return out


class TestClassifyBodySplit:
    def test_two_article_concatenation_flags(self) -> None:
        from precis.ingest.paper_hygiene import classify_body_split

        v = classify_body_split(list(enumerate(_concat_specs())))
        assert v is not None
        assert v.boundary_ord == 7
        assert (v.left_chunks, v.right_chunks) == (7, 30)
        assert v.jaccard == 0.0

    def test_single_paper_sections_do_not_flag(self) -> None:
        from precis.ingest.paper_hygiene import classify_body_split

        base = {"graphene", "transistor", "mobility", "carrier"}
        kws = [sorted(base | {f"section-{i // 10}-term"}) for i in range(40)]
        assert classify_body_split(list(enumerate(kws))) is None

    def test_early_chunks_count_toward_left_union(self) -> None:
        from precis.ingest.paper_hygiene import classify_body_split

        # {a} also appears in chunks 0-1, so it is shared across the
        # ord-3 boundary: no split despite chunks 3.. carrying only {a}.
        kws = [["a"], ["a"], ["b"]] + [["a"] for _ in range(9)]
        assert classify_body_split(list(enumerate(kws))) is None

    def test_short_body_is_unjudgeable(self) -> None:
        from precis.ingest.paper_hygiene import classify_body_split

        assert classify_body_split(list(enumerate(_concat_specs(3, 5)))) is None

    def test_sparse_keyword_coverage_is_unjudgeable(self) -> None:
        from precis.ingest.paper_hygiene import classify_body_split

        specs: list[list[str] | None] = _concat_specs()
        # Drop keywords from most chunks: below the 0.5 coverage floor.
        for i in range(len(specs)):
            if i % 4:
                specs[i] = None
        assert classify_body_split(list(enumerate(specs))) is None

    def test_keywordless_side_is_unjudgeable(self) -> None:
        from precis.ingest.paper_hygiene import classify_body_split

        specs: list[list[str] | None] = _concat_specs()
        # The 7-chunk tail carries no labels at all: the boundary's left
        # side has too few keyword-carrying chunks to judge.
        for i in range(7):
            specs[i] = None
        assert classify_body_split(list(enumerate(specs))) is None

    def test_tiny_unrelated_run_does_not_flag(self) -> None:
        from precis.ingest.paper_hygiene import classify_body_split

        base = ["graphene", "transistor", "mobility", "carrier"]
        specs: list[list[str]] = [base for _ in range(20)]
        # A single chunk of unrelated keywords mid-paper: every boundary
        # still has the paper's core terms overlapping on both sides, so
        # no near-zero boundary exists.
        specs[10] = _GECKO_KWS
        assert classify_body_split(list(enumerate(specs))) is None


class TestBodySplitRouting:
    """gr658650: OCR-concatenated refs divert to the split route, not
    title-fix."""

    def _concat(self, store: Store, *, slug: str, title: str = "__") -> int:
        rid = store.insert_ref(kind="paper", slug=slug, title=title).id
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET created_at = now() - interval '30 days' "
                "WHERE ref_id = %s",
                (rid,),
            )
        _kw_body(store, rid, _concat_specs())
        return rid

    def _stamp(self, store: Store, paper_id: int) -> None:
        from precis.ingest.paper_hygiene import _TITLE_REPAIR_VERSION

        store.stamp_ref_meta(paper_id, {"title_repair_v": _TITLE_REPAIR_VERSION})

    def _todos(self, store: Store, paper_id: int) -> list[Any]:
        with store.pool.connection() as conn:
            return conn.execute(
                "SELECT ref_id, title, meta->>'source' AS source FROM refs "
                "WHERE kind='todo' AND meta->>'paper_ref_id' = %s",
                (str(paper_id),),
            ).fetchall()

    def test_repair_diverts_before_any_registry_lookup(self, store: Store) -> None:
        from precis.ingest.paper_hygiene import (
            _TITLE_REPAIR_VERSION,
            repair_junk_title_papers,
        )

        rid = self._concat(store, slug="concat-repair")
        out = repair_junk_title_papers(
            store,
            dry_run=False,
            crossref_fn=_no_calling(),
            openalex_fn=_no_calling(),
            s2_fn=_no_calling(),
        )
        # The network fns would have raised had the cascade reached them.
        assert [(o.ref_id, o.action) for o in out] == [(rid, "split")]
        assert "ord 7" in out[0].detail
        # Terminal like any failed leg: the raise leg may file on it.
        assert _meta(store, rid).get("title_repair_v") == _TITLE_REPAIR_VERSION

    def test_repair_diversion_dry_run_writes_nothing(self, store: Store) -> None:
        from precis.ingest.paper_hygiene import repair_junk_title_papers

        rid = self._concat(store, slug="concat-dry")
        out = repair_junk_title_papers(store, dry_run=True)
        assert [(o.ref_id, o.action) for o in out] == [(rid, "split")]
        assert "title_repair_v" not in _meta(store, rid)

    def test_raise_files_body_split_todo_not_title_fix(self, store: Store) -> None:
        from precis.ingest.paper_hygiene import (
            BODY_SPLIT_TAG,
            SPLIT_FIX_SOURCE,
            SPLIT_FIX_TAG,
        )

        rid = self._concat(store, slug="concat-raise")
        self._stamp(store, rid)
        assert raise_junk_title_papers(store, dry_run=True) == [rid]
        assert raise_junk_title_papers(store, dry_run=False) == [rid]

        (todo,) = self._todos(store, rid)
        assert todo[2] == SPLIT_FIX_SOURCE
        assert "Split body" in todo[1]
        # The instruction must not prescribe a whole-body op for a
        # partition job: split_body/move_body_to move the ENTIRE body,
        # so the todo names the missing partition op instead.
        assert "do NOT run split_body" in todo[1]
        assert "partition" in todo[1]
        rendered = {str(t) for t in store.tags_for(todo[0])}
        assert SPLIT_FIX_TAG in rendered, rendered
        assert "waiting-for:reto" in rendered, rendered
        assert not any("paper-title-fix" in r for r in rendered), rendered
        # The paper itself is flagged so operators / views see the class.
        assert BODY_SPLIT_TAG in _tags(store, rid)

    def test_raise_split_route_is_idempotent(self, store: Store) -> None:
        rid = self._concat(store, slug="concat-idem")
        self._stamp(store, rid)
        raise_junk_title_papers(store, dry_run=False)
        assert raise_junk_title_papers(store, dry_run=False) == []
        assert len(self._todos(store, rid)) == 1

    def test_plain_junk_paper_still_gets_title_fix(self, store: Store) -> None:
        """A keyword-barren junk title (the common backlog shape) keeps the
        normal title-fix route — the divergence check must not starve it."""
        rid = store.insert_ref(kind="paper", slug="plain-junk", title="__").id
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET created_at = now() - interval '30 days' "
                "WHERE ref_id = %s",
                (rid,),
            )
        self._stamp(store, rid)
        assert raise_junk_title_papers(store, dry_run=False) == [rid]
        (todo,) = self._todos(store, rid)
        assert todo[2] == "paper_hygiene:title_fix"
        rendered = {str(t) for t in store.tags_for(todo[0])}
        assert "paper-title-fix" in rendered, rendered

    def test_existing_title_fix_todo_is_rerouted_to_split(self, store: Store) -> None:
        """Backlog case: a title-fix todo filed before the split signal
        existed must not block the split route (it is closed, a split todo
        is filed)."""
        from precis.ingest.paper_hygiene import BODY_SPLIT_TAG, SPLIT_FIX_SOURCE
        from precis.store.types import Tag

        rid = self._concat(store, slug="concat-backlog")
        self._stamp(store, rid)
        old = store.insert_ref(
            kind="todo",
            slug=None,
            title="Fix title",
            meta={"source": "paper_hygiene:title_fix", "paper_ref_id": rid},
        )
        store.add_tag(old.id, Tag.closed("STATUS", "open"), set_by="system")

        assert raise_junk_title_papers(store, dry_run=False) == [rid]

        by_source = {t[2]: t[0] for t in self._todos(store, rid)}
        assert set(by_source) == {"paper_hygiene:title_fix", SPLIT_FIX_SOURCE}
        old_tags = {str(t) for t in store.tags_for(old.id)}
        assert any("done" in t for t in old_tags), old_tags
        assert BODY_SPLIT_TAG in _tags(store, rid)
        # Settled: nothing further to file.
        assert raise_junk_title_papers(store, dry_run=False) == []

    def test_reroute_leaves_wontfix_title_fix_todo_untouched(
        self, store: Store
    ) -> None:
        """A title-fix todo closed non-done (the human's won't-do / wontfix
        verdict) must survive the body-split reroute untouched — only
        OPEN todos are superseded; rewriting the verdict to done would
        falsify it."""
        from precis.ingest.paper_hygiene import BODY_SPLIT_TAG, SPLIT_FIX_SOURCE
        from precis.store.types import Tag

        rid = self._concat(store, slug="concat-wontfix")
        self._stamp(store, rid)
        old = store.insert_ref(
            kind="todo",
            slug=None,
            title="Fix title (declined)",
            meta={"source": "paper_hygiene:title_fix", "paper_ref_id": rid},
        )
        store.add_tag(old.id, Tag.closed("STATUS", "won't-do"), set_by="system")

        assert raise_junk_title_papers(store, dry_run=False) == [rid]

        by_source = {t[2]: t[0] for t in self._todos(store, rid)}
        assert set(by_source) == {"paper_hygiene:title_fix", SPLIT_FIX_SOURCE}
        # The split todo was still filed...
        assert by_source[SPLIT_FIX_SOURCE] != old.id
        # ...but the won't-do verdict stands: no done tag, verdict intact.
        old_tags = {str(t) for t in store.tags_for(old.id)}
        assert any("won't-do" in t for t in old_tags), old_tags
        assert not any("done" in t for t in old_tags), old_tags
        assert BODY_SPLIT_TAG in _tags(store, rid)

    def test_open_title_fix_todo_on_plain_junk_is_not_refiled(
        self, store: Store
    ) -> None:
        rid = store.insert_ref(kind="paper", slug="plain-open", title="__").id
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET created_at = now() - interval '30 days' "
                "WHERE ref_id = %s",
                (rid,),
            )
        self._stamp(store, rid)
        assert raise_junk_title_papers(store, dry_run=False) == [rid]
        assert raise_junk_title_papers(store, dry_run=False) == []
        assert len(self._todos(store, rid)) == 1


# ── spliced journal issues (gr658690) ─────────────────────────────


def _spliced_body(store: Store, ref_id: int) -> None:
    """Three concatenated letters: acks ×3 + a mid-stream title+byline
    restart — two splice signals on stored body chunks."""
    chunks = [
        (
            0,
            "Magnetic-field pumping through a Schottky diode\nM. Buess & G. Aeppli",
            "paragraph",
        ),
        (1, "The diode pumps a magnetic field through the junction. " * 4, "paragraph"),
        (2, "Acknowledgements. We thank the Swiss NSF for support.", "paragraph"),
        (3, "- 1. Buess, M. et al. Nature 413, 822 (2001).", "references"),
        (
            4,
            "Imaging the surface reconstruction of alpha-alumina\nJ. V. Barth & M. Reichling",
            "paragraph",
        ),
        (5, "The alumina surface reconstructs under the probe tip. " * 4, "paragraph"),
        (6, "Acknowledgements. We thank the DFG for funding.", "paragraph"),
        (7, "- 1. Barth, J. V. et al. Nature 414, 1 (2001).", "references"),
        (
            8,
            "Adaptive quantum control of a photochemical reaction\nT. Brixner & G. Gerber",
            "paragraph",
        ),
        (9, "Adaptive femtosecond pulse shaping controls the yield. " * 4, "paragraph"),
        (10, "Acknowledgements. We thank the Hamburg cluster.", "paragraph"),
        (11, "- 1. Brixner, T. et al. Nature 414, 57 (2001).", "references"),
    ]
    for ord_, text, kind in chunks:
        _kind_chunk(store, ref_id, ord_, text, kind)


def _mild_body(store: Store, ref_id: int, *, doi: str = "") -> None:
    """The gr658690 mild variant (pa478920): a stray foreign
    references/acknowledgement tail ahead of the record's one dominant
    article — the dominant identity is clear, so a normal title fix
    resolves it; it is the explicitly excluded case and must NOT flag.
    ``doi`` is the dominant article's DOI when the record carries one."""
    chunks = [
        (0, "- 9. Foreign, A. Nature 399, 1 (1999).", "references"),
        (1, "Acknowledgements. We thank the foreign funder.", "paragraph"),
        (2, "- 10. Tail, B. Science 401, 2 (2000).", "references"),
        (
            3,
            "Adaptive routing in mesh sensor networks\nJ. Doe & R. Roe"
            + (f"\ndoi:{doi}" if doi else ""),
            "paragraph",
        ),
        *[
            (i, f"Routing metrics hold across topologies {i}. " * 4, "paragraph")
            for i in range(4, 11)
        ],
        (11, "Acknowledgements. We thank the reviewers.", "paragraph"),
        (12, "- 1. Doe, J. et al. Nature 400, 5 (1999).", "references"),
    ]
    for ord_, text, kind in chunks:
        _kind_chunk(store, ref_id, ord_, text, kind)


class TestFlagSplicedIssuePapers:
    def _junk_old(self, store: Store, *, slug: str, title: str = "__") -> int:
        rid = _paper(store, slug=slug, title=title)
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET created_at = now() - interval '30 days' "
                "WHERE ref_id = %s",
                (rid,),
            )
        return rid

    def _splice_events(self, store: Store, ref_id: int) -> list[Any]:
        with store.pool.connection() as conn:
            return conn.execute(
                "SELECT payload FROM ref_events WHERE ref_id = %s "
                "AND source = 'ingest:spread' AND event = 'issue_splice'",
                (ref_id,),
            ).fetchall()

    def test_dry_run_writes_nothing(self, store: Store) -> None:
        rid = self._junk_old(store, slug="splicedry")
        _spliced_body(store, rid)
        assert flag_spliced_issue_papers(store, dry_run=True) == [rid]
        assert self._splice_events(store, rid) == []

    def test_apply_journals_event_and_tag(self, store: Store) -> None:
        rid = self._junk_old(store, slug="spliceapp")
        _spliced_body(store, rid)
        assert flag_spliced_issue_papers(store, dry_run=False) == [rid]

        (row,) = self._splice_events(store, rid)
        assert row[0]["ack_ords"] == [2, 6, 10]
        assert row[0]["restart_ord"] == 4
        rendered = {str(t) for t in store.tags_for(rid)}
        assert any("multi-article" in r for r in rendered), rendered

    def test_idempotent_flagged_once(self, store: Store) -> None:
        rid = self._junk_old(store, slug="spliceidem")
        _spliced_body(store, rid)
        assert flag_spliced_issue_papers(store, dry_run=False) == [rid]
        assert flag_spliced_issue_papers(store, dry_run=False) == []
        assert len(self._splice_events(store, rid)) == 1

    def test_real_titled_paper_out_of_scope(self, store: Store) -> None:
        """The backlog sweep only covers junk-titled papers — a spliced
        body behind a real title is left to the ingest path / a human."""
        rid = self._junk_old(store, slug="splicereal", title="A Real Title")
        _spliced_body(store, rid)
        assert flag_spliced_issue_papers(store, dry_run=False) == []

    def test_junk_paper_without_signature_not_flagged(self, store: Store) -> None:
        rid = self._junk_old(store, slug="splicenone")
        for ord_ in range(12):
            _kind_chunk(
                store, rid, ord_, f"Plain body text of one article. {ord_} " * 8
            )
        assert flag_spliced_issue_papers(store, dry_run=False) == []

    def test_long_junk_title_is_flagged(self, store: Store) -> None:
        """Review td659685: filename/export junk runs well past 40 chars
        — the old SQL length bound silently dropped real candidates; the
        Python predicates are the only title gate."""
        rid = self._junk_old(
            store,
            slug="splicelong",
            title="Microsoft Word - nnano_2006_77_feature_final_submission.indd",
        )
        _spliced_body(store, rid)
        assert flag_spliced_issue_papers(store, dry_run=False) == [rid]

    def test_foreign_prefix_paper_not_flagged(self, store: Store) -> None:
        """Review td659685: the mild variant (pa478920) — a stray foreign
        references/acknowledgement tail plus one dominant article — is a
        title fix, not a splice: the derived article boundary suppresses
        the flag even though the unmarked body carries two signals."""
        rid = self._junk_old(store, slug="splicemild")
        store.set_ref_identifier(rid, "doi", "10.1038/323533a0", source="manual")
        _mild_body(store, rid, doi="10.1038/323533a0")
        # The same body unmarked fires (foreign ack + restart + own ack).
        from precis.ingest.article_spread import detect_issue_splice

        assert detect_issue_splice(store.chunks.list_chunks_for_ref(rid)) is not None
        assert flag_spliced_issue_papers(store, dry_run=False) == []

    def test_mild_variant_junk_title_no_doi_not_flagged(self, store: Store) -> None:
        """Review td659685 regression: with a junk title and no stored DOI
        ``leading_foreign_ord`` has no anchor at all — the structural
        boundary (:func:`foreign_tail_ord`) is what suppresses the flag,
        so normal title repair still owns the record."""
        rid = self._junk_old(store, slug="splicemild2")
        _mild_body(store, rid)
        from precis.ingest.article_spread import detect_issue_splice

        assert detect_issue_splice(store.chunks.list_chunks_for_ref(rid)) is not None
        assert flag_spliced_issue_papers(store, dry_run=False) == []
        assert self._splice_events(store, rid) == []


class TestRaiseRoutesSplicedIssues:
    """gr658690: a spliced-issue paper gets a split todo, not a
    title-fix todo — no single title exists to assign."""

    def _old(self, store: Store, *, slug: str, title: str = "__") -> int:
        rid = _paper(store, slug=slug, title=title)
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET created_at = now() - interval '30 days' "
                "WHERE ref_id = %s",
                (rid,),
            )
        return rid

    def _stamp(self, store: Store, ref_id: int) -> None:
        from precis.ingest.paper_hygiene import _TITLE_REPAIR_VERSION

        store.stamp_ref_meta(ref_id, {"title_repair_v": _TITLE_REPAIR_VERSION})

    def _splice_event(self, store: Store, ref_id: int) -> None:
        from precis.ingest.add import ARTICLE_SPREAD_SOURCE
        from precis.ingest.article_spread import ISSUE_SPLICE_EVENT

        store.append_event(
            ref_id,
            source=ARTICLE_SPREAD_SOURCE,
            event=ISSUE_SPLICE_EVENT,
            payload={"ack_ords": [2, 6, 10], "restart_ord": 4},
        )

    def _todos(self, store: Store, paper_id: int) -> list[Any]:
        with store.pool.connection() as conn:
            return conn.execute(
                "SELECT ref_id, title, meta FROM refs WHERE kind='todo' "
                "AND meta->>'paper_ref_id' = %s",
                (str(paper_id),),
            ).fetchall()

    def _todo_status(self, store: Store, todo_id: int) -> str | None:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT tg.value FROM ref_tags rt JOIN tags tg USING (tag_id) "
                "WHERE rt.ref_id = %s AND tg.namespace = 'STATUS'",
                (todo_id,),
            ).fetchone()
        return str(row[0]) if row else None

    def test_files_split_todo_not_title_fix(self, store: Store) -> None:
        from precis.ingest.paper_hygiene import SPLIT_FIX_SOURCE

        rid = self._old(store, slug="split01")
        self._stamp(store, rid)
        self._splice_event(store, rid)

        assert raise_junk_title_papers(store, dry_run=True) == [rid]
        assert raise_junk_title_papers(store, dry_run=False) == [rid]

        (todo,) = self._todos(store, rid)
        rendered = {str(t) for t in store.tags_for(todo[0])}
        assert any("paper-needs-split" in r for r in rendered), rendered
        assert not any("paper-title-fix" in r for r in rendered), rendered
        assert "waiting-for:reto" in rendered, rendered
        assert todo[2]["source"] == SPLIT_FIX_SOURCE
        assert "Split" in todo[1] and "scanned journal issue" in todo[1]

    def test_not_refiled_while_open(self, store: Store) -> None:
        rid = self._old(store, slug="split02")
        self._stamp(store, rid)
        self._splice_event(store, rid)
        raise_junk_title_papers(store, dry_run=False)
        assert raise_junk_title_papers(store, dry_run=False) == []
        assert len(self._todos(store, rid)) == 1

    def test_open_title_fix_todo_is_closed(self, store: Store) -> None:
        """A title-fix todo filed before the splice was detected is
        auto-resolved when the split todo lands — the queue never holds
        both asks at once."""
        from precis.ingest.paper_hygiene import TITLE_FIX_SOURCE

        rid = self._old(store, slug="split03")
        self._stamp(store, rid)
        with store.tx() as conn:
            old_todo = store.insert_ref(
                kind="todo",
                slug=None,
                title="Fix title",
                meta={"source": TITLE_FIX_SOURCE, "paper_ref_id": rid},
                conn=conn,
            )
            store.add_tag(
                old_todo.id,
                Tag.closed("STATUS", "open"),
                set_by="system",
                replace_prefix=True,
                conn=conn,
            )
        self._splice_event(store, rid)

        assert raise_junk_title_papers(store, dry_run=False) == [rid]
        todos = self._todos(store, rid)
        assert len(todos) == 2
        assert self._todo_status(store, old_todo.id) == "done"
        # The new todo is the split ask.
        new_todo = next(t for t in todos if t[0] != old_todo.id)
        rendered = {str(t) for t in store.tags_for(new_todo[0])}
        assert any("paper-needs-split" in r for r in rendered), rendered

    def test_unspliced_paper_still_gets_title_fix(self, store: Store) -> None:
        """No splice event → the title-fix route is unchanged."""
        rid = self._old(store, slug="split04")
        self._stamp(store, rid)
        assert raise_junk_title_papers(store, dry_run=False) == [rid]
        (todo,) = self._todos(store, rid)
        rendered = {str(t) for t in store.tags_for(todo[0])}
        assert any("paper-title-fix" in r for r in rendered), rendered

    def test_split_todo_needs_no_repair_stamp(self, store: Store) -> None:
        """Review td659685: the split route is independent of title
        repair — a confirmed splice files without the terminal
        ``title_repair_v`` stamp (a transient-failure loop could never
        produce one, so the stamp gate must not gate splices)."""
        from precis.ingest.paper_hygiene import SPLIT_FIX_SOURCE

        rid = self._old(store, slug="split05")
        self._splice_event(store, rid)
        assert raise_junk_title_papers(store, dry_run=False) == [rid]
        (todo,) = self._todos(store, rid)
        assert todo[2]["source"] == SPLIT_FIX_SOURCE

    def test_split_todo_despite_doi_placeholder(self, store: Store) -> None:
        """A DOI'd placeholder is normally enrich's business — but a
        confirmed splice still gets the split ask; the DOI can name at
        most one of the spliced articles."""
        from precis.ingest.paper_hygiene import SPLIT_FIX_SOURCE

        rid = self._old(store, slug="split06", title=PLACEHOLDER_TITLE)
        store.set_ref_identifier(rid, "doi", "10.1038/323533a0", source="manual")
        self._splice_event(store, rid)
        assert raise_junk_title_papers(store, dry_run=False) == [rid]
        (todo,) = self._todos(store, rid)
        assert todo[2]["source"] == SPLIT_FIX_SOURCE

    def test_split_todo_despite_repaired_title(self, store: Store) -> None:
        """A splice confirmed before a repair 'fixed' the title still
        files the split ask — the signature is the terminal verdict, not
        the title."""
        from precis.ingest.paper_hygiene import SPLIT_FIX_SOURCE

        rid = self._old(store, slug="split07", title="A plausible real title")
        self._splice_event(store, rid)
        assert raise_junk_title_papers(store, dry_run=False) == [rid]
        (todo,) = self._todos(store, rid)
        assert todo[2]["source"] == SPLIT_FIX_SOURCE

    def test_spliced_paper_skips_repair(self, store: Store) -> None:
        """A flagged splice never enters the title-repair cascade — no
        single title exists to resolve, and a lucky registry hit would
        only disguise the splice."""
        from precis.ingest.paper_hygiene import repair_junk_title_papers

        def refuse(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("network resolver should not be called")

        rid = self._old(store, slug="split08")
        self._splice_event(store, rid)
        assert (
            repair_junk_title_papers(
                store,
                dry_run=False,
                crossref_fn=refuse,
                openalex_fn=refuse,
                s2_fn=refuse,
            )
            == []
        )

    def test_mild_variant_keeps_the_title_fix_route(self, store: Store) -> None:
        """Review td659685 end to end: the excluded mild variant (junk
        title, no DOI, one dominant article behind a stray foreign tail)
        is never flagged, so the normal route still owns it — a stamped
        junk title files a title-fix todo, not a split."""
        rid = self._old(store, slug="split09")
        _mild_body(store, rid)
        assert flag_spliced_issue_papers(store, dry_run=False) == []
        self._stamp(store, rid)
        assert raise_junk_title_papers(store, dry_run=False) == [rid]
        (todo,) = self._todos(store, rid)
        rendered = {str(t) for t in store.tags_for(todo[0])}
        assert any("paper-title-fix" in r for r in rendered), rendered
        assert not any("paper-needs-split" in r for r in rendered), rendered
