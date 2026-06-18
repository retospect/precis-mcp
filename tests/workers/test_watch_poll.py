"""watch_poll — citation-forward stub minting over the salience field.

Exercises the worker's pure loop with an injected ``fetch_cited_by`` (no
network): salient seed selection, stub minting + provenance tags,
idempotency, the per-seed cap, and seed rotation.
"""

from __future__ import annotations

from typing import Any

from precis.store import Store
from precis.workers.watch_poll import run_watch_pass


def _mk_chunk(store: Store, ref_id: int, text: str) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
            "VALUES (%s, 0, 'paragraph', %s, '{}'::jsonb) RETURNING chunk_id",
            (ref_id, text),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _seed_paper(store: Store, doi: str) -> tuple[int, int]:
    """A salient corpus paper: stub w/ a DOI + a body chunk that's hot."""
    ref_id, _ = store.upsert_stub_paper(
        identifiers=[("doi", doi)], title="Seed paper", set_by="system"
    )
    cid = _mk_chunk(store, ref_id, "seed body")
    store.bump_salience([cid])  # make it the most-due watch seed
    return ref_id, cid


def _tags(store: Store, ref_id: int) -> set[str]:
    return {str(t) for t in store.tags_for(ref_id)}


def _papers_with_tag(store: Store, tag_value: str) -> list[int]:
    """Live paper refs carrying an open tag with ``tag_value``."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id FROM refs r "
            "JOIN ref_tags rt ON rt.ref_id = r.ref_id "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE r.kind = 'paper' AND r.deleted_at IS NULL AND t.value = %s",
            (tag_value,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _watch_score(store: Store, chunk_id: int) -> float:
    """``last_seen - last_watched`` in seconds (>0 = due, <=0 = rotated)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT EXTRACT(EPOCH FROM (last_seen - last_watched)) "
            "FROM chunks WHERE chunk_id = %s",
            (chunk_id,),
        ).fetchone()
    assert row is not None
    return float(row[0])


def test_mints_stubs_for_forward_citations(store: Store) -> None:
    _seed_paper(store, "10.seed/1")
    cited = [
        {"doi": "10.cite/1", "title": "Citer One", "year": 2025},
        {"doi": "10.cite/2", "title": "Citer Two", "year": 2024},
    ]
    res = run_watch_pass(store, fetch_cited_by=lambda _id: cited)
    assert res == {"claimed": 1, "ok": 2, "failed": 0}
    # Two citing papers now exist as stubs with provenance tags.
    minted = _papers_with_tag(store, "source:semantic-scholar")
    assert len(minted) == 2
    for rid in minted:
        tags = _tags(store, rid)
        assert any(t.startswith("discovered-via:cite:") for t in tags)
        # Minted as a stub — no PDF, so fetch_oa will OA-gate it later.
        ref = store.get_ref(kind="paper", id=rid)
        assert ref is not None and ref.pdf_sha256 is None


def test_idempotent_no_dup_stubs(store: Store) -> None:
    _, cid = _seed_paper(store, "10.seed/2")
    cited = [{"doi": "10.cite/9", "title": "Citer", "year": 2025}]
    assert run_watch_pass(store, fetch_cited_by=lambda _i: cited)["ok"] == 1
    # Re-heat the seed so it's selectable again; same cited_by → no new stub.
    store.bump_salience([cid])
    assert run_watch_pass(store, fetch_cited_by=lambda _i: cited)["ok"] == 0


def test_seed_rotates_after_poll(store: Store) -> None:
    _, cid = _seed_paper(store, "10.seed/3")
    # Before: due (bumped, so last_seen > last_watched).
    assert _watch_score(store, cid) > 0
    assert store.select_salient("watch", kinds=("paper",))[0] == cid
    run_watch_pass(store, fetch_cited_by=lambda _i: [])
    # After: rotated out (last_watched advanced past last_seen → score <= 0).
    assert _watch_score(store, cid) <= 0


def test_per_seed_cap(store: Store) -> None:
    _seed_paper(store, "10.seed/4")
    cited = [{"doi": f"10.cap/{i}", "title": f"C{i}", "year": 2025} for i in range(20)]
    res = run_watch_pass(store, fetch_cited_by=lambda _i: cited, max_per_seed=5)
    assert res["ok"] == 5  # capped; the other 15 dropped this pass


def test_fetch_failure_counts_and_rotates(store: Store) -> None:
    _, cid = _seed_paper(store, "10.seed/5")

    def _boom(_id: str) -> list[dict[str, Any]]:
        raise RuntimeError("S2 down")

    res = run_watch_pass(store, fetch_cited_by=_boom)
    assert res == {"claimed": 1, "ok": 0, "failed": 1}
    # Even on failure the seed rotates so the pass doesn't spin on it.
    assert _watch_score(store, cid) <= 0


def test_citing_without_identifier_skipped(store: Store) -> None:
    _seed_paper(store, "10.seed/6")
    cited = [{"title": "No ids here", "year": 2025}]  # nothing to dedup/fetch on
    res = run_watch_pass(store, fetch_cited_by=lambda _i: cited)
    assert res["ok"] == 0
