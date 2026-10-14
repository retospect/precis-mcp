"""``/pourbaix/{handle}`` — the resting-state surface-Pourbaix page.

Real-store integration (the route's SQL is real); the sweeps are synthetic
payloads through :func:`precis_pathway.surface_pourbaix.sweep`, the same
shape ``coverage_job._dispatch`` stamps as ``meta.che_sweep``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.handlers.structure import StructureHandler
from precis.store.types import Tag
from precis_pathway import surface_pourbaix as sp
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.pourbaix_svg import map_svg

AREA = 10.0


def _payload(model: str, gammas: dict[str, list[float]]) -> dict[str, Any]:
    ads = {}
    for frag, gs in gammas.items():
        ads[frag] = {
            "mu": -3.0,
            "site": "fcc",
            "c_ads": 0.0,
            "points": [
                {
                    "n": i + 1,
                    "theta": (i + 1) / 4,
                    "energy": -100.0 + g * AREA,
                    "gamma": g,
                    "converged": True,
                    "detached": False,
                }
                for i, g in enumerate(gs)
            ],
            "best": {"theta": 0.25, "n": 1, "gamma": min(gs)},
        }
    return {
        "model": model,
        "facets": [
            {
                "facet": "111",
                "miller": [1, 1, 1],
                "area": AREA,
                "n_top": 4,
                "n_slab": 36,
                "e_slab": -100.0,
                "e_bulk_per_atom": -3.0,
                "gamma_clean": 0.1,
                "adsorbates": ads,
                "winner": {"adsorbate": None, "theta": 0.0, "gamma": 0.0},
            }
        ],
        "warnings": [],
    }


def _sweep() -> dict[str, Any]:
    return sp.sweep(
        {"m": _payload("m", {"H": [-0.02]})},
        u_window=(-0.5, 0.5),
        grid=21,
        sigma_e_ev=0.05,
    )


@pytest.fixture
def pb_client(runtime_with_store, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _seed_structure(runtime_with_store, *, with_scan: bool = True) -> int:
    hub = runtime_with_store.hub
    store = runtime_with_store.store
    StructureHandler(hub=hub).put(
        id="pd_map",
        text=json.dumps(
            {
                "cell": {
                    "a": 10.0,
                    "b": 10.0,
                    "c": 10.0,
                    "pbc": [True, True, False],
                },
                "ops": [{"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]}],
            }
        ),
    )
    st = store.get_ref(kind="structure", id="pd_map")
    if with_scan:
        job = store.insert_ref(
            kind="job",
            slug=None,
            title="scan m",
            parent_id=st.id,
            meta={
                "job_type": "surface_coverage_scan",
                "anchor_key": "a" * 64,
                "model": "m",
                "engine_version": "0.24.0",
                "corrections": None,
                "che_sweep": _sweep(),
            },
        )
        store.add_tag(
            job.id,
            Tag.closed("STATUS", "succeeded"),
            set_by="agent",
            replace_prefix=True,
        )
    return st.id


def test_pourbaix_unknown_handle_404(pb_client: TestClient) -> None:
    r = pb_client.get("/pourbaix/nope")
    assert r.status_code == 404


def test_pourbaix_structure_page_shows_map_provenance_and_incomplete(
    pb_client: TestClient, runtime_with_store
) -> None:
    sid = _seed_structure(runtime_with_store)
    r = pb_client.get(f"/pourbaix/st{sid}")
    assert r.status_code == 200
    text = r.text
    assert "<svg" in text and "U (V vs RHE)" in text
    # provenance footer: anchor key, model, engine, corrections, scan job
    assert "anchor_key" in text and "0.24.0" in text
    assert "scan jobs:" in text
    # one model → the model-form band is absent and the page says incomplete
    assert "incomplete:" in text and "model-form" in text
    assert "uncorrected anchors" in text


def test_pourbaix_structure_page_no_anchors(
    pb_client: TestClient, runtime_with_store
) -> None:
    _seed_structure(runtime_with_store, with_scan=False)
    st = runtime_with_store.store.get_ref(kind="structure", id="pd_map")
    r = pb_client.get(f"/pourbaix/st{st.id}")
    assert r.status_code == 200
    assert "no anchors" in r.text


# ── slice 3: the staircase panel on the shared slider bank ───────────────


def _pathway_results() -> dict[str, Any]:
    """A 0.25.0-shape results.json (the ``che_affine`` export + HER)."""
    return {
        "substrate": "NO",
        "target": "NH3",
        "network_digest": "d" * 16,
        "corrections": {"gas": "catsel19"},
        "nodes": {"*": {"mean": 0.0, "std": 0.0}},
        "U_L": -0.15,
        "che_affine": {
            "scale": "RHE",
            "T": 298.15,
            "pH": 0.0,
            "per_route": [
                {
                    "route": "* -> NO -> N+OH -> *~nh3",
                    "steps": [
                        {"edge": "no_ads", "delta_g0": -0.4, "n_e": 0},
                        {"edge": "no_reduce", "delta_g0": 0.3, "n_e": 2},
                        {"edge": "nh3_desorb", "delta_g0": 0.3, "n_e": 0},
                    ],
                    "U_L": -0.15,
                    "U_opt": -0.1,
                    "span_at_UL": 0.7,
                }
            ],
        },
        "her": {
            "U_L_her": -0.2,
            "product": "H2",
            "leaf": "*~h2",
            "dG_H_star": -0.1,
            "edges": [
                {
                    "step": "Volmer",
                    "edge": "*->H",
                    "reactant": "*",
                    "product": "H",
                    "delta_g0": -0.1,
                    "n_e": 1,
                }
            ],
        },
    }


def _pathway(store: Any, slug: str, results: dict[str, Any] | None) -> Any:
    return store.insert_ref(
        kind="pathway",
        slug=slug,
        title="NO → NH3 on Pd",
        meta={
            "status": "ready",
            "autocatpath_version": "0.25.0",
            "results": results or {},
        },
    )


def test_pourbaix_page_shows_map_and_staircase_on_one_bank(
    pb_client: TestClient, runtime_with_store
) -> None:
    store = runtime_with_store.store
    sid = _seed_structure(runtime_with_store)
    pw = _pathway(store, "pw_linked", _pathway_results())
    store.add_link(
        src_ref_id=sid, dst_ref_id=pw.id, relation="related-to", set_by="system"
    )
    r = pb_client.get(f"/pourbaix/st{sid}")
    assert r.status_code == 200
    text = r.text
    # the shared slider bank
    assert 'id="pbx-u"' in text and 'id="pbx-ph"' in text
    # the map carries the U marker + the U_L/U_opt/U_L_her verticals
    assert "pbx-u-marker" in text
    assert "U_L1" in text and "U_opt1" in text and "U_L_her" in text
    # the staircase panel: rungs with affine terms, the pathway link
    assert "pbx-staircase" in text and "no_reduce" in text
    assert 'href="/refs/pathway/' in text
    assert "HER competitor" in text


def test_pourbaix_page_staircase_only_when_no_anchors(
    pb_client: TestClient, runtime_with_store
) -> None:
    store = runtime_with_store.store
    _seed_structure(runtime_with_store, with_scan=False)
    st = store.get_ref(kind="structure", id="pd_map")
    pw = _pathway(store, "pw_only", _pathway_results())
    store.add_link(
        src_ref_id=st.id, dst_ref_id=pw.id, relation="related-to", set_by="system"
    )
    r = pb_client.get(f"/pourbaix/st{st.id}")
    assert r.status_code == 200
    assert "no anchors" in r.text
    assert "pbx-staircase" in r.text and "no_reduce" in r.text
    assert "unverified" in r.text  # no resting map — starting surface unverified


def test_pourbaix_page_map_only_when_no_pathway(
    pb_client: TestClient, runtime_with_store
) -> None:
    sid = _seed_structure(runtime_with_store)
    r = pb_client.get(f"/pourbaix/st{sid}")
    assert r.status_code == 200
    assert "<svg" in r.text and "pbx-staircase" not in r.text


def test_pourbaix_quest_page_maps_candidates(
    pb_client: TestClient, runtime_with_store
) -> None:
    hub = runtime_with_store.hub
    store = runtime_with_store.store
    sid = _seed_structure(runtime_with_store)
    from precis.handlers.quest import QuestHandler

    created = QuestHandler(hub=hub).put(text="map quest")
    import re

    match = re.search(r"qu(\d+)", created.body)
    assert match is not None
    qid = int(match.group(1))
    store.add_link(src_ref_id=sid, dst_ref_id=qid, relation="serves")
    r = pb_client.get(f"/pourbaix/qu{qid}")
    assert r.status_code == 200
    assert f"st{sid}" in r.text and "<svg" in r.text


def test_map_svg_draws_regions_bands_and_bulk_overlay() -> None:
    from precis.quest import pourbaix_map as pm

    m = pm.build_map(
        [
            {
                "ref_id": 9,
                "model": "m",
                "anchor_key": "a" * 64,
                "engine_version": "0.24.0",
                "corrections": None,
                "che_sweep": _sweep(),
            }
        ],
        bulk={
            "present": True,
            "point": {"U_RHE": -0.2, "pH": 7.0, "verdict": "stable", "domain": ["Pd"]},
            "window": None,
            "worst_in_window": "stable",
            "cells": [
                {"U_RHE": -0.4, "pH": 7.0, "verdict": "stable", "domain": ["Pd"]},
                {"U_RHE": 0.0, "pH": 10.0, "verdict": "dissolved", "domain": ["PdO2"]},
            ],
            "job_id": 55,
            "uncertain": True,
            "note": pm.BULK_LAYER_NOTE,
        },
    )
    svg = map_svg(m)
    assert "<svg" in svg and "stroke-dasharray" in svg
    assert "bulk layer (uncertain)" in svg
    assert "H* at 0.25 ML" in svg and "clean" in svg
    assert "mfHatch" in svg, "the model-form band hatch pattern is defined"


def test_map_svg_empty_map_is_an_honest_placeholder() -> None:
    svg = map_svg({"U_RHE_window": [None, None]})
    assert "no sweep window" in svg


# ── slice 7: the reset layer on the map ──────────────────────────────────


def _reset_results() -> dict[str, Any]:
    """A route whose reset layer is non-degenerate over the swept
    window: e1 flat-downhill (guaranteed everywhere), e2 electrochemical
    (viable U <= 0, guaranteed U <= -0.2 at η = 0.2)."""
    r = _pathway_results()
    che = r["che_affine"]
    assert isinstance(che, dict)
    route = che["per_route"][0]
    route["route"] = "* -> NO -> *~nh3"
    route["steps"] = [
        {"edge": "e1", "delta_g0": -0.6, "n_e": 0},
        {"edge": "e2", "delta_g0": 0.0, "n_e": 1},
    ]
    return r


def test_map_svg_draws_the_reset_layer_apart_from_the_dft_bands() -> None:
    m = {
        "U_RHE_window": [-0.5, 0.5],
        "pH_window": [0.0, 14.0],
        "regions": [],
        "boundaries": [],
    }
    svg = map_svg(
        m,
        reset=[
            {
                "name": "r1",
                "color": "#7c3aed",
                "ok": [[-0.5, -0.2]],
                "band": [[-0.2, 0.0]],
            }
        ],
    )
    assert 'class="pbx-reset"' in svg
    assert 'stroke-dasharray="3 3"' in svg  # the hysteretic band edge
    assert "r1 reset (dashed = hysteretic)" in svg
    svg_off = map_svg(m)
    assert "pbx-reset" not in svg_off


def test_pourbaix_page_draws_reset_layer_and_ships_rung_margins(
    pb_client: TestClient, runtime_with_store
) -> None:
    store = runtime_with_store.store
    sid = _seed_structure(runtime_with_store)
    pw = _pathway(store, "pw_reset", _reset_results())
    store.add_link(
        src_ref_id=sid, dst_ref_id=pw.id, relation="related-to", set_by="system"
    )
    r = pb_client.get(f"/pourbaix/st{sid}")
    assert r.status_code == 200
    text = r.text
    assert 'id="pbx-reset-toggle"' in text
    assert 'class="pbx-reset"' in text
    assert "hysteretic" in text
    # the client-side verdict needs the per-rung required overdrive
    assert "data-rmargin" in text and "m_e" in text
