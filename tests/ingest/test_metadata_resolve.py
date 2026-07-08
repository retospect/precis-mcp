"""PDF-free metadata re-resolution (Bucket B) — gate logic + apply path.

Resolver clients are injected so nothing hits the network.
"""

from __future__ import annotations

from typing import Any

from precis.ingest.metadata_resolve import (
    TRIAGE_TAG,
    _title_candidates,
    _triage_refs,
    resolve_triage,
)
from precis.store import Store


def _triage_paper(
    store: Store,
    *,
    slug: str,
    title: str = "",
    year: int | None = None,
    doi: str | None = None,
    chunk0: str | None = None,
    held_no_chunks: bool = False,
    n: int = 0,
) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=title, year=year)
    with store.tx() as conn:
        if doi:
            store.set_ref_identifier(ref.id, "doi", doi, source="system", conn=conn)
        if chunk0 is not None:
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, 0, 'paragraph', %s)",
                (ref.id, chunk0),
            )
        store.add_tag(ref.id, TRIAGE_TAG, set_by="system", conn=conn)
    if held_no_chunks:
        sha = f"{n or 1:064x}"
        with store.pool.connection() as conn:
            with conn.transaction():
                conn.execute(
                    "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
                    "size_bytes, storage_path) VALUES (%s, %s, 1, 100, '') "
                    "ON CONFLICT (pdf_sha256) DO NOTHING",
                    (sha, sha),
                )
                conn.execute(
                    "UPDATE refs SET pdf_sha256=%s WHERE ref_id=%s", (sha, ref.id)
                )
    return ref.id


def _fake_crossref(result: dict[str, Any] | None):
    def fn(doi: str, mailto: str) -> dict[str, Any] | None:
        return result

    return fn


def _fake_s2(result: dict[str, Any] | None):
    def fn(title: str, api_key: str) -> dict[str, Any] | None:
        return result

    return fn


def _no_call(*_a: Any, **_k: Any):  # pragma: no cover - asserts it's unused
    raise AssertionError("resolver should not be called")


def _verdict(results: list, ref_id: int) -> Any:
    for r in results:
        if r.ref_id == ref_id:
            return r
    raise AssertionError(f"no result for #{ref_id}")


def _ref(store: Store, ref_id: int):
    return store.fetch_refs_by_ids([ref_id], include_deleted=True)[ref_id]


def _has_triage(store: Store, ref_id: int) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id=rt.tag_id "
            "WHERE rt.ref_id=%s AND t.namespace='OPEN' AND t.value='needs-triage'",
            (ref_id,),
        ).fetchone()
    return row is not None


# ── Track 1: DOI → Crossref ───────────────────────────────────────


def test_track1_doi_auto_applies(store: Store) -> None:
    rid = _triage_paper(store, slug="j1", title="doi:10.x/y", doi="10.1234/real")
    meta: dict[str, Any] = {
        "title": "Gas Chromatography of Volatile Compounds",
        "authors": [{"name": "A. Chemist"}],
        "year": 2003,
        "journal": "J. Chromatography",
        "abstract": "We report a method.",
        "doi": "10.1234/real",
    }
    out = resolve_triage(
        store, apply=True, crossref_fn=_fake_crossref(meta), s2_fn=_no_call
    )
    r = _verdict(out, rid)
    assert r.verdict == "auto" and r.track == "doi"
    assert _ref(store, rid).title == "Gas Chromatography of Volatile Compounds"
    assert (_ref(store, rid).meta or {}).get("journal") == "J. Chromatography"
    assert not _has_triage(store, rid)  # tag dropped


def test_track1_book_doi_discarded(store: Store) -> None:
    rid = _triage_paper(
        store, slug="bk", title="Index", doi="10.1016/b978-0-12-814608-8.09993-x"
    )
    out = resolve_triage(store, apply=True, crossref_fn=_no_call, s2_fn=_no_call)
    r = _verdict(out, rid)
    assert r.verdict == "discard" and "book" in r.reason
    assert _has_triage(store, rid)  # not touched


def test_track1_crossref_junk_title_discarded(store: Store) -> None:
    rid = _triage_paper(store, slug="jk", title="", doi="10.1/x")
    meta: dict[str, Any] = {"title": "", "authors": [], "year": None, "doi": "10.1/x"}
    out = resolve_triage(
        store, apply=True, crossref_fn=_fake_crossref(meta), s2_fn=_no_call
    )
    assert _verdict(out, rid).verdict == "discard"


# ── Track 2: title search → S2 ────────────────────────────────────


