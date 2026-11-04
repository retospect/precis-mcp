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
import subprocess
from contextlib import contextmanager
from html.parser import HTMLParser
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pytest

from precis.store.types import ResolvedHandle
from precis.structure import apply_ops
from precis.structure.cell import Cell
from precis.structure.elements import covalent_radius
from precis.structure.scene import Scene
from precis.utils import handle_registry
from precis_web.routes.structure import _geom_payload
from tests._node import needs_node

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

    store.fetch_refs_by_ids = fetch

    # Slug + pw<id> doors (the public-id redirect route resolves through
    # get_ref/resolve_handle, neither of which the fake scans extra_refs for).
    original_get_ref = store.get_ref

    def get_ref(*, kind: str, id: Any, include_deleted: bool = False) -> Any:
        if kind == "pathway":
            hit = next(
                (
                    r
                    for r in extra_refs.values()
                    if r.kind == kind and (r.slug == id or r.id == id)
                ),
                None,
            )
            if hit is not None:
                return hit
        return original_get_ref(kind=kind, id=id)

    store.get_ref = get_ref

    original_resolve = store.resolve_handle

    def resolve_handle(handle: str, *, conn: Any = None) -> Any:
        parsed = handle_registry.parse(handle)
        if parsed is not None and parsed[:2] == ("pathway", False):
            hit = extra_refs.get(parsed[2])
            if hit is not None:
                return ResolvedHandle(
                    ref_id=int(hit.id),
                    kind="pathway",
                    public_id=hit.slug or str(hit.id),
                )
        return original_resolve(handle, conn=conn)

    store.resolve_handle = resolve_handle

    blocks = []
    if body_text is not None:
        blocks.append(
            SimpleNamespace(ord=0, text=body_text, chunk_kind="pathway_body", meta={})
        )
    store._conv_blocks[171696] = blocks


def _pd_n_scene(n_frac: tuple[float, float, float]) -> Scene:
    """A minimal Pd(111)-ish slab (two Pd, so the element repeats) plus a
    single adsorbed N (a scene singleton) at ``n_frac`` — the explorer's
    identity-drift worked example (the explorer's measure-verification AC):
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


#: A 3-state graph (node_link_data shape — precis.utils.reaction_graph's
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


@pytest.mark.parametrize("matched", ["kinetics", "kinetics_v2"])
def test_pathway_environment_filters_each_kinetics_record(matched: str) -> None:
    from precis_pathway.environment import Environment
    from precis_web.pathway_environment import matching_kinetics_results

    environment = Environment.from_dict({"T": 350})
    other = "kinetics_v2" if matched == "kinetics" else "kinetics"
    meta = {
        "results": {
            matched: {"environment": {"T": 350}, "tof": 1},
            other: {"environment": {"T": 450}, "tof": 999},
        }
    }
    results = matching_kinetics_results(meta, environment)
    assert results[matched] == meta["results"][matched]
    assert other not in results
    assert other in meta["results"]  # read-only selection


def test_pathway_environment_promotes_previous_solve_for_kinetics_panel() -> None:
    from precis_pathway.environment import Environment
    from precis_web.pathway_environment import (
        environment_context,
        matching_kinetics_results,
    )
    from precis_web.pathway_kinetics import kinetics_payload

    environment = Environment.from_dict({"T": 350})
    previous: dict[str, Any] = {"environment": {"T": 350}, "tof_mf": 2.5}
    current: dict[str, Any] = {"environment": {"T": 450}, "tof_mf": 99.0}
    meta: dict[str, Any] = {
        "results": {"kinetics_v2": current, "kinetics_v2_previous": previous}
    }
    context = environment_context(meta, environment)
    assert context is not None
    assert context["lookup"]["status"] == "exact"
    assert context["lookup"]["source"] == "kinetics_v2_previous"
    selected = matching_kinetics_results(meta, environment)
    assert selected["kinetics_v2"] == previous
    payload = kinetics_payload(selected)
    assert payload is not None
    assert payload["ml"]["tof"] == pytest.approx(2.5)
    assert meta["results"]["kinetics_v2"] == current
    assert meta["results"]["kinetics_v2_previous"] == previous

    # A matching current solve keeps priority over its retained predecessor.
    current_environment = Environment.from_dict({"T": 450})
    current_payload = kinetics_payload(
        matching_kinetics_results(meta, current_environment)
    )
    assert current_payload is not None
    assert current_payload["ml"]["tof"] == pytest.approx(99.0)


def test_pathway_previous_solve_conditions_match_selected_panel(
    client, runtime
) -> None:
    """gr559350: both conditions labels must describe the selected solve."""
    from precis_pathway.environment import Environment

    current: dict[str, Any] = {"environment": {"T": 450}, "tof_mf": 99.0}
    previous: dict[str, Any] = {"environment": {"T": 350}, "tof_mf": 2.5}
    meta: dict[str, Any] = {
        "results": {"kinetics_v2": current, "kinetics_v2_previous": previous}
    }
    _seed_pathway(runtime.store, meta=meta, body_text="# Methods\n\nStored methods.")
    environment = Environment.from_dict({"T": 350})
    response = client.get(f"/refs/pathway/171696?{environment.to_url()}")

    assert response.status_code == 200
    match = re.search(
        r'<script type="application/json" id="kinetics-data">(.*?)</script>',
        response.text,
        re.S,
    )
    assert match is not None
    assert json.loads(match.group(1))["ml"]["tof"] == pytest.approx(2.5)
    assert response.text.count("kinetics at T = 350 K") == 2
    assert "## Conditions" in response.text
    assert "Kinetics: T = 350 K" in response.text
    assert "T = 450 K" not in response.text
    assert meta["results"]["kinetics_v2"] == current
    assert meta["results"]["kinetics_v2_previous"] == previous


@pytest.mark.parametrize("source", ["kinetics_sweep", "kinetics_sweep_previous"])
def test_pathway_environment_displays_matching_sweep_rate(
    source: str, client, runtime
) -> None:
    from precis_pathway.environment import Environment

    environment = Environment.from_dict({"T": 350, "gases": {"inputs": {"NO": 1}}})
    meta: dict[str, Any] = {
        "results": {
            "kinetics_v2": {
                "environment": {"T": 450, "gases": {"inputs": {"NO": 1}}},
                "tof_mf": 999,
            },
            source: {
                "points": [
                    {"T": 350, "p_total": 1, "pressures": {"NO": 1}, "tof_mf": 2}
                ]
            },
        }
    }
    _seed_pathway(runtime.store, meta=meta, body_text=None)
    response = client.get(f"/refs/pathway/171696?{environment.to_url()}")
    assert response.status_code == 200
    assert 'data-status="exact"' in response.text
    assert 'id="pw-environment-sweep-rate"' in response.text
    assert "Stored sweep TOF (MF): 2 s⁻¹" in response.text
    assert (
        "previous sweep" if source.endswith("_previous") else "current sweep"
    ) in response.text
    assert not any(args.get("kind") == "pathway" for _, args in runtime.calls)


@needs_node
@pytest.mark.parametrize(
    ("template", "function"),
    [
        ("refs/pathway_detail.html.j2", "pwQueryString"),
        ("pourbaix/detail.html.j2", "bankParams"),
    ],
)
def test_environment_url_preserves_missing_coordinates_until_edited(
    tmp_path: Path, template: str, function: str
) -> None:
    from precis_pathway.environment import Environment

    text = (
        Path(__file__).resolve().parents[2] / "src/precis_web/templates" / template
    ).read_text(encoding="utf-8")
    match = re.search(rf"  function {function}\(\) \{{.*?\n  \}}", text, re.S)
    assert match is not None
    source = match.group(0)
    observed = _node_json(
        tmp_path,
        """
const linkedEnvironment = {T:350}, V = {environment:linkedEnvironment};
const currentU = -0.5, phInput = {value:'7'}, selected = null, selectedPath = null;
const uEl = {value:'-0.5', dataset:{default:'0'}}, phEl = {value:'7', dataset:{default:'0'}};
const resetT = {checked:true}, near = (a,b) => Math.abs(a-b) < 1e-9;
const layerSelection = {}, pwWriteLayers = () => {};
const filterState = {}, PathwayFilters = {write: () => {}};
let environmentUDirty = false, environmentPhDirty = false;
"""
        + source
        + f"\nconst read = () => Object.fromEntries(new URLSearchParams({function}()));"
        + """
