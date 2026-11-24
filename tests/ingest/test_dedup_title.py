"""Title-similarity dedup — mint-time guard + the Phase 3 reconcile.

Covers the id-less-title-only-stub-duplicating-a-held-paper class:

* ``upsert_stub_paper`` fuzzy-matches a title-only acquire against held
  papers, so re-acquiring a held paper by title alone is idempotent.
* ``reconcile_by_title_similarity`` folds an *existing* leaked stub into
  the held paper (high-confidence band only), and leaves ambiguous
  matches for review.

Plus the id-bearing twin (gr784146): ``fold_stub_into_held_twin`` folds
an acquire-resolved stub that carries an arXiv/DOI id into the held
paper that never recorded it.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers import paper as paper_mod
from precis.handlers.paper import PaperHandler
from precis.ingest.dedup import (
    TitleMatchReview,
    fold_stub_into_held_twin,
    reconcile_by_title_similarity,
)
from precis.quest.search import apply_fetches
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
            "SELECT retired_at IS NOT NULL, meta->>'superseded_by' "
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
        title="Attention Mechanisms Replace Recurrence Entirely Transformer Architecture",
        authors=("Ashish Vaswani",),
        year=2017,
    )
    ref_id, created = store.upsert_stub_paper(
        identifiers=[],
        title="Attention Mechanisms Replace Recurrence Entirely Transformer Architecture",
        year=2017,
    )
    assert created is False
    assert ref_id == held


def test_title_match_with_conflicting_supplied_author_mints(store: Store) -> None:
    """A supplied byline must prevent merging unrelated same-title works."""
    title = "Advanced Graph Algorithms for Distributed Network Analysis"
    held = _held(
        store,
        n=1,
        slug="smith24",
        title=title,
        authors=("John Smith",),
        year=2024,
    )
    ref_id, created = store.upsert_stub_paper(
        identifiers=[], title=title, year=2024, authors=["Doe, Jane"]
    )
    assert created is True
    assert ref_id != held


def test_title_match_with_matching_supplied_author_merges(store: Store) -> None:
    """Matching supplied and held surnames still allow title deduplication."""
    title = "Advanced Graph Algorithms for Distributed Network Analysis"
    held = _held(
        store,
        n=1,
        slug="smith24",
        title=title,
        authors=("John Smith",),
        year=2024,
    )
    ref_id, created = store.upsert_stub_paper(
        identifiers=[], title=title, year=2024, authors=["Smith, John"]
    )
    assert created is False
    assert ref_id == held


def test_title_only_acquire_of_new_paper_still_mints(store: Store) -> None:
    _held(
        store,
        n=2,
        slug="vaswani17",
        title="Attention Mechanisms Replace Recurrence Entirely Transformer Architecture",
        year=2017,
    )
    ref_id, created = store.upsert_stub_paper(
        identifiers=[], title="A Totally Different Paper About Frogs", year=2021
    )
    assert created is True
    assert ref_id  # a fresh stub


def test_title_match_but_year_far_off_mints(store: Store) -> None:
    """Same title, incompatible year → not the same paper; mint a stub."""
    _held(
        store,
        n=3,
        slug="dupe20",
        title="Comprehensive Theory Concerning Widgets Manufacturing Reliability Models",
        year=2005,
    )
    ref_id, created = store.upsert_stub_paper(
        identifiers=[],
        title="Comprehensive Theory Concerning Widgets Manufacturing Reliability Models",
        year=2020,
    )
    assert created is True


# ── reconcile_by_title_similarity ─────────────────────────────────


def test_reconcile_folds_leaked_stub_into_held(store: Store) -> None:
    held = _held(
        store,
        n=4,
        slug="vaswani17",
        title="Attention Mechanisms Replace Recurrence Entirely Transformer Architecture",
        authors=("Ashish Vaswani",),
        year=2017,
    )
    # A pre-existing leaked stub (predates the mint guard): id-less,
    # title-only, no PDF — insert directly to bypass the guard.
    stub = store.insert_ref(
        kind="paper",
        slug="attention17",
        title="Attention Mechanisms Replace Recurrence Entirely Transformer Architecture",
        year=2017,
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
    _held(
        store,
        n=5,
        slug="held5",
        title="Graphene Synthesis Routes Using Chemical Vapor Deposition Methods",
        year=2019,
    )
    stub, _ = store.upsert_stub_paper(
        identifiers=[("doi", "10.1/xyz")],
        title="Graphene Synthesis Routes Using Chemical Vapor Deposition Methods",
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
    embeddings do, which the guard doesn't require). A
    sha-without-chunks ref is therefore a bad/partial import (e.g. a PDF
    Marker couldn't parse), and folding a stub into it would assert we
    have content we don't. Declining is the truthful choice; if that ref
    later gains chunks, the next reconcile pass will merge then."""
    ref = store.insert_ref(
        kind="paper",
        slug="flag20",
        title="Ghost Paper Concerning Phantom Measurement Artifacts Thoroughly",
        year=2020,
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
        kind="paper",
        slug="ghost20",
        title="Ghost Paper Concerning Phantom Measurement Artifacts Thoroughly",
        year=2020,
    ).id
    applied = reconcile_by_title_similarity(store, dry_run=False)
    assert all(stub not in o.duplicate_ref_ids for o in applied)
    assert _ref_state(store, stub)["deleted"] is False

    # And the mint-time guard likewise won't collapse onto the ghost.
    rid, created = store.upsert_stub_paper(
        identifiers=[],
        title="Ghost Paper Concerning Phantom Measurement Artifacts Thoroughly",
        year=2020,
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


# ── corroboration gate (gr264748) ─────────────────────────────────


def _reviews_for(store: Store) -> tuple[list, list[TitleMatchReview]]:
    review: list[TitleMatchReview] = []
    applied = reconcile_by_title_similarity(store, dry_run=False, review_out=review)
    return applied, review


def test_title_merge_requires_corroboration(store: Store) -> None:
    """Same title but NULL stub year -> review, not merge; a DISTINCT
    short title with the same year -> review as well."""
    held = _held(
        store,
        n=20,
        slug="held20",
        title="Defects in Carbon Nanotubes",
        year=2002,
    )
    stub = store.insert_ref(
        kind="paper", slug="defects", title="Defects in Carbon Nanotubes", year=None
    ).id
    applied, review = _reviews_for(store)
    assert all(stub not in o.duplicate_ref_ids for o in applied)
    assert _ref_state(store, stub)["deleted"] is False
    assert [(r.stub_ref_id, r.held_ref_id) for r in review] == [(stub, held)]

    # Same year but a distinct 4-token title (< 5) -> still review as
    # short-title (gr515509 waived the token count only for titles
    # identical after normalisation).
    stub2 = store.insert_ref(
        kind="paper",
        slug="defects02",
        title="Defects in Carbon Nanotubes Reviewed",
        year=2002,
    ).id
    applied, review = _reviews_for(store)
    assert all(stub2 not in o.duplicate_ref_ids for o in applied)
    assert _ref_state(store, stub2)["deleted"] is False
    assert any(r.stub_ref_id == stub2 and r.reason == "short-title" for r in review)


def test_reconcile_merges_identical_short_titles(store: Store) -> None:
    """gr515509: an identical normalised title with matching year AND
    first author skips the token-count leg — a true duplicate pair like
    "The rise of graphene" folds."""
    held = _held(
        store,
        n=30,
        slug="held30",
        title="The rise of graphene",
        authors=("K. S. Novoselov",),
        year=2004,
    )
    stub = store.insert_ref(
        kind="paper",
        slug="graphene04",
        title="The rise of graphene",
        authors=[{"name": "Novoselov, K. S."}],
        year=2004,
    ).id
    applied, review = _reviews_for(store)
    assert any(
        o.survivor_ref_id == held and stub in o.duplicate_ref_ids for o in applied
    )
    st = _ref_state(store, stub)
    assert st["deleted"] is True and st["superseded_by"] == str(held)
    assert all(r.stub_ref_id != stub for r in review)


def test_reconcile_identical_short_title_without_authors_reviews(
    store: Store,
) -> None:
    """The waiver needs an author on BOTH sides (gr515509 review) — an
    authorless stub with an identical short title goes to review, never
    merges, however generic the title."""
    held = _held(
        store,
        n=31,
        slug="held31",
        title="Editorial Board",
        authors=("Some Author",),
        year=2004,
    )
    stub = store.insert_ref(
        kind="paper", slug="edboard04", title="Editorial Board", year=2004
    ).id
    applied, review = _reviews_for(store)
    assert all(stub not in o.duplicate_ref_ids for o in applied)
    assert _ref_state(store, stub)["deleted"] is False
    assert any(r.stub_ref_id == stub and r.held_ref_id == held for r in review)


def test_title_merge_refusal_identical_titles_skip_token_count() -> None:
    """gr515509 (+ Opus review): the identical-title waiver needs >=2
    content tokens (stopwords excluded), a known year on both sides and
    matching first authors — and waives ONLY the token leg."""
    from precis.utils.title_merge import _title_identity_key, title_merge_refusal

    # Full waiver: identical, both years, matching authors, >=2 content
    # tokens ("rise" + "graphene"; "the"/"of" don't count).
    assert (
        title_merge_refusal(
            "The rise of graphene",
            2004,
            ["K. S. Novoselov"],
            "The Rise of Graphene",
            2004,
            ["Novoselov, K. S."],
        )
        is None
    )
    # An authorless side never earns the waiver — the mint-guard hole.
    assert (
        title_merge_refusal(
            "The rise of graphene",
            2004,
            None,
            "The rise of graphene",
            2004,
            ["Novoselov, K. S."],
        )
        == "short-title"
    )
    # A missing year never earns the waiver.
    assert (
        title_merge_refusal(
            "The rise of graphene",
            None,
            ["A Smith"],
            "The rise of graphene",
            2004,
            ["A Smith"],
        )
        == "short-title"
    )
    # A one-word title never earns the waiver even with year + author.
    assert (
        title_merge_refusal(
            "Editorial", 2004, ["A Smith"], "Editorial", 2004, ["A Smith"]
        )
        == "short-title"
    )
    # Two raw tokens but only one CONTENT token — a stopword does not
    # count toward the bar (Opus re-review nit).
    for generic in ("The Editorial", "In Memoriam"):
        assert (
            title_merge_refusal(generic, 2004, ["A Smith"], generic, 2004, ["A Smith"])
            == "short-title"
        )
    # Waiver earned, but the year leg still refuses.
    assert (
        title_merge_refusal(
            "The rise of graphene",
            2001,
            ["A Smith"],
            "The rise of graphene",
            2020,
            ["A Smith"],
        )
        == "year-mismatch"
    )
    # Distinct short titles still refuse.
    assert (
        title_merge_refusal("Note", 2020, None, "Editorial", 2020, None)
        == "short-title"
    )
    # Two empty titles are not "identical".
    assert title_merge_refusal(None, 2020, None, None, 2020, None) == "short-title"
    # Greek letters keep their identity — α- and γ-polymorphs and α-/β-
    # synucleins are NOT identical titles (Opus review of gr515509).
    assert _title_identity_key("α-Fe2O3 films") != _title_identity_key("γ-Fe2O3 films")
    assert _title_identity_key("α-synuclein") != _title_identity_key("β-synuclein")
    assert (
        title_merge_refusal(
            "α-Fe2O3 films",
            2020,
            [{"name": "A Smith"}],
            "γ-Fe2O3 films",
            2020,
            [{"name": "A Smith"}],
        )
        == "short-title"
    )


def test_title_merge_refuses_null_year_and_author_mismatch(store: Store) -> None:
    title = "Attention Mechanisms Replace Recurrence Entirely Transformer Architecture"
    held = _held(
        store, n=21, slug="held21", title=title, authors=("Ashish Vaswani",), year=2017
    )
    nullyear = store.insert_ref(kind="paper", slug="ny17", title=title).id
    other = store.insert_ref(
        kind="paper",
        slug="oa17",
        title=title,
        authors=[{"name": "Jane Doe"}],
        year=2017,
    ).id
    applied, review = _reviews_for(store)
    assert all(
        nullyear not in o.duplicate_ref_ids and other not in o.duplicate_ref_ids
        for o in applied
    )
    reasons = {r.stub_ref_id: r.reason for r in review}
    assert reasons[nullyear] == "year-unknown"
    assert reasons[other] == "author-mismatch"
    assert _ref_state(store, held)["deleted"] is False


def test_upsert_stub_paper_short_title_mints(store: Store) -> None:
    """A DISTINCT short title still mints (gr515509 waived the token
    count only for titles identical after normalisation)."""
    held = _held(
        store, n=22, slug="held22", title="Defects in Carbon Nanotubes", year=2002
    )
    for year in (None, 2002):
        ref_id, created = store.upsert_stub_paper(
            identifiers=[], title="Defects in Carbon Nanotubes Reviewed", year=year
        )
        assert created is True
        assert ref_id != held


def test_upsert_stub_paper_identical_short_title_mints(store: Store) -> None:
    """gr515509 review: a title-only acquire carries no authors, so the
    identical-title waiver can never apply in the mint guard — even an
    identical short title mints instead of collapsing onto a held paper.
    ``Editorial`` / ``Reply``-class titles stay acquireable."""
    held = _held(store, n=24, slug="held24", title="The rise of graphene", year=2004)
    held_ed = _held(store, n=25, slug="held25", title="Editorial", year=2004)
    held_board = _held(store, n=26, slug="held26", title="Editorial Board", year=2004)
    for title, held_id in (
        ("The rise of graphene", held),
        ("Editorial", held_ed),
        ("Editorial Board", held_board),
    ):
        for year in (None, 2004):
            ref_id, created = store.upsert_stub_paper(
                identifiers=[], title=title, year=year
            )
            assert created is True
            assert ref_id != held_id


def test_upsert_stub_paper_yearless_long_title_returns_held(store: Store) -> None:
    """A year-less title-only acquire of a held paper stays idempotent."""
    title = "Attention Mechanisms Replace Recurrence Entirely Transformer Architecture"
    held = _held(store, n=23, slug="held23", title=title, year=2017)
    ref_id, created = store.upsert_stub_paper(identifiers=[], title=title, year=None)
    assert created is False
    assert ref_id == held


def test_first_family_normalisation() -> None:
    from precis.utils.title_merge import _first_family

    assert _first_family([{"name": "Anna van der Berg"}]) == _first_family(
        [{"family": "Berg"}]
    )
    assert _first_family([{"family": "Müller"}]) == _first_family(
        [{"name": "Hans Muller"}]
    )
    assert _first_family([{"name": "Smith, J."}]) == "smith"


def test_first_author_agreement() -> None:
    """gr574291: name *format* differences are not author-mismatches, and
    absent/placeholder bylines are *no evidence* (``None``), never a
    refusal."""
    from precis.utils.title_merge import first_author_agreement as agree

    # Same person, different string shapes.
    assert agree([{"name": "chenyu zhang"}], [{"family": "Zhang", "given": "Chen-Yu"}])
    assert agree([{"name": "Zhang Chenyu"}], [{"family": "Zhang"}])  # family-first
    assert agree([{"name": "Zhang, Chen-Yu"}], [{"name": "Chenyu Zhang"}])
    assert agree([{"name": "ZHANG Chen-Yu"}], [{"name": "Chenyu Zhang"}])
    assert agree([{"name": "C. Zhang"}], [{"name": "Chenyu Zhang"}])
    assert agree([{"name": "Z. Chenyu"}], [{"name": "Chenyu Zhang"}])
    assert agree(["Zhang, Chenyu"], [{"name": "Zhang et al"}])
    # Absent / placeholder / collective evidence → unknown, not a verdict.
    assert agree(None, [{"family": "Zhang"}]) is None
    assert agree([], [{"family": "Zhang"}]) is None
    assert agree([{"name": "Anonymous"}], [{"family": "Zhang"}]) is None
    assert agree([{"name": "et al"}], [{"family": "Zhang"}]) is None
    assert agree([{"name": "The QD Consortium"}], [{"family": "Zhang"}]) is None
    # A consortium heading the byline: the first *person* decides.
    assert agree(
        [{"name": "The QD Consortium"}, {"name": "Chenyu Zhang"}],
        [{"family": "Zhang"}],
    )
    # Confidently different people still refuse.
    assert agree([{"name": "Alvarez, Maria"}], [{"name": "Becker, Tom"}]) is False
    assert agree([{"name": "Zhang Chenyu"}], [{"name": "Li Zhang"}]) is False
    assert agree([{"family": "Müller"}], [{"name": "Anna Schmidt"}]) is False
    # A token SUBSET is not agreement (td575118): the extra full token is
    # a different explicit surname, not an initial — both directions.
    assert agree([{"name": "John Smith"}], [{"name": "John Smith Jones"}]) is False
    assert agree([{"name": "John Smith Jones"}], [{"name": "John Smith"}]) is False
    assert (
        agree(
            [{"family": "Smith", "given": "John"}],
            [{"name": "John Smith Jones"}],
        )
        is False
    )


# ── identifier-bearing stub vs held twin (gr784146) ───────────────


@pytest.mark.parametrize(
    ("scheme", "value"), [("arxiv", "2602.23489"), ("pmcid", "pmc1234567")]
)
def test_acquire_stub_folds_into_held_twin_without_year(
    store: Store, monkeypatch: pytest.MonkeyPatch, scheme: str, value: str
) -> None:
    title = "Advanced Graph Algorithms for Distributed Network Analysis"
    held = _held(store, n=3, slug="bochkarev00", title=title, year=None)
    stub, created = store.upsert_stub_paper(
        identifiers=[(scheme, value)], title=title, year=2026
    )
    assert created is True
    monkeypatch.setattr(
        paper_mod, "_lookup_acquire_metadata", lambda *_: {"title": title, "year": 2026}
    )
    result = PaperHandler(hub=Hub(store=store)).acquire(identifier=f"{scheme}:{value}")
    assert result.ref_id == held
    assert _ref_state(store, stub)["superseded_by"] is not None
    again, created = store.upsert_stub_paper(identifiers=[(scheme, value)], title=title)
    assert (again, created) == (held, False)


def test_fold_stub_leaves_unrelated_titles_alone(store: Store) -> None:
    _held(
        store,
        n=4,
        slug="smith24",
        title="Advanced Graph Algorithms for Distributed Network Analysis",
    )
    stub, _ = store.upsert_stub_paper(
        identifiers=[("arxiv", "2602.00001")],
        title="A Totally Different Paper About Frogs Jumping",
        year=2026,
    )
    with store.tx() as conn:
        assert (
            fold_stub_into_held_twin(
                store,
                stub,
                resolved_title="A Totally Different Paper About Frogs Jumping",
                source="test",
                conn=conn,
            )
            is None
        )


@pytest.mark.parametrize("existing_stub", [True, False])
@pytest.mark.parametrize("resolved_title", [None, "A Different Paper About Frogs"])
def test_acquire_title_hint_does_not_establish_identity(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    existing_stub: bool,
    resolved_title: str | None,
) -> None:
    title = "Advanced Graph Algorithms for Distributed Network Analysis"
    arxiv = "2602.23489"
    held = _held(store, n=5, slug="bochkarev00", title=title, year=None)
    if existing_stub:
        store.upsert_stub_paper(identifiers=[("arxiv", arxiv)], title=title, year=2026)
    monkeypatch.setattr(
        paper_mod,
        "_lookup_acquire_metadata",
        lambda *_: {"title": resolved_title, "year": 2026} if resolved_title else None,
    )

    result = PaperHandler(hub=Hub(store=store)).acquire(
        identifier=f"arxiv:{arxiv}", title=title
    )

    assert result.ref_id is not None and result.ref_id != held
    assert store.find_ref_by_identifier("arxiv", arxiv, kind="paper") == result.ref_id
    assert "pinned to front of fetch queue" in result.body


@pytest.mark.parametrize("byline_source", ["supplied", "resolved"])
def test_arxiv_put_keeps_stub_with_conflicting_author(
    store: Store, monkeypatch: pytest.MonkeyPatch, byline_source: str
) -> None:
    title = "Advanced Graph Algorithms for Distributed Network Analysis"
    arxiv = "2602.23489"
    held = _held(store, n=5, slug="bochkarev00", title=title, year=None)
    metadata: dict[str, Any] = {"title": title, "year": 2026, "arxiv_id": arxiv}
    authors = ["Another Researcher"]
    if byline_source == "resolved":
        metadata["authors"] = [{"name": authors[0]}]
    monkeypatch.setattr(paper_mod, "_lookup_acquire_metadata", lambda *_: metadata)

    result = PaperHandler(hub=Hub(store=store)).put(
        args={
            "arxiv": arxiv,
            "authors": authors if byline_source == "supplied" else None,
        }
    )

    assert result.ref_id is not None and result.ref_id != held
    assert store.find_ref_by_identifier("arxiv", arxiv, kind="paper") == result.ref_id
    assert "pinned to front of fetch queue" in result.body


def test_quest_fetch_fold_names_held_paper_and_preserves_cap(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    title = "Advanced Graph Algorithms for Distributed Network Analysis"
    arxiv = "2602.23489"
    held = _held(store, n=6, slug="bochkarev00", title=title, year=None)
    stub, _ = store.upsert_stub_paper(
        identifiers=[("arxiv", arxiv)], title=title, year=2026
    )
    pending, _ = store.upsert_stub_paper(identifiers=[], title="A different work")
    quest_id = store.insert_ref(kind="quest", slug=None, title="Fetch fold").id
    monkeypatch.setattr(paper_mod, "_lookup_doi_registry", lambda _: None)
    monkeypatch.setattr(
        paper_mod,
        "_lookup_acquire_metadata",
        lambda *_: {"title": title, "year": 2026, "arxiv_id": arxiv},
    )

    assert (
        apply_fetches(
            store, quest_id, [{"paper": f"pa{stub}"}, {"paper": f"pa{pending}"}], cap=1
        )
        == 1
    )

    assert _ref_state(store, stub)["deleted"]
    notes = [chunk.text for chunk in store.chunks.list_chunks_for_ref(quest_id)]
    assert any(f"pa{held}: full text already held" in note for note in notes)
    assert [note for note in notes if note.startswith("lit-fetch:")] == [
        f"lit-fetch: requested full text for pa{pending}"
    ]


def test_quest_fetch_refuses_pdf_without_readable_chunks(store: Store) -> None:
    """A PDF-on-file record never enters the fetch queue
    (``pin_stub_for_fetch`` no-ops on it), so a fetch request is refused
    and spends no slot — the repair is ``reextract``."""
    title = "Advanced Graph Algorithms for Distributed Network Analysis"
    arxiv = "2602.23489"
    paper_id = _held(store, n=7, slug="bochkarev00", title=title)
    with store.tx() as conn:
        conn.execute("DELETE FROM chunks WHERE ref_id = %s", (paper_id,))
    store.insert_ref_identifiers(paper_id, [("arxiv", arxiv, "test")])
    quest_id = store.insert_ref(kind="quest", slug=None, title="Fetch PDF").id

    assert apply_fetches(store, quest_id, [{"paper": f"pa{paper_id}"}], cap=1) == 0
    notes = [chunk.text for chunk in store.chunks.list_chunks_for_ref(quest_id)]
    assert any("PDF on file" in note and "reextract" in note for note in notes)
    assert not any(note.startswith("lit-fetch:") for note in notes)


def test_fold_refuses_held_candidate_with_conflicting_identifier(
    store: Store,
) -> None:
    """The held paper claims a DIFFERENT value of an identifier kind the
    stub carries — merging would put both ids on one record."""
    title = "Advanced Graph Algorithms for Distributed Network Analysis"
    held = _held(store, n=8, slug="bochkarev00", title=title, year=2026)
    store.insert_ref_identifiers(held, [("arxiv", "2602.11111", "test")])
    stub, _ = store.upsert_stub_paper(
        identifiers=[("arxiv", "2602.23489")], title=title, year=2026
    )
    with store.tx() as conn:
        assert (
            fold_stub_into_held_twin(
                store, stub, resolved_title=title, source="test", conn=conn
            )
            is None
        )
    assert _ref_state(store, stub)["deleted"] is False


def test_supplied_byline_does_not_mask_resolver_author_conflict(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resolver's byline is the fold's author evidence — a supplied
    hint that matches the held paper can't mask a conflicting resolver
    first author."""
    title = "Advanced Graph Algorithms for Distributed Network Analysis"
    arxiv = "2602.23489"
    held = _held(store, n=9, slug="bochkarev00", title=title, year=None)
    monkeypatch.setattr(
        paper_mod,
        "_lookup_acquire_metadata",
        lambda *_: {
            "title": title,
            "year": 2026,
            "arxiv_id": arxiv,
            "authors": [{"name": "Jane Doe"}],
        },
    )

    result = PaperHandler(hub=Hub(store=store)).put(
        args={"arxiv": arxiv, "authors": ["Some Author"]}
    )

    assert result.ref_id is not None and result.ref_id != held
    assert store.find_ref_by_identifier("arxiv", arxiv, kind="paper") == result.ref_id
    assert "pinned to front of fetch queue" in result.body


def test_resolver_byline_corroborates_despite_conflicting_hint(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The identifier names the resolver's paper, not the hint's: when the
    resolver's first author agrees with the held paper a conflicting
    supplied hint does not block the fold (and is not applied)."""
    title = "Advanced Graph Algorithms for Distributed Network Analysis"
    arxiv = "2602.23489"
    held = _held(store, n=10, slug="bochkarev00", title=title, year=None)
    monkeypatch.setattr(
        paper_mod,
        "_lookup_acquire_metadata",
        lambda *_: {
            "title": title,
            "year": 2026,
            "arxiv_id": arxiv,
            "authors": [{"name": "Some Author"}],
        },
    )

    result = PaperHandler(hub=Hub(store=store)).put(
        args={"arxiv": arxiv, "authors": ["Jane Doe"]}
    )

    assert result.ref_id == held
    assert store.find_ref_by_identifier("arxiv", arxiv, kind="paper") == held
