"""Deterministic paper-hygiene heals — card drift, superseded chains, links."""

from __future__ import annotations

from typing import Any

from precis.identity import PLACEHOLDER_TITLE
from precis.ingest.paper_hygiene import (
    collapse_superseded_chains,
    heal_drifted_cards,
    is_filename_like_title,
    migrate_dangling_paper_links,
    raise_junk_title_papers,
    requeue_papers_for_enrich,
    requeue_placeholder_title_papers,
)
from precis.store import Store


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


def _bodiless_paper(store: Store, *, slug: str, storage_path: str) -> tuple[int, str]:
    """A live paper that holds a PDF (``pdf_sha256`` set, ``pdfs`` row at
    ``storage_path``) and carries only its ``ord < 0`` card — the bodiless
    shape the heal targets."""
    rid = _paper(store, slug=slug, title=f"Paper {slug}")
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


def _body_count(store: Store, ref_id: int) -> int:
    return store.chunks.count_chunks(ref_id)


def _fake_body(n_chunks: int, *, meta: dict[str, Any] | None = None) -> Any:
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
    )


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

        def never(path: Any, paper_id: str) -> Any:
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

        def never(path: Any, paper_id: str) -> Any:
            raise AssertionError("extractor must not run on a text-less PDF")

        out = heal_bodiless_pdfs(
            store, corpus_dirs=(tmp_path,), dry_run=False, extractor=never
        )
        assert [(o.ref_id, o.outcome) for o in out] == [(rid, "unreadable")]
        assert out[0].reason.startswith("scanned:")

    def test_extracts_readable_pdf_and_journals_chunks(
        self, store: Store, tmp_path: Any
    ) -> None:
        from precis.ingest.paper_hygiene import heal_bodiless_pdfs

        pdf = tmp_path / "ok.pdf"
        _write_pdf(pdf, text="A real body of text on the page.")
        rid, sha = _bodiless_paper(store, slug="bl-ok", storage_path=str(pdf))
        seen: list[tuple[Any, str]] = []

        def fake(path: Any, paper_id: str) -> Any:
            seen.append((path, paper_id))
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
        rid, _ = _bodiless_paper(store, slug="bl-nobody", storage_path=str(pdf))

        def nothing(path: Any, paper_id: str) -> Any:
            return _fake_body(
                0,
                meta={"extract_used_fallback": True, "extract_fallback_reason": "boom"},
            )

        out = heal_bodiless_pdfs(
            store, corpus_dirs=(tmp_path,), dry_run=False, extractor=nothing
        )
        assert [(o.ref_id, o.outcome) for o in out] == [(rid, "unreadable")]
        assert "no body chunks" in out[0].reason and "boom" in out[0].reason
        assert _body_count(store, rid) == 0

    def test_dry_run_probes_but_writes_nothing(
        self, store: Store, tmp_path: Any
    ) -> None:
        from precis.ingest.paper_hygiene import heal_bodiless_pdfs

        pdf = tmp_path / "dry.pdf"
        _write_pdf(pdf, text="Readable text.")
        rid_ok, _ = _bodiless_paper(store, slug="bl-dry-ok", storage_path=str(pdf))
        rid_prev, _ = _bodiless_paper(store, slug="bl-dry-prev", storage_path=str(pdf))
        _fetch_event(
            store, rid_prev, "fetch_ok", hours_ago=2, source="fetcher:elsevier"
        )

        def never(path: Any, paper_id: str) -> Any:
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
        rid_a, _ = _bodiless_paper(store, slug="bl-cap-a", storage_path=str(pdf))
        rid_b, _ = _bodiless_paper(store, slug="bl-cap-b", storage_path=str(pdf))

        out = heal_bodiless_pdfs(
            store,
            corpus_dirs=(tmp_path,),
            dry_run=False,
            extract_limit=1,
            extractor=lambda p, pid: _fake_body(2),
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
            extractor=lambda p, pid: _fake_body(2),
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

    def test_files_one_todo_for_underscore_title(self, store: Store) -> None:
        rid = self._old(store, slug="anon24d", title="__")
        assert raise_junk_title_papers(store, dry_run=False) == [rid]
        (todo,) = self._todos(store, rid)
        assert "anon24d" in todo[1] and "'__'" in todo[1]
        rendered = {str(t) for t in store.tags_for(todo[0])}
        assert any("paper-title-fix" in r for r in rendered), rendered
        assert any("waiting-for:" in r for r in rendered), rendered
        assert any("open" in r for r in rendered), rendered

    def test_not_refiled_while_open(self, store: Store) -> None:
        rid = self._old(store, slug="anon24f", title="-")
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
        raise_junk_title_papers(store, dry_run=False)
        self._close(store, rid, "done")
        assert raise_junk_title_papers(store, dry_run=False) == [rid]
        assert raise_junk_title_papers(store, dry_run=False) == []
        assert len(self._todos(store, rid)) == 2

    def test_not_refiled_when_closed_wontfix(self, store: Store) -> None:
        rid = self._old(store, slug="anon24i", title="__")
        raise_junk_title_papers(store, dry_run=False)
        self._close(store, rid, "wontfix")
        assert raise_junk_title_papers(store, dry_run=False) == []
        assert len(self._todos(store, rid)) == 1

    def test_dry_run_writes_nothing(self, store: Store) -> None:
        rid = self._old(store, slug="anon24g", title="...")
        assert raise_junk_title_papers(store, dry_run=True) == [rid]
        assert self._todos(store, rid) == []

    def test_skips_fixable_and_young_papers(self, store: Store) -> None:
        self._old(store, slug="good24", title="A Real Title")
        self._old(store, slug="stub24", title=PLACEHOLDER_TITLE, doi="10.1234/z")
        store.insert_ref(kind="paper", slug="young24", title="__")
        assert raise_junk_title_papers(store, dry_run=True) == []