def test_track2_title_auto_recovers_doi(store: Store) -> None:
    rid = _triage_paper(
        store,
        slug="t2",
        title="",  # junk/empty → falls back to chunk 0
        year=1996,
        chunk0="An Aperiodic Set of Wang Cubes 1\n\nKarel Culik II",
    )
    cand: dict[str, Any] = {
        "title": "An Aperiodic Set of Wang Cubes",
        "authors": [{"name": "Karel Culik II"}],
        "year": 1996,
        "doi": "10.3217/jucs-recovered",
        "journal": "JUCS",
        "abstract": "",
    }
    out = resolve_triage(store, apply=True, crossref_fn=_no_call, s2_fn=_fake_s2(cand))
    r = _verdict(out, rid)
    assert r.verdict == "auto" and r.track == "title"
    # The recovered DOI is attached — the prize.
    with store.pool.connection() as conn:
        doi = conn.execute(
            "SELECT id_value FROM ref_identifiers WHERE ref_id=%s AND id_kind='doi'",
            (rid,),
        ).fetchone()
    assert doi is not None and doi[0] == "10.3217/jucs-recovered"
    assert not _has_triage(store, rid)


def test_track2_year_mismatch_goes_to_review(store: Store) -> None:
    rid = _triage_paper(store, slug="ym", title="A Study of Widget Dynamics", year=2005)
    cand: dict[str, Any] = {
        "title": "A Study of Widget Dynamics",  # identical → high sim
        "authors": [{"name": "X"}],
        "year": 2020,  # but incompatible year
        "doi": "10.1/w",
    }
    out = resolve_triage(store, apply=True, crossref_fn=_no_call, s2_fn=_fake_s2(cand))
    r = _verdict(out, rid)
    assert r.verdict == "review" and r.reason == "year-mismatch"
    assert _has_triage(store, rid)  # not applied


def test_track2_s2_miss(store: Store) -> None:
    rid = _triage_paper(store, slug="ms", title="Some Obscure Title Here")
    out = resolve_triage(store, apply=True, crossref_fn=_no_call, s2_fn=_fake_s2(None))
    assert _verdict(out, rid).verdict == "miss"


def test_track2_recovered_doi_collision_is_review(store: Store) -> None:
    # A held paper already owns the DOI the title search would recover.
    owner = store.insert_ref(kind="paper", slug="own", title="Owner Paper")
    with store.tx() as conn:
        store.set_ref_identifier(
            owner.id, "doi", "10.9/dup", source="system", conn=conn
        )
    rid = _triage_paper(store, slug="col", title="Deep Residual Learning Networks")
    cand: dict[str, Any] = {
        "title": "Deep Residual Learning Networks",
        "authors": [{"name": "K. He"}],
        "year": None,
        "doi": "10.9/dup",
    }
    out = resolve_triage(store, apply=True, crossref_fn=_no_call, s2_fn=_fake_s2(cand))
    r = _verdict(out, rid)
    assert r.verdict == "review" and "owned-by" in r.reason
    assert _has_triage(store, rid)


# ── network guards ────────────────────────────────────────────────


def test_slow_lookup_times_out_to_miss(store: Store) -> None:
    """A lookup that overruns the wall-clock cap is a miss, not a hang."""
    import time as _t

    rid = _triage_paper(store, slug="slow", title="A Slow To Resolve Title")

    def _slow(_title: str, _key: str) -> dict[str, Any]:
        _t.sleep(1.0)
        return {"title": "whatever", "doi": "10.1/x"}

    out = resolve_triage(
        store,
        apply=True,
        crossref_fn=_no_call,
        s2_fn=_slow,
        call_timeout=0.15,
        delay=0.0,
    )
    r = _verdict(out, rid)
    assert r.verdict == "miss" and r.reason == "s2-timeout"


# ── discard lane ──────────────────────────────────────────────────


def test_held_without_chunks_flagged_discard(store: Store) -> None:
    rid = _triage_paper(store, slug="gh", title="Ghost", held_no_chunks=True, n=7)
    out = resolve_triage(store, apply=True, crossref_fn=_no_call, s2_fn=_no_call)
    r = _verdict(out, rid)
    assert r.verdict == "discard" and "chunks" in r.reason
    assert _has_triage(store, rid)  # only flagged, never deleted


# ── multi-chunk title candidates + widened selection ──────────────


def _fake_s2_by_query(mapping: dict[str, dict[str, Any] | None]):
    """S2 fake that answers per query title (multi-candidate tests)."""

    def fn(title: str, api_key: str) -> dict[str, Any] | None:
        return mapping.get(title)

    return fn


