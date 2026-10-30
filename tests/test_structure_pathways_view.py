"""``get(kind='structure', id='<slug>', view='pathways')`` — the
per-candidate pathway list (gr540871): every live pathway whose
``meta.candidate_ref`` names this structure, with status, tier and a
results-keys summary (``che_affine``/``her`` readable without a SQL
detour). Meta-only, so it answers for a candidate whose atoms never
landed — the seed below is a bare structure ref.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.structure import StructureHandler
from precis.store import Store


@pytest.fixture
def structure(store):
    return StructureHandler(hub=Hub(store=store))


def _seed_structure(store: Store, slug: str) -> int:
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="structure", slug=slug, title=f"{slug} candidate", meta={}, conn=conn
        )
    return int(ref.id)


def _seed_pathway(store: Store, slug: str, meta: dict[str, Any]) -> int:
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="pathway", slug=slug, title=f"{slug} pathway", meta=meta, conn=conn
        )
    return int(ref.id)


def test_pathways_lists_candidate_runs(structure, store):
    cand = _seed_structure(store, "pd_cand")
    _seed_pathway(
        store,
        "pd-rx-ready",
        {
            "status": "ready",
            "candidate_ref": cand,
            "tier": "screening",
            "results": {
                "che_affine": {"per_route": []},
                "her": {"edges": []},
                "nodes": {},
                "edges": [],
            },
        },
    )
    _seed_pathway(
        store,
        "pd-rx-computing",
        {"status": "computing", "candidate_ref": cand},
    )
    body = structure.get(id="pd_cand", view="pathways").body
    assert "pd-rx-ready" in body and "pd-rx-computing" in body
    assert "ready" in body and "computing" in body and "screening" in body
    # the results-keys summary carries the staircase/HER exports
    assert "che_affine" in body and "her" in body
    # the computing run has no results yet — an honest "-", not a fake key list
    row = next(ln for ln in body.splitlines() if "pd-rx-computing" in ln)
    assert "che_affine" not in row


def test_pathways_scopes_to_this_candidate(structure, store):
    cand = _seed_structure(store, "pd_cand")
    other = _seed_structure(store, "pt_cand")
    _seed_pathway(
        store, "pd-rx", {"status": "ready", "candidate_ref": cand, "results": {}}
    )
    _seed_pathway(
        store, "pt-rx", {"status": "ready", "candidate_ref": other, "results": {}}
    )
    body = structure.get(id="pd_cand", view="pathways").body
    assert "pd-rx" in body and "pt-rx" not in body


def test_pathways_empty_and_retired(structure, store):
    cand = _seed_structure(store, "pd_cand")
    assert "no pathway" in structure.get(id="pd_cand", view="pathways").body
    pid = _seed_pathway(
        store, "pd-rx", {"status": "ready", "candidate_ref": cand, "results": {}}
    )
    store.retire_ref(pid)
    assert "no pathway" in structure.get(id="pd_cand", view="pathways").body
