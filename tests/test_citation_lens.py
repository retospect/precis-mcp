"""source-backfill slice 3 — the citation-graph lens.

Builds a small held corpus with external ids, monkeypatches the S2 fetch seam
(``citation_lens.fetch_citations``) so the tests never need the ``[paper]``
extra, and checks: edges materialise corpus-internally in the right direction,
non-held / body-less neighbours are handled correctly, the neighbour query ranks
+ excludes, and the merge into the text lens badges agreement.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from precis.backfill import candidates as candmod
from precis.backfill import citation_lens as cl
from precis.backfill.candidates import LENS_CITATION, LENS_TEXT, Candidate
from precis.dispatch import Hub
from precis.store.types import BlockInsert


def _paper(store, slug: str, *, body: bool = True) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=f"Paper {slug}")
    if body:
        store.insert_blocks(ref.id, [BlockInsert(pos=0, text=f"body of {slug}")])
    return int(ref.id)


def _add_id(store, ref_id: int, id_kind: str, id_value: str) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES (%s, %s, %s, %s)",
            (id_kind, id_value, ref_id, "test"),
        )
        conn.commit()


def _cites_edges(store) -> set[tuple[int, int]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT src_ref_id, dst_ref_id FROM links WHERE relation = 'cites'"
        ).fetchall()
    return {(int(r[0]), int(r[1])) for r in rows}


def _build_graph(store) -> dict[str, int]:
    """A = cited seed; B = a reference of A (held, body); C = a citer of A (held,
    body, DOI-addressed); E = a held reference of A with **no body**; D = a
    reference of A **not** in the corpus."""
    a = _paper(store, "wang")
    b = _paper(store, "kumar")
    c = _paper(store, "li")
    e = _paper(store, "stub", body=False)
    _add_id(store, a, "s2", "S2A")
    _add_id(store, b, "s2", "S2B")
    _add_id(store, c, "doi", "10.1/ccc")  # stored normalised (lowercase)
    _add_id(store, e, "s2", "S2E")

    def fake_fetch(paper_id: str) -> dict[str, list[dict[str, object]]]:
        assert paper_id == "S2A"  # A is s2-addressed
        return {
            "references": [
                {"s2_id": "S2B", "title": "Kumar"},
                {"s2_id": "S2E", "title": "Stub"},
                {"s2_id": "S2D", "title": "Not held"},  # D — not in corpus
            ],
            "cited_by": [{"doi": "10.1/CCC", "title": "Li"}],  # upper → normalises
        }

    cl.fetch_citations = fake_fetch  # type: ignore[assignment]
    return {"a": a, "b": b, "c": c, "e": e}


@pytest.fixture(autouse=True)
def _restore_fetch() -> object:
    original = cl.fetch_citations
    yield
    cl.fetch_citations = original


def test_materialize_writes_corpus_internal_edges_both_directions(hub: Hub) -> None:
    g = _build_graph(hub.store)
    written = cl.materialize_citation_edges(hub.store, {g["a"]}, ttl_days=30)

    edges = _cites_edges(hub.store)
    assert (g["a"], g["b"]) in edges  # A references B  → A cites B
    assert (g["a"], g["e"]) in edges  # A references E  → A cites E (held stub)
    assert (g["c"], g["a"]) in edges  # C cited-by-of A → C cites A
    # D is not held, so no edge mentions it (no phantom ref id).
    held = {g["a"], g["b"], g["c"], g["e"]}
    assert all(s in held and d in held for s, d in edges)
    assert written == 3
    # freshness stamped on A
    assert hub.store.events_for(g["a"], source="citation_edges") != []


def test_materialize_skips_when_fresh(hub: Hub) -> None:
    g = _build_graph(hub.store)
    cl.materialize_citation_edges(hub.store, {g["a"]}, ttl_days=30)

    calls = {"n": 0}

    def counting_fetch(paper_id: str) -> dict[str, list[dict[str, object]]]:
        calls["n"] += 1
        return {"references": [], "cited_by": []}

    cl.fetch_citations = counting_fetch  # type: ignore[assignment]
    written = cl.materialize_citation_edges(hub.store, {g["a"]}, ttl_days=30)
    assert written == 0
    assert calls["n"] == 0  # fresh → S2 not re-hit


def test_neighbor_degrees_excludes_cited_and_bodyless(hub: Hub) -> None:
    g = _build_graph(hub.store)
    cl.materialize_citation_edges(hub.store, {g["a"]}, ttl_days=30)

    degrees = cl.citation_neighbor_degrees(hub.store, {g["a"]}, exclude={g["a"]})
    ids = {rid for rid, _ in degrees}
    assert ids == {g["b"], g["c"]}  # E has no body → not a candidate; D not held

    # excluding B (e.g. already dismissed) drops it
    only_c = cl.citation_neighbor_degrees(hub.store, {g["a"]}, exclude={g["a"], g["b"]})
    assert {rid for rid, _ in only_c} == {g["c"]}


def test_find_citation_candidates_builds_lead_chunk_candidates(hub: Hub) -> None:
    g = _build_graph(hub.store)
    cands = cl.find_citation_candidates(hub.store, {g["a"]}, exclude={g["a"]}, limit=8)
    by_ref = {c.ref_id: c for c in cands}
    assert set(by_ref) == {g["b"], g["c"]}
    for c in cands:
        assert c.lenses == (LENS_CITATION,)
        assert c.chunk_handle.startswith("pc")  # opened at a real body chunk
        assert c.score == 1.0  # co-citation degree 1


def test_disabled_by_env(hub: Hub, monkeypatch: pytest.MonkeyPatch) -> None:
    g = _build_graph(hub.store)
    monkeypatch.setenv("PRECIS_BACKFILL_CITATION_LENS", "0")
    assert (
        cl.find_citation_candidates(hub.store, {g["a"]}, exclude=set(), limit=8) == []
    )


def test_merge_badges_agreement_and_appends_citation_only(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A text candidate for B (found by the text lens)…
    text_b = Candidate(
        ref_id=101,
        ref=NS(kind="paper", title="Kumar"),
        chunk_id=1,
        chunk_handle="pc1",
        score=2.0,
        lenses=(LENS_TEXT,),
    )
    out = [text_b]
    # …and the citation lens finds B (agreement) + C (citation-only).
    cite_b = Candidate(
        ref_id=101,
        ref=text_b.ref,
        chunk_id=9,
        chunk_handle="pc9",
        score=1.0,
        lenses=(LENS_CITATION,),
    )
    cite_c = Candidate(
        ref_id=202,
        ref=NS(kind="paper", title="Li"),
        chunk_id=5,
        chunk_handle="pc5",
        score=1.0,
        lenses=(LENS_CITATION,),
    )
    monkeypatch.setattr(
        cl, "find_citation_candidates", lambda *a, **k: [cite_b, cite_c]
    )

    candmod._merge_citation_lens(hub.store, out, {1}, set(), 8)

    assert out[0].ref_id == 101
    assert out[0].lenses == (LENS_TEXT, LENS_CITATION)  # agreement badge
    assert out[0].chunk_handle == "pc1"  # kept the text lens's chunk
    assert [c.ref_id for c in out] == [101, 202]  # citation-only C appended
    assert out[1].lenses == (LENS_CITATION,)