def _titleless_paper(
    store: Store,
    *,
    slug: str,
    n: int,
    title: str = "",
    chunks: tuple[str, ...] = (),
    doi: str | None = None,
    tag: bool = False,
) -> int:
    """A chunked (``pdf_sha256`` set) paper with N body chunks. ``tag=False``
    leaves it *untagged* — the case the widened selection must now reach."""
    ref = store.insert_ref(kind="paper", slug=slug, title=title)
    sha = f"{n:064x}"
    with store.pool.connection() as conn, conn.transaction():
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, size_bytes, "
            "storage_path) VALUES (%s, %s, 1, 100, '') ON CONFLICT DO NOTHING",
            (sha, sha),
        )
        conn.execute("UPDATE refs SET pdf_sha256=%s WHERE ref_id=%s", (sha, ref.id))
        for i, text in enumerate(chunks):
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, %s, 'paragraph', %s)",
                (ref.id, i, text),
            )
        if doi:
            store.set_ref_identifier(ref.id, "doi", doi, source="system", conn=conn)
        if tag:
            store.add_tag(ref.id, TRIAGE_TAG, set_by="system", conn=conn)
    return ref.id


def test_title_candidates_scans_beyond_chunk0(store: Store) -> None:
    # chunk 0 is a masthead; the real title sits in chunk 1. The old
    # chunk-0-only query would have searched "Contents lists available…".
    rid = _titleless_paper(
        store,
        slug="beyond0",
        n=0x101,
        chunks=(
            "Contents lists available at ScienceDirect",
            "Continuous deformations in random networks",
            "M.F. Thorpe, Cavendish Laboratory",
        ),
    )
    cands = _title_candidates(store, _ref(store, rid))
    assert "Continuous deformations in random networks" in cands
    assert all("ScienceDirect" not in c for c in cands)


def test_title_candidates_filter_furniture_and_strip_markdown(store: Store) -> None:
    rid = _titleless_paper(
        store,
        slug="furn",
        n=0x102,
        chunks=(
            "Received 2 Sep 2015 | Accepted 4 Mar 2016\n"
            "https://doi.org/10.1000/xyz\n"
            "Journal of Business Volume 10\n"
            "**Large Dual Encoders Are Generalizable Retrievers**<sup>1</sup>\n"
            "Jianmo Ni, Chen Qu",
        ),
    )
    cands = _title_candidates(store, _ref(store, rid))
    # Furniture (received-line, URL, masthead) dropped; markdown stripped; the
    # real title is the first candidate (it precedes the author line).
    assert cands[0] == "Large Dual Encoders Are Generalizable Retrievers"
    assert not any("Received" in c or "doi.org" in c or "Volume" in c for c in cands)


def test_track2_scans_past_missing_candidate(store: Store) -> None:
    # First candidate misses S2; the resolver must keep scanning to the real
    # title in chunk 1 rather than giving up (the old single-query behaviour).
    rid = _titleless_paper(
        store,
        slug="scanpast",
        n=0x103,
        title="",
        chunks=(
            "Preliminary Notes And Acknowledgements Section",
            "Continuous deformations in random networks",
        ),
    )
    title = "Continuous deformations in random networks"
    mapping: dict[str, dict[str, Any] | None] = {
        "Preliminary Notes And Acknowledgements Section": None,
        title: {
            "title": title,
            "authors": [{"name": "Thorpe, M"}],
            "year": None,
            "doi": "10.1016/scanpast",
        },
    }
    out = resolve_triage(
        store, apply=True, crossref_fn=_no_call, s2_fn=_fake_s2_by_query(mapping)
    )
    r = _verdict(out, rid)
    assert r.verdict == "auto" and r.doi == "10.1016/scanpast"


def test_widened_selection_reaches_untagged_titleless(store: Store) -> None:
    # The core gap: a chunked paper with an empty title but NO needs-triage tag
    # (what the dedup-split fix mints) must now be in the resolver's cohort.
    untagged = _titleless_paper(store, slug="untag", n=0x201, title="", chunks=("x",))
    tagged = _titleless_paper(
        store, slug="tag", n=0x202, title="", chunks=("y",), tag=True
    )
    titled = _titleless_paper(
        store, slug="hastitle", n=0x203, title="A Perfectly Good Title", chunks=("z",)
    )
    ids = {r.id for r in _triage_refs(store, None)}
    assert untagged in ids  # widened selection now reaches it
    assert tagged in ids  # old cohort still covered
    assert titled not in ids  # a populated title is never re-resolved
