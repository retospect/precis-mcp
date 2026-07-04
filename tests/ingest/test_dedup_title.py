"""Title-similarity dedup — mint-time guard + the Phase 3 reconcile.

Covers the id-less-title-only-stub-duplicating-a-held-paper class:

* ``upsert_stub_paper`` fuzzy-matches a title-only acquire against held
  papers, so re-acquiring a held paper by title alone is idempotent.
* ``reconcile_by_title_similarity`` folds an *existing* leaked stub into
  the held paper (high-confidence band only), and leaves ambiguous
  matches for review.
"""

from __future__ import annotations

from typing import Any

from precis.ingest.dedup import TitleMatchReview, reconcile_by_title_similarity
from precis.store import Store


def _held(
    store: Store,
    *,
    n: int,
    slug: str,
    title: str,
    authors: tuple[str, ...] = ("Some Author",),
    year: int | None = 2020,
) -> int:
    """Insert a *held* paper (a ref with a real pdf_sha256 + pdfs row)."""
    ref = store.insert_ref(
        kind="paper",
        slug=slug,
        title=title,
        authors=[{"name": a} for a in authors],
        year=year,
    )
    sha = f"{n:064x}"
    with store.pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
                "size_bytes, storage_path) VALUES (%s, %s, 1, 100, '') "
                "ON CONFLICT (pdf_sha256) DO NOTHING",
                (sha, sha),
            )
            conn.execute(
                "UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s", (sha, ref.id)
            )
            # A truly-held survivor has ingested body chunks, not just the
            # pdf_sha256 flag — the dedup guards require this.
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, 0, 'paragraph', %s)",
                (ref.id, f"body text of {title}"),
            )
    return ref.id


def _ref_state(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT deleted_at IS NOT NULL, meta->>'superseded_by' "
            "FROM refs WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()
    assert row is not None
    return {"deleted": bool(row[0]), "superseded_by": row[1]}


# ── mint-time fuzzy guard ─────────────────────────────────────────


def test_title_only_acquire_of_held_paper_is_idempotent(store: Store) -> None:
    held = _held(
        store,
        n=1,
        slug="vaswani17",
        title="Attention Is All You Need",
        authors=("Ashish Vaswani",),
        year=2017,
    )
    ref_id, created = store.upsert_stub_paper(
        identifiers=[], title="Attention Is All You Need", year=2017
    )
    assert created is False
    assert ref_id == held


def test_title_only_acquire_of_new_paper_still_mints(store: Store) -> None:
    _held(store, n=2, slug="vaswani17", title="Attention Is All You Need", year=2017)
    ref_id, created = store.upsert_stub_paper(
        identifiers=[], title="A Totally Different Paper About Frogs", year=2021
    )
    assert created is True
    assert ref_id  # a fresh stub


def test_title_match_but_year_far_off_mints(store: Store) -> None:
    """Same title, incompatible year → not the same paper; mint a stub."""
    _held(store, n=3, slug="dupe20", title="On the Theory of Widgets", year=2005)
    ref_id, created = store.upsert_stub_paper(
        identifiers=[], title="On the Theory of Widgets", year=2020
    )
    assert created is True


# ── reconcile_by_title_similarity ─────────────────────────────────


def test_reconcile_folds_leaked_stub_into_held(store: Store) -> None:
    held = _held(
        store,
        n=4,
        slug="vaswani17",
        title="Attention Is All You Need",
        authors=("Ashish Vaswani",),
        year=2017,
    )
    # A pre-existing leaked stub (predates the mint guard): id-less,
    # title-only, no PDF — insert directly to bypass the guard.
    stub = store.insert_ref(
        kind="paper", slug="attention17", title="Attention Is All You Need", year=2017
    ).id

    dry = reconcile_by_title_similarity(store, dry_run=True)
    assert any(o.survivor_ref_id == held and stub in o.duplicate_ref_ids for o in dry)
    assert _ref_state(store, stub)["deleted"] is False  # dry-run wrote nothing

    applied = reconcile_by_title_similarity(store, dry_run=False)
    assert any(
        o.survivor_ref_id == held and stub in o.duplicate_ref_ids for o in applied
    )
    st = _ref_state(store, stub)
    assert st["deleted"] is True
    assert st["superseded_by"] == str(held)
    assert _ref_state(store, held)["deleted"] is False


def test_reconcile_skips_stub_with_external_id(store: Store) -> None:
    """A stub carrying a DOI is out of scope — the id paths handle it."""
    _held(store, n=5, slug="held5", title="Graphene Synthesis Routes", year=2019)
    stub, _ = store.upsert_stub_paper(
        identifiers=[("doi", "10.1/xyz")],
        title="Graphene Synthesis Routes",
        year=2019,
    )
    applied = reconcile_by_title_similarity(store, dry_run=False)
    assert all(stub not in o.duplicate_ref_ids for o in applied)
    assert _ref_state(store, stub)["deleted"] is False


def test_no_merge_into_held_flag_without_chunks(store: Store) -> None:
    """A ref with a pdf_sha256 but NO body chunks is not a *demonstrable*
    copy, so the truth guard declines to merge into it.

    Ingest writes pdfs → refs.pdf_sha256 → chunks in one atomic tx
    (``write_paper``), so a real, freshly-ingested paper never shows this
    state to another transaction — chunks don't lag the held-flag (only
    embeddings do, per ADR 0007, which the guard doesn't require). A
    sha-without-chunks ref is therefore a bad/partial import (e.g. a PDF
    Marker couldn't parse), and folding a stub into it would assert we
    have content we don't. Declining is the truthful choice; if that ref
    later gains chunks, the next reconcile pass will merge then."""
    ref = store.insert_ref(
        kind="paper", slug="flag20", title="Ghost Paper Title", year=2020
    )
    with store.pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
                "size_bytes, storage_path) VALUES (%s, %s, 1, 100, '') "
                "ON CONFLICT (pdf_sha256) DO NOTHING",
                (f"{99:064x}", f"{99:064x}"),
            )
            conn.execute(
                "UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s",
                (f"{99:064x}", ref.id),
            )
            # deliberately NO chunk insert
    stub = store.insert_ref(
        kind="paper", slug="ghost20", title="Ghost Paper Title", year=2020
    ).id
    applied = reconcile_by_title_similarity(store, dry_run=False)
    assert all(stub not in o.duplicate_ref_ids for o in applied)
    assert _ref_state(store, stub)["deleted"] is False

    # And the mint-time guard likewise won't collapse onto the ghost.
    rid, created = store.upsert_stub_paper(
        identifiers=[], title="Ghost Paper Title", year=2020
    )
    assert created is True
    assert rid != ref.id


def test_reconcile_does_not_auto_merge_a_weak_match(store: Store) -> None:
    """A stub whose title only loosely resembles a held paper is never
    auto-merged (it may be surfaced for review, but not retired)."""
    _held(
        store,
        n=6,
        slug="held6",
        title="Deep Residual Learning for Image Recognition",
        year=2016,
    )
    stub = store.insert_ref(
        kind="paper",
        slug="other16",
        title="Recurrent Models of Visual Attention",
        year=2016,
    ).id
    review: list[TitleMatchReview] = []
    applied = reconcile_by_title_similarity(store, dry_run=False, review_out=review)
    assert all(stub not in o.duplicate_ref_ids for o in applied)
    assert _ref_state(store, stub)["deleted"] is False