const original = read();
environmentUDirty = true;
const changedU = read();
environmentUDirty = false;
environmentPhDirty = true;
const changedPh = read();
console.log(JSON.stringify([original, changedU, changedPh]));
""",
    )
    assert (
        Environment.from_url(observed[0]).basis
        == Environment.from_dict({"T": 350}).basis
    )
    assert Environment.from_url(observed[1]).electrochemical == {"U_vs_RHE": -0.5}
    assert Environment.from_url(observed[2]).electrochemical == {"pH": 7}
    assert all("u" not in params and "ph" not in params for params in observed)


def test_pathway_environment_link_reports_missing_result(client, runtime) -> None:
    from precis_pathway.environment import Environment

    _seed_pathway(runtime.store, meta={"status": "ready"}, body_text=None)
    environment = Environment.from_config(
        {
            "environment": {
                "T": 350,
                "p_total": 10,
                "gases": {"inputs": {"NO": 1}, "outputs": "far"},
                "electrochemical": {"U_vs_RHE": -0.3, "pH": 7, "ion_conc_M": 0.1},
            }
        }
    )
    response = client.get("/refs/pathway/171696?" + environment.to_url())
    assert response.status_code == 200
    assert 'id="pw-environment-status"' in response.text
    assert 'data-status="none"' in response.text
    assert "No stored result at this point" in response.text
    assert 'id="kinetics-data"' not in response.text
    assert "qs.set('env', JSON.stringify(env))" in response.text


def test_pathway_environment_link_refuses_bad_values(client, runtime) -> None:
    _seed_pathway(runtime.store, meta={"status": "ready"}, body_text=None)
    response = client.get("/refs/pathway/171696", params={"env": '{"T": -1}'})
    assert response.status_code == 400
    assert "next" in response.text.lower()


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


def test_pathway_slug_and_pw_handle_redirect_to_numeric_reader(client, runtime) -> None:
    """gr553326: ``/refs/pathway/<slug>`` 422'd — the detail route keys on
    ``ref_id: int``, so a slug (or the ``pw<id>`` handle the reads now
    print) never reached the page. The public-id door resolves both and
    303s to the numeric reader."""
    _seed_pathway(runtime.store, meta={}, body_text=None)

    resp = client.get("/refs/pathway/q1cand-abc123-rx-def456", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/refs/pathway/171696"

    resp = client.get("/refs/pathway/pw171696", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/refs/pathway/171696"

    assert client.get("/refs/pathway/no-such-slug").status_code == 404


# ── Orphaned-pathway-stub status banner (no graph -> clear state) ───────


def test_pathway_detail_computing_no_graph_shows_in_flight_banner(
    client, runtime
) -> None:
    """A still-``computing`` stub with no graph yet renders the "in
    flight/queued" banner instead of a bare empty-diagram message — the
    orphaned-pathway-stub sweep (``precis.quest.loop.
    _reconcile_orphaned_pathways``) is what eventually resolves a dead one,
    but a live one should read as "still running", not blank."""
    _seed_pathway(
        runtime.store,
        meta={"status": "computing", "candidate_ref": 166700},
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert "STATUS:computing" in resp.text
    assert "barrier computation in flight/queued" in resp.text
    assert "no energetics graph recorded for this pathway yet" not in resp.text


def test_pathway_detail_failed_no_graph_shows_reason_banner(client, runtime) -> None:
    """A pathway the sweep stamped ``failed`` renders its
    ``failed_reason`` instead of a blank diagram."""
    _seed_pathway(
        runtime.store,
        meta={
            "status": "failed",
            "failed_reason": "orphaned (no live compute)",
            "candidate_ref": 166700,
        },
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert "STATUS:failed" in resp.text
    assert "compute failed" in resp.text
    assert "orphaned (no live compute)" in resp.text


def test_pathway_detail_superseded_no_graph_links_the_successor(
    client, runtime
) -> None:
    """A ``superseded`` pathway (a content-key change re-dispatch) links to
    its successor rather than rendering blank."""
    _seed_pathway(
        runtime.store,
        meta={"status": "superseded", "superseded_by": 171700},
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert "superseded by a fresher run" in resp.text
    assert "/refs/pathway/171700" in resp.text


# ── Interactive explorer (clickable diagram + 3D cell + measures) ───────


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


def _flux_graph() -> dict[str, Any]:
    """The gr553482 worked example as a ``meta.graph``: shared step R->S,
    then two parallel routes to NH3 — the WEAKER branch (through B) is
    enumerated first so the flux sort has to move it."""
    edges = [("R", "S"), ("S", "B"), ("B", "NH3"), ("S", "A"), ("A", "NH3")]
    return {
        "nodes": [
            {
                "id": nid,
                "energy": -1.0,
                "energy_std": 0.0,
                "rel_energy": 0.1,
                "low_confidence": False,
            }
            for nid in ("R", "S", "B", "A", "NH3")
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
            for u, v in edges
        ],
    }


def _flux_record() -> dict[str, Any]:
    """The matching ``kinetics.solve`` record: R->S carries 10, the A and
    B branches split it 7:3, the NH3 desorption closes the cycle at 10 —
    so the A route holds 70% of the TOF and B 30%."""

    def t(a: str, b: str, r: float, **kw: Any) -> dict[str, Any]:
        return {
            "name": f"{a}->{b}",
            "from": a,
            "to": b,
            "kind": "step",
            "net_rate": r,
            **kw,
        }

    return {
        "conditions": {"temperature": 500.0, "pressures": {"N2": 1.0}},
        "product": "NH3",
        "root": "R",
        "tof": 10.0,
        "warnings": [],
        "transitions": [
            t("R", "S", 10.0),
            t("S", "A", 7.0),
            t("A", "NH3", 7.0),
            t("S", "B", 3.0),
            t("B", "NH3", 3.0),
            t("NH3", "R", 10.0, kind="desorption", gas="NH3"),
        ],
    }


def test_pathway_route_flux_shares_sorted_with_dominant(client, runtime) -> None:
    """gr553482: the picker's ``route_flux`` payload maps the greedy
    decomposition onto the enumerated routes — every route's TOF share,
    the dominant one flagged, display order by yield (the stronger A
    route jumps the earlier-enumerated B route) — plus the note saying
    what a share means."""
    _seed_pathway(
        runtime.store,
        meta={
            "graph": _flux_graph(),
            "results": {"target": "NH3", "kinetics": _flux_record()},
        },
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    diagram = _extract_json_object(resp.text, "diagram: ")
    assert diagram["paths"] == [["R", "S", "B", "NH3"], ["R", "S", "A", "NH3"]]
    rf = diagram["route_flux"]
    small, big = rf["routes"]
    assert small["share"] == pytest.approx(0.3) and not small["dominant"]
    assert big["share"] == pytest.approx(0.7) and big["dominant"]
    assert big["log10"] == pytest.approx(math.log10(7.0))
    assert rf["order"] == [1, 0]
    assert rf["remainder_share"] == pytest.approx(0.0)
    # the mandated one-liner, with the conditions the flux is at
    assert "kinetics solves all routes together" in resp.text
    assert "shares are flux at the stated conditions" in resp.text
    assert "(T = 500 K)" in resp.text
    assert 'id="pw-route-flux-environment"' not in resp.text


def test_pathway_moved_environment_labels_original_route_flux(client, runtime) -> None:
    from precis_pathway.environment import Environment

    environment = Environment.from_dict(
        {
            "T": 500,
            "gases": {"inputs": {"N2": 1}},
            "electrochemical": {"U_vs_RHE": -0.3, "pH": 7},
        }
    )
    record = {**_flux_record(), "environment": environment.to_dict()}
    _seed_pathway(
        runtime.store,
        meta={"graph": _flux_graph(), "results": {"target": "NH3", "kinetics": record}},
        body_text=None,
    )
    response = client.get("/refs/pathway/171696?" + environment.to_url())
    assert response.status_code == 200
    notice = re.search(r'<p id="pw-route-flux-environment".*?</p>', response.text, re.S)
    assert notice is not None
    assert "initial stored environment" in notice.group(0)
    assert "T = 500" in notice.group(0)
    assert "-0.3 V vs RHE" in notice.group(0)
    assert "pH = 7" in notice.group(0)
    assert "fluxEnvironment.classList.remove('hidden')" in response.text


def test_pathway_no_kinetics_renders_picker_as_before(client, runtime) -> None:
    """gr553482: no usable kinetics record -> no ``route_flux`` key and no
    share note — the legend is byte-identical to the pre-shares one."""
    _seed_pathway(
        runtime.store,
        meta={"graph": _flux_graph(), "results": {"target": "NH3"}},
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    diagram = _extract_json_object(resp.text, "diagram: ")
    assert "route_flux" not in diagram
    assert "kinetics solves all routes together" not in resp.text


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
    assert "scrollRowInList" in resp.text
    # the slider re-render must not scroll the page (highlightState scrolls
    # only when asked to), and the selection is echoed above the diagram
    assert "row.scrollIntoView" not in resp.text
    assert 'id="pw-selection"' in resp.text
    assert 'id="pw-diagram-key"' in resp.text
    assert "lowConfidenceReason" in resp.text
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
    ).read_text(encoding="utf-8")
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
    ).read_text(encoding="utf-8")
    assert "xScale(xOf[id] - half)" not in tpl
    assert "xScale(xOf[id] + half)" not in tpl
    assert "xScale(xOf[e.source] + half)" not in tpl
    assert "xScale(xOf[e.target] - half)" not in tpl
    assert "xScale(xOf[s.src] + half)" not in tpl
    assert "xScale(xOf[s.tgt] - half)" not in tpl
    # The fixed pixel-space forms — half applied to the already-scaled x.
    assert "const cx = xScale(xOf[id]);" in tpl
    assert "const x0 = cx - half, x1 = cx + half;" in tpl
    assert (
        "const x0 = xScale(xOf[s.src]) + half, x1 = xScale(xOf[s.tgt]) - half;" in tpl
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
    # slug the fixture seeds, rendered by its universal handle, gr549584
    # item 2) + logbook + the candidate structure's fisheye (gr549580 —
    # never a dossier draft link).
    assert "/refs/structure/166700" in full
    assert "/refs/quest/1" in full
    assert "/refs/quest/1/logbook" in full
    assert 'quest <a href="/refs/quest/1"' in full
    assert ">qu1</a>" in full
    assert "/eye/st166700" in full
    assert ">fisheye</a>" in full
    assert "dossier" not in full

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
    def connection(self):
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
    def connection(self):
        yield _RunJobsConn(self._rows)


def test_pathway_detail_ran_on_shown_when_set(client, runtime) -> None:
    """``meta.ran_on`` renders as "ran on <node>" in the provenance strip
    (only when NO run-jobs resolved — per-job hosts carry it otherwise),
    linked to the host's services/config page (gr549584 item 4 — the
    machine-kind page proposed in the linked gripe doesn't exist yet;
    ``/status?tab=services&host=`` is the page that best shows a host)."""
    _seed_pathway(
        runtime.store,
        meta={"results": {"substrate": "NO", "target": "NH3"}, "ran_on": "spark"},
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert "ran on" in resp.text
    assert "/status?tab=services&amp;host=spark" in resp.text or (
        "/status?tab=services&host=spark" in resp.text
    )


def test_pathway_detail_ran_on_hidden_when_jobs_carry_the_host(client, runtime) -> None:
    """gr549584 comment 1: with run-jobs present, the one page-level
    ``meta.ran_on`` value is suppressed — the host rides each job chip
    instead (seed 0 and seed 1 may have run on different machines)."""
    _seed_pathway(
        runtime.store,
        meta={"results": {"substrate": "NO", "target": "NH3"}, "ran_on": "pollux"},
        body_text=None,
    )
    runtime.store.pool = _RunJobsPool(
        [
            (
                190002,
                "autocatpath_seed",
                "1",
                "spark",
                "322.1",
                "STATUS:succeeded",
                "autocatpath_seed: seed=1 model=mace:medium: 29 state(s), 8 warning(s).",
            ),
        ]
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert "ran on" not in resp.text
    assert "seed 1 on spark" in resp.text


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
    names this pathway into per-job chips — ``seed <n> on <host>`` /
    ``aggregate on <host>`` labels (gr549584 comment 1), each with a
    mouseover naming what the job is, its status and its summary line."""
    _seed_pathway(
        runtime.store,
        meta={"results": {"substrate": "NO", "target": "NH3"}},
        body_text=None,
    )
    runtime.store.pool = _RunJobsPool(
        [
            (
                190001,
                "autocatpath_seed",
                "1",
                "spark",
                "322.1",
                "STATUS:succeeded",
                "autocatpath_seed: seed=1 model=mace:medium: "
                "29 state(s), 8 warning(s).",
            ),
            (190000, "autocatpath_aggregate", None, "pollux", "40.0", None, None),
        ]
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert "/refs/job/190001" in resp.text
    assert ">seed 1 on spark<" in resp.text
    assert ">aggregate on pollux<" in resp.text
    assert "job #190001 (autocatpath_seed)" in resp.text
    assert "one seed&#39;s simulation run" in resp.text
    assert "succeeded — a finished record" in resp.text
    assert "322.1 s on spark" in resp.text
    assert "29 state(s), 8 warning(s)" in resp.text
    assert "combines the seeds&#39; partial results" in resp.text


def test_pathway_detail_fisheye_falls_back_to_pathway_without_candidate(
    client, runtime
) -> None:
    """gr549580: a candidate-less pathway's provenance strip links its OWN
    fisheye (``/eye/pw<id>``) instead of a structure's — and ``/eye/pw<id>``
    links back to this page via its "open in its reader" link
    (``ref_url``'s generic-detail fallback)."""
    from precis_web.ref_urls import ref_url

    _seed_pathway(
        runtime.store,
        meta={"results": {"substrate": "NO", "target": "NH3"}},
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert "/eye/pw171696" in resp.text
    assert "dossier" not in resp.text
    # the eye's reader link for a pw handle is this very page
    assert ref_url("pathway", 171696) == "/refs/pathway/171696"


def test_pathway_detail_dossier_link_never_renders(client, runtime) -> None:
    """gr549580 acceptance: nothing in the strip links a ``dossier`` draft
    — not even when the quest has one (the route no longer looks it up)."""
    _seed_pathway(
        runtime.store,
        meta={"candidate_ref": 166700, "results": {"substrate": "NO", "target": "NH3"}},
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert ">dossier<" not in resp.text
    assert "dossier_url" not in resp.text


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


# ── calculator identity (gripe 161576 remainder) ─────────────────────────


class _StructRunsConn:
    """Answers ONLY the calc-identity SELECT
    (``_pathway_state_calc_identities``) with canned rows; every other query
    degrades the same as the base ``FakeStore``'s ``_FakeConn`` (empty).
    Mirrors ``_RunJobsConn`` for a single-query pool override."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def execute(self, sql: str, params: Any = None) -> Any:
        rows = list(self._rows) if "FROM struct_runs" in sql else []
        return SimpleNamespace(
            fetchall=lambda: rows, fetchone=lambda: rows[0] if rows else None
        )

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


class _StructRunsPool:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    @contextmanager
    def connection(self):
        yield _StructRunsConn(self._rows)


def test_pathway_state_panel_shows_calc_identity_when_run_data_exists(
    client, runtime
) -> None:
    """Each state's panel row gets a compact ``calc:`` line — the same
    identity string ``view='atom'`` shows on the structure handler
    (``format_calc_identity``, shared) — a computed run's model+params
    digest, or an external import's method fingerprint + marker. A state
    with no run row (s3 here) gets none — never invented."""
    _seed_pathway(
        runtime.store,
        meta={"graph": _GRAPH3, "structure_refs": _GRAPH3_STRUCTURE_REFS},
        body_text=None,
    )
    _seed_explorer_scenes(runtime.store)
    runtime.store.pool = _StructRunsPool(
        [
            (900001, "computed", "mace_mp", {"steps": 200}, None),
            (900002, "external", None, None, {"functional": "PBE", "cutoff_eV": 500}),
        ]
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert "calc: mace_mp (steps=200)" in resp.text
    assert "calc: PBE/500eV — external" in resp.text
    # exactly the two seeded rows — s3 (900003) has none.
    assert resp.text.count("calc:") == 2


def test_pathway_state_panel_calc_identity_absent_without_a_run(
    client, runtime
) -> None:
    """The default fake pool answers every query empty (no struct_runs rows
    at all) -> no ``calc:`` line anywhere, no crash."""
    _seed_pathway(
        runtime.store,
        meta={"graph": _GRAPH3, "structure_refs": _GRAPH3_STRUCTURE_REFS},
        body_text=None,
    )
    _seed_explorer_scenes(runtime.store)
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert "calc:" not in resp.text


# ── potential lever (the explorer's CHE U-slider) ─────────────────────────

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
    assert "(bounded;" not in resp.text


def test_pathway_u_opt_outside_the_swept_window_is_bounded(client, runtime) -> None:
    """gr489166 (Reto 2026-10-15: "bound"): an engine U_opt outside the
    swept -1.0…+0.5 V window (prod pw483780: -4.24 V) is gas evolution, no
    operating point. The readout shows it at the nearest edge with the
    engine's value beside it, drops span(U_opt) (taken at the raw point),
    and the snap button carries the bounded value."""
    _seed_pathway(
        runtime.store,
        meta={"graph": _GRAPH3_NH, "results": {**_ELECTRO_RESULTS, "U_opt": -4.24}},
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert "U_opt = -1.000 V" in resp.text
    assert "(bounded; engine -4.24 V)" in resp.text
    assert "span(U_opt)" not in resp.text
    assert 'id="pw-u-snap-uopt" data-u="-1.0"' in resp.text
    assert "setU(parseFloat(snapUopt.dataset.u), 'U_opt')" in resp.text


def test_pathway_u_opt_inside_the_window_is_not_bounded(client, runtime) -> None:
    _seed_pathway(
        runtime.store,
        meta={"graph": _GRAPH3_NH, "results": _ELECTRO_RESULTS},
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert 'id="pw-u-snap-uopt" data-u="-0.3"' in resp.text
    assert "(bounded;" not in resp.text


def test_pathway_explorer_defaults_ph7_and_u_fallback(client, runtime) -> None:
    """gr549591: the explorer opens at pH 7 (always) and at the engine's
    bounded U_opt when the route isn't blocked — else the fixed fallback;
    the readout says which it used, never a bare 0 V."""
    _seed_pathway(
        runtime.store,
        meta={"graph": _GRAPH3_NH, "results": _ELECTRO_RESULTS},
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert 'id="pw-ph-input" value="7"' in resp.text
    # U_opt = -0.30, route unblocked -> the default IS U_opt
    assert "'default: U_opt'" in resp.text
    assert "{ v: +_uOpt, note: 'default: U_opt' }" in resp.text
    # fixed fallback for the no-U_opt / blocked case
    assert "PW_U_FALLBACK = -0.5" in resp.text
    # explicit u=/ph= in the URL still wins, and pwQueryString always
    # writes BOTH params so an explicit pH 0 / U 0 survives a copied link
    assert "qs.set('u', String(+currentU.toFixed(2)))" in resp.text
    assert "Number.isFinite(ph)) { qs.set('ph'" in resp.text
    assert "uNote = '';  // an explicit u= beats either default" in resp.text


def test_pathway_explorer_u_default_blocked_route_uses_fallback(
    client, runtime
) -> None:
    """A blocked route (``blocked_by`` non-empty) must not trust U_opt as
    the default — the fixed fallback applies, and the page says so."""
    _seed_pathway(
        runtime.store,
        meta={"graph": _GRAPH3_NH, "results": _ELECTRO_RESULTS},
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert "_uBlocked" in resp.text
    assert "default: fixed " in resp.text


@needs_node
def test_pathway_diagram_level_bands_carry_path_membership(
    client, runtime, tmp_path: Path
) -> None:
    """gr549590: every click band (level, floor placeholder, shoulder)
    carries ``data-paths`` so applyPathSelection can drop pointer-events
    on elements outside the isolated path — a faded state can no longer
    steal clicks meant for the selected path's bar."""
    _seed_parked(runtime, _parked_graph())
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    svg = _render_diagram_svg(tmp_path, resp.text)
    bands = re.findall(r'<rect class="pw-svg-node"([^>]*)>', svg)
    assert bands
    assert all('data-paths="|' in b for b in bands)
    # the faded-elements click guard ships on the page
    assert "elm.style.pointerEvents = on ? '' : 'none'" in resp.text
    # nearest-band click resolution ships too
    assert "rect.pw-svg-node" in resp.text


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


# ── tier-ladder UX (screening -> neb -> verify) ─────────────────────────


def _seed_tier_sibling(runtime: Any, sibling_ref: Any, *, refines_from: int) -> None:
    """Wire a second ``pathway`` ref into the fake store as ``refines_from``'s
    outgoing ``refines`` target (mirrors ``precis.quest.compute._link_refines``
    — a verify pathway's own outgoing edge names its parked sibling), by
    layering onto ``fetch_refs_by_ids`` (same pattern as ``_seed_pathway``)
    and monkeypatching ``links_for`` directly (the fake's default answers
    only ``cites``)."""
    orig_fetch = runtime.store.fetch_refs_by_ids

    def fetch(ids: Any, **kw: Any) -> dict[int, Any]:
        out = dict(orig_fetch(ids, **kw))
        if sibling_ref.id in ids:
            out[sibling_ref.id] = sibling_ref
        return out

    runtime.store.fetch_refs_by_ids = fetch

    def links_for(
        ref_id: Any, *, direction: str = "both", relation: Any = None
    ) -> list[Any]:
        if ref_id == refines_from and direction == "out" and relation == "refines":
            return [SimpleNamespace(src_ref_id=refines_from, dst_ref_id=sibling_ref.id)]
        return []

    runtime.store.links_for = links_for


def test_pathway_tier_chip_explicit_meta_tier(client, runtime) -> None:
    """Item 1: an explicit ``meta.tier`` renders verbatim as the chip text."""
    _seed_pathway(runtime.store, meta={"tier": "verify"}, body_text=None)
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert re.search(r">\s*verify\s*</span>", resp.text)


def test_pathway_tier_chip_inferred_screening(client, runtime) -> None:
    """Item 1: no ``meta.tier`` but ``results.screening is True`` infers
    ``screening`` (so a legacy pathway still labels sensibly)."""
    _seed_pathway(runtime.store, meta={"results": {"screening": True}}, body_text=None)
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert re.search(r">\s*screening\s*</span>", resp.text)


def test_pathway_tier_chip_inferred_verify_via_coadsorbed_template(
    client, runtime
) -> None:
    """Item 1: ``results.template == 'coadsorbed'`` infers ``verify``."""
    _seed_pathway(
        runtime.store, meta={"results": {"template": "coadsorbed"}}, body_text=None
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert re.search(r">\s*verify\s*</span>", resp.text)


def test_pathway_tier_chip_inferred_neb_default(client, runtime) -> None:
    """Item 1: neither signal at all falls back to ``neb`` — today's
    straight-to-NEB shape, unchanged."""
    _seed_pathway(runtime.store, meta={}, body_text=None)
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert re.search(r">\s*neb\s*</span>", resp.text)


def test_pathway_tier_toggle_and_delta_via_refines_link(client, runtime) -> None:
    """Item 2: a verify pathway's own outgoing ``refines`` edge names its
    parked sibling — the toggle renders low->high fidelity ("neb" before
    "verified", each tier under its own word so two non-verify rungs never
    read as an identical "screen | screen"), the sibling side links out, and
    (both sides carrying a barrier figure) a "verified − neb" delta line
    renders too."""
    _seed_pathway(
        runtime.store,
        meta={"tier": "verify", "rate_Ea": 0.96},
        body_text=None,
    )
    sibling_ref = make_ref(
        id=171700,
        kind="pathway",
        title="parked sibling",
        meta={"tier": "neb", "rate_Ea": 0.84},
    )
    _seed_tier_sibling(runtime, sibling_ref, refines_from=171696)

    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert '<span class="font-semibold text-fg-subtle">view:</span>' in resp.text
    assert ">neb<" in resp.text
    assert "verified" in resp.text
    assert "/refs/pathway/171700" in resp.text
    assert "verified − neb: +0.12 eV" in resp.text


def test_pathway_tier_toggle_absent_without_sibling(client, runtime) -> None:
    """Item 2's flip side: no ``refines`` link either way and no other-tier
    pathway shares this one's (absent) candidate — the toggle is omitted
    entirely, not rendered broken."""
    _seed_pathway(runtime.store, meta={"tier": "verify"}, body_text=None)
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert '<span class="font-semibold text-fg-subtle">view:</span>' not in resp.text


# ── screening-tier honesty banner (gr559345) ────────────────────────────

_SCREENING_BANNER_TEXT = (
    "screening tier: no measured barriers; estimates shown are BEP "
    "&alpha;&middot;&Delta;E+&beta; &plusmn; band."
)


def test_pathway_tier_banner_fires_screening_no_measured_barrier(
    client, runtime
) -> None:
    """gr559345: screening tier, every link's ``barrier_source`` is "bep"
    or "none" — the honesty banner renders Reto's wording verbatim above
    the diagram."""
    graph = {
        "nodes": [{"id": "A", "rel_energy": 0.0}, {"id": "B", "rel_energy": 0.2}],
        "links": [
            {
                "source": "A",
                "target": "B",
                "kind": "reaction",
                "barrier": None,
                "delta_e": 0.2,
            }
        ],
    }
    _seed_pathway(
        runtime.store,
        meta={
            "tier": "screening",
            "graph": graph,
            "results": {
                "edges": [{"name": "A->B", "bep": {"alpha": 0.8, "beta": 0.05}}]
            },
        },
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert _SCREENING_BANNER_TEXT in resp.text


def test_pathway_tier_banner_absent_with_a_measured_edge(client, runtime) -> None:
    """The flip side: still screening tier, but one link carries a
    measured (``barrier_source == "neb"``) edge — the per-edge hover/hump
    already shows the distinction, so the page-level banner would be
    redundant and does not render."""
    graph = {
        "nodes": [
            {"id": "A", "rel_energy": 0.0},
            {"id": "B", "rel_energy": 0.2},
            {"id": "C", "rel_energy": 0.4},
        ],
        "links": [
            {"source": "A", "target": "B", "kind": "reaction", "barrier": None},
            {"source": "B", "target": "C", "kind": "reaction", "barrier": None},
        ],
    }
    _seed_pathway(
        runtime.store,
        meta={
            "tier": "screening",
            "graph": graph,
            "results": {"edges": [{"name": "A->B", "barrier": {"mean": 0.3}}]},
        },
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert _SCREENING_BANNER_TEXT not in resp.text


def test_pathway_tier_banner_absent_outside_screening_tier(client, runtime) -> None:
    """Same all-estimated graph, but a ``neb``-tier pathway — the banner is
    a screening-tier-only honesty note, not a generic "no barriers" flag."""
    graph = {
        "nodes": [{"id": "A", "rel_energy": 0.0}, {"id": "B", "rel_energy": 0.2}],
        "links": [{"source": "A", "target": "B", "kind": "reaction", "barrier": None}],
    }
    _seed_pathway(runtime.store, meta={"tier": "neb", "graph": graph}, body_text=None)
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert _SCREENING_BANNER_TEXT not in resp.text


# ── Kinetics panel (meta.results.kinetics -> vendored report panel) ─────


def test_pathway_detail_kinetics_panel_payload_and_renderer(client, runtime) -> None:
    """A pathway whose run folded a kinetics record into ``meta.results``
    ships the trimmed payload (verdict included) in the #kinetics-data JSON
    tag plus the vendored renderer — never the raw record (the solver's
    ``skipped_steps``/``unreachable_states`` internals stay server-side)."""
    _seed_pathway(
        runtime.store,
        meta={
            "status": "ready",
            "results": {
                "target": "NH3",
                "kinetics": {
                    "conditions": {"temperature": 500.0, "pressures": {"NO": 0.05}},
                    "product": "NH3",
                    "tof": 3.2e-4,
                    "mari_seed": "NO@fcc",
                    "coverages": {"NO@fcc": 0.61, "*": 0.39},
                    "skipped_steps": ["internal-only"],
                    "drc": {"X_RC": {"NO*->NOH*": 0.8}},
                    "transitions": [
                        {
                            "name": "NO* -> NOH*",
                            "from": "NO@fcc",
                            "to": "NOH@fcc",
                            "kind": "step",
                            "dG_eV": 0.3,
                            "barrier_eV": 0.9,
                            "k_f": 10.0,
                            "k_b": 1.0,
                            "net_rate": 3e-4,
                            "gas": None,
                        }
                    ],
                    "warnings": ["one solve warning"],
                },
            },
        },
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert 'id="kinetics-data"' in resp.text
    assert "/static/pathway-kinetics.js" in resp.text
    m = re.search(
        r'<script type="application/json" id="kinetics-data">(.*?)</script>',
        resp.text,
        re.S,
    )
    assert m is not None
    payload = json.loads(m.group(1))
    assert set(payload) == {"ml"}
    assert payload["ml"]["verdict"]["headline"].startswith("Turns over")
    assert payload["ml"]["warnings"] == ["one solve warning"]
    assert "skipped_steps" not in payload["ml"]
    assert "gas" not in payload["ml"]["transitions"][0]


def test_pathway_detail_kinetics_panel_shows_tof_sign_label(client, runtime) -> None:
    """gr559294: a classified negative ``tof_mf`` (``mf_status.tof_sign``)
    renders its plain-words label in the panel payload — a pre-cut record
    (no key) renders exactly as before."""
    _seed_pathway(
        runtime.store,
        meta={
            "status": "ready",
            "results": {
                "target": "NH3",
                "kinetics_v2": {
                    "tof_mf": -2.45e-7,
                    "warnings": [],
                    "mf_status": {
                        "ok": True,
                        "tof_sign": {
                            "class": "consumed_by_other",
                            "sinks": {"NH3": -1e-7},
                            "sources": {"N2": 5e-8},
                        },
                    },
                },
            },
        },
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    m = re.search(
        r'<script type="application/json" id="kinetics-data">(.*?)</script>',
        resp.text,
        re.S,
    )
    assert m is not None
    s = json.loads(m.group(1))["ml"]["v2_solve"]
    assert s["tof_sign"]["label"] == "consumed by another reaction: NH3"
    assert "consumed by another reaction" in s["headline"]
    assert "sinks: NH3" in s["tof_sign"]["detail"]


def test_pathway_detail_kinetics_panel_unclassified_negative_unchanged(
    client, runtime
) -> None:
    """A negative ``tof_mf`` with no ``tof_sign`` key keeps today's
    rendering: no label, the bare no-positive-steady-state headline."""
    _seed_pathway(
        runtime.store,
        meta={
            "status": "ready",
            "results": {
                "target": "NH3",
                "kinetics_v2": {"tof_mf": -1e-3, "warnings": []},
            },
        },
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    m = re.search(
        r'<script type="application/json" id="kinetics-data">(.*?)</script>',
        resp.text,
        re.S,
    )
    assert m is not None
    s = json.loads(m.group(1))["ml"]["v2_solve"]
    assert s["tof_sign"] is None
    assert "no positive steady state" in s["headline"]


def test_pathway_detail_no_kinetics_no_panel(client, runtime) -> None:
    """A kinetics-less pathway (older run, kinetics-less slice) omits the
    panel and its renderer entirely — no empty shell."""
    _seed_pathway(runtime.store, meta={"results": {"target": "NH3"}}, body_text=None)
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert "kinetics-data" not in resp.text
    assert "pathway-kinetics.js" not in resp.text


def test_pathway_detail_kinetics_error_shows_reason(client, runtime) -> None:
    """``run_kinetics`` never fails the run — its failure lands as
    ``results.kinetics_error``, and the page says so instead of silently
    dropping the section."""
    _seed_pathway(
        runtime.store,
        meta={"results": {"kinetics_error": "engine 0.4.1 lacks kinetics"}},
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert "kinetics did not run" in resp.text
    assert "engine 0.4.1 lacks kinetics" in resp.text
    assert "kinetics-data" not in resp.text


def test_pathway_detail_conditions_note_and_methods(client, runtime) -> None:
    """gr553328: a pathway whose kinetics solved at a stated environment
    shows BOTH condition sets — the one-line note beside the energy profile
    and the kinetics panel, and the ``## Conditions`` block appended to the
    Methods body — all off the stored record's values."""
    _seed_pathway(
        runtime.store,
        meta={
            "status": "ready",
            "results": {
                "target": "NH3",
                "thermo": {"mode": "table", "temperature": 298.15, "pressures": {}},
                "kinetics": {"environment_basis": "ab" * 32},
                "kinetics_v2": {
                    "tof_mf": 1.0,
                    "environment": {
                        "T": 500.0,
                        "p_total": 1.05,
                        "p_total_stated": True,
                        "gases": {
                            "inputs": {"NO": 0.05},
                            "outputs": {"NH3": 1e-5},
                            "unlisted": "refuse",
                        },
                    },
                },
            },
            "config": {"conditions": {"temperature": 298.15, "pressures": {}}},
        },
        body_text=(
            "# Methods\n\nAll values are Gibbs free-energy differences at "
            "298.15 K (all gases at the 1 bar reference)."
        ),
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    # the one-line note beside the energy profile and the kinetics panel
    assert resp.text.count("conditions: energy profile and span at") == 2
    assert "T = 298.15 K, all gases at the 1 bar reference" in resp.text
    assert "kinetics at T = 500 K" in resp.text
    # the appended Methods block, values off the record
    assert "## Conditions" in resp.text
    assert "p(NO) = 0.05 bar" in resp.text
    assert "p(NH3) = 1e-05 bar" in resp.text
    assert "basis abababababab" in resp.text
    assert "kT·ln(p/p°)" in resp.text


def test_pathway_detail_no_kinetics_env_keeps_methods_unchanged(
    client,
    runtime,
) -> None:
    """A kinetics-less pathway renders the stored Methods verbatim — no
    appended conditions block, no note lines."""
    _seed_pathway(
        runtime.store,
        meta={"results": {"target": "NH3"}},
        body_text="# Methods\n\nplain body.",
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert "plain body." in resp.text
    assert "## Conditions" not in resp.text
    assert "conditions: energy profile" not in resp.text


# ── step folding + annotations (pathway diagram step annotations) ──────

_TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "precis_web"
    / "templates"
    / "refs"
    / "pathway_detail.html.j2"
)


#: (id, n_H, rel_energy) — the parked-template NO -> NH3 / H2O network the
#: backlog item's acceptance reads: the N+O dissociation, one supply edge per
#: hydrogenation (X -> X+H), N+O parked into N+H / O+H, NH3 and H2O leaves.
_PARKED_NODES: tuple[tuple[str, int, float], ...] = (
    ("NO@N", 0, 0.0),
    ("N+O", 0, -0.2),
    ("N+H", 1, -0.1),
    ("NH", 1, -0.5),
    ("NH+H", 2, -0.4),
    ("NH2", 2, -0.6),
    ("NH2+H", 3, -0.5),
    ("NH3", 3, -0.9),
    ("O+H", 1, -0.3),
    ("OH", 1, -0.4),
    ("OH+H", 2, -0.3),
    ("H2O", 2, -0.7),
)
_PARKED_LINKS: tuple[tuple[str, str, str, float | None], ...] = (
    ("NO@N", "N+O", "reaction", 0.6),
    ("N+O", "N+H", "supply", None),
    ("N+H", "NH", "reaction", 0.5),
    ("NH", "NH+H", "supply", None),
    ("NH+H", "NH2", "reaction", 0.4),
    ("NH2", "NH2+H", "supply", None),
    ("NH2+H", "NH3", "reaction", 0.3),
    ("N+O", "O+H", "supply", None),
    ("O+H", "OH", "reaction", 0.45),
    ("OH", "OH+H", "supply", None),
    ("OH+H", "H2O", "reaction", 0.35),
)


def _parked_graph(**link_extra: Any) -> dict[str, Any]:
    """``_PARKED_*`` as a node_link_data graph; ``link_extra`` is merged into
    every link (e.g. catpath's ``added``/``removed``)."""
    return {
        "nodes": [
            {
                "id": nid,
                "energy": -1.0,
                "energy_std": 0.0,
                "rel_energy": e,
                "low_confidence": False,
                "n_H": nh,
            }
            for nid, nh, e in _PARKED_NODES
        ],
        "links": [
            {
                "source": s,
                "target": t,
                "kind": k,
                "barrier": b,
                "barrier_std": 0.0,
                "delta_e": 0.0,
                "delta_e_std": 0.0,
                "low_confidence": False,
                **link_extra,
            }
            for s, t, k, b in _PARKED_LINKS
        ],
    }


def _annot_block() -> str:
    tpl = _TEMPLATE.read_text(encoding="utf-8")
    start = tpl.index("// pw-annot:begin")
    end = tpl.index("// pw-annot:end")
    return _step_kinetics_script() + "\n" + tpl[start:end]


def _step_kinetics_script() -> str:
    return (_TEMPLATE.parents[2] / "static" / "pathway-step-kinetics.js").read_text(
        encoding="utf-8"
    )


def _node_json(tmp_path: Path, js: str) -> Any:
    script = tmp_path / "probe.js"
    script.write_text(js, encoding="utf-8")
    out = subprocess.run(
        ["node", str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=60,
    )
    return json.loads(out.stdout)


#: Loads the pure fold/annotation block and, for every root->leaf path of a
#: graph, prints ``[[src, tgt, shoulder, "text"], ...]`` per path.
_NODE_STEPS_JS = """
const api = new Function(%(block)s + '; return {pwFoldPath, pwStepAnnotations,'
  + ' pwFormatAnnotations, pwLinkAnnotations};')();
const g = %(graph)s, paths = %(paths)s;
const byPair = {};
g.links.forEach((e) => { byPair[e.source + '\\u0001' + e.target] = e; });
const hasOut = new Set(g.links.map((e) => e.source));
const isLeaf = (id) => !hasOut.has(id);
console.log(JSON.stringify(paths.map((p) => api.pwFoldPath(p, byPair).map((s) => [
  s.src, s.tgt, s.shoulder,
  api.pwFormatAnnotations(api.pwStepAnnotations(s, isLeaf))]))));
"""

_PROTON_DOWN = "+H⁺+e⁻ ↓"
_NH3_ROUTE = [
    ["NO@N", "N+O", None, "O*"],
    ["N+O", "NH", "N+H", _PROTON_DOWN],
    ["NH", "NH2", "NH+H", _PROTON_DOWN],
    ["NH2", "NH3", "NH2+H", _PROTON_DOWN + " · −NH₃ ↑"],
]
_H2O_ROUTE_TAIL = [
    ["N+O", "OH", "O+H", _PROTON_DOWN],
    ["OH", "H2O", "OH+H", _PROTON_DOWN + " · −H₂O ↑"],
]


def _graph_paths(graph: dict[str, Any]) -> list[list[str]]:
    from precis_web.routes.refs import _pathway_paths

    ids = [n["id"] for n in graph["nodes"]]
    return _pathway_paths(ids, graph["links"], "NH3")


@needs_node
def test_pathway_fold_and_annotations_infer_from_labels_on_parked_graph(
    tmp_path: Path,
) -> None:
    """Item 4 (label inference, graph with NO ``added``/``removed``): the
    NH3 route folds to NO@N -> N+O -> NH -> NH2 -> NH3 (shoulders N+H, NH+H,
    NH2+H), carries ``+H+e ↓`` on each hydrogenation and ``O*`` on the
    dissociation; the O route ends ``-H2O ↑``."""
    graph = _parked_graph()
    paths = _graph_paths(graph)
    assert paths[0][-1] == "NH3" and paths[1][-1] == "H2O"
    js = _NODE_STEPS_JS % {
        "block": json.dumps(_annot_block()),
        "graph": json.dumps(graph),
        "paths": json.dumps(paths),
    }
    got = _node_json(tmp_path, js)
    assert got[0] == _NH3_ROUTE
    # the O route's step 0 is the shared prefix (drawn once, by path 0); its
    # own context would call N the spectator instead
    assert got[1][1:] == _H2O_ROUTE_TAIL


@needs_node
def test_pathway_annotations_coadsorbed_and_landing_states(tmp_path: Path) -> None:
    """Label inference over the coadsorbed / landing-state vocabulary:
    ``* -> NO@N`` is ``+NO ↓``, a landing state's ``~tag`` gas leaves
    (``-NH3`` / ``-N2O`` / ``-NH2OH``), a second NO arriving is ``+NO ↓``,
    and an explicit ``added``/``removed`` on the link wins over labels
    (parked species in ``removed`` are not molecules leaving)."""
    js = """
const api = new Function(%(block)s + '; return {pwLinkAnnotations,'
  + ' pwFormatAnnotations};')();
const f = (a, b, l, c) => api.pwFormatAnnotations(api.pwLinkAnnotations(a, b, l || {}, c));
console.log(JSON.stringify({
  adsorb: f('*', 'NO@N', {kind: 'reaction'}),
  second_no: f('N', 'N+NO', {kind: 'reaction'}),
  nh3_gas: f('NH2+O+H', 'O~nh3', {kind: 'reaction'}),
  n2o_gas: f('N+NO', '*~n2o', {kind: 'reaction'}),
  nh2oh_gas: f('NH2OH', '*~nh2oh', {kind: 'desorption'}),
  n2_gas: f('N+N+O', 'O~n2', {kind: 'reaction'}),
  supply: f('NH+O', 'NH+O+H', {kind: 'supply'}),
  isomer_stripped: f('NO@N', 'N+O@top', {kind: 'reaction'}, {after: [
    {from: 'N+O@top', to: 'N+H', kind: 'supply'},
    {from: 'N+H', to: 'NH', kind: 'reaction'}]}),
  no_lookahead: f('NO@N', 'N+O', {kind: 'reaction'}),
  explicit_h: f('NH', 'NH+H', {kind: 'supply', added: {H: 2}}),
  explicit_gas: f('NH2+H', 'NH3', {kind: 'reaction', removed: {NH3: 1}}),
  explicit_parked: f('N+O', 'N+H', {kind: 'supply', added: {H: 1}, removed: {O: 1}}),
}));
""".replace("%(block)s", json.dumps(_annot_block()))
    got = _node_json(tmp_path, js)
    up, down = "↑", "↓"
    assert got == {
        "adsorb": f"+NO {down}",
        "second_no": f"+NO {down}",
        "nh3_gas": f"−NH₃ {up}",
        "n2o_gas": f"−N₂O {up}",
        "nh2oh_gas": f"−NH₂OH {up}",
        "n2_gas": f"−N₂ {up}",
        "supply": _PROTON_DOWN,
        "isomer_stripped": "O*",
        "no_lookahead": "",
        "explicit_h": f"+2H⁺+2e⁻ {down}",
        "explicit_gas": f"−NH₃ {up}",
        "explicit_parked": _PROTON_DOWN,
    }


#: Stubs just enough DOM to run the page's diagram script headlessly and
#: dump the SVG string ``renderDiagram`` writes into #pw-diagram.
_NODE_PAGE_JS = """
const fs = require('fs');
const script = fs.readFileSync(process.argv[2], 'utf-8');
const els = {};
const mk = () => ({
  innerHTML: '', style: {}, dataset: {}, childElementCount: 0, value: '0',
  classList: { toggle() {}, contains() { return false; }, add() {} },
  setAttribute() {}, getAttribute() { return null; }, addEventListener() {},
  appendChild() {}, querySelectorAll() { return []; }, closest() { return null; },
});
global.document = {
  getElementById: (id) => els[id] || (els[id] = mk()),
  querySelectorAll: () => [], addEventListener() {}, createElement: mk,
  readyState: 'complete',
};
global.window = { location: { search: '' } };
global.setTimeout = () => 0;
global.CSS = { escape: (s) => s };
(0, eval)(script);
console.log(JSON.stringify(els['pw-diagram'].innerHTML));
"""


def _explorer_script(html: str) -> str:
    """Load both browser dependencies before the inline explorer script."""
    scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
    (script,) = [s for s in scripts if "function renderDiagram" in s]
    filters = Path("src/precis_web/static/pathway-filters.js").read_text(
        encoding="utf-8"
    )
    return _step_kinetics_script() + "\n" + filters + "\n" + script


def _render_diagram_svg(tmp_path: Path, html: str) -> str:
    """Run the page's own diagram script under node with a stub DOM and
    return the SVG markup ``renderDiagram`` produced (no browser)."""
    script = _explorer_script(html)
    (tmp_path / "page.js").write_text(script, encoding="utf-8")
    driver = tmp_path / "driver.js"
    driver.write_text(_NODE_PAGE_JS, encoding="utf-8")
    out = subprocess.run(
        ["node", str(driver), str(tmp_path / "page.js")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=60,
    )
    svg = json.loads(out.stdout)
    assert isinstance(svg, str) and svg.startswith("<svg")
    return svg


def _seed_parked(runtime: Any, graph: dict[str, Any]) -> None:
    _seed_pathway(
        runtime.store,
        meta={
            "graph": graph,
            "results": {"substrate": "NO", "target": "NH3"},
        },
        body_text=None,
    )


@needs_node
def test_pathway_layer_helpers_defaults_and_each_off_state(tmp_path: Path) -> None:
    """Every layer has a named, explained input and resolves independently."""
    got = _node_json(
        tmp_path,
        _annot_block()
        + """
const keys = PW_LAYERS.map((l) => l.key);
const ignored = [];
const parsed = pwReadLayers(new URLSearchParams('layer_feed=0&layer_energy=1&layer_ea=no'), ignored);
const qs = new URLSearchParams('state=N%2BH&layer_energy=1');
pwWriteLayers(qs, parsed);
console.log(JSON.stringify({
  layers: PW_LAYERS, defaults: pwLayerDefaults(),
  off: keys.map((key) => pwLayerPresentation({[key]: false})),
  parsed, ignored, query: qs.toString(), restored: pwReadLayers(qs),
  node: [pwNodeLabel({id:'NH2', rel_energy:-4.47, energy_std:0.03}),
    pwNodeLabel({id:'NH2', rel_energy:-4.47, energy_std:0.03}, {energy:false}),
    pwNodeLabel({id:'NH2', rel_energy:-4.47, energy_std:0})],
  humps: [pwShowHump({barrier:0.5}), pwShowHump({barrier:0.5}, {measured:false}),
    pwShowHump({barrier:null,bepEa:0.4}),
    pwShowHump({barrier:null,bepEa:0.4}, {bep:false}),
    pwShowHump({barrier:null,bepEa:null})],
  ea: [pwEaLabel({barrier:0.5,bsd:0.03}, 0.25),
    pwEaLabel({barrier:0.5}, 0.25, {ea:false}),
    pwEaLabel({barrier:0.5}, 0.25, {forks:false}),
    pwEaLabel({barrier:0.5}, 0.25, {ea:false,forks:false}),
    pwEaLabel({barrier:null,bepEa:0.4}, null),
    pwEaLabel({barrier:null,bepEa:null}, null),
    pwEaLabel({barrier:0}, 0.25)],
}));
""",
    )
    keys = [
        "supply",
        "feed",
        "energy",
        "desorption",
        "ea",
        "measured",
        "bep",
        "forks",
        "confidence",
        "states",
        "kinetics",
        "kin_source",
        "kin_ea_forward",
        "kin_ea_reverse",
        "kin_k_f",
        "kin_k_b",
        "kin_net_rate",
        "kin_z_i",
        "kin_X_RC",
        "kin_selectivity",
        "barrier_filter",
        "turnover_filter",
    ]
    defaults = {key: i < 12 or key == "turnover_filter" for i, key in enumerate(keys)}
    assert [layer["key"] for layer in got["layers"]] == keys
    assert all(layer["label"] and layer["help"] for layer in got["layers"])
    assert got["defaults"] == defaults
    assert got["off"] == [defaults | {key: False} for key in keys]
    assert got["parsed"] == defaults | {"feed": False}
    assert got["ignored"] == ["layer_ea"]
    assert got["query"] == "state=N%2BH&layer_feed=0"
    assert got["restored"] == got["parsed"]
    assert got["node"] == ["NH2 (-4.47±0.03)", "NH2", "NH2"]
    assert got["humps"] == [True, False, True, False, False]
    assert got["ea"] == [
        "Ea=0.50±0.03 (25%)",
        "25%",
        "Ea=0.50",
        "",
        "Ea≈0.40 (BEP)",
        "Ea n/a",
        "",
    ]


@needs_node
def test_pathway_layers_filter_feed_and_hydrogen_desorption(tmp_path: Path) -> None:
    """H₂ works through both explicit removed metadata and landing labels."""
    got = _node_json(
        tmp_path,
        _annot_block()
        + """
const link = {source:'NH', target:'NH2', kind:'reaction', added:{H:1}, removed:{H2:1}};
const step = {src:'NH', tgt:'NH2', shoulder:null, react:link};
const f = (layers) => pwFormatAnnotations(pwStepAnnotations(step, () => false, layers));
console.log(JSON.stringify({
  both:f(), feedOff:f({feed:false}), desorptionOff:f({desorption:false}),
  bothOff:f({feed:false,desorption:false}),
  landing:pwFormatAnnotations(pwLinkAnnotations('H+H', '*~h2', {kind:'reaction'})),
}));
""",
    )
    assert got == {
        "both": "+H⁺+e⁻ ↓ · −H₂ ↑",
        "feedOff": "−H₂ ↑",
        "desorptionOff": "+H⁺+e⁻ ↓",
        "bothOff": "",
        "landing": "−H₂ ↑",
    }


@needs_node
@pytest.mark.parametrize("folded", [False, True])
def test_pathway_diagram_unknown_barrier_is_na_not_a_hump(
    client, runtime, tmp_path: Path, folded: bool
) -> None:
    """A failed NEB is a dashed unknown connection, not a barrierless curve."""
    ids = ["a", "a+H", "b"] if folded else ["a", "b"]
    links: list[dict[str, Any]] = (
        [{"source": "a", "target": "a+H", "kind": "supply"}] if folded else []
    )
    links.append(
        {
            "source": ids[-2],
            "target": "b",
            "kind": "reaction",
            "barrier": None,
            "barrier_std": None,
        }
    )
    graph = {
        "nodes": [{"id": sid, "rel_energy": 0.1 * i} for i, sid in enumerate(ids)],
        "links": links,
    }
    _seed_parked(runtime, graph)

    response = client.get("/refs/pathway/171696")
    assert response.status_code == 200
    svg = _render_diagram_svg(tmp_path, response.text)

    segment = re.search(r'<polyline class="pw-seg"[^>]*>', svg)
    assert segment is not None
    assert 'stroke-dasharray="4 3"' in segment[0]
    points = re.search(r'points="([^"]*)"', segment[0])
    assert points is not None and len(points[1].split()) == 2
    assert re.search(r"<text[^>]*>Ea n/a</text>", svg)
    assert "Ea=0.00" not in svg
    assert "NaN" not in svg


@needs_node
@pytest.mark.parametrize(("kind", "barrier"), [("reaction", 0.0), ("supply", None)])
def test_pathway_diagram_measured_zero_barrier_stays_solid(
    client, runtime, tmp_path: Path, kind: str, barrier: float | None
) -> None:
    graph = {
        "nodes": [{"id": sid, "rel_energy": 0.0} for sid in ["a", "b"]],
        "links": [{"source": "a", "target": "b", "kind": kind, "barrier": barrier}],
    }
    _seed_parked(runtime, graph)

    response = client.get("/refs/pathway/171696")
    assert response.status_code == 200
    svg = _render_diagram_svg(tmp_path, response.text)

    segment = re.search(r'<polyline class="pw-seg"[^>]*>', svg)
    assert segment is not None and "stroke-dasharray" not in segment[0]
    assert "Ea n/a" not in svg


@needs_node
def test_pathway_diagram_bep_estimated_hump_is_dashed_with_band(
    client, runtime, tmp_path: Path
) -> None:
    """gr559345: no measured barrier but a usable BEP fit on the joined
    ``results.edges`` record — the hump still draws through ``humpPoints``
    (not the bare 2-point "Ea n/a" line), stays dashed, carries a
    translucent +/-rmse band, and labels ``Ea≈<value> (BEP)``."""
    graph = {
        "nodes": [{"id": "A", "rel_energy": 0.0}, {"id": "B", "rel_energy": 0.1}],
        "links": [
            {
                "source": "A",
                "target": "B",
                "kind": "reaction",
                "barrier": None,
                "delta_e": 0.2,
            }
        ],
    }
    _seed_pathway(
        runtime.store,
        meta={
            "graph": graph,
            "results": {
                "edges": [
                    {"name": "A->B", "bep": {"alpha": 0.8, "beta": 0.05, "rmse": 0.03}}
                ]
            },
        },
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    svg = _render_diagram_svg(tmp_path, resp.text)

    segment = re.search(r'<polyline class="pw-seg"[^>]*>', svg)
    assert segment is not None
    assert 'stroke-dasharray="4 3"' in segment[0]
    points = re.search(r'points="([^"]*)"', segment[0])
    assert points is not None and len(points[1].split()) > 2

    assert "Ea≈0.21 (BEP)" in svg
    assert "Ea n/a" not in svg
    assert re.search(r'<line[^>]*opacity="0\.4"[^>]*/>', svg)


@needs_node
@pytest.mark.parametrize("hidden", ["measured", "bep"])
def test_pathway_hump_layers_hide_only_their_own_curve_and_band(
    client, runtime, tmp_path: Path, hidden: str
) -> None:
    graph = {
        "nodes": [
            {"id": "A", "rel_energy": 0.0},
            {"id": "B", "rel_energy": 0.1},
            {"id": "C", "rel_energy": 0.2},
        ],
        "links": [
            {
                "source": "A",
                "target": "B",
                "kind": "reaction",
                "barrier": 0.5,
                "barrier_std": 0.04,
            },
            {
                "source": "B",
                "target": "C",
                "kind": "reaction",
                "barrier": None,
                "delta_e": 0.2,
            },
        ],
    }
    _seed_pathway(
        runtime.store,
        meta={
            "graph": graph,
            "results": {
                "edges": [
                    {"name": "A->B", "barrier": {"mean": 0.5, "std": 0.04}},
                    {"name": "B->C", "bep": {"alpha": 0.8, "beta": 0.05, "rmse": 0.03}},
                ]
            },
        },
        body_text=None,
    )
    html = client.get("/refs/pathway/171696").text
    default = _run_url_probe(tmp_path, html)["svg"]
    off = _run_url_probe(tmp_path, html, search=f"?layer_{hidden}=0")["svg"]

    def transition(svg: str, source: str) -> tuple[int, str, str]:
        match = re.search(
            rf'<polyline class="pw-seg"[^>]*data-from="{source}"[^>]*'
            r'points="([^"]*)"[^>]*><title>([^<]*)</title></polyline>'
            r"(<line[^>]*/>)?",
            svg,
        )
        assert match is not None
        return len(match[1].split()), match[2], match[3] or ""

    for source, layer, value, origin in (
        ("A", "measured", "Ea=0.50 eV", "source=neb"),
        ("B", "bep", "Ea≈0.21 eV", "source=bep"),
    ):
        default_points, default_title, default_band = transition(default, source)
        points, title, band = transition(off, source)
        assert default_points > 2
        assert default_band
        assert title == default_title
        assert value in title and origin in title
        if layer == hidden:
            assert points == 2
            assert band == ""
        else:
            assert points > 2
            assert band


@needs_node
def test_pathway_diagram_hover_title_names_barrier_source_every_step(
    client, runtime, tmp_path: Path
) -> None:
    """gr559345: every transition's hover title now names its
    ``barrier_source`` (neb/bep/none), not just the formerly-unknown case."""
    graph = {
        "nodes": [
            {"id": "A", "rel_energy": 0.0},
            {"id": "B", "rel_energy": 0.3},
            {"id": "C", "rel_energy": 0.5},
            {"id": "D", "rel_energy": 0.6},
        ],
        "links": [
            {"source": "A", "target": "B", "kind": "reaction", "barrier": 0.3},
            {
                "source": "B",
                "target": "C",
                "kind": "reaction",
                "barrier": None,
                "delta_e": 0.2,
            },
            {"source": "C", "target": "D", "kind": "reaction", "barrier": None},
        ],
    }
    _seed_pathway(
        runtime.store,
        meta={
            "graph": graph,
            "results": {
                "edges": [
                    {"name": "A->B", "barrier": {"mean": 0.3}},
                    {"name": "B->C", "bep": {"alpha": 0.8, "beta": 0.05}},
                ]
            },
        },
        body_text=None,
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    svg = _render_diagram_svg(tmp_path, resp.text)

    def _title_for(src: str, tgt: str) -> str:
        seg = re.search(
            rf'<polyline class="pw-seg"[^>]*data-from="{src}" data-to="{tgt}"[^>]*>'
            rf"<title>([^<]*)</title>",
            svg,
        )
        assert seg is not None, svg
        return seg[1]

    assert "source=neb" in _title_for("A", "B")
    assert "source=bep" in _title_for("B", "C")
    assert "source=none" in _title_for("C", "D")


@needs_node
def test_pathway_diagram_renders_folded_steps_with_annotations(
    client, runtime, tmp_path: Path
) -> None:
    """The rendered SVG (page script run under node, stub DOM): one solid
    hump per chemical step — 4 on the NH3 route + 2 new on the O route — no
    dashed supply segment, 5 shoulder ticks (the X+H levels, clickable by
    state id), 7 level columns (chemical steps only: viewBox width
    130*5+60 = 710), and the annotation texts in step order."""
    _seed_parked(runtime, _parked_graph())
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    svg = _render_diagram_svg(tmp_path, resp.text)

    assert 'viewBox="0 0 710 300"' in svg
    segs = re.findall(
        r'<polyline class="pw-seg"[^>]*data-from="([^"]*)" data-to="([^"]*)"', svg
    )
    assert segs == [
        ("NO@N", "N+O"),
        ("N+O", "NH"),
        ("NH", "NH2"),
        ("NH2", "NH3"),
        ("N+O", "OH"),
        ("OH", "H2O"),
    ]
    assert 'stroke-dasharray="4 3"' not in svg
    shoulders = re.findall(r'<line class="pw-shoulder"[^>]*data-state="([^"]*)"', svg)
    assert sorted(shoulders) == ["N+H", "NH+H", "NH2+H", "O+H", "OH+H"]
    # the drop line, the tick and the transparent hit-rect over it all carry
    # the shoulder's hover title (the rect is what the pointer actually hits)
    for tag in ("line", "rect"):
        titled = re.findall(
            rf'<{tag} class="pw-(?:shoulder(?:-drop)?|svg-node)"[^>]*>'
            rf"(?:<title>[^<]*</title>)?",
            svg,
        )
        assert titled
    drops = re.findall(r'<line class="pw-shoulder-drop"[^>]*><title>', svg)
    hits = re.findall(
        r'<rect class="pw-svg-node"[^>]*data-state="[^"]*\+H"[^>]*><title>', svg
    )
    assert len(drops) == len(hits) == 5
    # level labels: one per chemical node, none for the shoulder states
    # (centred anchors only — a stacked hydro label is also bold but
    # start-anchored, and at the non-zero default U it crowds enough to
    # stack, gr549591)
    labels = re.findall(
        r'font-weight="bold"[^>]*text-anchor="middle"[^>]*>([^<(]*?)(?: \(|<)', svg
    )
    assert sorted(labels) == sorted(["NO@N", "N+O", "NH", "NH2", "NH3", "OH", "H2O"])

    annots = re.findall(
        r'<text class="pw-annot" data-paths="([^"]*)"[^>]*>([^<]*)', svg
    )
    # the proton feed is an arrow (pw-hydro-arrow), not part of this text
    assert [a[1] for a in annots] == ["O*", "−NH₃ ↑", "−H₂O ↑"]
    # fade hook: the shared first step belongs to both paths, the NH3 leaf
    # to path 0 only, the H2O leaf to path 1 only
    assert [a[0] for a in annots] == ["|0|1|", "|0|", "|1|"]
    arrows = re.findall(
        r'<g class="pw-hydro-arrow" data-paths="([^"]*)" data-from="([^"]*)" data-to="([^"]*)"',
        svg,
    )
    assert arrows == [
        ("|0|", "N+O", "NH"),
        ("|0|", "NH", "NH2"),
        ("|0|", "NH2", "NH3"),
        ("|1|", "N+O", "OH"),
        ("|1|", "OH", "H2O"),
    ]


@needs_node
def test_pathway_diagram_annotations_survive_explicit_link_fields(
    client, runtime, tmp_path: Path
) -> None:
    """Graphs carrying catpath's own ``added``/``removed`` render the same
    annotations as label inference (the template consumes them when
    present): H on the supply links, the leaving molecule on the leaf link."""
    graph = _parked_graph()
    for e in graph["links"]:
        if e["kind"] == "supply":
            e["added"] = {"H": 1}
        elif e["target"] == "NH3":
            e["removed"] = {"NH3": 1}
        elif e["target"] == "H2O":
            e["removed"] = {"H2O": 1}
    _seed_parked(runtime, graph)
    resp = client.get("/refs/pathway/171696")
    diagram = _extract_json_object(resp.text, "diagram: ")
    adds = [link["added"] for link in diagram["links"]]
    assert adds.count({"H": 1}) == 5 and adds.count(None) == 6
    svg = _render_diagram_svg(tmp_path, resp.text)
    texts = re.findall(r'<text class="pw-annot"[^>]*>([^<]*)', svg)
    assert texts == ["O*", "−NH₃ ↑", "−H₂O ↑"]
    assert len(re.findall(r'class="pw-hydro-arrow"', svg)) == 5


def test_pathway_graph_payload_passes_link_added_removed_through() -> None:
    from precis_web.routes.refs import _pathway_graph_payload

    graph = {
        "nodes": [{"id": "X"}, {"id": "Y"}, {"id": "Z"}],
        "links": [
            {"source": "X", "target": "Y", "kind": "supply", "added": {"H": 1}},
            {
                "source": "Y",
                "target": "Z",
                "kind": "desorption",
                "removed": {"NH3": 1, "bad": "x", "zero": 0},
            },
        ],
    }
    payload = _pathway_graph_payload(graph)
    assert payload is not None
    first, second = payload["links"]
    assert first["added"] == {"H": 1} and first["removed"] is None
    assert second["kind"] == "desorption"
    assert second["removed"] == {"NH3": 1} and second["added"] is None


def test_pathway_graph_payload_joins_bep_and_barrier_source_by_edge_name() -> None:
    """gr559345: ``results.edges`` joins onto each link by physical
    ``(source, target)`` name (``~history`` landing suffixes stripped, same
    rule as ``flux_share._phys``) and attaches ``bep``/``barrier_source`` —
    ``"neb"`` when a finite measured barrier is on record, ``"bep"`` when
    only a usable BEP fit is, else ``"none"``. A degenerate fit (missing or
    non-finite alpha/beta) is treated the same as no fit at all."""
    from precis_web.routes.refs import _pathway_graph_payload

    graph = {
        "nodes": [{"id": "A"}, {"id": "B~history"}, {"id": "C"}, {"id": "D"}],
        "links": [
            {"source": "A", "target": "B~history", "kind": "reaction"},
            {"source": "B~history", "target": "C", "kind": "reaction"},
            {"source": "C", "target": "D", "kind": "reaction"},
        ],
    }
    edges = [
        {
            "name": "A->B",
            "barrier": {"mean": 0.42},
            "bep": {"alpha": 0.9, "beta": 0.1, "rmse": 0.05},
        },
        {"name": "B->C", "bep": {"alpha": 0.9, "beta": 0.1}},
        {"name": "C->D", "bep": {"alpha": float("nan"), "beta": 0.1}},
    ]
    payload = _pathway_graph_payload(graph, edges=edges)
    assert payload is not None
    neb, bep, none = payload["links"]

    # a finite measured barrier wins even though a bep fit is also on
    # record — "neb" carries the bep dict along anyway (the client only
    # draws through it when barrier is itself null)
    assert neb["barrier_source"] == "neb"
    assert neb["bep"] == {"alpha": 0.9, "beta": 0.1, "rmse": 0.05}

    # no measured barrier, a usable fit, no rmse recorded — rmse omitted
    # rather than fabricated
    assert bep["barrier_source"] == "bep"
    assert bep["bep"] == {"alpha": 0.9, "beta": 0.1}

    # a degenerate fit (alpha non-finite) is no fit at all
    assert none["barrier_source"] == "none"
    assert none["bep"] is None


def test_pathway_graph_payload_barrier_source_none_off_stored_graph() -> None:
    """No ``results.edges`` block at all (a pathway stored before catpath
    exported it, or no ``edges`` kwarg passed) -> every link is
    ``barrier_source == "none"``/``bep is None``, not a crash."""
    from precis_web.routes.refs import _pathway_graph_payload

    graph = {
        "nodes": [{"id": "A"}, {"id": "B"}],
        "links": [{"source": "A", "target": "B", "kind": "reaction", "barrier": 0.2}],
    }
    payload = _pathway_graph_payload(graph)
    assert payload is not None
    (link,) = payload["links"]
    assert link["barrier_source"] == "none" and link["bep"] is None


def _typed_link(
    source: str, target: str, link_type: str, **fields: Any
) -> dict[str, Any]:
    """Engine wire format: typed reservoir links retain kind=supply."""
    return {
        "source": source,
        "target": target,
        "kind": "supply",
        "link_type": link_type,
        **fields,
    }


@needs_node
@pytest.mark.parametrize(
    ("links", "expected"),
    [
        (
            [
                _typed_link("NH3+O", "O", "desorption", removed={"NH3": 1}),
                {"source": "O", "target": "O@top", "kind": "reaction"},
            ],
            [["NH3+O", "O", None, "−NH₃ ↑"], ["O", "O@top", None, ""]],
        ),
        (
            [
                _typed_link("*", "NH3", "adsorption", added={"N": 1, "H": 3}),
                {"source": "NH3", "target": "NH2+H", "kind": "reaction"},
            ],
            [["*", "NH3", None, "+NH₃ ↓"], ["NH3", "NH2+H", None, ""]],
        ),
        (
            [
                _typed_link("NH3", "NH3+H", "supply", added={"H": 1}),
                _typed_link("NH3+H", "H", "desorption", removed={"NH3": 1}),
            ],
            [["NH3", "NH3+H", None, _PROTON_DOWN], ["NH3+H", "H", None, "−NH₃ ↑"]],
        ),
        (
            [
                _typed_link("NH", "NH+H", "supply", added={"H": 1}),
                {"source": "NH+H", "target": "NH2", "kind": "reaction"},
            ],
            [["NH", "NH2", "NH+H", _PROTON_DOWN]],
        ),
    ],
    ids=[
        "desorption-reaction",
        "adsorption-reaction",
        "supply-desorption",
        "supply-reaction",
    ],
)
def test_pathway_typed_links_keep_chemical_steps(
    tmp_path: Path, links: list[dict[str, Any]], expected: list[list[Any]]
) -> None:
    from precis_web.routes.refs import _pathway_graph_payload

    path = [links[0]["source"], *(e["target"] for e in links)]
    graph = _pathway_graph_payload({"nodes": [{"id": n} for n in path], "links": links})
    assert graph is not None
    assert graph["links"][0]["kind"] == "supply"  # kinetic semantics unchanged
    assert graph["links"][0]["link_type"] == links[0]["link_type"]
    got = _node_json(
        tmp_path,
        _NODE_STEPS_JS
        % {
            "block": json.dumps(_annot_block()),
            "graph": json.dumps(graph),
            "paths": json.dumps([path]),
        },
    )
    assert got == [expected]


@needs_node
def test_pathway_typed_desorption_renders_its_own_level(
    client, runtime, tmp_path: Path
) -> None:
    """Stored engine-format links survive the route and rendered SVG."""
    ids = ["NH3+O", "O", "O@top", "O@top+H", "OH"]
    graph = {
        "nodes": [
            {"id": n, "rel_energy": i * 0.1, "n_H": 0} for i, n in enumerate(ids)
        ],
        "links": [
            _typed_link(ids[0], ids[1], "desorption", removed={"NH3": 1}),
            {"source": ids[1], "target": ids[2], "kind": "reaction", "barrier": 0.2},
            _typed_link(ids[2], ids[3], "supply", added={"H": 1}),
            {"source": ids[3], "target": ids[4], "kind": "reaction", "barrier": 0.3},
        ],
    }
    _seed_parked(runtime, graph)
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    svg = _render_diagram_svg(tmp_path, resp.text)
    segs = re.findall(
        r'<polyline class="pw-seg"[^>]*data-from="([^"]*)" data-to="([^"]*)"', svg
    )
    assert segs == [(ids[0], ids[1]), (ids[1], ids[2]), (ids[2], ids[4])]
    assert re.findall(r'<line class="pw-shoulder"[^>]*data-state="([^"]*)"', svg) == [
        ids[3]
    ]
    assert set(_level_bars(svg)) == {ids[0], ids[1], ids[2], ids[4]}
    assert "−NH₃ ↑" in svg
    assert len(re.findall(r'class="pw-hydro-arrow"', svg)) == 1


def test_pathway_diagram_template_has_no_dashed_supply_branch() -> None:
    """Static guard (runs without node): the dashed supply segment is gone,
    the fold + inference block markers are present, and the fork-probability
    guard still skips supply links."""
    tpl = _TEMPLATE.read_text(encoding="utf-8")
    assert 'stroke="#94a3b8" stroke-width="1.5" stroke-dasharray="4 3"' not in tpl
    assert "// pw-annot:begin" in tpl and "// pw-annot:end" in tpl
    assert "function pwFoldPath(" in tpl and "function pwLinkAnnotations(" in tpl
    assert "if (e.kind === 'supply') { return; }" in tpl  # fork guard


def _level_bars(svg: str) -> dict[str, tuple[float, float, float]]:
    """state id -> (bar x0, bar x1, bar y) from the level click bands (each is
    centred on its bar: rect y = bar y - 14, same x extent as the bar)."""
    out: dict[str, tuple[float, float, float]] = {}
    for sid, x, y, w in re.findall(
        r'<rect class="pw-svg-node"(?: data-paths="[^"]*")? data-state="([^"]*)" '
        r'x="([\d.]+)" y="([\d.-]+)" width="([\d.]+)" height="28"',
        svg,
    ):
        out[sid] = (float(x), float(x) + float(w), float(y) + 14)
    return out


_HYDRO_ARROW_RE = (
    r'<g class="pw-hydro-arrow"[^>]*data-to="([^"]*)">'
    r'<line x1="([\d.-]+)" y1="([\d.-]+)" x2="([\d.-]+)" y2="([\d.-]+)"[^>]*/>'
    r'<polygon points="([\d.-]+),([\d.-]+) [^"]*"[^>]*/>'
    r'(?:<line class="pw-hydro-leader"[^>]*/>)?'
    r'(?:<text class="pw-hydro-label[^"]*" x="([\d.-]+)" y="([\d.-]+)" [^>]*fill="([^"]*)"'
    r"[^>]*>([^<]*)</text>)?(?:<title>[^<]*</title>)?</g>"
)


@needs_node
def test_pathway_hydrogenation_arrow_tip_lands_on_fed_level_from_upper_right(
    client, runtime, tmp_path: Path
) -> None:
    """Each hydrogenation step's arrow enters at an angle from the upper
    right: the head's tip vertex sits on the PRODUCT level bar it feeds (x
    inside the bar, y on the bar) and the tail is right of and above it.
    Arrows sharing an x-column (NH/OH, NH2/H2O) are each labelled when their
    tips are >= 11px apart."""
    _seed_parked(runtime, _parked_graph())
    svg = _render_diagram_svg(tmp_path, client.get("/refs/pathway/171696").text)
    bars = _level_bars(svg)
    groups = re.findall(_HYDRO_ARROW_RE, svg)
    assert [g[0] for g in groups] == ["NH", "NH2", "NH3", "OH", "H2O"]
    by_to = {g[0]: g for g in groups}
    for to, tx, ty, bx, by, hx, hy, lx, ly, fill, text in groups:
        x0, x1, y = bars[to]
        tail = (float(tx), float(ty))
        tip = (float(hx), float(hy))
        assert x0 < tip[0] < x1, (to, tip, x0, x1)
        assert tip[1] == pytest.approx(y, abs=1e-6), (to, tip, y)
        assert tail[0] > tip[0] and tail[1] < tip[1]  # right of and above
        # every shaft is the same short 45 degree one: (+22, -22) from its tip
        assert (tail[0] - tip[0], tip[1] - tail[1]) == pytest.approx((22, 22), abs=1e-6)
        # the line stops at the head's base, on the tail -> tip chord
        base = (float(bx), float(by))
        assert (tip[0] - tail[0]) * (base[1] - tail[1]) == pytest.approx(
            (tip[1] - tail[1]) * (base[0] - tail[0]), abs=1e-6
        )
        assert lx, to  # every arrow is labelled in the SVG itself (print/export)
        if text.endswith("+H⁺+e⁻") and text != "+H⁺+e⁻":
            # stacked: right of the tail AND the bars, prefixed with its product
            assert text == f"{to}: +H⁺+e⁻", text
            assert float(lx) > tail[0] and float(lx) >= x1, (to, lx, x1)
        else:
            assert float(lx) == tail[0] and float(ly) < tail[1]
            assert text == "+H⁺+e⁻"
    # every arrow is labelled (gr478861), in its own path colour
    assert all(g[7] for g in groups)
    assert all(g[9] != "#64748b" for g in groups)
    # closer than 11px -> the column's labels are stacked, never overlapping:
    # distinct y, >= 9px apart, none above its own tail's label line
    for xs in {round(float(g[5]) // 40) for g in groups}:
        col = sorted(
            (g for g in groups if round(float(g[5]) // 40) == xs),
            key=lambda g: float(g[6]),
        )
        stacked = [g for g in col if g[10].startswith(g[0] + ": ")]
        ys = [float(g[8]) for g in stacked]
        assert all(b - a >= 9.99 for a, b in pairwise(ys)), (xs, ys)
        if len(col) > 1 and any(
            float(b[6]) - float(a[6]) < 11 for a, b in pairwise(col)
        ):
            assert len(stacked) == len(col)  # all-or-nothing within a column
    assert svg.count("feeds ") >= len(groups)  # hover title on each arrow


@needs_node
def test_pathway_hydrogenation_labels_stack_when_tips_crowd(
    client, runtime, tmp_path: Path
) -> None:
    """NH/OH and NH2/H2O land within a pixel or two of each other (energies
    0.01 eV apart): the column's labels STACK instead of falling back to
    hover — every arrow labelled in the SVG text, right of the bars, >= 9px
    apart, prefixed with the product it feeds (a text distinction that
    survives black-and-white print), in the arrow's own path colour."""
    graph = _parked_graph()
    for n in graph["nodes"]:
        if n["id"] == "OH":
            n["rel_energy"] = -0.51
        if n["id"] == "H2O":
            n["rel_energy"] = -0.61
    _seed_parked(runtime, graph)
    svg = _render_diagram_svg(tmp_path, client.get("/refs/pathway/171696").text)
    bars = _level_bars(svg)
    groups = re.findall(_HYDRO_ARROW_RE, svg)
    by_to = {g[0]: g for g in groups}
    assert set(by_to) == {"NH", "NH2", "NH3", "OH", "H2O"}
    # the pinned precondition: these tips really are < 11px apart
    for a, b in (("NH", "OH"), ("NH2", "H2O")):
        assert abs(float(by_to[a][6]) - float(by_to[b][6])) < 11
    stacked = {g[0]: g for g in groups if g[10].startswith(g[0] + ": ")}
    assert {"NH", "OH", "NH2", "H2O"} <= set(stacked)
    for to, g in stacked.items():
        assert g[10] == f"{to}: +H⁺+e⁻"
        assert float(g[7]) > float(g[5]) and float(g[7]) >= bars[to][1]
        assert g[9] != "#64748b"
    assert float(stacked["OH"][8]) - float(stacked["NH"][8]) >= 9.99
    assert float(stacked["H2O"][8]) - float(stacked["NH2"][8]) >= 9.99
    assert all(g[7] for g in groups)  # NH3 (uncrowded) keeps its own label too
    assert 'class="pw-hydro-leader"' in svg


def test_pathway_graph_payload_carries_low_confidence_reasons() -> None:
    """catpath's ``trust_reasons`` ride into ``low_confidence_reasons`` on
    nodes and links; an older stored graph yields ``[]`` (the tooltip then
    falls back to its inferred text)."""
    from precis_web.routes.refs import _pathway_graph_payload

    graph = {
        "nodes": [
            {"id": "A", "low_confidence": True, "trust_reasons": ["spread"]},
            {"id": "B", "low_confidence": True},
        ],
        "links": [
            {
                "source": "A",
                "target": "B",
                "low_confidence": True,
                "trust_reasons": ["insufficient_samples", "tr123"],
            }
        ],
    }
    out = _pathway_graph_payload(graph)
    assert out is not None
    assert [n["low_confidence_reasons"] for n in out["nodes"]] == [["spread"], []]
    assert out["links"][0]["low_confidence_reasons"] == [
        "insufficient_samples",
        "tr123",
    ]


@needs_node
def test_pathway_pw455722_shape_draws_no_grey_dashed_sawtooth(
    client, runtime, tmp_path: Path
) -> None:
    """Prod pathway 455722 (screening tier, parked template): its candidate
    carries a NEB sibling whose states map onto this graph's ids, which used
    to draw a grey dashed "ghost" polyline (stroke #94a3b8, dasharray
    "6 4") zig-zagging through the level midpoints. The page carries no
    dashed element at all whatever the sibling holds."""
    _seed_pathway(
        runtime.store,
        meta={"tier": "screening", "candidate_ref": 209903, "graph": _parked_graph()},
        body_text=None,
    )
    sibling_graph = {
        "nodes": [
            {"id": nid, "rel_energy": e + 0.3 * (i % 2), "n_H": nh}
            for i, (nid, nh, e) in enumerate(_PARKED_NODES)
        ],
        "links": [],
    }
    sibling_ref = make_ref(
        id=246279,
        kind="pathway",
        title="neb",
        meta={"tier": "neb", "graph": sibling_graph},
    )
    _seed_tier_sibling(runtime, sibling_ref, refines_from=171696)
    resp = client.get("/refs/pathway/171696")
    svg = _render_diagram_svg(tmp_path, resp.text)
    assert "stroke-dasharray" not in svg
    assert "#94a3b8" not in "".join(re.findall(r"<polyline[^>]*>", svg))


# ── shareable link state (gr486822) ─────────────────────────────────────

#: Runs the page script under node with a fuller stub DOM than
#: ``_NODE_PAGE_JS``: elements record event handlers and attributes,
#: ``document.createElement``'d nodes are tracked (the path-legend
#: buttons), delegated document handlers are captured for dispatch, and
#: ``history.replaceState``/``navigator.clipboard`` calls are recorded —
#: so the selection -> URL -> restored-selection round trip runs headlessly
#: end to end. ``AFTER`` (argv[4]) is evaluated after the page script, with
#: ``els``/``created``/``docHandlers`` in scope, to drive user actions.
_NODE_URL_JS = r"""
const fs = require('fs');
const script = fs.readFileSync(process.argv[2], 'utf-8');
const SEARCH = process.argv[3] || '';
const AFTER = process.argv[4] || '';
const HASH = process.argv[5] || '';
const els = {};
const created = [];
let diagramRects = [];
const docHandlers = {};
const rsCalls = [];
const clipCalls = [];
const scrolls = [], kineticsClicks = [];
const mk = () => {
  const el = {
    innerHTML: '', textContent: '', style: {}, dataset: {}, value: '0',
    className: '', attrs: {}, children: [], handlers: {},
    classList: {
      toggle(c, on) {
        const want = on === undefined ? !el.classList.contains(c) : !!on;
        const ws = el.className.split(/\s+/).filter((w) => w && w !== c);
        if (want) { ws.push(c); }
        el.className = ws.join(' ');
        return want;
      },
      contains(c) { return el.className.split(/\s+/).includes(c); },
      add(c) { el.classList.toggle(c, true); },
      remove(c) { el.classList.toggle(c, false); },
    },
    setAttribute(k, v) { el.attrs[k] = String(v); },
    getAttribute(k) { return k in el.attrs ? el.attrs[k] : null; },
    addEventListener(t, f) { (el.handlers[t] = el.handlers[t] || []).push(f); },
    appendChild(c) { el.children.push(c); },
    remove() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    getBoundingClientRect() { return { top: 0, bottom: 0 }; },
    scrollIntoView() { scrolls.push(el); },
    scrollTop: 0,
    title: '',
    type: '',
  };
  Object.defineProperty(el, 'childElementCount', {
    get: () => el.children.length,
  });
  return el;
};
const byClass = (cls) => [...created, ...diagramRects].filter((el) => el.classList.contains(cls));
global.document = {
  getElementById: (id) => {
    if (!els[id]) {
      els[id] = mk();
      // the real element ships class="hidden" — mirror that initial state
      if (id === 'pw-link-note') { els[id].className = 'hidden'; }
      if (id === 'pw-diagram') {
        let markup = '';
        Object.defineProperty(els[id], 'innerHTML', {
          get: () => markup,
          set: (value) => {
            markup = value;
            // A redraw replaces the SVG nodes and their selection attributes.
            diagramRects = [...value.matchAll(/<rect\b([^>]*)>/g)].map((match) => {
              const rect = mk();
              for (const [, key, val] of match[1].matchAll(/([\w-]+)="([^"]*)"/g)) {
                rect.setAttribute(key, val);
              }
              rect.className = rect.getAttribute('class') || '';
              return rect;
            });
          },
        });
      }
    }
    return els[id];
  },
  querySelectorAll: (sel) => (sel.charAt(0) === '.' ? byClass(sel.slice(1)) : []),
  querySelector: () => null,
  addEventListener: (t, f) => {
    (docHandlers[t] = docHandlers[t] || []).push(f);
  },
  createElement: (tag) => {
    const el = mk();
    el.tag = tag;
    created.push(el);
    return el;
  },
  readyState: 'complete',
};
global.window = {
  location: new URL('http://precis.test/refs/pathway/171696' + SEARCH + HASH),
};
global.history = {
  replaceState: (a, b, url) => {
    rsCalls.push(url);
    global.window.location.href = 'http://precis.test' + url;
  },
};
// Node >= 21 ships a getter-only global navigator — a plain assignment is
// silently ignored, so define it.
Object.defineProperty(globalThis, 'navigator', {
  value: {
    clipboard: {
      writeText: (t) => {
        clipCalls.push(t);
        return { then: (ok) => { ok(); return { catch() {} }; } };
      },
    },
  },
  configurable: true,
});
global.setTimeout = () => 0;
global.CSS = { escape: (s) => s };
(0, eval)(script);
// Dispatch an emitted entity link through both delegation levels. A
// modified click must survive the document's generic data-state handler.
const clickKinetics = (attrs, flags = {}) => {
  const a = mk();
  a.attrs = attrs;
  a.closest = selector => {
    if (selector === 'a.kinetics-selection') return a;
    if (selector === '[data-state]' && 'data-state' in attrs) return a;
    return null;
  };
  const ev = {
    target: a, button: 0, ...flags, prevented: false, stopped: false,
    preventDefault() { this.prevented = true; },
    stopPropagation() { this.stopped = true; },
  };
  (els.kinetics.handlers.click || []).forEach(f => f(ev));
  if (!ev.stopped) (docHandlers.click || []).forEach(f => f(ev));
  kineticsClicks.push({prevented: ev.prevented, stopped: ev.stopped});
};
// direct eval (not (0, eval)) — AFTER must see this scope's els/handlers
if (AFTER) { eval(AFTER); }
const note = els['pw-link-note'];
console.log(JSON.stringify({
  sel: els['pw-selection'] ? els['pw-selection'].innerHTML : null,
  note: note ? note.textContent : null,
  noteHidden: note ? note.classList.contains('hidden') : null,
  u: els['pw-u-slider'] ? els['pw-u-slider'].value : null,
  ph: els['pw-ph-input'] ? els['pw-ph-input'].value : null,
  uReadout: els['pw-u-readout'] ? els['pw-u-readout'].textContent : null,
  pressed: created
    .filter((el) => el.classList.contains('pw-path-btn'))
    .map((b) => b.attrs['aria-pressed'] || null),
  rs: rsCalls,
  clip: clipCalls,
  copyLabel: els['pw-copy-link'] ? els['pw-copy-link'].textContent : null,
  layers: created.filter((el) => el.classList.contains('pw-layer-toggle'))
    .map((el) => ({key: el.dataset.layer, checked: el.checked, title: el.title})),
  svg: els['pw-diagram'] ? els['pw-diagram'].innerHTML : null,
  diagramRects: diagramRects.map((rect) => rect.attrs),
  statesHidden: els['pw-states-panel'] ? els['pw-states-panel'].classList.contains('hidden') : null,
  hash: window.location.hash,
  explorerScrolls: scrolls.filter(el => el === els['pw-explorer']).length,
  kineticsClicks,
}));
"""


def _run_url_probe(
    tmp_path: Path, html: str, search: str = "", after: str = "", hash_: str = ""
) -> Any:
    """Run the page's own script under :data:`_NODE_URL_JS` and return the
    dumped selection/URL observations."""
    script = _explorer_script(html)
    (tmp_path / "page.js").write_text(script, encoding="utf-8")
    driver = tmp_path / "url_driver.js"
    driver.write_text(_NODE_URL_JS, encoding="utf-8")
    out = subprocess.run(
        ["node", str(driver), str(tmp_path / "page.js"), search, after, hash_],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=60,
    )
    return json.loads(out.stdout)


def test_pathway_detail_copy_link_and_note_rendered(client, runtime) -> None:
    """The copy-link control sits next to the selection readout and the
    stale-parameter notice element is on the page (hidden until needed)."""
    _seed_parked(runtime, _parked_graph())
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert 'id="pw-copy-link"' in resp.text
    assert 'id="pw-link-note"' in resp.text
    assert 'id="pw-selection"' in resp.text
    assert "history.replaceState" in resp.text


@needs_node
def test_pathway_url_state_round_trip(client, runtime, tmp_path: Path) -> None:
    """Selection -> URL -> restored selection: a state click, a path
    isolation, a U move and a pH entry each rewrite the address bar via
    ``history.replaceState`` (no reload), the copy-link button hands out
    that URL, and loading it restores the same view — the same state
    selected, the same path pressed, U/pH back in their controls — without
    itself rewriting the address bar."""
    _seed_parked(runtime, _parked_graph())
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200

    after = """
docHandlers.click.forEach((f) => f({target: {closest: () => ({getAttribute: () => 'NH3'})}}));
created.filter((e) => e.classList.contains('pw-path-btn'))[0].handlers.click[0]();
els['pw-u-slider'].value = '-1';
els['pw-u-slider'].handlers.input[0]();
els['pw-ph-input'].value = '7';
els['pw-ph-input'].handlers.input[0]();
els['pw-copy-link'].handlers.click[0]();
"""
    out = _run_url_probe(tmp_path, resp.text, after=after)
    # gr549591: u and ph are now ALWAYS written (defaults -0.5 / the
    # stub's ph value 0) — an explicit 0 must survive a copied link.
    assert out["rs"] == [
        "/refs/pathway/171696?state=NH3&u=-0.5&ph=0",
        "/refs/pathway/171696?state=NH3&path=NH3&u=-0.5&ph=0",
        "/refs/pathway/171696?state=NH3&path=NH3&u=-1&ph=0",
        "/refs/pathway/171696?state=NH3&path=NH3&u=-1&ph=7",
    ]
    assert out["clip"] == ["http://precis.test" + out["rs"][-1]]
    assert out["copyLabel"] == "copied ✓"

    restored = _run_url_probe(
        tmp_path, resp.text, search="?state=NH3&path=NH3&u=-1&ph=7"
    )
    assert restored["rs"] == []  # loading a link never rewrites it
    assert restored["noteHidden"] is True
    assert "NH3" in restored["sel"]
    assert restored["pressed"] == ["true", "false"]
    assert restored["u"] == "-1"
    assert restored["ph"] == "7"
    assert "-1.00" in restored["uReadout"]
    assert "pH 7" in restored["uReadout"]


@needs_node
def test_pathway_url_state_invalid_params_fall_back_with_notice(
    client, runtime, tmp_path: Path
) -> None:
    """Stale/invalid params each fall back to the default with a one-line
    notice naming what was ignored — never an error — while the valid ones
    still apply."""
    _seed_parked(runtime, _parked_graph())
    html = client.get("/refs/pathway/171696").text

    out = _run_url_probe(tmp_path, html, search="?state=zzz&path=zzz&u=abc&ph=xyz")
    assert out["noteHidden"] is False
    for p in ("state", "path", "u", "ph"):
        assert p in out["note"]
    # the default view: first state selected, lever defaults (gr549591:
    # fixed -0.5 U fallback; the stub's ph input reads '0'), nothing pressed
    assert "NO@N" in out["sel"]
    assert out["u"] == "-0.5"
    assert out["ph"] == "0"
    assert out["pressed"] == ["false", "false"]

    # a bad path leaf doesn't take the valid state down with it
    out = _run_url_probe(tmp_path, html, search="?state=NH3&path=zzz")
    assert "NH3" in out["sel"]
    assert out["noteHidden"] is False
    assert "path" in out["note"]

    # a valid leaf alone restores the path, keyed by leaf id not position
    out = _run_url_probe(tmp_path, html, search="?path=H2O")
    assert out["pressed"] == ["false", "true"]
    assert out["noteHidden"] is True


@needs_node
def test_pathway_url_state_escapes_state_ids(client, runtime, tmp_path: Path) -> None:
    """A state id with a literal ``+`` (coadsorbed fragments) percent-
    encodes into ``%2B`` — a bare ``+`` decodes as a space and resolves
    nothing — and decodes back to the same selection."""
    _seed_parked(runtime, _parked_graph())
    html = client.get("/refs/pathway/171696").text
    after = (
        "docHandlers.click.forEach((f) => f({target: {closest: () => "
        "({getAttribute: () => 'N+H'})}}));"
    )
    out = _run_url_probe(tmp_path, html, after=after)
    assert out["rs"][-1] == "/refs/pathway/171696?state=N%2BH&u=-0.5&ph=0"

    restored = _run_url_probe(tmp_path, html, search="?state=N%2BH")
    assert "N+H" in restored["sel"]
    assert restored["noteHidden"] is True

    # a raw '+' decodes to a space -> unknown state -> notice + default
    raw = _run_url_probe(tmp_path, html, search="?state=N+H")
    assert raw["noteHidden"] is False
    assert "N+H" not in (raw["sel"] or "")


@needs_node
def test_pathway_layers_controls_render_and_url_round_trip(
    client, runtime, tmp_path: Path
) -> None:
    """Checkbox changes redraw layers and survive copying a link with a hash."""
    _seed_parked(runtime, _parked_graph())
    html = client.get("/refs/pathway/171696").text
    defaults = _run_url_probe(tmp_path, html)
    assert len(defaults["layers"]) == 20
    assert all(layer["title"] for layer in defaults["layers"])
    assert [layer["checked"] for layer in defaults["layers"]] == [True] * 12 + [
        False
    ] * 8
    assert defaults["statesHidden"] is False
    assert 'class="pw-shoulder-drop"' in defaults["svg"]
    assert 'class="pw-hydro-arrow"' in defaults["svg"]
    assert "−NH₃ ↑" in defaults["svg"]
    explicit_defaults = "?" + "&".join(
        "layer_" + layer["key"] + ("=1" if layer["checked"] else "=0")
        for layer in defaults["layers"]
    )
    assert (
        _run_url_probe(tmp_path, html, search=explicit_defaults)["svg"]
        == defaults["svg"]
    )

    after = """
created.filter((el) => el.classList.contains('pw-layer-toggle')).forEach((el) => {
  el.checked = false;
  el.handlers.change[0]();
});
els['pw-copy-link'].handlers.click[0]();
"""
    off = _run_url_probe(tmp_path, html, after=after, hash_="#kinetics")
    assert all(not layer["checked"] for layer in off["layers"])
    assert off["statesHidden"] is True
    for marker in (
        'class="pw-shoulder-drop"',
        'class="pw-shoulder"',
        'class="pw-hydro-arrow"',
        "−NH₃ ↑",
    ):
        assert marker not in off["svg"]
    assert not re.search(r"<text[^>]*>Ea(?:=|≈| n/a)", off["svg"])
    assert set(_level_bars(off["svg"])) == set(_level_bars(defaults["svg"]))
    assert len(off["rs"]) == 20
    assert off["rs"][-1].endswith("#kinetics")
    assert off["clip"] == ["http://precis.test" + off["rs"][-1]]
    query = off["rs"][-1].split("?", 1)[1].split("#", 1)[0]
    assert query.count("layer_") == 12
    restored = _run_url_probe(tmp_path, html, search="?" + query, hash_="#kinetics")
    assert restored["rs"] == []
    assert restored["layers"] == off["layers"]
    assert restored["svg"] == off["svg"]
    assert restored["statesHidden"] is True
    assert restored["noteHidden"] is True

    back_on = _run_url_probe(
        tmp_path,
        html,
        search="?" + query,
        after="""
created.filter((el) => el.classList.contains('pw-layer-toggle')).forEach((el) => {
  el.checked = true;
  el.handlers.change[0]();
});
""",
        hash_="#kinetics",
    )
    all_on_query = "?" + "&".join(
        "layer_" + layer["key"] + "=1" for layer in defaults["layers"]
    )
    assert back_on["svg"] == _run_url_probe(tmp_path, html, search=all_on_query)["svg"]
    assert set(_level_bars(back_on["svg"])) == set(_level_bars(defaults["svg"]))
    assert all(layer["checked"] for layer in back_on["layers"])
    assert back_on["statesHidden"] is False
    assert back_on["rs"][-1].count("layer_") == 8
    for key in (
        "ea_forward",
        "ea_reverse",
        "k_f",
        "k_b",
        "net_rate",
        "z_i",
        "X_RC",
        "selectivity",
    ):
        assert "layer_kin_" + key + "=1" in back_on["rs"][-1]
    assert back_on["rs"][-1].endswith("#kinetics")


@needs_node
def test_pathway_states_layer_without_diagram_keeps_navigation(
    client, runtime, tmp_path: Path
) -> None:
    """gr559346: a copied layer setting cannot hide the only state navigation."""
    _seed_pathway(
        runtime.store,
        meta={"structure_refs": _GRAPH3_STRUCTURE_REFS},
        body_text=None,
    )
    _seed_explorer_scenes(runtime.store)
    resp = client.get("/refs/pathway/171696?layer_states=0")
    assert resp.status_code == 200
    assert 'id="pw-diagram"' not in resp.text
    for element_id in ("pw-states-panel", "pw-state-list", "pw-prev", "pw-next"):
        assert f'id="{element_id}"' in resp.text

    out = _run_url_probe(tmp_path, resp.text, search="?layer_states=0")
    assert out["statesHidden"] is False
    assert "s1" in out["sel"]


@needs_node
def test_pathway_states_layer_preserves_measures_and_warnings(
    client, runtime, tmp_path: Path
) -> None:
    """td563361: hiding state navigation leaves measure values and caveats visible."""
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
    resp = client.get("/refs/pathway/171696?layer_states=0")
    assert resp.status_code == 200
    out = _run_url_probe(tmp_path, resp.text, search="?layer_states=0")
    assert out["statesHidden"] is True

    # The JS probe stubs elements independently; parse the real markup to
    # catch measures accidentally nested under the hidden navigation panel.
    class PanelContents(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.div_ids: list[str | None] = []
            self.inside_ids: set[str | None] = set()
            self.outside_values: set[str | None] = set()
            self.outside_text: list[str] = []
            self.in_script = False

        def handle_starttag(
            self, tag: str, attrs: list[tuple[str, str | None]]
        ) -> None:
            attributes = dict(attrs)
            if tag == "script":
                self.in_script = True
            if tag == "div":
                self.div_ids.append(attributes.get("id"))
            if "pw-states-panel" in self.div_ids:
                self.inside_ids.add(attributes.get("id"))
            elif "data-measure-value" in attributes:
                self.outside_values.add(attributes["data-measure-value"])

        def handle_endtag(self, tag: str) -> None:
            if tag == "script":
                self.in_script = False
            if tag == "div":
                self.div_ids.pop()

        def handle_data(self, data: str) -> None:
            if not self.in_script and "pw-states-panel" not in self.div_ids:
                self.outside_text.append(data)

    panel = PanelContents()
    panel.feed(resp.text)
    assert {"pw-state-list", "pw-prev", "pw-next"} <= panel.inside_ids
    assert panel.outside_values == {"N-Pd", "N-Pd1"}
    outside_text = " ".join(panel.outside_text)
    assert "Measures" in outside_text
    assert "unverified across states" in outside_text
    assert "unknown op" in outside_text


@needs_node
def test_pathway_energy_and_confidence_layers_only_hide_presentation(
    client, runtime, tmp_path: Path
) -> None:
    _seed_pathway(runtime.store, meta={"graph": _GRAPH3}, body_text=None)
    html = client.get("/refs/pathway/171696").text
    defaults = _run_url_probe(tmp_path, html)
    off = _run_url_probe(tmp_path, html, search="?layer_energy=0&layer_confidence=0")
    assert "s1 (+0.00±0.01)" in defaults["svg"]
    assert "s3 ⚠" in defaults["svg"]
    assert 'stroke="#d9534f"' in defaults["svg"]
    assert "s1 (+0.00±0.01)" not in off["svg"]
    assert "s3 ⚠" not in off["svg"]
    assert 'stroke="#d9534f"' not in off["svg"]
    assert _level_bars(defaults["svg"]) == _level_bars(off["svg"])


@needs_node
def test_pathway_layer_invalid_url_value_has_notice(
    client, runtime, tmp_path: Path
) -> None:
    _seed_parked(runtime, _parked_graph())
    html = client.get("/refs/pathway/171696").text
    out = _run_url_probe(tmp_path, html, search="?layer_supply=maybe&layer_feed=0")
    layers = {layer["key"]: layer["checked"] for layer in out["layers"]}
    assert layers["supply"] is True
    assert layers["feed"] is False
    assert out["noteHidden"] is False
    assert "layer_supply" in out["note"]
    assert 'class="pw-shoulder-drop"' in out["svg"]
    assert 'class="pw-hydro-arrow"' not in out["svg"]


def _kinetics_page_links(
    client: Any, runtime: Any, tmp_path: Path
) -> tuple[str, list[dict[str, str]]]:
    """Get actual panel links from the graph JSON shipped by the page."""
    from tests.precis_web.test_pathway_kinetics import (
        _panel_probe,
        _record,
        _verdict_markup,
    )

    graph = _parked_graph()
    _seed_pathway(
        runtime.store,
        meta={
            "graph": graph,
            "results": {
                "substrate": "NO",
                "target": "NH3",
                "kinetics": _record(
                    coverages={"NH2+H": 0.9},
                    drc={"X_RC": {"NO@N -> N+O": 0.8}},
                    thermodynamic_drc={"X_TRC": {}},
                    transitions=[
                        {
                            "name": "NO@N -> N+O",
                            "from": "NO@N",
                            "to": "N+O",
                            "kind": "step",
                        }
                    ],
                ),
            },
        },
        body_text=None,
    )
    response = client.get("/refs/pathway/171696")
    assert response.status_code == 200
    html = response.text
    assert 'id="pw-explorer"' in html
    blobs = {
        name: json.loads(value)
        for name, value in re.findall(
            r'<script type="application/json" id="(kinetics[^\"]*)">(.*?)</script>',
            html,
            re.S,
        )
    }
    explorer = blobs["kinetics-explorer-data"]
    assert set(explorer["states"]) == {node["id"] for node in graph["nodes"]}
    assert explorer["paths"] == _graph_paths(graph)
    panel = _panel_probe(tmp_path, blobs["kinetics-data"], explorer=explorer)
    links = [
        link
        for link in _verdict_markup(panel["html"]).links
        if "kinetics-selection" in link.get("class", "")
    ]
    return html, links


@needs_node
def test_pathway_kinetics_emitted_links_select_state_and_toggle_path(
    client, runtime, tmp_path: Path
) -> None:
    html, links = _kinetics_page_links(client, runtime, tmp_path)
    state = next(link for link in links if "data-state" in link)
    path = next(link for link in links if "data-kinetics-path" in link)
    assert state["data-state"] == "NH2+H"
    assert path["data-kinetics-path"] == "0"
    after = f"clickKinetics({json.dumps(state)}); clickKinetics({json.dumps(path)});"
    out = _run_url_probe(tmp_path, html, after=after, hash_="#kinetics-coverages")
    assert "NH2+H" in out["sel"]
    assert out["pressed"] == ["true", "false"]
    assert out["kineticsClicks"] == [{"prevented": True, "stopped": True}] * 2
    assert out["explorerScrolls"] == 2
    assert out["hash"] == "#pw-explorer"
    assert out["rs"] == [
        "/refs/pathway/171696?state=NH2%2BH&u=-0.5&ph=0#pw-explorer",
        "/refs/pathway/171696?state=NH2%2BH&path=NH3&u=-0.5&ph=0#pw-explorer",
    ]
    toggled = _run_url_probe(
        tmp_path, html, after=after + f"clickKinetics({json.dumps(path)});"
    )
    assert toggled["pressed"] == ["false", "false"]
    assert "path=" not in toggled["rs"][-1]
    # Opening an actual href in a new tab restores the same selection.
    for link in (state, path):
        url = urlsplit(link["href"])
        restored = _run_url_probe(
            tmp_path, html, search="?" + url.query, hash_="#" + url.fragment
        )
        assert restored["hash"] == "#pw-explorer"
        assert restored["rs"] == []
        if link is state:
            assert "NH2+H" in restored["sel"]
        else:
            assert restored["pressed"] == ["true", "false"]


@needs_node
@pytest.mark.parametrize(
    "flags",
    [
        {"ctrlKey": True},
        {"metaKey": True},
        {"shiftKey": True},
        {"altKey": True},
        {"button": 1},
    ],
)
def test_pathway_kinetics_modified_click_keeps_browser_navigation(
    client, runtime, tmp_path: Path, flags: dict[str, Any]
) -> None:
    html, links = _kinetics_page_links(client, runtime, tmp_path)
    after = "".join(
        f"clickKinetics({json.dumps(link)}, {json.dumps(flags)});" for link in links
    )
    out = _run_url_probe(tmp_path, html, after=after, hash_="#kinetics-steps")
    assert out["rs"] == []
    assert "NO@N" in out["sel"]
    assert out["pressed"] == ["false", "false"]
    assert out["hash"] == "#kinetics-steps"
    assert out["explorerScrolls"] == 0
    assert out["kineticsClicks"] == [{"prevented": False, "stopped": False}] * len(
        links
    )


@needs_node
def test_pathway_url_preserves_section_hash_through_controls_and_copy(
    client, runtime, tmp_path: Path
) -> None:
    _seed_parked(runtime, _parked_graph())
    html = client.get("/refs/pathway/171696").text
    after = """
docHandlers.click.forEach(f => f({target: {closest: () => ({getAttribute: () => 'NH3'})}}));
created.filter(e => e.classList.contains('pw-path-btn'))[0].handlers.click[0]();
els['pw-u-slider'].value = '-1';
els['pw-u-slider'].handlers.input[0]();
els['pw-ph-input'].value = '7';
els['pw-ph-input'].handlers.input[0]();
els['pw-copy-link'].handlers.click[0]();
"""
    out = _run_url_probe(tmp_path, html, after=after, hash_="#kinetics-steps")
    assert out["rs"] == [
        "/refs/pathway/171696?state=NH3&u=-0.5&ph=0#kinetics-steps",
        "/refs/pathway/171696?state=NH3&path=NH3&u=-0.5&ph=0#kinetics-steps",
        "/refs/pathway/171696?state=NH3&path=NH3&u=-1&ph=0#kinetics-steps",
        "/refs/pathway/171696?state=NH3&path=NH3&u=-1&ph=7#kinetics-steps",
    ]
    assert out["clip"] == ["http://precis.test" + out["rs"][-1]]
    copied = urlsplit(out["clip"][0])
    restored = _run_url_probe(
        tmp_path,
        html,
        search="?" + copied.query,
        hash_="#" + copied.fragment,
        after="els['pw-copy-link'].handlers.click[0]();",
    )
    assert restored["rs"] == []
    assert restored["hash"] == "#kinetics-steps"
    assert "NH3" in restored["sel"]
    assert restored["pressed"] == ["true", "false"]
    assert restored["u"] == "-1" and restored["ph"] == "7"
    assert restored["clip"] == out["clip"]


@needs_node
def test_pathway_kinetics_shared_leaf_link_restores_the_selected_route(
    client, runtime, tmp_path: Path
) -> None:
    """A leaf alone cannot identify the second arm of a diamond graph."""
    from tests.precis_web.test_pathway_kinetics import (
        _panel_probe,
        _record,
        _verdict_markup,
    )

    graph = {
        "nodes": [
            {"id": sid, "rel_energy": 0.1 * i}
            for i, sid in enumerate(["A", "B", "C", "D"])
        ],
        "links": [
            {"source": s, "target": t, "kind": "reaction", "barrier": 0.5}
            for s, t in [("A", "B"), ("B", "D"), ("A", "C"), ("C", "D")]
        ],
    }
    _seed_pathway(
        runtime.store,
        meta={
            "graph": graph,
            "results": {
                "target": "D",
                "kinetics": _record(
                    product="D",
                    coverages={},
                    thermodynamic_drc={"X_TRC": {}},
                    drc={"X_RC": {"C -> D": 0.8}},
                    transitions=[
                        {"name": "C -> D", "from": "C", "to": "D", "kind": "step"}
                    ],
                ),
            },
        },
        body_text=None,
    )
    html = client.get("/refs/pathway/171696").text
    blobs = {
        name: json.loads(value)
        for name, value in re.findall(
            r'<script type="application/json" id="(kinetics[^\"]*)">(.*?)</script>',
            html,
            re.S,
        )
    }
    explorer = blobs["kinetics-explorer-data"]
    path_index = explorer["paths"].index(["A", "C", "D"])
    panel = _panel_probe(tmp_path, blobs["kinetics-data"], explorer=explorer)
    (link,) = [
        link
        for link in _verdict_markup(panel["html"]).links
        if "data-kinetics-path" in link
    ]
    assert link["data-kinetics-path"] == str(path_index)
    url = urlsplit(link["href"])
    query = parse_qs(url.query)
    assert query["path"] == ["D"]
    assert json.loads(query["route"][0]) == ["A", "C", "D"]
    clicked = _run_url_probe(
        tmp_path,
        html,
        after=f"clickKinetics({json.dumps(link)}); els['pw-copy-link'].handlers.click[0]();",
    )
    expected = ["true" if i == path_index else "false" for i in range(2)]
    assert clicked["pressed"] == expected
    copied = urlsplit(clicked["clip"][0])
    assert json.loads(parse_qs(copied.query)["route"][0]) == ["A", "C", "D"]
    for location in (url, copied):
        restored = _run_url_probe(
            tmp_path,
            html,
            search="?" + location.query,
            hash_="#" + location.fragment,
        )
        assert restored["pressed"] == expected
        assert restored["hash"] == "#pw-explorer"
        assert restored["rs"] == []
        assert restored["noteHidden"] is True


@needs_node
@pytest.mark.parametrize("suffix", ["", "~history"])
def test_folded_reaction_warning_highlights_rendered_segment(
    client, runtime, tmp_path: Path, suffix: str
) -> None:
    """The warning names N+H -> NH, while the profile draws N -> NH."""
    from precis_web.pathway_kinetics import kinetics_payload
    from tests.precis_web.test_pathway_kinetics import _panel_probe, _record

    states = ["N", "N+H" + suffix, "NH" + suffix, "NH2"]
    graph = {
        "nodes": [
            {"id": state, "rel_energy": i * 0.1} for i, state in enumerate(states)
        ],
        "links": [
            {
                "source": source,
                "target": target,
                "kind": "supply" if i == 0 else "reaction",
                "barrier": 0.3,
            }
            for i, (source, target) in enumerate(pairwise(states))
        ],
    }
    _seed_pathway(runtime.store, meta={"graph": graph}, body_text=None)
    html = client.get("/refs/pathway/171696").text
    svg = _render_diagram_svg(tmp_path, html)
    segments = re.findall(r'<polyline class="pw-seg"([^>]*)>', svg)
    profile = [dict(re.findall(r'([\w-]+)="([^"]*)"', segment)) for segment in segments]
    assert [(segment["data-from"], segment["data-to"]) for segment in profile] == [
        ("N", "NH" + suffix),
        ("NH" + suffix, "NH2"),
    ]
    payload = kinetics_payload(
        {
            "kinetics": _record(
                warnings=["kinetics: X_RC of N+H->NH is undefined"],
                transitions=[
                    {"name": "N+H->NH", "from": "N+H", "to": "NH", "kind": "step"}
                ],
            )
        }
    )
    assert payload is not None
    out = _panel_probe(
        tmp_path,
        payload,
        profile=profile,
        after="""
const anchor = {getAttribute: () => '#kinetics-step-0'};
const target = {closest: selector => selector === "a[href^='#kinetics-']" ? anchor : null};
handlers.click.forEach(f => f({target, button: 0}));
""",
    )
    assert out["highlighted"] == ["kinetics-step-0"]
    assert "kinetics-steps" in out["open"]
    assert out["profileHighlights"] == [True, False]
