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
import re
from types import SimpleNamespace
from typing import Any

import numpy as np

from precis.structure import apply_ops
from precis.structure.cell import Cell
from precis.structure.scene import Scene

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
