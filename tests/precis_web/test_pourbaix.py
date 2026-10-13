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
