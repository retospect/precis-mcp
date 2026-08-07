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
from contextlib import contextmanager
from pathlib import Path
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


def test_pathway_detail_state_tracking_and_detail_refresh_js_present(
    client, runtime
) -> None:
    """Browser-feedback fix: the States list tracks the active state
    (scroll-into-view) and the atom/bond detail panel re-renders against the
    newly displayed geometry on every state switch (untestable without a
    browser — this asserts the JS + payload field are actually shipped)."""
    _seed_pathway(
        runtime.store,
        meta={"graph": _GRAPH3, "structure_refs": _GRAPH3_STRUCTURE_REFS},
        body_text=None,
    )
    _seed_explorer_scenes(runtime.store)
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert "refreshDetail(" in resp.text
    assert "scrollIntoView" in resp.text
    assert '"r_cov"' in resp.text


def test_structure_detail_template_ships_refresh_detail_js() -> None:
    """Same fix on ``/refs/structure/{id}`` — the structure detail template
    isn't rendered from this module's fixtures, so read its source directly
    to confirm the copy-pasted ``refreshDetail``/``r_cov`` client logic
    shipped there too (mirror, not shared-JS, per this repo's convention)."""
    tpl = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "precis_web"
        / "templates"
        / "structure"
        / "detail.html.j2"
    ).read_text()
    assert "refreshDetail(" in tpl
    assert "r_cov" in tpl


def test_geom_payload_atoms_carry_covalent_radius() -> None:
    """Every atom dict carries ``r_cov`` — the covalent radius the client
    needs to recompute a bond's length + Pauling strength for a pair not in
    this geometry's own bond graph (a "broken" bond after a state switch)."""
    scene = _pd_n_scene((0.5, 0.5, 0.15))
    payload = _geom_payload(scene, "test")
    assert payload["atoms"]
    for a in payload["atoms"]:
        assert a["r_cov"] == round(covalent_radius(a["element"]), 3)


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


# ── Feedback round 3 (item A-E) ──────────────────────────────────────────


def test_pathway_state_warnings_maps_by_exact_state_prefix() -> None:
    """Item B: a warning's state prefix (the text before ``" seed="``) must
    EXACTLY match a known state id to be attached to a row; INFEASIBLE /
    wrong-site / detached mark ``"bad"`` (red), everything else (e.g.
    "RESEATED ok") is ``"info"`` (amber). A prefix matching nothing on this
    graph, or a warning with no ``" seed="`` marker at all, maps nowhere —
    it stays general-only (still shown in the unchanged Energetics list)."""
    from precis_web.routes.refs import _pathway_state_warnings

    state_ids = ["NO@top", "NH3"]
    warnings = [
        "NO@top seed=0 INFEASIBLE: site swap onto a Cu bridge",
        "NH3 seed=0 RESEATED ok: reseated after relax",
        "unowned-state seed=2 INFEASIBLE: matches nothing on this graph",
        "a general note carrying no seed= marker at all",
    ]
    out = _pathway_state_warnings(warnings, state_ids)
    assert out["NO@top"] == [
        {
            "text": "NO@top seed=0 INFEASIBLE: site swap onto a Cu bridge",
            "severity": "bad",
        }
    ]
    assert out["NH3"] == [
        {"text": "NH3 seed=0 RESEATED ok: reseated after relax", "severity": "info"}
    ]
    assert "unowned-state" not in out
    assert len(out) == 2


