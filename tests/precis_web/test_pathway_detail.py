"""``/refs/pathway/<id>`` — the autocatpath reaction-energetics detail page.

``pathway`` is an EXTERNAL plugin kind (the autocatpath bridge): its handler
isn't loaded in this test process (or a dev session), so — unlike most
other kinds — this page is verified WITHOUT ever dispatching ``get()``.
Everything is monkeypatched straight onto the fake ``Store`` (mirroring
``test_quest_dashboard.py``'s pattern), never onto ``runtime`` — proving
the route really is read-only-off-storage for this kind. Modeled on
``tests/precis_web/test_quest_dashboard.py``.
"""

from __future__ import annotations

import json
import math
import re
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from precis.structure import apply_ops
from precis.structure.cell import Cell
from precis.structure.elements import covalent_radius
from precis.structure.scene import Scene
from precis_web.routes.structure import _geom_payload

from .conftest import make_ref


def _seed_pathway(store: Any, *, meta: dict[str, Any], body_text: str | None) -> None:
    """Wire a pathway ref (id=171696) + its linked structures + one
    ``pathway_body`` chunk into the fake store, the same way
    ``test_quest_dashboard.py`` layers extra refs onto ``fetch_refs_by_ids``."""
    pathway_ref = make_ref(
        id=171696,
        kind="pathway",
        slug="q1cand-abc123-rx-def456",
        title="NO -> NH3 on Cu(111): candidate rx pathway",
        meta=meta,
    )
    extra_refs = {171696: pathway_ref}
    if "candidate_ref" in meta:
        extra_refs[meta["candidate_ref"]] = make_ref(
            id=meta["candidate_ref"], kind="structure", title="Cu(111) slab"
        )
    for label, sid in (meta.get("structure_refs") or {}).items():
        extra_refs[sid] = make_ref(id=sid, kind="structure", title=f"{label} adsorbed")

    original_fetch = store.fetch_refs_by_ids

    def fetch(ids: Any, **kw: Any) -> dict[int, Any]:
        out = dict(original_fetch(ids, **kw))
        for i in ids:
            if i in extra_refs:
                out[i] = extra_refs[i]
        return out

    store.fetch_refs_by_ids = fetch  # type: ignore[method-assign]

    blocks = []
    if body_text is not None:
        blocks.append(
            SimpleNamespace(pos=0, text=body_text, chunk_kind="pathway_body", meta={})
        )
    store._conv_blocks[171696] = blocks


def _pd_n_scene(n_frac: tuple[float, float, float]) -> Scene:
    """A minimal Pd(111)-ish slab (two Pd, so the element repeats) plus a
    single adsorbed N (a scene singleton) at ``n_frac`` — the explorer's
    identity-drift worked example (reaction-pathway-explorer.md AC3):
    ``min_distance`` anchored on the singleton N is identity-free/verified;
    ``distance`` anchored on one of the two Pd atoms is not."""
    scene = Scene(cell=Cell(np.eye(3) * 12.0, (True, True, False)))
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "frac": [0.3, 0.0, 0.0]},
            {"op": "add_atom", "element": "N", "frac": list(n_frac)},
        ],
    )
    return scene


#: A 3-state graph (node_link_data shape — precis_pathway.analysis's
#: documented ``nodes``/``links`` field set) shared by the explorer tests.
_GRAPH3: dict[str, Any] = {
    "nodes": [
        {
            "id": "s1",
            "energy": -10.0,
            "energy_std": 0.01,
            "rel_energy": 0.0,
            "low_confidence": False,
        },
        {
            "id": "s2",
            "energy": -9.6,
            "energy_std": 0.02,
            "rel_energy": 0.4,
            "low_confidence": False,
        },
        {
            "id": "s3",
            "energy": -9.9,
            "energy_std": 0.0,
            "rel_energy": 0.1,
            "low_confidence": True,
        },
    ],
    "links": [
        {
            "source": "s1",
            "target": "s2",
            "kind": "reaction",
            "barrier": 0.55,
            "barrier_std": 0.05,
            "delta_e": 0.4,
            "delta_e_std": 0.02,
            "low_confidence": False,
        },
        {
            "source": "s2",
            "target": "s3",
            "kind": "reaction",
            "barrier": 0.2,
            "barrier_std": 0.0,
            "delta_e": -0.3,
            "delta_e_std": 0.0,
            "low_confidence": False,
        },
    ],
}

