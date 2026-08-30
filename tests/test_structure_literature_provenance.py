"""structure ↔ literature loop (gripes 161578 + 161577).

Part A: ``view='literature'`` builds a deterministic (no-LLM) paper-search
query from a design's own composition/description and runs it against the
paper corpus in-process via ``PaperHandler.search_hits``.

Part B: paper-provenance links (``rel='cites'`` — or any other registered
relation — to a paper ref) surface in the structure TOC's Provenance:
section, with an optional per-link rationale note (``links.meta['note']``).
('motivated-by' would read better but isn't a seeded relation — links.relation
FKs against the `relations` table, so minting it needs a migration; deferred.)
"""

from __future__ import annotations

import json

import pytest

from precis.dispatch import Hub
from precis.handlers.structure import StructureHandler, paper_provenance_rows
from precis.store import ChunkInsert, Store

_PD_ADSORBATE = json.dumps(
    {
        "cell": {"a": 10.0, "b": 10.0, "c": 24.0, "pbc": [True, True, False]},
        "ops": [
            {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "frac": [0.25, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.25, 0.0]},
            {"op": "add_atom", "element": "Pd", "frac": [0.25, 0.25, 0.0]},
            {"op": "add_atom", "element": "O", "frac": [0.1, 0.1, 0.2]},
        ],
        "description": "pd catalyst slab for CO2 reduction screening",
    }
)


_PD_AG_ADSORBATE = json.dumps(
    {
        "cell": {"a": 10.0, "b": 10.0, "c": 24.0, "pbc": [True, True, False]},
        "ops": [
            {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "frac": [0.25, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.25, 0.0]},
            {"op": "add_atom", "element": "Ag", "frac": [0.25, 0.25, 0.0]},
            {"op": "add_atom", "element": "O", "frac": [0.1, 0.1, 0.2]},
            {"op": "add_atom", "element": "O", "frac": [0.4, 0.4, 0.2]},
        ],
    }
)


@pytest.fixture
def structure(store):
    return StructureHandler(hub=Hub(store=store))


def _seed_paper(
    store: Store, *, slug: str, title: str, body: str, meta: dict | None = None
) -> int:
    """Minimal paper: a ref + one lexically-searchable body block (no
    embedder needed — the handler fixture wires no embedder, so search
    degrades to lexical, same as ``test_search_finds_by_description``)."""
    ref = store.insert_ref(kind="paper", slug=slug, title=title, meta=meta or {})
    store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text=body)])
    return ref.id


# ── Part A: view='literature' ──────────────────────────────────────────


def test_literature_view_builds_query_from_composition_and_description(structure):
    structure.put(id="pd_slab", text=_PD_ADSORBATE)
    resp = structure.get(id="pd_slab", view="literature")
    assert "pd catalyst slab for CO2 reduction screening" in resp.body
    assert "O on Pd" in resp.body
    # the glued formula (a supercell atom count, not a real stoichiometry)
    # is deliberately *not* part of the searched query — see _literature_query.
    assert "O1Pd4" not in resp.body


def test_literature_query_is_deterministic(structure):
    """Same scene + meta -> the same generated query, on two designs
    seeded from the same spec (and re-derived on a second read)."""
    structure.put(id="pd_slab_a", text=_PD_ADSORBATE)
    structure.put(id="pd_slab_b", text=_PD_ADSORBATE)
    ref_a = structure.store.get_ref(kind="structure", id="pd_slab_a")
    ref_b = structure.store.get_ref(kind="structure", id="pd_slab_b")
    scene_a, _ = structure.store.structure_load(ref_a.id)
    scene_b, _ = structure.store.structure_load(ref_b.id)
    q_a = structure._literature_query(scene_a, ref_a)
    q_b = structure._literature_query(scene_b, ref_b)
    assert q_a == q_b
    # re-reading the same design gives the same query again
    scene_a_again, _ = structure.store.structure_load(ref_a.id)
    assert structure._literature_query(scene_a_again, ref_a) == q_a


def test_literature_query_keeps_both_metals_of_a_bimetallic_host(structure):
    """A Pd/Ag bimetallic host (composition-derived, no external provenance)
    must keep BOTH metals in the query — dropping the minority metal (Ag)
    is exactly the bug this recipe exists to avoid for a real bimetallic
    catalyst design (Pd/Ag, Cu/Ni/Pt/Pd, ...). Ordered count-desc then
    alpha: Pd (3 atoms) before Ag (1 atom)."""
    structure.put(id="pdag_slab", text=_PD_AG_ADSORBATE)
    ref = structure.store.get_ref(kind="structure", id="pdag_slab")
    scene, _ = structure.store.structure_load(ref.id)
    query = structure._literature_query(scene, ref)
    assert "Pd" in query and "Ag" in query
    assert "on Pd Ag" in query
    assert "O on Pd Ag" in query  # O is the adsorbate, not a third host metal