def test_pathway_fragment_diff_and_supply_sibling_leaf_resolution() -> None:
    """Item C's pure helpers: ``N+O -> N+H`` adds ``H`` (from the reservoir)
    and drops ``O``; given the worked branching-tree fixture (a target path
    through ``N+H`` and a sibling branch through ``O+H`` -> ``H2O``) plus the
    ``N+O -> O+H`` supply edge, the dropped ``O`` resolves to the ``H2O``
    branch's leaf — the sibling supply edge whose own target's fragments
    cover the dropped one. No sibling supply edge at all falls back to
    ``None`` (the caller's bare "parked in reservoir" wording)."""
    from precis_web.routes.refs import (
        _pathway_fragment_diff,
        _pathway_owner_of,
        _pathway_supply_sibling_leaf,
    )

    added, dropped = _pathway_fragment_diff("N+O", "N+H")
    assert added == frozenset({"H"})
    assert dropped == frozenset({"O"})

    paths = [
        ["NO", "N+O", "N+H", "NH", "NH+H", "NH2", "NH2+H", "NH3"],
        ["NO", "N+O", "O+H", "OH", "OH+H", "H2O"],
    ]
    links = [
        {"source": "N+O", "target": "N+H", "kind": "supply"},
        {"source": "N+O", "target": "O+H", "kind": "supply"},
    ]
    owner_of = _pathway_owner_of(paths)
    assert (
        _pathway_supply_sibling_leaf("N+O", dropped, "N+H", links, owner_of, paths)
        == "H2O"
    )
    # No sibling at all -> no resolution, not a crash.
    assert (
        _pathway_supply_sibling_leaf("N+O", dropped, "N+H", [], owner_of, paths) is None
    )


def test_pathway_diagram_template_uses_pixel_space_half_not_index_space() -> None:
    """Item A regression guard: ``half`` is a PIXEL margin
    (``Math.min(0.34 * (plotW / n), 42)``) — it must be applied AFTER
    ``xScale``, never mixed into step-index space before scaling (the old
    ``xScale(xOf[id] - half)`` / ``xScale(xOf[e.source] + half)`` forms put
    every level bar and hump endpoint thousands of px off-canvas for any
    real ``plotW``)."""
    tpl = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "precis_web"
        / "templates"
        / "refs"
        / "pathway_detail.html.j2"
    ).read_text()
    assert "xScale(xOf[id] - half)" not in tpl
    assert "xScale(xOf[id] + half)" not in tpl
    assert "xScale(xOf[e.source] + half)" not in tpl
    assert "xScale(xOf[e.target] - half)" not in tpl
    # The fixed pixel-space forms — half applied to the already-scaled x.
    assert "const cx = xScale(xOf[id]);" in tpl
    assert "const x0 = cx - half, x1 = cx + half;" in tpl
    assert (
        "const x0 = xScale(xOf[e.source]) + half, x1 = xScale(xOf[e.target]) - half;"
        in tpl
    )


#: The branching-tree fixture item C's docstrings are worked over: a target
#: path NO->N+O->N+H->NH->NH+H->NH2->NH2+H->NH3 plus a branch off N+O via a
#: supply edge to O+H->OH->OH+H->H2O — the N+O->N+H supply edge drops O,
#: which the N+O->O+H sibling supply edge resolves to the H2O branch.
def _pathway_c_node(nid: str) -> dict[str, Any]:
    return {
        "id": nid,
        "energy": -1.0,
        "energy_std": 0.0,
        "rel_energy": 0.1,
        "low_confidence": False,
    }


def _pathway_c_edge(
    src: str, tgt: str, kind: str, barrier: float | None = None
) -> dict[str, Any]:
    return {
        "source": src,
        "target": tgt,
        "kind": kind,
        "barrier": barrier,
        "barrier_std": 0.0,
        "delta_e": 0.0,
        "delta_e_std": 0.0,
        "low_confidence": False,
    }


_PATHWAY_C_NODE_IDS: tuple[str, ...] = (
    "NO",
    "N+O",
    "N+H",
    "NH",
    "NH+H",
    "NH2",
    "NH2+H",
    "NH3",
    "O+H",
    "OH",
    "OH+H",
    "H2O",
)

_PATHWAY_C_GRAPH: dict[str, Any] = {
    "nodes": [_pathway_c_node(nid) for nid in _PATHWAY_C_NODE_IDS],
    "links": [
        _pathway_c_edge("NO", "N+O", "reaction", 0.1),
        _pathway_c_edge("N+O", "N+H", "supply"),
        _pathway_c_edge("N+H", "NH", "reaction", 0.2),
        _pathway_c_edge("NH", "NH+H", "supply"),
        _pathway_c_edge("NH+H", "NH2", "reaction", 0.3),
        _pathway_c_edge("NH2", "NH2+H", "supply"),
        _pathway_c_edge("NH2+H", "NH3", "reaction", 0.4),
        _pathway_c_edge("N+O", "O+H", "supply"),
        _pathway_c_edge("O+H", "OH", "reaction", 0.15),
        _pathway_c_edge("OH", "OH+H", "supply"),
        _pathway_c_edge("OH+H", "H2O", "reaction", 0.25),
    ],
}