_GRAPH3_STRUCTURE_REFS = {"s1": 900001, "s2": 900002, "s3": 900003}

_GRAPH3_MEASURES = [
    {"name": "N-Pd", "op": "min_distance", "atoms": ["aN1"], "element": "Pd"},
    {"name": "N-Pd1", "op": "distance", "atoms": ["aN1", "aPd1"]},
    {"name": "bad", "op": "levitate", "atoms": ["aN1"]},
]


def _seed_explorer_scenes(store: Any) -> None:
    """Seed the fake ``structure_load`` (tests/precis_web/conftest.py) with
    one Pd+N scene per ``_GRAPH3_STRUCTURE_REFS`` state — the N drifts a
    little state-to-state so the ``min_distance`` trace actually varies."""
    for i, sid in enumerate(_GRAPH3_STRUCTURE_REFS.values()):
        store.structure_scenes[sid] = _pd_n_scene((0.5, 0.5, 0.15 + 0.02 * i))


def test_pathway_detail_renders_dedicated_page_not_generic(client, runtime) -> None:
    _seed_pathway(
        runtime.store,
        meta={
            "status": "ready",
            "rate_Ea": 0.87,
            "n_structures": 5,
            "slice": 2,
            "produced_by": "autocatpath_explore",
            "autocatpath_version": "0.4.1",
            "candidate_ref": 166700,
            "structure_refs": {"NO": 166708, "NH3": 166716},
            "results": {
                "substrate": "Cu(111)",
                "target": "NH3",
                "backend": "emt",
                "energy_reference": "NO*+3H*",
                "nodes": ["NO*", "NOH*", "NH3*"],
                "edges": [("NO*", "NOH*"), ("NOH*", "NH3*")],
                "warnings": ["low-confidence barrier on edge 2"],
            },
            "config_snapshot_yaml": "backend: emt\nslice: 2\n",
        },
        body_text="## Methods\n\nRelaxed via EMT; barriers via NEB.",
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    # Dedicated-page markers — no equivalent in the generic detail template.
    assert "Energetics" in resp.text
    assert "Linked structures" in resp.text
    assert "Pathway meta" in resp.text
    # Header fields.
    assert "STATUS:ready" in resp.text
    assert "Ea = 0.87 eV" in resp.text
    assert "autocatpath 0.4.1" in resp.text
    # Meta panel.
    assert "Cu(111) slab" in resp.text
    assert "/refs/structure/166700" in resp.text
    # Linked structures.
    assert "NO adsorbed" in resp.text
    assert "NH3 adsorbed" in resp.text
    assert "/refs/structure/166708" in resp.text
    # Energetics summary, defensively pulled from meta['results'].
    assert "Cu(111)" in resp.text
    assert "emt" in resp.text
    assert "low-confidence barrier on edge 2" in resp.text
    # Body chunk, rendered.
    assert "Relaxed via EMT" in resp.text
    # Never dispatched a handler get() for this kind — rendered purely off
    # stored data (the whole point: the autocatpath handler isn't loaded here).
    pathway_calls = [a for v, a in runtime.calls if a.get("kind") == "pathway"]
    assert pathway_calls == []


def test_pathway_detail_sparse_meta_no_500(client, runtime) -> None:
    """A pathway with almost nothing in ``meta`` still renders — every
    field the page reads is optional, per the autocatpath data model."""
    _seed_pathway(runtime.store, meta={}, body_text=None)
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert "none recorded" in resp.text
    assert "no linked adsorbate/intermediate structures recorded" in resp.text
    assert "no computed results recorded yet" in resp.text
    assert "no body chunk recorded for this pathway" in resp.text


# ── Interactive explorer (docs/proposals/reaction-pathway-explorer.md) ──


def test_pathway_detail_ac1_diagram_container_and_node_click_targets(
    client, runtime
) -> None:
    """AC1: a pathway with a graph renders the SVG diagram mount point plus a
    click target for every state node (the diagram's own nodes are drawn by
    client-side JS off the embedded graph JSON — untestable without a
    browser — but the state-list rows are real, static, per-node click
    targets that resolve to the same ``showState(id)``, so their presence +
    count is what this test can assert without executing JS)."""
    _seed_pathway(
        runtime.store,
        meta={"graph": _GRAPH3, "structure_refs": _GRAPH3_STRUCTURE_REFS},
        body_text=None,
    )
    _seed_explorer_scenes(runtime.store)
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert 'id="pw-diagram"' in resp.text
    assert resp.text.count('class="pw-state-node') == len(_GRAPH3["nodes"])
    for node in _GRAPH3["nodes"]:
        assert f'data-state="{node["id"]}"' in resp.text


def test_pathway_detail_ac2_all_state_geoms_and_stepper(client, runtime) -> None:
    """AC2: every linked state's geometry payload is inlined into the page
    JSON (all 3 seeded states reachable, mirroring "the 16 states of 175949
    are all reachable"), and the stepper markup is present."""
    _seed_pathway(
        runtime.store,
        meta={"graph": _GRAPH3, "structure_refs": _GRAPH3_STRUCTURE_REFS},
        body_text=None,
    )
    _seed_explorer_scenes(runtime.store)
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    # One embedded geometry ("xyz" + atom labels) per state.
    assert resp.text.count('"xyz"') == len(_GRAPH3_STRUCTURE_REFS)
    assert resp.text.count('"aN1"') == len(_GRAPH3_STRUCTURE_REFS)
    for sid in _GRAPH3_STRUCTURE_REFS:
        assert f'"{sid}"' in resp.text
    # Stepper — always present (works even if the SVG interactions fail).
    assert 'id="pw-prev"' in resp.text
    assert 'id="pw-next"' in resp.text
    assert "3/3 with geometry" in resp.text


def test_pathway_detail_ac3_identity_flag_only_on_repeated_element_anchor(
    client, runtime
) -> None:
    """AC3: the singleton-anchored ``min_distance`` (N -> nearest Pd) is
    identity-free by construction and renders solid; the ``distance``
    anchored on one of the two (repeated) Pd atoms is unverified and
    renders flagged. The unrecognised third op is skipped with a note."""
    _seed_pathway(
        runtime.store,
        meta={
            "graph": _GRAPH3,
            "structure_refs": _GRAPH3_STRUCTURE_REFS,
            "measures": _GRAPH3_MEASURES,
        },
        body_text=None,
    )
    _seed_explorer_scenes(runtime.store)
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200

    full = resp.text
    # Each card's header/chip row runs from its ``data-measure="…"`` open tag
    # to its ``data-measure-value="…"`` marker (the value row right below the
    # chip) — a precise, name-scoped slice so "N-Pd" doesn't accidentally
    # swallow "N-Pd1"'s markup or vice versa.
    n_pd_block = full[
        full.index('data-measure="N-Pd"') : full.index('data-measure-value="N-Pd"')
    ]
    pd1_block = full[
        full.index('data-measure="N-Pd1"') : full.index('data-measure-value="N-Pd1"')
    ]
    assert "unverified across states" not in n_pd_block
    assert "solid trace" in n_pd_block
    assert "unverified across states" in pd1_block
    assert "solid trace" not in pd1_block

    # Exactly one measure carries the unverified chip (the Pd-anchored one).
    assert resp.text.count("unverified across states") == 1
    # The unknown-op measure never became a card; it's a legible note instead.
    assert 'data-measure="bad"' not in resp.text
    assert "unknown op" in resp.text


def test_pathway_detail_ac4_no_structure_refs_degrades_cleanly(client, runtime) -> None:
    """AC4: a graph with no linked geometry still renders the diagram (off
    ``meta.graph`` alone) and shows "no geometry linked" per state instead
    of 500ing or silently dropping the states section.

    ``structure_refs`` is seeded here (unlike a bare graph-only pathway) so
    every state has an sid the route actually calls ``structure_load(sid)``
    for — but none of those refs carries a seeded scene, so this exercises
    the fake's real-store-parity degrade (``structure_load`` on an unseeded
    ref -> an atom-less ``Scene``, not a ``KeyError``) through the route's
    ``not scene.atoms`` branch, rather than short-circuiting on ``sid is
    None`` before ``structure_load`` is ever called."""
    _seed_pathway(
        runtime.store,
        meta={"graph": _GRAPH3, "structure_refs": _GRAPH3_STRUCTURE_REFS},
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert 'id="pw-diagram"' in resp.text
    # Exact badge text (distinct from the viewer panel's longer "…for this
    # state." fallback string) — one per state row.
    assert resp.text.count("no geometry linked</span>") == len(_GRAPH3["nodes"])
    assert "0/3 with geometry" in resp.text


def _extract_json_array(html: str, marker: str) -> Any:
    """Pull the JSON array embedded after ``marker`` (e.g. ``"measures: "``)
    out of the rendered ``const V = {...}`` blob via bracket-balancing —
    robust to nested ``[``/``]`` inside the payload (a measure's own
    ``atoms`` list, ``per_state`` sub-dicts, …), unlike a naive non-greedy
    regex which would stop at the first inner close-bracket."""
    start = html.index(marker) + len(marker)
    depth = 0
    end = start
    for i in range(start, len(html)):
        ch = html[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    return json.loads(html[start:end])


def _extract_json_object(html: str, marker: str) -> Any:
    """Like :func:`_extract_json_array`, but for a top-level ``{...}`` blob
    (e.g. ``"diagram: "``) — balances whichever bracket char immediately
    follows ``marker`` (``{``/``}`` here) rather than assuming an array."""
    start = html.index(marker) + len(marker)
    open_ch = html[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    end = start
    for i in range(start, len(html)):
        ch = html[i]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    return json.loads(html[start:end])


def _branching_graph(
    extra_nodes: list[str] | None = None,
    extra_links: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """The explorer's worked branching-tree fixture: a main chain
    A->B->C->T plus a B->X->Y branch — mirrors real pathway 175949 (a tree
    rooted at one state with multiple outgoing branches). ``extra_nodes``/
    ``extra_links`` bolt on additional, disconnected structure (e.g. a
    2-cycle unreachable from the root) for the off-path-node case."""
    ids = ["A", "B", "C", "T", "X", "Y", *(extra_nodes or [])]
    nodes = [
        {
            "id": nid,
            "energy": -1.0,
            "energy_std": 0.0,
            "rel_energy": 0.1,
            "low_confidence": False,
        }
        for nid in ids
    ]
    edges = [
        ("A", "B"),
        ("B", "C"),
        ("C", "T"),
        ("B", "X"),
        ("X", "Y"),
        *(extra_links or []),
    ]
    links = [
        {
            "source": u,
            "target": v,
            "kind": "reaction",
            "barrier": 0.1,
            "barrier_std": 0.0,
            "delta_e": 0.0,
            "delta_e_std": 0.0,
            "low_confidence": False,
        }
        for u, v in edges
    ]
    return {"nodes": nodes, "links": links}


def test_pathway_state_ids_follow_target_path_then_branch(client, runtime) -> None:
    """Item 1 (state ordering): the target's own path (A->B->C->T) comes
    first and contiguous; the sibling branch (X->Y off of B) follows after
    — not the graph's raw JSON node-array order."""
    _seed_pathway(
        runtime.store,
        meta={"graph": _branching_graph(), "results": {"target": "T"}},
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    state_ids = _extract_json_array(resp.text, "stateIds: ")
    assert state_ids == ["A", "B", "C", "T", "X", "Y"]


def test_pathway_diagram_paths_target_first_and_offpath_node_still_listed(
    client, runtime
) -> None:
    """Item 2: ``diagram["paths"]`` puts the target's leaf-matching path
    first; a node that's unreachable from any root (here a 2-cycle W<->V,
    disconnected from the main tree — neither has indegree 0, so the DFS
    never visits them) still lands in ``state_ids``, appended after every
    path-covered state."""
    graph = _branching_graph(
        extra_nodes=["W", "V"], extra_links=[("W", "V"), ("V", "W")]
    )
    _seed_pathway(
        runtime.store,
        meta={"graph": graph, "results": {"target": "T"}},
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    diagram = _extract_json_object(resp.text, "diagram: ")
    assert diagram["paths"][0] == ["A", "B", "C", "T"]
    assert diagram["paths"][1] == ["A", "B", "X", "Y"]
    state_ids = _extract_json_array(resp.text, "stateIds: ")
    assert state_ids == ["A", "B", "C", "T", "X", "Y", "W", "V"]


def test_pathway_diagram_paths_no_target_longest_first(client, runtime) -> None:
    """Item 3: with no ``results.target`` at all, path order is deterministic
    off length alone — the longer branch (R->P1->P2->P3) sorts before the
    shorter one (R->Q1)."""
    graph = {
        "nodes": [
            {
                "id": nid,
                "energy": -1.0,
                "energy_std": 0.0,
                "rel_energy": 0.1,
                "low_confidence": False,
            }
            for nid in ("R", "P1", "P2", "P3", "Q1")
        ],
        "links": [
            {
                "source": u,
                "target": v,
                "kind": "reaction",
                "barrier": 0.1,
                "barrier_std": 0.0,
                "delta_e": 0.0,
                "delta_e_std": 0.0,
                "low_confidence": False,
            }
            for u, v in [("R", "P1"), ("P1", "P2"), ("P2", "P3"), ("R", "Q1")]
        ],
    }
    _seed_pathway(runtime.store, meta={"graph": graph}, body_text=None)
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    diagram = _extract_json_object(resp.text, "diagram: ")
    assert diagram["paths"][0] == ["R", "P1", "P2", "P3"]
    assert diagram["paths"][1] == ["R", "Q1"]


def test_pathway_linked_structures_follow_state_order_not_alphabetical(
    client, runtime
) -> None:
    """Scope addition: the "Linked structures" list follows the same
    reaction order as the state list/stepper (``state_ids``), not a plain
    alphabetical sort of the ``structure_refs`` labels — including for a
    label that isn't a graph node at all (out-of-state, appended last no
    matter where it'd alphabetically sort). Targets the branch leaf ``Y``
    (rather than ``T``) specifically so the expected order diverges from
    alphabetical order — a regression a naive ``sorted()`` would pass by
    accident against the letter-for-letter A..T..X..Y fixture."""
    graph = _branching_graph()
    structure_refs = {"C": 900201, "X": 900202, "T": 900203, "0extra": 900204}
    _seed_pathway(
        runtime.store,
        meta={
            "graph": graph,
            "results": {"target": "Y"},
            "structure_refs": structure_refs,
        },
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    state_ids = _extract_json_array(resp.text, "stateIds: ")
    assert state_ids == ["A", "B", "X", "Y", "C", "T"]
    struct_section = resp.text[resp.text.index("Linked structures") :]
    idx = {
        label: struct_section.index(f">{label}</span>")
        for label in ("C", "X", "T", "0extra")
    }
    assert idx["X"] < idx["C"] < idx["T"] < idx["0extra"]


def test_pathway_diagram_and_geom_payloads_carry_expected_keys(client, runtime) -> None:
    """Template smoke (item 5): the rendered page embeds the per-path
    ``diagram.paths`` JSON and every state geometry payload's ranked
    ``neighbors`` list."""
    _seed_pathway(
        runtime.store,
        meta={"graph": _GRAPH3, "structure_refs": _GRAPH3_STRUCTURE_REFS},
        body_text=None,
    )
    _seed_explorer_scenes(runtime.store)
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert '"paths"' in resp.text
    assert '"neighbors"' in resp.text


def test_geom_payload_neighbors_mic_across_periodic_boundary() -> None:
    """Item 4: ``_geom_payload``'s per-atom ``neighbors`` uses the MIC
    distance, not the raw Cartesian gap — two Pd atoms placed 9.6 Å apart
    directly are actually 0.4 Å apart THROUGH the periodic boundary, and
    that's the pair (and distance) that must show up, ranked strongest-first
    against a weaker, non-wrapping third neighbour."""
    scene = Scene(cell=Cell(np.eye(3) * 10.0, (True, True, True)))
    apply_ops(
        scene,
        [
            {
                "op": "add_atom",
                "element": "Pd",
                "frac": [0.03, 0.0, 0.0],
                "label": "PdA",
            },
            {
                "op": "add_atom",
                "element": "Pd",
                "frac": [0.99, 0.0, 0.0],
                "label": "PdB",
            },
            {
                "op": "add_atom",
                "element": "Pd",
                "frac": [0.38, 0.0, 0.0],
                "label": "PdC",
            },
        ],
    )
    payload = _geom_payload(scene, "test")
    by_label = {a["label"]: a for a in payload["atoms"]}
    neighbors = by_label["PdA"]["neighbors"]
    assert [nb["label"] for nb in neighbors] == ["PdB", "PdC"]
    pd_b = neighbors[0]
    assert pd_b["d"] == pytest.approx(0.4, abs=1e-6)
    r0 = covalent_radius("Pd") + covalent_radius("Pd")
    expected_s = math.exp((r0 - pd_b["d"]) / 0.37)
    assert pd_b["s"] == pytest.approx(round(expected_s, 2), abs=0.05)
    # Sorted strongest-first: the 0.4 Å wrap-around pair beats the 3.5 Å
    # direct one.
    assert neighbors[0]["s"] > neighbors[1]["s"]


def test_atom_neighbors_skipped_above_atom_cap() -> None:
    """The O(n²) neighbour pass degrades to empty lists (not a server stall)
    above ``_NEIGHBOR_MAX_ATOMS`` — a pathway page runs it per state, so an
    outsized supercell must short-circuit."""
    from precis_web.routes.structure import _NEIGHBOR_MAX_ATOMS, _atom_neighbors

    scene = Scene(cell=Cell(np.eye(3) * 50.0, (True, True, True)))
    n = _NEIGHBOR_MAX_ATOMS + 1
    ops = [
        {
            "op": "add_atom",
            "element": "Pd",
            "frac": [(i % 10) / 10.0, ((i // 10) % 10) / 10.0, (i // 100) / 10.0],
            "label": f"Pd{i}",
        }
        for i in range(n)
    ]
    apply_ops(scene, ops)
    atoms = [{"label": f"Pd{i}", "element": "Pd"} for i in range(n)]
    _atom_neighbors(scene, atoms)
    assert all(a["neighbors"] == [] for a in atoms)


def test_pathway_paths_dense_dag_enumeration_is_bounded() -> None:
    """A dense (non-tree) DAG — 7 chained diamonds, 2^7 = 128 root→leaf
    paths — must stop at ``_PATHWAY_MAX_PATHS``, not enumerate them all."""
    from precis_web.routes.refs import _PATHWAY_MAX_PATHS, _pathway_paths

    node_ids: list[str] = ["M0"]
    links: list[dict[str, Any]] = []
    for i in range(1, 8):
        prev, mid = f"M{i - 1}", f"M{i}"
        node_ids += [f"A{i}", f"B{i}", mid]
        links += [
            {"source": prev, "target": f"A{i}"},
            {"source": prev, "target": f"B{i}"},
            {"source": f"A{i}", "target": mid},
            {"source": f"B{i}", "target": mid},
        ]
    paths = _pathway_paths(node_ids, links, target=None)
    assert len(paths) == _PATHWAY_MAX_PATHS
    assert all(p[0] == "M0" and p[-1] == "M7" for p in paths)


def test_pathway_detail_duplicate_measure_names_dont_collide(client, runtime) -> None:
    """Regression: two measures that share a display name (both fall back to
    their shared unnamed op, "distance") must not collapse onto one shared
    per-state dict — the fix keys ``per_state``/``verified`` by the
    measure's list INDEX internally, so each keeps its own independently
    computed values even though their display names are identical."""
    measures = [
        {"op": "distance", "atoms": ["aN1", "aPd1"]},
        {"op": "distance", "atoms": ["aN1", "aPd2"]},
    ]
    _seed_pathway(
        runtime.store,
        meta={
            "graph": _GRAPH3,
            "structure_refs": _GRAPH3_STRUCTURE_REFS,
            "measures": measures,
        },
        body_text=None,
    )
    _seed_explorer_scenes(runtime.store)
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200

    # Both entries render as their own card — same display name ("distance",
    # the shared op fallback) doesn't dedupe them into one.
    assert resp.text.count('data-measure="distance"') == 2

    payload = _extract_json_array(resp.text, "measures: ")
    assert len(payload) == 2
    v0 = payload[0]["per_state"]["s1"]["value"]
    v1 = payload[1]["per_state"]["s1"]["value"]
    # Each measure computed its OWN value off its own atom pair — before the
    # fix, both measures shared one name-keyed dict, so the second
    # measure's write silently overwrote the first's for every state.
    assert v0 is not None
    assert v1 is not None
    assert v0 != v1


def test_pathway_detail_null_rel_energy_state_still_boots_viewer(
    client, runtime
) -> None:
    """A state whose graph node carries ``energy_std`` but a still-null
    ``rel_energy`` (computed but not yet available) is exactly the
    combination that used to throw inside the client JS's ``renderDiagram``
    (``nd.rel_energy.toFixed`` on ``null``, guarded only by ``sd > 1e-6``)
    — server-side this must still render 200 with the stepper/3D-viewer
    boot markup intact, and the state gets its own distinct "no energy yet"
    badge rather than a misleading energy-0 plot."""
    graph = {
        "nodes": [
            {
                "id": "s1",
                "energy": -10.0,
                "energy_std": 0.01,
                "rel_energy": 0.0,
                "low_confidence": False,
            },
            {
                "id": "s2",
                "energy": None,
                "energy_std": 0.02,
                "rel_energy": None,
                "low_confidence": False,
            },
        ],
        "links": [
            {
                "source": "s1",
                "target": "s2",
                "kind": "reaction",
                "barrier": None,
                "barrier_std": None,
                "delta_e": None,
                "delta_e_std": None,
                "low_confidence": False,
            },
        ],
    }
    structure_refs = {"s1": 900001, "s2": 900002}
    _seed_pathway(
        runtime.store,
        meta={"graph": graph, "structure_refs": structure_refs},
        body_text=None,
    )
    for sid in structure_refs.values():
        runtime.store.structure_scenes[sid] = _pd_n_scene((0.5, 0.5, 0.15))

    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    # Diagram + stepper + 3D viewer boot markup all present — proves the
    # response completed cleanly rather than 500ing or truncating.
    assert 'id="pw-diagram"' in resp.text
    assert 'id="pw-mol"' in resp.text
    assert 'id="pw-prev"' in resp.text
    assert 'id="pw-next"' in resp.text
    # The null-energy state gets a distinct badge, not silently dropped.
    assert "no energy yet" in resp.text
    # rel_energy: null round-trips into the embedded diagram JSON as JS
    # null, never coerced to 0.
    assert re.search(r'"id":\s*"s2"[^}]*"rel_energy":\s*null', resp.text) is not None