def test_literature_query_falls_back_when_external_host_is_an_alloy_formula(
    structure, store
):
    """An external ``surface_composition`` that's an alloy-formula string
    (e.g. "Ag3Pd") names no single composition element symbol — using it
    verbatim would produce a redundant query ("Ag Pd on Ag3Pd"). It must
    fall back to the composition-derived host-metal list instead."""
    structure.put(id="pdag_ext_slab", text=_PD_AG_ADSORBATE)
    ref = structure.store.get_ref(kind="structure", id="pdag_ext_slab")
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO struct_runs "
            "(ref_id, fidelity, on_version, energy, provenance, method) "
            "VALUES (%s, 'dft-tight', 1, -1.0, 'external', %s::jsonb)",
            (ref.id, json.dumps({"surface_composition": "Ag3Pd", "facet": "111"})),
        )
    scene, _ = structure.store.structure_load(ref.id)
    query = structure._literature_query(scene, ref)
    assert "Ag3Pd" not in query
    assert "Pd" in query and "Ag" in query
    assert "(111)" in query  # facet still carries through the fallback


def test_literature_view_surfaces_a_seeded_matching_paper(structure, store):
    structure.put(id="pd_slab", text=_PD_ADSORBATE)
    _seed_paper(
        store,
        slug="pd_co2_paper",
        title="Pd catalysts for CO2 reduction",
        body="a study of pd catalyst slabs with O on Pd for CO2 reduction screening",
    )
    # an unrelated paper must not crowd out the real match
    _seed_paper(
        store,
        slug="unrelated_paper",
        title="Unrelated topic",
        body="a completely unrelated discussion of tectonic plates",
    )
    resp = structure.get(id="pd_slab", view="literature")
    assert "Pd catalysts for CO2 reduction" in resp.body
    assert "Unrelated topic" not in resp.body


def test_literature_view_no_match_gives_a_recovery_hint(structure):
    structure.put(id="pd_slab", text=_PD_ADSORBATE)
    resp = structure.get(id="pd_slab", view="literature")
    assert "no matching papers" in resp.body
    assert "search(kind='paper'" in resp.body


# ── Part B: paper-provenance links + rationale note ────────────────────


def test_toc_surfaces_paper_provenance_link_with_doi(structure, store):
    structure.put(id="pd_slab", text=_PD_ADSORBATE)
    store.insert_ref(
        kind="paper",
        slug="yaghi2023",
        title="Yaghi et al. 2023",
        meta={"doi": "10.1000/yaghi2023"},
    )
    structure.link(id="pd_slab", target="paper:yaghi2023", rel="cites")
    toc = structure.get(id="pd_slab")
    assert "Provenance:" in toc.body
    assert "cites" in toc.body
    assert "Yaghi et al. 2023" in toc.body
    assert "10.1000/yaghi2023" in toc.body


def test_rationale_note_round_trips_to_toc_and_provenance_rows(structure, store):
    structure.put(id="pd_slab", text=_PD_ADSORBATE)
    store.insert_ref(kind="paper", slug="yaghi2023", title="Yaghi et al. 2023")
    structure.link(
        id="pd_slab",
        target="paper:yaghi2023",
        rel="cites",
        note="doped this way because Yaghi 2023 showed X improves conductivity",
    )
    ref = store.get_ref(kind="structure", id="pd_slab")
    rows = paper_provenance_rows(store, ref.id)
    assert len(rows) == 1
    assert rows[0]["note"] == (
        "doped this way because Yaghi 2023 showed X improves conductivity"
    )
    toc = structure.get(id="pd_slab")
    assert "improves conductivity" in toc.body


def test_rationale_note_updates_on_relink(structure, store):
    structure.put(id="pd_slab", text=_PD_ADSORBATE)
    store.insert_ref(kind="paper", slug="yaghi2023", title="Yaghi et al. 2023")
    structure.link(
        id="pd_slab", target="paper:yaghi2023", rel="cites", note="first cut"
    )
    structure.link(
        id="pd_slab",
        target="paper:yaghi2023",
        rel="cites",
        note="revised rationale",
    )
    ref = store.get_ref(kind="structure", id="pd_slab")
    rows = paper_provenance_rows(store, ref.id)
    assert len(rows) == 1
    assert rows[0]["note"] == "revised rationale"


def test_provenance_rows_ignore_non_paper_links(structure, store):
    structure.put(id="pd_slab", text=_PD_ADSORBATE)
    structure.put(id="pd_slab_other", text=_PD_ADSORBATE)
    structure.link(id="pd_slab", target="structure:pd_slab_other", rel="related-to")
    ref = store.get_ref(kind="structure", id="pd_slab")
    assert paper_provenance_rows(store, ref.id) == []