def test_pathway_detail_provenance_strip_branch_sections_and_annotations(
    client, runtime
) -> None:
    """Items C + D together: the provenance strip resolves the candidate
    (from ``candidate_ref``) + the owning quest (recovered from this
    pathway's own ``q1cand-…`` slug, ``_seed_pathway``'s fixture) + its
    logbook; the state list groups into the target path's own section
    first (``&rarr; NH3``) then the ``O+H``/``H2O`` branch section (tagged
    "branches from N+O" — the last state the branch shares with the target
    path); and the ``N+O -> N+H`` supply edge's dropped ``O`` resolves to
    "continues in &rarr; H2O" via the sibling ``N+O -> O+H`` supply edge."""
    _seed_pathway(
        runtime.store,
        meta={
            "candidate_ref": 166700,
            "graph": _PATHWAY_C_GRAPH,
            "results": {"substrate": "NO", "target": "NH3"},
        },
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    full = resp.text

    # Provenance strip — candidate + quest (from the "q1cand-…" pathway
    # slug the fixture seeds) + logbook. No dossier seeded -> omitted.
    assert "/refs/structure/166700" in full
    assert "/refs/quest/1" in full
    assert "/refs/quest/1/logbook" in full

    # Branch-grouped sections: target path's section (leaf NH3) precedes
    # the branch section (leaf H2O), which carries the branch subtitle.
    idx_nh3 = full.index("&rarr; NH3")
    idx_h2o = full.index("&rarr; H2O")
    assert idx_nh3 < idx_h2o
    assert "branches from N+O" in full

    # Supply-edge annotation: the dropped O off N+O -> N+H resolves via its
    # sibling supply edge (N+O -> O+H) to the H2O branch.
    assert "+H* from reservoir" in full
    assert "continues in → H2O" in full

    # Stepper JS ships unconditionally (no siblings seeded here -> no
    # rendered stepper control, but the ?state= carry-forward JS is
    # present regardless).
    assert "Candidates (rank #" not in full
    assert "URLSearchParams" in full
    assert "get('state')" in full


class _SiblingConn:
    """Answers ONLY the pathway-siblings SELECT (``_pathway_candidate_
    stepper``) with canned rows; every other query degrades the same as the
    base ``FakeStore``'s ``_FakeConn`` (empty). Mirrors ``test_drafts.py``'s
    ``_WSConn`` pattern for a single-query pool override."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def execute(self, sql: str, params: Any = None) -> Any:
        if "FROM refs" in sql and "kind = 'pathway'" in sql:
            rows = list(self._rows)
        else:
            rows = []
        return SimpleNamespace(
            fetchall=lambda: rows, fetchone=lambda: rows[0] if rows else None
        )

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


class _SiblingPool:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    @contextmanager
    def connection(self):  # type: ignore[no-untyped-def]
        yield _SiblingConn(self._rows)


def test_pathway_detail_candidate_stepper_present_with_siblings(
    client, runtime
) -> None:
    """Item E: three pathway candidates share this reaction's substrate/
    target, ranked by ``rate_Ea`` ascending — the stepper renders the
    rank/N control plus prev/next links to the neighbouring candidates."""
    _seed_pathway(
        runtime.store,
        meta={"results": {"substrate": "NO", "target": "NH3"}},
        body_text=None,
    )
    runtime.store.pool = _SiblingPool(
        [
            (171690, "0.10", "1.0"),
            (171696, "0.50", "2.0"),
            (171700, "0.90", "3.0"),
        ]
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert "Candidates (rank #2 of 3)" in resp.text
    assert "/refs/pathway/171690" in resp.text
    assert "/refs/pathway/171700" in resp.text


def test_pathway_detail_candidate_stepper_absent_without_siblings(
    client, runtime
) -> None:
    """Item E's flip side: the FakeStore's default pool cursor never parses
    SQL (always empty rows), so with no siblings seeded the stepper must be
    silently omitted rather than rendering a broken "rank #1 of 0"."""
    _seed_pathway(
        runtime.store,
        meta={"results": {"substrate": "NO", "target": "NH3"}},
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert "Candidates (rank #" not in resp.text


class _RunJobsConn:
    """Answers ONLY the run-jobs SELECT (``_pathway_run_jobs``) with canned
    rows; every other query degrades the same as the base ``FakeStore``'s
    ``_FakeConn`` (empty). Mirrors ``_SiblingConn`` for a single-query pool
    override."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def execute(self, sql: str, params: Any = None) -> Any:
        if "FROM refs" in sql and "kind = 'job'" in sql:
            rows = list(self._rows)
        else:
            rows = []
        return SimpleNamespace(
            fetchall=lambda: rows, fetchone=lambda: rows[0] if rows else None
        )

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


class _RunJobsPool:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    @contextmanager
    def connection(self):  # type: ignore[no-untyped-def]
        yield _RunJobsConn(self._rows)


def test_pathway_detail_ran_on_shown_when_set(client, runtime) -> None:
    """``meta.ran_on`` renders as plain "ran on <node>" text in the
    provenance strip — not a link, just where the compute happened."""
    _seed_pathway(
        runtime.store,
        meta={"results": {"substrate": "NO", "target": "NH3"}, "ran_on": "spark"},
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert "ran on spark" in resp.text


def test_pathway_detail_ran_on_omitted_when_unset(client, runtime) -> None:
    _seed_pathway(
        runtime.store,
        meta={"results": {"substrate": "NO", "target": "NH3"}},
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert "ran on" not in resp.text


def test_pathway_detail_run_job_links_render_from_query_rows(client, runtime) -> None:
    """``_pathway_run_jobs`` resolves the job(s) whose meta.pathway_ref
    names this pathway into "run job #N (label)" links in the strip."""
    _seed_pathway(
        runtime.store,
        meta={"results": {"substrate": "NO", "target": "NH3"}},
        body_text=None,
    )
    runtime.store.pool = _RunJobsPool(
        [(190001, "autocatpath_seed"), (190000, "autocatpath_aggregate")]
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert "run job #190001 (autocatpath_seed)" in resp.text
    assert "/refs/job/190001" in resp.text
    assert "run job #190000 (autocatpath_aggregate)" in resp.text


def test_pathway_detail_run_jobs_degrade_cleanly_without_pool_support(
    client, runtime
) -> None:
    """The FakeStore's default pool cursor never parses SQL (always empty
    rows) -> no run-job links, no crash — same degrade as the sibling
    stepper (item E)."""
    _seed_pathway(
        runtime.store,
        meta={"results": {"substrate": "NO", "target": "NH3"}},
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert "run job #" not in resp.text


# ── potential lever (slice 3, docs/proposals/pathway-potential-lever.md) ──

#: ``_GRAPH3`` with a per-node ``n_H`` (reservoir H atoms absorbed relative
#: to the root, "R") — s1 is the root, s2/s3 have each absorbed one H.
_GRAPH3_NH: dict[str, Any] = {
    "nodes": [dict(n, n_H=(0 if n["id"] == "s1" else 1)) for n in _GRAPH3["nodes"]],
    "links": _GRAPH3["links"],
}

_ELECTRO_RESULTS: dict[str, Any] = {
    "substrate": "Cu(111)",
    "target": "s3",
    "U_L": -0.42,
    "U_opt": -0.30,
    "span_at_UL": 0.55,
    "span_at_Uopt": 0.48,
    "P_side": 0.12,
    "T": 300.0,
}


def test_pathway_payload_passes_n_h_and_has_n_h_flag_when_present(
    client, runtime
) -> None:
    """Item 1: a node's ``n_H`` (present, possibly 0 on the root) round-trips
    into the diagram JSON, and ``diagram.has_n_h`` flips true — the signal
    the U-slider control strip gates on."""
    _seed_pathway(
        runtime.store,
        meta={"graph": _GRAPH3_NH, "results": {"target": "s3"}},
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    diagram = _extract_json_object(resp.text, "diagram: ")
    assert diagram["has_n_h"] is True
    by_id = {n["id"]: n for n in diagram["nodes"]}
    assert by_id["s1"]["n_H"] == 0
    assert by_id["s2"]["n_H"] == 1
    assert by_id["s3"]["n_H"] == 1


def test_pathway_payload_omits_n_h_and_has_n_h_false_when_absent(
    client, runtime
) -> None:
    """Legacy graph (no node ever carried ``n_H``): every node's ``n_H``
    round-trips as JSON ``null`` and ``diagram.has_n_h`` is false."""
    _seed_pathway(
        runtime.store,
        meta={"graph": _GRAPH3, "structure_refs": _GRAPH3_STRUCTURE_REFS},
        body_text=None,
    )
    _seed_explorer_scenes(runtime.store)
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    diagram = _extract_json_object(resp.text, "diagram: ")
    assert diagram["has_n_h"] is False
    assert all(n["n_H"] is None for n in diagram["nodes"])


def test_pathway_u_slider_rendered_only_when_graph_carries_n_h(client, runtime) -> None:
    """Item 2: the whole U-lever control strip (slider + readout) is present
    when ``n_H`` is on the graph, absent (zero visual change) on a legacy
    graph without it."""
    _seed_pathway(
        runtime.store,
        meta={"graph": _GRAPH3_NH, "results": {"target": "s3"}},
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert 'id="pw-u-slider"' in resp.text
    assert 'id="pw-u-readout"' in resp.text
    assert 'id="pw-ph-input"' in resp.text


def test_pathway_u_slider_absent_on_legacy_graph(client, runtime) -> None:
    _seed_pathway(
        runtime.store,
        meta={"graph": _GRAPH3, "structure_refs": _GRAPH3_STRUCTURE_REFS},
        body_text=None,
    )
    _seed_explorer_scenes(runtime.store)
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert 'id="pw-u-slider"' not in resp.text
    assert 'id="pw-u-lever"' not in resp.text


def test_pathway_u_readout_scalars_rendered_when_present(client, runtime) -> None:
    """Item 3: U_L / U_opt / span(U_opt) / P_side / T from ``meta.results``
    all surface in the readout strip, alongside the "-> U_L" / "-> U_opt"
    snap buttons."""
    _seed_pathway(
        runtime.store,
        meta={"graph": _GRAPH3_NH, "results": _ELECTRO_RESULTS},
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert "U_L = -0.420 V" in resp.text
    assert "U_opt = -0.300 V" in resp.text
    assert "span(U_opt) = 0.480 eV" in resp.text
    assert "P_side = 12.0%" in resp.text
    assert "T = 300.00 K" in resp.text
    assert 'id="pw-u-snap-ul"' in resp.text
    assert 'id="pw-u-snap-uopt"' in resp.text


def test_pathway_u_readout_scalars_omitted_when_absent(client, runtime) -> None:
    """A pathway with ``n_H`` on the graph but no computed electrochemistry
    results yet (early-slice) shows the slider but no scalar readouts/snap
    buttons — and defaults T to 298.15 K rather than omitting it."""
    _seed_pathway(
        runtime.store,
        meta={"graph": _GRAPH3_NH, "results": {"target": "s3"}},
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert 'id="pw-u-slider"' in resp.text
    assert "U_L =" not in resp.text
    assert "U_opt =" not in resp.text
    assert "P_side =" not in resp.text
    assert 'id="pw-u-snap-ul"' not in resp.text
    assert 'id="pw-u-snap-uopt"' not in resp.text
    assert "T = 298.15 K" in resp.text


def test_pathway_fork_probability_js_and_lever_js_shipped(client, runtime) -> None:
    """Item 4 (client-computed, untestable without a browser): the guarded
    fork-probability function and the RHE<->SHE conversion ship on the page,
    and the payload each depends on (``n_H``, numeric ``barrier``,
    ``low_confidence``) is present in the embedded diagram JSON."""
    _seed_pathway(
        runtime.store,
        meta={"graph": _GRAPH3_NH, "results": _ELECTRO_RESULTS},
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert "function computeForkProbabilities(g)" in resp.text
    assert "function sheFromRhe(uRhe, pH, T)" in resp.text
    assert "_baseRelEnergy" in resp.text
    diagram = _extract_json_object(resp.text, "diagram: ")
    for link in diagram["links"]:
        assert "barrier" in link
        assert "low_confidence" in link
    for node in diagram["nodes"]:
        assert "n_H" in node
        assert "low_confidence" in node
