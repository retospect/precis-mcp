"""Surface-Pourbaix slice 2: the resting-state validity map over stored
``meta.che_sweep`` rows — the pure builder, the footing partition, the bulk
overlay, and both kind views.

The synthetic coverage payload mirrors ``tests/test_surface_pourbaix.py``'s
fixture (one facet, ``adsorbates[frag].points[{n, theta, energy, gamma}]``);
the sweep it feeds is the same shape ``coverage_job._dispatch`` stamps.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.handlers.structure import StructureHandler
from precis.quest import pourbaix_map as pm
from precis.store import Store
from precis.store.types import Tag
from precis_pathway import surface_pourbaix as sp

AREA = 10.0
KEY = "a" * 64


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


def _sweep(payloads: dict[str, dict[str, Any]], **kw: Any) -> dict[str, Any]:
    kw.setdefault("u_window", (-0.5, 0.5))
    kw.setdefault("grid", 21)
    kw.setdefault("sigma_e_ev", 0.05)
    return sp.sweep(payloads, **kw)


def _job(
    ref_id: int,
    model: str,
    sweep: dict[str, Any] | None,
    *,
    anchor_key: str = KEY,
    engine_version: str = "0.24.0",
    corrections: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ref_id": ref_id,
        "model": model,
        "anchor_key": anchor_key,
        "engine_version": engine_version,
        "corrections": corrections,
        "che_sweep": sweep,
    }


# ── build_map (pure) ────────────────────────────────────────────────────


def test_map_regions_split_the_window_at_the_boundary() -> None:
    """H* wins below U* = 0.2 V, clean above: two validity regions, each
    carrying its U interval, backing models and trust label."""
    sweep = _sweep({"m": _payload("m", {"H": [-0.02]})})
    m = pm.build_map([_job(7, "m", sweep)])
    assert [r["label"] for r in m["regions"]] == ["H* at 0.25 ML", "clean"]
    assert m["regions"][0]["U_RHE_range"] == pytest.approx([-0.5, 0.2], abs=0.06)
    assert m["regions"][1]["U_RHE_range"][1] == pytest.approx(0.5)
    assert m["regions"][0]["jobs"] == [7]
    assert m["regions"][1]["jobs"] == [7], "the clean slab is measured too"
    assert "uncorrected footing" in m["regions"][0]["trust"]
    assert m["uncorrected"] is True
    (b,) = m["boundaries"]
    assert b["U_RHE"] == pytest.approx(0.2)
    assert b["model_form_complete"] is False
    assert b["propagated_complete"] is True


def test_neighbouring_regions_share_the_boundary_edge() -> None:
    """Sampled rows stop one grid step short of the crossing; both regions
    end at the stored boundary U, so a U between two samples is covered."""
    from precis.quest import staircase

    sweep = _sweep({"m": _payload("m", {"H": [-0.02]})}, grid=6)
    m = pm.build_map([_job(7, "m", sweep)])
    lo, hi = m["regions"]
    assert lo["U_RHE_range"][1] == hi["U_RHE_range"][0] == pytest.approx(0.2)
    assert staircase.resting_label(m, 0.15) == "H* at 0.25 ML"
    assert staircase.resting_label(m, 0.25) == "clean"


def test_propagated_band_missing_is_incomplete() -> None:
    sweep = _sweep({"m": _payload("m", {"H": [-0.02]})})
    for b in sweep["boundaries"]:
        b["band_propagated"] = None
    (b,) = pm.build_map([_job(7, "m", sweep)])["boundaries"]
    assert b["propagated_complete"] is False


def test_incomplete_marks_missing_uncertainty_inputs_never_invented() -> None:
    """One model → model-form band absent and the map says so; no recorded
    correction set → the footing is provenance-incomplete. Neither gets an
    invented sigma."""
    m = pm.build_map([_job(1, "m", _sweep({"m": _payload("m", {"H": [-0.02]})}))])
    assert any("model-form" in i for i in m["incomplete"])
    assert any("correction set unrecorded" in i for i in m["incomplete"])
    m2 = pm.build_map(
        [
            _job(
                1,
                "a",
                _sweep(
                    {
                        "a": _payload("a", {"H": [-0.02]}),
                        "b": _payload("b", {"H": [-0.03]}),
                    }
                ),
            ),
            _job(2, "b", None, corrections={"gas": "catsel19"}),
        ]
    )
    assert not any("model-form" in i for i in m2["incomplete"])
    assert not any("correction set" in i for i in m2["incomplete"])
    assert m2["uncorrected"] is False
    assert m2["sweep_job"] == 1, "the newest job carrying a sweep supplies it"


def test_group_without_a_sweep_is_an_empty_map_not_a_fabricated_one() -> None:
    m = pm.build_map([_job(3, "m", None)])
    assert m["regions"] == [] and m["boundaries"] == []
    assert any("no pooled che_sweep" in i for i in m["incomplete"])


def test_bulk_layer_is_present_labelled_uncertain() -> None:
    meta = {
        "pourbaix_verdict": "stable",
        "pourbaix_worst_in_window": "dissolved",
        "pourbaix_dG_eV_atom": -0.03,
        "pourbaix_domain": ["Pd"],
        "pourbaix_basis": {
            "U_RHE": -0.2,
            "pH": 7,
            "ion_conc_M": 1e-6,
            "window": {"U_RHE": [-0.4, 0], "pH": [7, 10]},
            "matched_phase": "Pd",
            "mp_version": "v2026.10",
        },
        "pourbaix_job_id": 55,
    }
    verdict = {
        "grid": [
            {"U_RHE": -0.4, "pH": 7, "verdict": "stable", "domain": ["Pd"]},
            {"U_RHE": 0.0, "pH": 10, "verdict": "dissolved", "domain": ["PdO2"]},
        ],
        "source": "Materials Project v2026.10, CC-BY 4.0",
    }
    layer = pm.bulk_layer(meta, verdict)
    assert layer is not None and layer["uncertain"] is True
    assert layer["point"]["verdict"] == "stable"
    assert layer["worst_in_window"] == "dissolved"
    assert len(layer["cells"]) == 2
    assert "uncertain" in layer["note"] and "never merged" in layer["note"]
    assert pm.bulk_layer({}, None) is None


def test_render_map_names_provenance_bands_and_bulk() -> None:
    sweep = _sweep({"m": _payload("m", {"H": [-0.02]})})
    m = pm.build_map(
        [_job(9, "m", sweep)],
        bulk={
            "present": True,
            "point": {
                "U_RHE": -0.2,
                "pH": 7,
                "verdict": "stable",
                "domain": ["Pd"],
                "dG_pbx_eV_atom": -0.03,
            },
            "window": None,
            "worst_in_window": "stable",
            "cells": [],
            "job_id": 55,
            "uncertain": True,
            "note": pm.BULK_LAYER_NOTE,
        },
    )
    text = pm.render_map(m)
    assert "anchor_key" in text and "0.24.0" in text and "jo9" in text
    assert "H* at 0.25 ML" in text and "clean" in text
    assert "model-form n/a" in text and "incomplete" in text
    assert "bulk layer" in text and "uncertain" in text
    assert "needs_anchor" in text


# ── store collectors + footing partition ────────────────────────────────


def _scan_ref(
    store: Store,
    *,
    parent_id: int,
    model: str,
    status: str,
    anchor_key: str = KEY,
    sweep: dict[str, Any] | None = None,
    engine_version: str = "0.24.0",
    corrections: dict[str, Any] | None = None,
) -> int:
    meta: dict[str, Any] = {
        "job_type": pm.SCAN_JOB_TYPE,
        "anchor_key": anchor_key,
        "model": model,
        "engine_version": engine_version,
        "corrections": corrections,
    }
    if sweep is not None:
        meta["che_sweep"] = sweep
    ref = store.insert_ref(
        kind="job", slug=None, title=f"scan {model}", parent_id=parent_id, meta=meta
    )
    store.add_tag(
        ref.id, Tag.closed("STATUS", status), set_by="agent", replace_prefix=True
    )
    return ref.id


def _structure(store: Store, hub: Hub, slug: str = "pd111") -> Any:
    StructureHandler(hub=hub).put(
        id=slug,
        text=json.dumps(
            {
                "cell": {"a": 10.0, "b": 10.0, "c": 10.0, "pbc": [True, True, False]},
                "ops": [{"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]}],
            }
        ),
    )
    return store.get_ref(kind="structure", id=slug)


def test_scan_jobs_follow_the_parent_convention(store: Store, hub: Hub) -> None:
    st = _structure(store, hub)
    other = _structure(store, hub, "other_slab")
    todo = store.insert_ref(
        kind="todo", slug=None, title="scan todo", parent_id=st.id, meta={}
    )
    direct = _scan_ref(
        store,
        parent_id=st.id,
        model="m",
        status="succeeded",
        sweep=_sweep({"m": _payload("m", {"H": [-0.02]})}),
    )
    via_todo = _scan_ref(
        store,
        parent_id=todo.id,
        model="n",
        status="succeeded",
        sweep=_sweep({"n": _payload("n", {"H": [-0.03]})}),
    )
    _scan_ref(store, parent_id=st.id, model="m", status="failed")
    _scan_ref(store, parent_id=other.id, model="m", status="succeeded")
    found = pm.scan_jobs(store, st.id)
    assert [j["ref_id"] for j in found] == [via_todo, direct]


def test_mixed_footings_partition_never_blend(store: Store, hub: Hub) -> None:
    """Two anchor keys (and a corrections difference on a third) give three
    maps — one footing each (ruling q2)."""
    st = _structure(store, hub)
    s = _sweep({"m": _payload("m", {"H": [-0.02]})})
    _scan_ref(store, parent_id=st.id, model="m", status="succeeded", sweep=s)
    _scan_ref(
        store,
        parent_id=st.id,
        model="m",
        status="succeeded",
        anchor_key="b" * 64,
        sweep=s,
    )
    _scan_ref(
        store,
        parent_id=st.id,
        model="m",
        status="succeeded",
        anchor_key="b" * 64,
        sweep=s,
        corrections={"gas": "catsel19"},
    )
    maps = pm.maps_for_structure(store, st)
    assert len(maps) == 3
    assert len({mp["anchor_key"] for mp in maps}) == 2
    assert sum(1 for mp in maps if mp["uncorrected"]) == 2


def _corr(model: str | None, *, version: str = "2026-10-02") -> dict[str, Any]:
    """An engine 0.25 correction block: both levers enabled, a set for
    ``model`` (``None`` → the table has no entry: nothing applied)."""
    key = model.split(":") if model else None
    return {
        "gas": {
            "anchors": ["H2", "N2", "H2O"],
            "enabled": True,
            "sets": {model: {"key": key, "values": {"NO": 0.15}}} if model else {},
            "unknown": [] if model else [["mace", "large"]],
        },
        "h_star": {
            "enabled": True,
            "host_metal": "Pd",
            "sets": {model: {"key": [*key, "Pd"]}} if key else {},
            "unanchored": [] if model else [["mace", "large", "Pd"]],
        },
        "version": version,
    }


def test_correction_footing_drops_the_per_model_fit_only() -> None:
    """Two models corrected under the same table share a footing; a model
    the table has no set for, or a different table version, does not."""
    a, b = _corr("mace:medium"), _corr("mace:small")
    assert pm.correction_footing(a) == pm.correction_footing(b)
    assert pm.correction_footing(a) != pm.correction_footing(_corr(None))
    assert pm.correction_footing(a) != pm.correction_footing(
        _corr("mace:medium", version="2026-11-01")
    )
    assert pm.correction_footing(None) is None
    assert pm.applied_levers(a) == ["gas", "h_star"]
    assert pm.applied_levers(_corr(None)) == []
    assert pm.applied_levers({"gas": "catsel19"}) == ["gas"]


def test_a_sweep_blended_across_footings_is_not_drawn() -> None:
    """gr489162 (jo486844 on qu164903): the mace:large job's stored sweep
    pooled mace:medium anchors recorded under a different correction
    footing. That footing's map must not draw the blend (ruling q2) — no
    regions, no bands, the reason named — and with nothing applied it is
    labelled uncorrected, not "corrected footing" (gr486949 comment 2)."""
    blended = _sweep(
        {
            "mace:large": _payload("mace:large", {"H": [-0.02]}),
            "mace:medium": _payload("mace:medium", {"H": [-0.05]}),
        }
    )
    m = pm.build_map([_job(486844, "mace:large", blended, corrections=_corr(None))])
    assert m["regions"] == [] and m["boundaries"] == []
    assert m["U_RHE_window"] == blended["U_RHE_window"]
    note = next(i for i in m["incomplete"] if "another footing" in i)
    assert "jo486844" in note and "mace:medium" in note
    assert m["uncorrected"] is True
    assert any("none applied" in i for i in m["incomplete"])
    assert "uncorrected (none applied)" in pm.render_map(m)


def test_a_sweep_stamped_with_another_footing_key_is_not_drawn() -> None:
    sweep = _sweep({"m": _payload("m", {"H": [-0.02]})})
    sweep["footing_key"] = pm.correction_footing(_corr("m"))
    ok = pm.build_map([_job(1, "m", sweep, corrections=_corr("m"))])
    assert ok["regions"] and not any("another footing" in i for i in ok["incomplete"])
    off = pm.build_map([_job(1, "m", sweep, corrections=_corr(None))])
    assert off["regions"] == []
    assert any("another footing" in i for i in off["incomplete"])


def test_models_corrected_alike_share_one_map(store: Store, hub: Hub) -> None:
    """Two models corrected under the same table are one footing — one map
    with a model-form band — even though each records its own fitted set."""
    st = _structure(store, hub)
    s = _sweep(
        {
            "mace:medium": _payload("mace:medium", {"H": [-0.02]}),
            "mace:small": _payload("mace:small", {"H": [-0.03]}),
        }
    )
    _scan_ref(
        store,
        parent_id=st.id,
        model="mace:medium",
        status="succeeded",
        corrections=_corr("mace:medium"),
    )
    _scan_ref(
        store,
        parent_id=st.id,
        model="mace:small",
        status="succeeded",
        sweep=s,
        corrections=_corr("mace:small"),
    )
    (mp,) = pm.maps_for_structure(store, st)
    assert mp["models"] == ["mace:medium", "mace:small"] and mp["n_models"] == 2
    assert mp["regions"] and mp["uncorrected"] is False


def test_structure_view_renders_the_map_and_the_no_anchor_state(
    store: Store, hub: Hub
) -> None:
    handler = StructureHandler(hub=hub)
    st = _structure(store, hub, "pd_anchored")
    _scan_ref(
        store,
        parent_id=st.id,
        model="m",
        status="succeeded",
        sweep=_sweep({"m": _payload("m", {"H": [-0.02]})}),
    )
    body = handler.get(id="pd_anchored", view="pourbaix").body
    assert "surface-Pourbaix" in body and "H* at 0.25 ML" in body
    assert "engine 0.24.0" in body and "uncorrected" in body
    bare = _structure(store, hub, "pd_bare")
    assert bare is not None
    body2 = handler.get(id="pd_bare", view="pourbaix").body
    assert "no anchors" in body2


def test_quest_view_maps_anchored_candidates_and_lists_the_rest(
    store: Store, hub: Hub
) -> None:
    qh = QuestHandler(hub=hub)
    created = qh.put(text="catalyst quest")
    match = re.search(r"qu(\d+)", created.body)
    assert match is not None
    qid = int(match.group(1))
    anchored = _structure(store, hub, "cand_a")
    bare = _structure(store, hub, "cand_b")
    for s in (anchored, bare):
        store.add_link(src_ref_id=s.id, dst_ref_id=qid, relation="serves")
    _scan_ref(
        store,
        parent_id=anchored.id,
        model="m",
        status="succeeded",
        sweep=_sweep({"m": _payload("m", {"H": [-0.02]})}),
    )
    body = qh.get(id=qid, view="pourbaix").body
    assert f"st{anchored.id}" in body and "H* at 0.25 ML" in body
    assert f"st{bare.id}" in body and "no anchors" in body


def test_structure_view_lists_pourbaix(store: Store, hub: Hub) -> None:
    """'pourbaix' is a registered structure view; an unknown view still
    raises BadInput (the error's next: enumerates _VIEWS)."""
    from precis.errors import BadInput
    from precis.handlers import structure as structure_mod

    assert "pourbaix" in structure_mod._VIEWS
    handler = StructureHandler(hub=hub)
    _structure(store, hub)
    with pytest.raises(BadInput):
        handler.get(id="pd111", view="definitely-not-a-view")


# ── pathway-side name ───────────────────────────────────────────────────


def test_pathway_module_reexports_the_same_builder() -> None:
    from precis_pathway import pourbaix_map as ppm

    assert ppm.build_map is pm.build_map
    assert ppm.SCAN_JOB_TYPE == pm.SCAN_JOB_TYPE == "surface_coverage_scan"
