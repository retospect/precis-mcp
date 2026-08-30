"""source-backfill slice 3 — the citation-graph lens.

Builds a small held corpus with external ids, monkeypatches the batched S2
fetch seam (``citation_recall.fetch_citations_batch``) so the tests never need
the ``[paper]`` extra, and checks: edges materialise corpus-internally in the
right direction, non-held / body-less neighbours are handled correctly, the
neighbour query ranks + excludes, the merge into the text lens badges
agreement, and the cold-paper fetch really is one batched call.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from precis.backfill import candidates as candmod
from precis.backfill import citation_recall as cl
from precis.backfill.candidates import LENS_CITATION, LENS_TEXT, RecallCandidate
from precis.dispatch import Hub
from precis.store.types import ChunkInsert


def _paper(store, slug: str, *, body: bool = True) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=f"Paper {slug}")
    if body:
        store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text=f"body of {slug}")])
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

    def fake_fetch_batch(
        paper_ids: list[str],
    ) -> dict[str, dict[str, list[dict[str, object]]]]:
        assert paper_ids == ["S2A"]  # A is s2-addressed, the only cold paper
        return {
            "S2A": {
                "references": [
                    {"s2_id": "S2B", "title": "Kumar"},
                    {"s2_id": "S2E", "title": "Stub"},
                    {"s2_id": "S2D", "title": "Not held"},  # D — not in corpus
                ],
                "cited_by": [{"doi": "10.1/CCC", "title": "Li"}],  # upper → normalises
            }
        }

    cl.fetch_citations_batch = fake_fetch_batch
    return {"a": a, "b": b, "c": c, "e": e}


@pytest.fixture(autouse=True)
def _restore_fetch() -> object:
    original = cl.fetch_citations
    original_batch = cl.fetch_citations_batch
    yield
    cl.fetch_citations = original
    cl.fetch_citations_batch = original_batch


def test_materialize_writes_corpus_internal_edges_both_directions(hub: Hub) -> None:
    g = _build_graph(hub.live_store)
    written = cl.materialize_citation_edges(hub.live_store, {g["a"]}, ttl_days=30)

    edges = _cites_edges(hub.live_store)
    assert (g["a"], g["b"]) in edges  # A references B  → A cites B
    assert (g["a"], g["e"]) in edges  # A references E  → A cites E (held stub)
    assert (g["c"], g["a"]) in edges  # C cited-by-of A → C cites A
    # D is not held, so no edge mentions it (no phantom ref id).
    held = {g["a"], g["b"], g["c"], g["e"]}
    assert all(s in held and d in held for s, d in edges)
    assert written == 3
    # freshness stamped on A
    assert hub.live_store.events_for(g["a"], source="citation_edges") != []


def test_materialize_skips_when_fresh(hub: Hub) -> None:
    g = _build_graph(hub.live_store)
    cl.materialize_citation_edges(hub.live_store, {g["a"]}, ttl_days=30)

    calls = {"n": 0}

    def counting_fetch_batch(
        paper_ids: list[str],
    ) -> dict[str, dict[str, list[dict[str, object]]]]:
        calls["n"] += 1
        return {pid: {"references": [], "cited_by": []} for pid in paper_ids}

    cl.fetch_citations_batch = counting_fetch_batch
    written = cl.materialize_citation_edges(hub.live_store, {g["a"]}, ttl_days=30)
    assert written == 0
    assert calls["n"] == 0  # fresh → S2 not re-hit


def test_materialize_batches_across_multiple_cold_papers(hub: Hub) -> None:
    """Two cold papers in one call → ONE ``fetch_citations_batch`` call
    carrying both qids, not two per-paper calls; a paper that's already
    fresh is excluded from the batch; the edges a batched result produces
    match exactly what the old per-paper loop produced for the same
    input (co-citation degree, direction, s2_neighbors row count)."""
    g = _build_graph(hub.live_store)  # seeds A (s2 S2A) + B/C/E, D not held

    # A second, independent seed paper F with its own held reference G.
    f = _paper(hub.live_store, "feng")
    guo = _paper(hub.live_store, "guo")
    _add_id(hub.live_store, f, "s2", "S2F")
    _add_id(hub.live_store, guo, "s2", "S2G")

    calls: list[list[str]] = []

    def batch_fetch(
        paper_ids: list[str],
    ) -> dict[str, dict[str, list[dict[str, object]]]]:
        calls.append(list(paper_ids))
        return {
            "S2A": {
                "references": [
                    {"s2_id": "S2B", "title": "Kumar"},
                    {"s2_id": "S2E", "title": "Stub"},
                    {"s2_id": "S2D", "title": "Not held"},
                ],
                "cited_by": [{"doi": "10.1/CCC", "title": "Li"}],
            },
            "S2F": {
                "references": [{"s2_id": "S2G", "title": "Guo"}],
                "cited_by": [],
            },
        }

    cl.fetch_citations_batch = batch_fetch

    # Materialise F on its own first, so it's already fresh by the time the
    # combined call below runs — the single-paper path here is exactly what
    # the old per-paper loop did, and doubles as the "matches per-paper
    # writes" baseline (b): 1 held edge, one s2_neighbors row, degree 1.
    written_f = cl.materialize_citation_edges(hub.live_store, {f}, ttl_days=30)
    assert written_f == 1
    assert calls == [["S2F"]]
    assert (f, guo) in _cites_edges(hub.live_store)
    assert [n.s2_id for n in hub.live_store.list_s2_neighbors(f, "cites")] == ["S2G"]

    written = cl.materialize_citation_edges(hub.live_store, {g["a"], f}, ttl_days=30)
    # F is fresh → excluded; only A's qid goes into the (single) new call.
    assert calls == [["S2F"], ["S2A"]]
    assert written == 3  # A's 3 held edges; F untouched (already materialised)

    edges = _cites_edges(hub.live_store)
    assert (g["a"], g["b"]) in edges
    assert (g["a"], g["e"]) in edges
    assert (g["c"], g["a"]) in edges
    assert (f, guo) in edges  # unchanged from the earlier fresh materialise


def test_neighbor_degrees_excludes_cited_and_bodyless(hub: Hub) -> None:
    g = _build_graph(hub.live_store)
    cl.materialize_citation_edges(hub.live_store, {g["a"]}, ttl_days=30)

    degrees = cl.citation_neighbor_degrees(hub.live_store, {g["a"]}, exclude={g["a"]})
    ids = {rid for rid, _ in degrees}
    assert ids == {g["b"], g["c"]}  # E has no body → not a candidate; D not held

    # excluding B (e.g. already dismissed) drops it
    only_c = cl.citation_neighbor_degrees(
        hub.live_store, {g["a"]}, exclude={g["a"], g["b"]}
    )
    assert {rid for rid, _ in only_c} == {g["c"]}


def test_find_citation_candidates_builds_lead_chunk_candidates(hub: Hub) -> None:
    g = _build_graph(hub.live_store)
    cands = cl.find_citation_candidates(
        hub.live_store, {g["a"]}, exclude={g["a"]}, limit=8
    )
    by_ref = {c.ref_id: c for c in cands}
    assert set(by_ref) == {g["b"], g["c"]}
    for c in cands:
        assert c.lenses == (LENS_CITATION,)
        assert c.chunk_handle.startswith("pc")  # opened at a real body chunk
        assert c.score == 1.0  # co-citation degree 1


def test_materialize_writes_s2_neighbors_both_directions(hub: Hub) -> None:
    """The Sources/Cited tabs' data side: the FULL neighbour list (held or not)
    lands in ``s2_neighbors``, not just the held↔held subset that gets a
    ``cites`` edge."""
    g = _build_graph(hub.live_store)
    cl.materialize_citation_edges(hub.live_store, {g["a"]}, ttl_days=30)

    cites = hub.live_store.list_s2_neighbors(g["a"], "cites")
    assert [n.s2_id for n in cites] == ["S2B", "S2E", "S2D"]  # ord = S2 list order
    by_s2 = {n.s2_id: n for n in cites}
    assert by_s2["S2B"].held_ref_id == g["b"]
    assert by_s2["S2E"].held_ref_id == g["e"]  # held stub — no body, still resolved
    assert by_s2["S2D"].held_ref_id is None  # D is not held — persisted anyway
    assert by_s2["S2D"].title == "Not held"

    cited_by = hub.live_store.list_s2_neighbors(g["a"], "cited_by")
    assert len(cited_by) == 1
    assert cited_by[0].doi == "10.1/CCC"
    assert cited_by[0].held_ref_id == g["c"]

    assert hub.live_store.s2_neighbors_fresh(g["a"]) is True
    # B was never fetched from as a seed — no neighbours persisted for it.
    assert hub.live_store.s2_neighbors_fresh(g["b"]) is False


def test_materialize_refresh_replaces_s2_neighbors(hub: Hub) -> None:
    g = _build_graph(hub.live_store)
    cl.materialize_citation_edges(hub.live_store, {g["a"]}, ttl_days=30)
    assert len(hub.live_store.list_s2_neighbors(g["a"], "cites")) == 3

    def shrunk_fetch_batch(
        paper_ids: list[str],
    ) -> dict[str, dict[str, list[dict[str, object]]]]:
        return {
            pid: {"references": [{"s2_id": "S2B", "title": "Kumar"}], "cited_by": []}
            for pid in paper_ids
        }

    cl.fetch_citations_batch = shrunk_fetch_batch
    cl.materialize_citation_edges(
        hub.live_store, {g["a"]}, ttl_days=0
    )  # force re-fetch

    cites = hub.live_store.list_s2_neighbors(g["a"], "cites")
    assert [n.s2_id for n in cites] == ["S2B"]  # E and D dropped, no dup rows
    assert hub.live_store.list_s2_neighbors(g["a"], "cited_by") == []  # cleared too


def test_ensure_s2_neighbors_fetches_once_then_skips_within_ttl(hub: Hub) -> None:
    g = _build_graph(hub.live_store)
    calls = {"n": 0}
    base_fetch_batch = cl.fetch_citations_batch

    def counting_fetch_batch(
        paper_ids: list[str],
    ) -> dict[str, dict[str, list[dict[str, object]]]]:
        calls["n"] += 1
        return base_fetch_batch(paper_ids)

    cl.fetch_citations_batch = counting_fetch_batch

    assert hub.live_store.s2_neighbors_fresh(g["a"]) is False
    fetched = cl.ensure_s2_neighbors(hub.live_store, g["a"], ttl_days=30)
    assert fetched is True
    assert calls["n"] == 1
    assert hub.live_store.s2_neighbors_fresh(g["a"]) is True
    assert len(hub.live_store.list_s2_neighbors(g["a"], "cites")) == 3

    # second call within the TTL: no re-fetch, S2 not hit again.
    fetched_again = cl.ensure_s2_neighbors(hub.live_store, g["a"], ttl_days=30)
    assert fetched_again is False
    assert calls["n"] == 1


def test_materialize_fetch_failure_leaves_unstamped_and_retries(hub: Hub) -> None:
    """An S2 failure (``fetch_citations_batch`` raises, e.g. the batch call
    dying after its retry budget) must persist NOTHING and stamp NOTHING —
    a partial result stored as truth would freeze an empty bibliography for
    a whole TTL. The next call retries and succeeds."""
    g = _build_graph(hub.live_store)
    good_fetch_batch = cl.fetch_citations_batch

    def failing_fetch_batch(
        paper_ids: list[str],
    ) -> dict[str, dict[str, list[dict[str, object]]]]:
        raise RuntimeError("s2 down")

    cl.fetch_citations_batch = failing_fetch_batch
    written = cl.materialize_citation_edges(hub.live_store, {g["a"]}, ttl_days=30)
    assert written == 0
    assert hub.live_store.events_for(g["a"], source="citation_edges") == []
    assert hub.live_store.s2_neighbors_fresh(g["a"]) is False

    cl.fetch_citations_batch = good_fetch_batch
    assert cl.ensure_s2_neighbors(hub.live_store, g["a"], ttl_days=30) is True
    assert len(hub.live_store.list_s2_neighbors(g["a"], "cites")) == 3


def test_ensure_s2_neighbors_refetches_fresh_stamp_with_no_rows(hub: Hub) -> None:
    """A pre-0106 ``citation_edges`` stamp is fresh while ``s2_neighbors``
    has no rows (the fetch predated persistence) — the stamp alone must not
    suppress the fetch, or old papers show empty Sources/Cited tabs until
    the TTL lapses."""
    g = _build_graph(hub.live_store)
    cl.ensure_s2_neighbors(hub.live_store, g["a"], ttl_days=30)
    with hub.live_store.pool.connection() as conn:
        conn.execute("DELETE FROM s2_neighbors WHERE ref_id = %s", (g["a"],))
    assert hub.live_store.s2_neighbors_fresh(g["a"]) is False

    calls = {"n": 0}
    base_fetch_batch = cl.fetch_citations_batch

    def counting_fetch_batch(
        paper_ids: list[str],
    ) -> dict[str, dict[str, list[dict[str, object]]]]:
        calls["n"] += 1
        return base_fetch_batch(paper_ids)

    cl.fetch_citations_batch = counting_fetch_batch

    fetched = cl.ensure_s2_neighbors(hub.live_store, g["a"], ttl_days=30)
    assert fetched is True
    assert calls["n"] == 1
    assert len(hub.live_store.list_s2_neighbors(g["a"], "cites")) == 3


def test_disabled_by_env(hub: Hub, monkeypatch: pytest.MonkeyPatch) -> None:
    g = _build_graph(hub.live_store)
    monkeypatch.setenv("PRECIS_BACKFILL_CITATION_RECALL", "0")
    assert (
        cl.find_citation_candidates(hub.live_store, {g["a"]}, exclude=set(), limit=8)
        == []
    )


def test_merge_badges_agreement_and_appends_citation_only(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A text candidate for B (found by the text lens)…
    text_b = RecallCandidate(
        ref_id=101,
        ref=NS(kind="paper", title="Kumar"),
        chunk_id=1,
        chunk_handle="pc1",
        score=2.0,
        lenses=(LENS_TEXT,),
    )
    out = [text_b]
    # …and the citation lens finds B (agreement) + C (citation-only).
    cite_b = RecallCandidate(
        ref_id=101,
        ref=text_b.ref,
        chunk_id=9,
        chunk_handle="pc9",
        score=1.0,
        lenses=(LENS_CITATION,),
    )
    cite_c = RecallCandidate(
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

    candmod._merge_citation_recall(hub.live_store, out, {1}, set(), 8)

    assert out[0].ref_id == 101
    assert out[0].lenses == (LENS_TEXT, LENS_CITATION)  # agreement badge
    assert out[0].chunk_handle == "pc1"  # kept the text lens's chunk
    assert [c.ref_id for c in out] == [101, 202]  # citation-only C appended
    assert out[1].lenses == (LENS_CITATION,)
