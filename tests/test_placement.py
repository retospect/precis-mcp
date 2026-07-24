"""Rung 6a of the paper-writing pipeline: deterministic paper→section
placement (docs/design/paper-writing-pipeline.md §Integrate — the tick
body, step 1: Place). Pure geometry — controlled one-hot vectors stand in
for real bge-m3 embeddings so centroid math + the floor/top-k gate are
exercised without a model in the loop.
"""

from __future__ import annotations

import numpy as np
import pytest

from precis.quest.placement import (
    DEFAULT_PLACEMENT_FLOOR,
    place_paper,
    place_papers,
    residual_paper_ids,
)
from precis.store import Store

_DEFAULT_EMBEDDER = "bge-m3"  # the migration-seeded default (embedders.dim=1024)
_DIM = 1024


def _onehot(i: int) -> list[float]:
    v = [0.0] * _DIM
    v[i] = 1.0
    return v


def _blend(*idxs: int) -> list[float]:
    """A unit vector equidistant from each ``e_i`` in ``idxs`` — a paper
    "between" that many sections."""
    v = [0.0] * _DIM
    for i in idxs:
        v[i] = 1.0
    norm = float(np.linalg.norm(v))
    return [x / norm for x in v]


def _embed(store: Store, chunk_id: int, vec: list[float]) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, attempts) "
            "VALUES (%s, %s, %s, 'ok', 1)",
            (chunk_id, _DEFAULT_EMBEDDER, vec),
        )


def _dossier_with_sections(store: Store, slug: str, n: int) -> tuple[int, list[str]]:
    """A draft with ``n`` scaffolded top-level headings, each heading's own
    chunk embedded at the ``i``-th one-hot vector — so with no body chunks
    under it, section ``i``'s centroid is exactly ``e_i``. Returns the
    dossier ref id and the sections' **legacy** ``handle``s (what
    ``draft_toc``/``place_papers`` rows key on — not ``scaffold_sections``'s
    ``dc`` return value, a different handle namespace)."""
    ref = store.insert_ref(kind="draft", slug=slug, title="Dossier", meta={})
    sections: list[tuple[str, str | None]] = [(f"Section {i}", None) for i in range(n)]
    dc_handles = store.scaffold_sections(ref.id, sections)
    handles = []
    for i, dc_handle in enumerate(dc_handles):
        chunk = store.get_draft_chunk(dc_handle)
        assert chunk is not None
        _embed(store, chunk.chunk_id, _onehot(i))
        handles.append(chunk.handle)
    return ref.id, handles


def _paper(store: Store, slug: str, vec: list[float] | None) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=slug, meta={})
    if vec is not None:
        cid = store.upsert_card_combined(ref.id, "gist")
        _embed(store, cid, vec)
    return ref.id


def test_aligned_paper_places_into_its_section_only(store: Store) -> None:
    dossier_id, handles = _dossier_with_sections(store, "d1", 2)
    paper_id = _paper(store, "p1", _onehot(0))

    placements = place_papers(store, dossier_id, [paper_id])

    rows = placements[paper_id]
    assert len(rows) == 1
    assert rows[0]["handle"] == handles[0]
    assert rows[0]["score"] == pytest.approx(1.0, abs=1e-4)


def test_orthogonal_paper_is_residual(store: Store) -> None:
    dossier_id, _handles = _dossier_with_sections(store, "d2", 2)
    paper_id = _paper(store, "p2", _onehot(500))  # orthogonal to sections 0/1

    placements = place_papers(store, dossier_id, [paper_id])

    assert placements[paper_id] == []
    assert residual_paper_ids(placements) == [paper_id]


def test_multi_place_up_to_top_k_sorted_by_score(store: Store) -> None:
    dossier_id, handles = _dossier_with_sections(store, "d3", 3)
    paper_id = _paper(store, "p3", _blend(0, 1))  # equidistant from 0 and 1

    placements = place_papers(store, dossier_id, [paper_id], top_k=3)

    rows = placements[paper_id]
    assert {r["handle"] for r in rows} == {handles[0], handles[1]}
    assert rows == sorted(rows, key=lambda r: r["score"], reverse=True)


def test_top_k_caps_multi_place(store: Store) -> None:
    dossier_id, _handles = _dossier_with_sections(store, "d3b", 3)
    paper_id = _paper(store, "p3b", _blend(0, 1, 2))  # equidistant from all 3

    placements = place_papers(store, dossier_id, [paper_id], top_k=2)

    assert len(placements[paper_id]) == 2


def test_empty_dossier_is_all_residual(store: Store) -> None:
    ref = store.insert_ref(kind="draft", slug="d4", title="Empty", meta={})
    paper_id = _paper(store, "p4", _onehot(0))

    placements = place_papers(store, ref.id, [paper_id])

    assert placements == {paper_id: []}


def test_unembedded_paper_is_residual(store: Store) -> None:
    dossier_id, _handles = _dossier_with_sections(store, "d5", 1)
    paper_id = _paper(store, "p5", None)  # no gist embedding at all

    placements = place_papers(store, dossier_id, [paper_id])

    assert placements[paper_id] == []


def test_place_paper_single_convenience(store: Store) -> None:
    dossier_id, handles = _dossier_with_sections(store, "d6", 1)
    paper_id = _paper(store, "p6", _onehot(0))

    rows = place_paper(store, dossier_id, paper_id)

    assert len(rows) == 1
    assert rows[0]["handle"] == handles[0]


def test_default_floor_is_030() -> None:
    assert DEFAULT_PLACEMENT_FLOOR == 0.30
