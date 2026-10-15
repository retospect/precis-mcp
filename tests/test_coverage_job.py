"""``surface_coverage_scan`` job (surface-Pourbaix slice 1): params scope,
the anchor key, sibling pooling across models, and the dispatch tail that
stamps the footing + the sweep onto the job's own meta.

The engine call itself (``runner.run_coverage_scan_subprocess``) is stubbed
with a synthetic ``coverage.json`` payload — the scan's physics is
catpath's; what is under test is the precis glue around it.
"""

from __future__ import annotations

import hashlib
import io
import math
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

pytest.importorskip("autocatpath")

from precis.quest import pourbaix_map
from precis.store import Store
from precis.store.types import Tag
from precis.structure import export
from precis.structure.cell import Cell
from precis.structure.scene import FIX_ALL, Atom, Scene
from precis_pathway import coverage_job, runner

CONFIG: dict[str, Any] = {
    "name": "cov_smoke",
    "substrate": "NO",
    "target": "NH3",
    "network": "ammonia",
    "slab": {"element": "Pd", "size": [2, 2, 3], "vacuum": 8.0, "fix_layers": 1},
    "mlip": {"backend": "emt"},
    "search": {"energy_thresh": 0.02},
}

AREA = 10.0


def _coverage(model: str, gamma_h: float) -> dict[str, Any]:
    return {
        "model": model,
        "facets": [
            {
                "facet": "111",
                "miller": [1, 1, 1],
                "area": AREA,
                "n_top": 4,
                "n_slab": 12,
                "e_slab": -50.0,
                "e_bulk_per_atom": -3.0,
                "gamma_clean": 0.1,
                "adsorbates": {
                    "H": {
                        "mu": -3.0,
                        "site": "fcc",
                        "c_ads": 0.0,
                        "points": [
                            {
                                "n": 1,
                                "theta": 0.25,
                                "energy": -50.0 + gamma_h * AREA,
                                "gamma": gamma_h,
                                "converged": True,
                                "detached": False,
                            }
                        ],
                        "best": {"theta": 0.25, "n": 1, "gamma": gamma_h},
                    }
                },
                "winner": {"adsorbate": "H", "theta": 0.25, "gamma": gamma_h},
            }
        ],
        "warnings": ["coverage: scan runs on the first model only"],
    }


class _FakeCtx:
    def __init__(
        self, *, store: Store, params: dict[str, Any], ref_id: int = 0
    ) -> None:
        self.store = store
        self.ref_id = ref_id
        self.meta: dict[str, Any] = {"params": params}
        self.status: str | None = None
        self.failure: str | None = None
        self.failure_class: str | None = None
        self.open_tag: str | None = None
        self.chunks: list[tuple[str, str]] = []
        self.meta_updates: dict[str, Any] = {}

    def record_failure(
        self,
        reason: str,
        *,
        failure_class: str | None = None,
        open_tag: str | None = None,
    ) -> None:
        self.failure = reason
        self.failure_class = failure_class
        self.open_tag = open_tag
        self.status = "failed"

    def set_status(self, value: str) -> None:
        self.status = value

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))

    def set_meta(self, **fields: Any) -> None:
        self.meta_updates.update(fields)


def _scan_job(
    store: Store, *, anchor_key: str, model: str, coverage: dict[str, Any], status: str
) -> int:
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title=f"surface_coverage_scan {model}",
        meta={
            "job_type": coverage_job.NAME,
            "executor": "ssh_node",
            "params": {},
            "anchor_key": anchor_key,
            "model": model,
            "coverage": coverage,
        },
    )
    store.add_tag(
        ref.id, Tag.closed("STATUS", status), set_by="agent", replace_prefix=True
    )
    return ref.id


# ── params + defaults ─────────────────────────────────────────────────────


def test_with_defaults_fills_the_slice_1_anchor_set() -> None:
    cfg = coverage_job.with_defaults(CONFIG)
    assert cfg["coverage"] == {
        "adsorbates": ["H", "O", "OH"],
        "coverages": [0.25, 0.5, 0.75, 1.0],
        "facets": [],
    }
    assert "coverage" not in CONFIG, "the input config is not mutated"
    custom = coverage_job.with_defaults({**CONFIG, "coverage": {"adsorbates": ["O"]}})
    assert custom["coverage"]["adsorbates"] == ["O"] and custom["coverage"][
        "coverages"
    ] == [0.25, 0.5, 0.75, 1.0]


@pytest.mark.parametrize(
    ("params", "needle"),
    [
        ({}, "params.config"),
        (
            {"config": {**CONFIG, "coverage": {"facets": [[1, 0, 0]]}}},
            "(111) facet only",
        ),
        (
            {"config": {**CONFIG, "slab": {**CONFIG["slab"], "miller": [1, 0, 0]}}},
            "(111) only",
        ),
        ({"config": CONFIG, "window_U_RHE": [0.5, -0.5]}, "window_U_RHE"),
    ],
)
def test_check_params_refuses_out_of_scope_inputs(
    params: dict[str, Any], needle: str
) -> None:
    reason = coverage_job.check_params(params)
    assert reason is not None and needle in reason


def test_check_params_accepts_the_111_scope() -> None:
    assert coverage_job.check_params({"config": CONFIG}) is None
    assert (
        coverage_job.check_params(
            {"config": {**CONFIG, "slab": {**CONFIG["slab"], "miller": [1, 1, 1]}}}
        )
        is None
    )
    # autocatpath >= 0.25.0 honours the injected slab: no longer a refusal.
    assert (
        coverage_job.check_params(
            {"config": CONFIG, "slab_extxyz": "2\nLattice\nPd 0 0 0\nPd 1 1 1\n"}
        )
        is None
    )


def test_anchor_key_pools_models_and_separates_anchor_sets() -> None:
    """The key fixes slab + adsorbates + coverages + footing; the MLIP block
    is excluded so several models pool under one key."""
    base = coverage_job.with_defaults(CONFIG)
    other_model = {**base, "mlip": {"backend": "mace", "model": "medium"}}
    other_set = coverage_job.with_defaults({**CONFIG, "coverage": {"coverages": [0.5]}})
    assert runner.coverage_anchor_key(base) == runner.coverage_anchor_key(other_model)
    assert runner.coverage_anchor_key(base) != runner.coverage_anchor_key(other_set)


# Prod jo482106's params.config verbatim (read 2026-10-13; stored
# meta.anchor_key 2f0d3e06fa39… was hashed pre-gr482107 on the RAW
# [0.25, 0.5, 0.75, 1.0] request, so the normalised key for this config is
# the new value pinned below — sibling pooling for it happens under the
# new key from here on).
JO482106_CONFIG: dict[str, Any] = {
    "mlip": {"dtype": "mixed", "model": "medium", "device": "cuda", "backend": "mace"},
    "name": "no_to_nh3_pd",
    "slab": {"size": [3, 3, 4], "vacuum": 10, "element": "Pd", "fix_layers": 2},
    "search": {
        "fmax": 0.1,
        "seeds": [0, 1],
        "neb_fmax": 0.15,
        "max_steps": 120,
        "neb_images": 5,
        "neb_retries": 2,
        "rmsd_thresh": 0.7,
        "energy_thresh": 0.05,
        "neb_max_steps": 160,
    },
    "target": "NH3",
    "thermo": {"mode": "vib"},
    "network": "ammonia",
    "poisons": ["CO"],
    "substrate": "NO",
}
#: ``coverage_anchor_key(with_defaults(JO482106_CONFIG))`` on
#: autocatpath 0.24.1 — the key carries the engine version, so this pin is
#: re-derived (not loosened) on any engine bump.
JO482106_KEY = "17c3b318fc0e42605d34fc3a8c0fbc8064e5a25a00d6b65a54a0234a78a9365c"


def test_anchor_key_normalises_requests_to_the_achieved_thetas() -> None:
    """gr482107: requests that snap to the same representable fractions pool
    under one key — [0.25, 0.5, 0.75, 1.0] on a 9-site (3x3) cell achieves
    [2/9, 4/9, 7/9, 1], so the two forms hash equal; a genuinely different
    site count still separates."""
    cell3 = {**CONFIG, "slab": {**CONFIG["slab"], "size": [3, 3, 4]}}
    asked = coverage_job.with_defaults(
        {**cell3, "coverage": {"coverages": [0.25, 0.5, 0.75, 1.0]}}
    )
    representable = coverage_job.with_defaults(
        {**cell3, "coverage": {"coverages": [2 / 9, 4 / 9, 7 / 9, 1.0]}}
    )
    assert asked["coverage"]["coverages"] == [0.25, 0.5, 0.75, 1.0], (
        "caller-set coverages stay verbatim through with_defaults"
    )
    assert runner.coverage_anchor_key(asked) == runner.coverage_anchor_key(
        representable
    )
    other = coverage_job.with_defaults(
        {**cell3, "coverage": {"coverages": [2 / 9, 7 / 9, 1.0]}}
    )
    assert runner.coverage_anchor_key(asked) != runner.coverage_anchor_key(other)


def test_anchor_key_pins_jo482106s_normalised_key() -> None:
    """The prod scan's config reproduces one exact key under the
    normalisation (engine-versioned: re-pin on an autocatpath bump)."""
    assert (
        runner.coverage_anchor_key(coverage_job.with_defaults(JO482106_CONFIG))
        == JO482106_KEY
    )


def test_with_defaults_snaps_the_default_coverages_to_the_slab() -> None:
    """On a 9-site cell the default fill is the representable set the
    engine would run (the snap the job event records); on a 2x2 cell the
    defaults are already representable and pass through unchanged."""
    snapped = coverage_job.with_defaults(
        {**CONFIG, "slab": {**CONFIG["slab"], "size": [3, 3, 4]}}
    )
    assert snapped["coverage"]["coverages"] == [2 / 9, 4 / 9, 7 / 9, 1.0]
    representable = coverage_job.with_defaults(CONFIG)  # 2x2 cell -> n_top 4
    assert representable["coverage"]["coverages"] == [0.25, 0.5, 0.75, 1.0]


# ── sibling pooling ───────────────────────────────────────────────────────


def test_find_sibling_scans_newest_succeeded_per_model(store: Store) -> None:
    key = "k" * 64
    old = _scan_job(
        store,
        anchor_key=key,
        model="emt",
        coverage=_coverage("emt", -0.05),
        status="succeeded",
    )
    new = _scan_job(
        store,
        anchor_key=key,
        model="emt",
        coverage=_coverage("emt", -0.02),
        status="succeeded",
    )
    _scan_job(
        store,
        anchor_key=key,
        model="mace:medium",
        coverage=_coverage("mace:medium", -0.03),
        status="failed",
    )
    _scan_job(
        store,
        anchor_key="other",
        model="mace:small",
        coverage=_coverage("mace:small", -0.03),
        status="succeeded",
    )
    found = coverage_job.find_sibling_scans(store, key)
    assert set(found) == {"emt"}
    assert (
        found["emt"]["facets"][0]["adsorbates"]["H"]["points"][0]["gamma"] == -0.02
    ), "newest wins"
    assert coverage_job.find_sibling_scans(store, key, exclude=new) == {
        "emt": _coverage("emt", -0.05)
    }, "excluding the newest falls back to the older succeeded scan"
    assert old < new


# ── dispatch ──────────────────────────────────────────────────────────────


def test_dispatch_stamps_footing_and_pooled_sweep(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One scan on ``mace:medium`` pools with an earlier ``emt`` scan on the
    same key: the meta carries the anchor footing (key, model, engine
    version, correction set) and a two-model sweep whose boundary has both
    bands; the summary names the resting termination at the point."""
    key = "a" * 64
    _scan_job(
        store,
        anchor_key=key,
        model="emt",
        coverage=_coverage("emt", -0.03),
        status="succeeded",
    )
    seen: dict[str, Any] = {}

    def _fake_scan(
        config: dict[str, Any], model_index: int, **kw: Any
    ) -> dict[str, Any]:
        seen.update(config=config, model_index=model_index, kw=kw)
        return {
            "model": "mace:medium",
            "model_index": model_index,
            "coverage": _coverage("mace:medium", -0.02),
            "engine_version": "0.23.0",
            "corrections": None,
        }

    monkeypatch.setattr(runner, "run_coverage_scan_subprocess", _fake_scan)
    ctx = _FakeCtx(
        store=store,
        params={
            "config": CONFIG,
            "model_index": 1,
            "anchor_key": key,
            "target_node": "spark",
            "point_U_RHE": -0.3,
            "pH": 7,
            "resources": {"wall_seconds": 600, "cpuset": "0-3"},
        },
    )
    coverage_job._dispatch(ctx, coverage_job.SPEC)

    assert ctx.failure is None, ctx.failure
    assert ctx.status == "succeeded"
    assert seen["config"]["coverage"]["adsorbates"] == ["H", "O", "OH"], (
        "defaults reach the engine"
    )
    assert seen["model_index"] == 1
    assert seen["kw"] == {
        "force_backend": None,
        "slab_extxyz": None,
        "timeout": 600,
        "cpuset": "0-3",
    }
    m = ctx.meta_updates
    assert (
        m["anchor_key"] == key and m["model"] == "mace:medium" and m["model_index"] == 1
    )
    assert m["engine_version"] == "0.23.0" and m["corrections"] is None
    assert m["coverage"]["model"] == "mace:medium"
    che = m["che_sweep"]
    assert che["models"] == ["emt", "mace:medium"] and che["n_models"] == 2
    assert che["sigma_e_eV"] == 0.02, "the run's own energy_thresh is the anchor bar"
    (b,) = che["boundaries"]
    assert b["below"] == {"adsorbate": "H", "n": 1} and b["above"] is None
    assert b["U_RHE"] == pytest.approx(0.25)
    assert b["band_model_form"] is not None and b["n_models"] == 2
    assert che["point"]["winner"]["adsorbate"] == "H"
    kinds = [k for k, _ in ctx.chunks]
    assert kinds == ["job_event", "job_summary"]
    summary = ctx.chunks[-1][1]
    assert "resting termination is the H* at 0.25 ML" in summary
    assert "scan warnings: coverage: scan runs on the first model only" in summary


def test_dispatch_event_records_the_coverage_snap(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a 9-site cell the job event names the achieved thetas and the
    requested set — the record that the defaults were snapped (gr482107)."""

    def _fake_scan(
        config: dict[str, Any], model_index: int, **kw: Any
    ) -> dict[str, Any]:
        return {
            "model": "emt",
            "model_index": model_index,
            "coverage": _coverage("emt", -0.02),
            "engine_version": "0.24.1",
            "corrections": None,
        }

    monkeypatch.setattr(runner, "run_coverage_scan_subprocess", _fake_scan)
    cell3 = {**CONFIG, "slab": {**CONFIG["slab"], "size": [3, 3, 4]}}
    ctx = _FakeCtx(store=store, params={"config": cell3})
    coverage_job._dispatch(ctx, coverage_job.SPEC)
    assert ctx.status == "succeeded"
    event = ctx.chunks[0][1]
    assert "coverages=[0.222222, 0.444444, 0.777778, 1]" in event
    assert "requested [0.25, 0.5, 0.75, 1.0], snapped to 9 top sites" in event


def test_dispatch_refuses_out_of_scope_params_before_any_compute(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*a: Any, **k: Any) -> dict[str, Any]:
        raise AssertionError("the engine must not be called")

    monkeypatch.setattr(runner, "run_coverage_scan_subprocess", _boom)
    ctx = _FakeCtx(
        store=store,
        params={"config": {**CONFIG, "coverage": {"facets": [[1, 0, 0]]}}},
    )
    coverage_job._dispatch(ctx, coverage_job.SPEC)
    assert ctx.status == "failed" and ctx.failure_class == "input"
    assert ctx.failure is not None and "facets" in ctx.failure


def test_dispatch_refuses_a_malformed_reservoir_before_any_compute(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*a: Any, **k: Any) -> dict[str, Any]:
        raise AssertionError("the engine must not be called")

    monkeypatch.setattr(runner, "run_coverage_scan_subprocess", _boom)
    ctx = _FakeCtx(
        store=store,
        params={"config": CONFIG, "reservoirs": [{"species": "NH2", "element": "N"}]},
    )
    coverage_job._dispatch(ctx, coverage_job.SPEC)
    assert ctx.status == "failed" and ctx.failure_class == "input"
    assert ctx.failure is not None and "reservoirs" in ctx.failure


def test_dispatch_takes_the_feed_as_the_spectator_reservoir(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An NO substrate sets mu_N by default (gr489164); an explicit empty
    ``reservoirs`` turns it off."""

    def _fake_scan(
        config: dict[str, Any], model_index: int, **kw: Any
    ) -> dict[str, Any]:
        return {
            "model": "emt",
            "model_index": model_index,
            "coverage": _coverage("emt", -0.02),
            "engine_version": "0.24.1",
            "corrections": None,
        }

    monkeypatch.setattr(runner, "run_coverage_scan_subprocess", _fake_scan)
    fed = _FakeCtx(store=store, params={"config": {**CONFIG, "substrate": "NO"}})
    coverage_job._dispatch(fed, coverage_job.SPEC)
    assert fed.status == "succeeded"
    (res,) = fed.meta_updates["che_sweep"]["reservoirs"]
    assert res["species"] == "NO" and res["element"] == "N"
    off = _FakeCtx(
        store=store,
        params={"config": {**CONFIG, "substrate": "NO"}, "reservoirs": []},
    )
    coverage_job._dispatch(off, coverage_job.SPEC)
    assert off.meta_updates["che_sweep"]["reservoirs"] == []


def test_dispatch_passes_the_injected_slab_through(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``slab_extxyz`` (autocatpath >= 0.25.0): handed to the scan child,
    folded into the anchor key via ``slab_key``, and the coverage.json
    provenance (``slab_source``/``lattice_source``) lands on the job meta."""
    extxyz = "2\nLattice\nPd 0 0 0\nPd 1 1 1\n"
    seen: dict[str, Any] = {}

    def _fake_scan(
        config: dict[str, Any], model_index: int, **kw: Any
    ) -> dict[str, Any]:
        seen.update(config=config, kw=kw)
        cov = _coverage("mace:medium", -0.02)
        cov["slab_source"] = "injected"
        cov["lattice_source"] = "slab.info"
        return {
            "model": "mace:medium",
            "model_index": model_index,
            "coverage": cov,
            "engine_version": "0.25.0",
            "corrections": {"gas": "selectivity-19", "h_star": -0.25},
        }

    monkeypatch.setattr(runner, "run_coverage_scan_subprocess", _fake_scan)
    ctx = _FakeCtx(store=store, params={"config": CONFIG, "slab_extxyz": extxyz})
    coverage_job._dispatch(ctx, coverage_job.SPEC)

    assert ctx.status == "succeeded", ctx.failure
    assert seen["kw"]["slab_extxyz"] == extxyz
    m = ctx.meta_updates
    built_key = runner.coverage_anchor_key(coverage_job.with_defaults(CONFIG))
    injected_key = runner.coverage_anchor_key(
        coverage_job.with_defaults(CONFIG), slab_key="x" * 64
    )
    assert m["anchor_key"] == runner.coverage_anchor_key(
        coverage_job.with_defaults(CONFIG),
        slab_key=hashlib.sha256(extxyz.encode()).hexdigest(),
    )
    assert m["anchor_key"] != built_key
    assert injected_key != built_key
    assert m["slab_source"] == "injected"
    assert m["lattice_source"] == "slab.info"
    assert m["corrections"] == {"gas": "selectivity-19", "h_star": -0.25}
    assert "slab=injected" in ctx.chunks[0][1]


def test_dispatch_drops_an_uncorrected_sibling_from_the_band(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr486949: a sibling scan recorded without a gas-correction set must
    not pool into the model-form band — the sweep reports the corrected
    model alone and the summary names the dropped model."""
    key = "c" * 64
    bad = _coverage("mace:large", -0.05)
    bad["corrections"] = {
        "gas": {
            "enabled": True,
            "sets": {},
            "unknown": [["mace", "large"]],
        },
        "h_star": {
            "enabled": True,
            "sets": {},
            "unanchored": [["mace", "large", "Pd"]],
        },
    }
    _scan_job(
        store, anchor_key=key, model="mace:large", coverage=bad, status="succeeded"
    )

    def _fake_scan(
        config: dict[str, Any], model_index: int, **kw: Any
    ) -> dict[str, Any]:
        cov = _coverage("mace:medium", -0.02)
        cov["corrections"] = {
            "gas": {
                "enabled": True,
                "sets": {"mace:medium": {"key": ["mace", "medium"]}},
                "unknown": [],
            },
            "h_star": {
                "enabled": True,
                "sets": {"mace:medium": {"key": ["mace", "medium", "Pd"]}},
                "unanchored": [],
            },
        }
        return {
            "model": "mace:medium",
            "model_index": model_index,
            "coverage": cov,
            "engine_version": "0.25.0",
            "corrections": cov["corrections"],
        }

    monkeypatch.setattr(runner, "run_coverage_scan_subprocess", _fake_scan)
    ctx = _FakeCtx(store=store, params={"config": CONFIG, "anchor_key": key})
    coverage_job._dispatch(ctx, coverage_job.SPEC)

    assert ctx.status == "succeeded", ctx.failure
    che = ctx.meta_updates["che_sweep"]
    assert che["models"] == ["mace:medium"] and che["n_models"] == 1
    assert che["dropped_uncorrected"] == ["mace:large"]
    (b,) = che["boundaries"]
    assert b["band_model_form"] is None and b["n_models"] == 1
    summary = ctx.chunks[-1][1]
    assert "mace:large" in summary and "dropped from the pooled sweep" in summary


def test_dispatch_keeps_its_own_footing_when_it_is_the_uncorrected_one(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr486949/gr489162's jo486844 case: the job that ran the uncorrected
    model stamps a sweep of its own anchors only. The corrected sibling is
    another footing — named as not pooled, never blended in. (Before
    gr489162 this sweep was the sibling's alone, so the uncorrected job's
    map drew another footing's anchors.)"""
    key = "d" * 64
    good = _coverage("mace:medium", -0.03)
    good["corrections"] = {
        "gas": {
            "enabled": True,
            "sets": {"mace:medium": {"key": ["mace", "medium"]}},
            "unknown": [],
        },
        "h_star": {
            "enabled": True,
            "sets": {"mace:medium": {"key": ["mace", "medium", "Pd"]}},
            "unanchored": [],
        },
    }
    sib = _scan_job(
        store, anchor_key=key, model="mace:medium", coverage=good, status="succeeded"
    )

    def _fake_scan(
        config: dict[str, Any], model_index: int, **kw: Any
    ) -> dict[str, Any]:
        cov = _coverage("mace:large", -0.05)
        cov["corrections"] = {
            "gas": {
                "enabled": True,
                "sets": {},
                "unknown": [["mace", "large"]],
            },
            "h_star": {"enabled": True, "sets": {}, "unanchored": []},
        }
        return {
            "model": "mace:large",
            "model_index": model_index,
            "coverage": cov,
            "engine_version": "0.25.0",
            "corrections": cov["corrections"],
        }

    monkeypatch.setattr(runner, "run_coverage_scan_subprocess", _fake_scan)
    ctx = _FakeCtx(store=store, params={"config": CONFIG, "anchor_key": key})
    coverage_job._dispatch(ctx, coverage_job.SPEC)

    assert ctx.status == "succeeded", ctx.failure
    m = ctx.meta_updates
    assert m["model"] == "mace:large" and m["coverage"]["model"] == "mace:large"
    che = m["che_sweep"]
    assert che["models"] == ["mace:large"] and che["n_models"] == 1
    assert che["not_pooled"] == [{"model": "mace:medium", "job": sib}]
    assert "dropped_uncorrected" not in che
    assert che["footing_key"] == pourbaix_map.correction_footing(m["corrections"])
    summary = ctx.chunks[-1][1]
    assert "mace:medium not pooled" in summary
    # the read side draws this sweep as the mace:large footing's own map
    (mp,) = pourbaix_map.maps_for_structure(
        store,
        SimpleNamespace(id=0, meta={}),
        jobs=[
            {
                "ref_id": 1,
                "model": m["model"],
                "anchor_key": key,
                "engine_version": m["engine_version"],
                "corrections": m["corrections"],
                "che_sweep": che,
            }
        ],
    )
    assert mp["regions"] and mp["uncorrected"] is True
    assert not any("another footing" in i for i in mp["incomplete"])


def test_dispatch_never_pools_a_sibling_from_another_correction_table(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr489162: both scans applied gas + H* corrections, so the gr486949
    lever-count check sees no difference, but under different correction
    table versions they are different footings — not pooled."""
    key = "e" * 64

    def _corr(model: str, version: str) -> dict[str, Any]:
        return {
            "gas": {"enabled": True, "sets": {model: {}}, "unknown": []},
            "h_star": {"enabled": True, "sets": {model: {}}, "unanchored": []},
            "version": version,
        }

    old = _coverage("mace:small", -0.05)
    old["corrections"] = _corr("mace:small", "2026-09-01")
    sib = _scan_job(
        store, anchor_key=key, model="mace:small", coverage=old, status="succeeded"
    )

    def _fake_scan(
        config: dict[str, Any], model_index: int, **kw: Any
    ) -> dict[str, Any]:
        cov = _coverage("mace:medium", -0.02)
        cov["corrections"] = _corr("mace:medium", "2026-10-02")
        return {
            "model": "mace:medium",
            "model_index": model_index,
            "coverage": cov,
            "engine_version": "0.25.0",
            "corrections": cov["corrections"],
        }

    monkeypatch.setattr(runner, "run_coverage_scan_subprocess", _fake_scan)
    ctx = _FakeCtx(store=store, params={"config": CONFIG, "anchor_key": key})
    coverage_job._dispatch(ctx, coverage_job.SPEC)
    assert ctx.status == "succeeded", ctx.failure
    che = ctx.meta_updates["che_sweep"]
    assert che["models"] == ["mace:medium"] and che["n_models"] == 1
    assert che["not_pooled"] == [{"model": "mace:small", "job": sib}]
    assert "dropped_uncorrected" not in che


def test_dispatch_classes_a_killed_child_as_infra(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _killed(*a: Any, **k: Any) -> dict[str, Any]:
        raise runner.ChildKilledError("rc=-9")

    monkeypatch.setattr(runner, "run_coverage_scan_subprocess", _killed)
    ctx = _FakeCtx(store=store, params={"config": CONFIG, "anchor_key": "b" * 64})
    coverage_job._dispatch(ctx, coverage_job.SPEC)
    assert ctx.status == "failed" and ctx.open_tag == "infra:child-killed"


def test_job_type_is_registered_by_entry_point() -> None:
    from importlib.metadata import entry_points

    names = {ep.name: ep.value for ep in entry_points(group="precis.job_types")}
    assert names.get(coverage_job.NAME) == "precis_pathway.coverage_job:SPEC"
    assert coverage_job.load() is coverage_job.SPEC
    assert coverage_job.SPEC.compatible_executors == frozenset({"ssh_node"})


# ── structure_ref resolution ─────────────────────────────────────────────


def _hex_slab_scene(*, fixed_bottom: bool = True) -> Scene:
    """An in-memory fcc(111)-style slab: hexagonal in-plane vectors at 60°,
    the third vector the surface normal, a vacuum gap along it, two layers
    of four atoms — the bottom one frozen unless ``fixed_bottom`` is off."""
    a = 5.0
    lat = np.array(
        [
            [a, 0.0, 0.0],
            [a / 2.0, a * math.sqrt(3.0) / 2.0, 0.0],
            [0.0, 0.0, 20.0],
        ]
    )
    scene = Scene(cell=Cell(lat, (True, True, True)))
    for i in range(8):
        layer, x, y = i // 4, (i % 2) * 0.5, ((i // 2) % 2) * 0.5
        scene.atoms[f"aPd{i}"] = Atom(
            label=f"aPd{i}",
            element="Pd",
            frac=np.array([x, y, 0.30 + 0.15 * layer]),
            fixed=FIX_ALL if fixed_bottom and layer == 0 else 0,
        )
    return scene


class _StructStore:
    """The two store calls the resolver needs, over an in-memory scene."""

    def __init__(self, scene: Scene | None) -> None:
        self.scene = scene

    def get_ref(self, *, kind: str, id: int) -> Any:
        if kind == "structure" and self.scene is not None:
            return SimpleNamespace(id=id, kind="structure")
        return None

    def structure_load(self, _sid: int) -> tuple[Scene, dict[str, int]]:
        assert self.scene is not None
        return self.scene, {}


def test_structure_slab_extxyz_exports_the_slab_with_fixatoms() -> None:
    """The resolved slab is byte-identical to the export the quest-minted
    scans inline, and the frozen layer round-trips as an ASE FixAtoms."""
    from ase.constraints import FixAtoms
    from ase.io import read as ase_read

    scene = _hex_slab_scene()
    slab, err = coverage_job.structure_slab_extxyz(_StructStore(scene), 42)
    assert err is None and slab is not None
    assert slab == export.to_extxyz(scene, constraints=True)
    atoms = ase_read(io.StringIO(slab), format="extxyz", index=0)
    assert not isinstance(atoms, list)  # index=0 reads one frame
    cons = [c for c in atoms.constraints if isinstance(c, FixAtoms)]
    assert cons and len(cons[0].get_indices()) == 4


def _one_atom_scene(lattice: np.ndarray) -> Scene:
    return Scene(
        cell=Cell(lattice, (True, True, True)),
        atoms={
            "aPd1": Atom(
                label="aPd1",
                element="Pd",
                frac=np.array([0.0, 0.0, 0.0]),
                fixed=FIX_ALL,
            )
        },
    )


@pytest.mark.parametrize(
    ("scene", "needle"),
    [
        (None, "no live structure"),
        (
            _one_atom_scene(np.diag([10.0, 10.0, 10.0])),
            "not a (111) surface cell",
        ),
        (
            # hexagonal in-plane vectors but a tilted third vector — a
            # sheared cut, not a surface cut the (111) scope covers
            _one_atom_scene(
                np.array([[5.0, 0.0, 0.0], [2.5, 4.33, 0.0], [3.0, 2.0, 20.0]])
            ),
            "not the surface normal",
        ),
        (_one_atom_scene(np.eye(3) * 0.0), "degenerate cell"),
    ],
    ids=["missing ref", "orthogonal non-(111) cell", "tilted cut", "degenerate"],
)
def test_structure_slab_extxyz_refuses_what_it_cannot_anchor(
    scene: Scene | None, needle: str
) -> None:
    slab, err = coverage_job.structure_slab_extxyz(_StructStore(scene), 42)
    assert slab is None and err is not None and needle in err


def test_structure_slab_extxyz_refuses_a_bulk_cell_and_free_atoms() -> None:
    """Two distinct refusals: a (111)-looking cell with no vacuum gap is
    bulk, not a slab; a real slab with nothing frozen would relax the
    layers the candidate pins."""
    scene = _hex_slab_scene()
    # pack atoms through the whole z range: no vacuum gap left
    for i, atom in enumerate(scene.atoms.values()):
        atom.frac = np.array([atom.frac[0], atom.frac[1], i / 8.0])
    slab, err = coverage_job.structure_slab_extxyz(_StructStore(scene), 42)
    assert slab is None and err is not None and "no vacuum gap" in err

    free = _hex_slab_scene(fixed_bottom=False)
    slab, err = coverage_job.structure_slab_extxyz(_StructStore(free), 42)
    assert slab is None and err is not None and "pins no atoms" in err
    assert "slab_extxyz" in err, "the refusal names the override"


def test_structure_slab_extxyz_refuses_a_non_ref() -> None:
    slab, err = coverage_job.structure_slab_extxyz(
        _StructStore(_hex_slab_scene()), "not-an-id"
    )
    assert slab is None and err is not None and "not a structure ref id" in err


def test_validate_submit_resolves_structure_ref_into_slab_extxyz() -> None:
    """put(kind='job') fills params.slab_extxyz server-side — the minted
    job is byte-identical in shape to a quest-minted scan."""
    scene = _hex_slab_scene()
    params: dict[str, Any] = {"config": CONFIG, "structure_ref": 42}
    assert coverage_job.validate_submit(_StructStore(scene), params=params) is None
    assert params["slab_extxyz"] == export.to_extxyz(scene, constraints=True)
    assert params["structure_ref"] == 42, "provenance stays on the params"


def test_validate_submit_refusal_surfaces_at_put_time() -> None:
    params: dict[str, Any] = {"config": CONFIG, "structure_ref": 42}
    err = coverage_job.validate_submit(_StructStore(None), params=params)
    assert err is not None and "no live structure" in err
    assert "slab_extxyz" not in params


def test_validate_submit_refuses_slab_and_structure_ref_together() -> None:
    """slab_extxyz and structure_ref are one-of (gr486841): both given is
    refused before the store is touched, and params stay unmodified."""

    class _Exploding:
        def get_ref(self, **_kw: Any) -> Any:
            raise AssertionError("the store must not be queried")

    slab = "2\nLattice\nPd 0 0 0\nPd 1 1 1\n"
    params: dict[str, Any] = {
        "config": CONFIG,
        "slab_extxyz": slab,
        "structure_ref": 42,
    }
    err = coverage_job.validate_submit(_Exploding(), params=params)
    assert err is not None and "one-of" in err
    assert params["slab_extxyz"] == slab


def test_validate_submit_explicit_slab_alone_never_loads() -> None:
    class _Exploding:
        def get_ref(self, **_kw: Any) -> Any:
            raise AssertionError("the store must not be queried")

    params: dict[str, Any] = {"config": CONFIG, "slab_extxyz": "2\nL\n"}
    assert coverage_job.validate_submit(_Exploding(), params=params) is None


def test_validate_submit_runs_the_scope_gate_first() -> None:
    err = coverage_job.validate_submit(
        _StructStore(_hex_slab_scene()),
        params={
            "config": {**CONFIG, "coverage": {"facets": [[1, 0, 0]]}},
            "structure_ref": 42,
        },
    )
    assert err is not None and "facets" in err


def test_dispatch_resolves_structure_ref_when_no_slab_given(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job minted off the put path (todo dispatch skips validate_submit)
    still resolves the slab at dispatch: the exported bytes reach the scan
    child and their digest folds into the anchor key."""
    pytest.importorskip("ase.build")
    from precis.structure.ops import apply_ops

    scene = Scene(cell=Cell(np.eye(3) * 10.0, (True, True, True)))
    apply_ops(
        scene,
        [{"op": "slab", "element": "Pd", "size": [2, 2, 3], "fix_layers": 1}],
    )
    ref, _created = store.structure_save(
        slug="pd111_test",
        title="Pd(111) test slab",
        scene=scene,
        version=1,
        card_text="test slab",
    )
    seen: dict[str, Any] = {}

    def _fake_scan(
        config: dict[str, Any], model_index: int, **kw: Any
    ) -> dict[str, Any]:
        seen.update(kw=kw)
        return {
            "model": "emt",
            "model_index": model_index,
            "coverage": _coverage("emt", -0.02),
            "engine_version": "0.25.0",
            "corrections": None,
        }

    monkeypatch.setattr(runner, "run_coverage_scan_subprocess", _fake_scan)
    ctx = _FakeCtx(store=store, params={"config": CONFIG, "structure_ref": ref.id})
    coverage_job._dispatch(ctx, coverage_job.SPEC)

    assert ctx.status == "succeeded", ctx.failure
    expected = export.to_extxyz(scene, constraints=True)
    assert seen["kw"]["slab_extxyz"] == expected
    assert ctx.meta_updates["anchor_key"] == runner.coverage_anchor_key(
        coverage_job.with_defaults(CONFIG),
        slab_key=hashlib.sha256(expected.encode("utf-8")).hexdigest(),
    )
    assert f"slab=st{ref.id}" in ctx.chunks[0][1]


def test_dispatch_refuses_a_structure_ref_it_cannot_anchor(store: Store) -> None:
    """The dispatch fallback refuses cleanly (failure_class=input) before
    any compute is scheduled."""
    ctx = _FakeCtx(store=store, params={"config": CONFIG, "structure_ref": 999999})
    coverage_job._dispatch(ctx, coverage_job.SPEC)
    assert ctx.status == "failed" and ctx.failure_class == "input"
    assert ctx.failure is not None and "no live structure" in ctx.failure


# ── the per-candidate recipe's idem key ──────────────────────────────────


def test_scan_idem_key_is_distinct_per_candidate_and_model() -> None:
    """The recipe's key separates (structure, model_index, config basis):
    a second candidate or a second model mints a fresh job, while the same
    put re-run collapses onto the first."""
    k = coverage_job.scan_idem_key(461558, 0, CONFIG)
    assert k == coverage_job.scan_idem_key(461558, 0, CONFIG)
    assert k.startswith("surface_coverage_scan:461558:m0:")
    assert k != coverage_job.scan_idem_key(261823, 0, CONFIG), "per candidate"
    assert k != coverage_job.scan_idem_key(461558, 1, CONFIG), "per model"
    changed = {**CONFIG, "coverage": {"adsorbates": ["N", "NH", "NH2", "NO", "NOH"]}}
    assert k != coverage_job.scan_idem_key(461558, 0, changed), "per anchor set"


# ── co-adsorbed mixtures (gr489475) ──────────────────────────────────────

_ASKED_MIXTURE: dict[str, Any] = {
    "species": ["NO", "OH"],
    "ratios": [[1, 1]],
    "coverages": [0.5],
}


def test_with_defaults_has_no_mixtures_by_default() -> None:
    assert "mixtures" not in coverage_job.with_defaults(CONFIG)["coverage"]


def test_with_defaults_strips_caller_mixtures_when_unsupported() -> None:
    cfg = {**CONFIG, "coverage": {"mixtures": [_ASKED_MIXTURE]}}
    assert "mixtures" not in coverage_job.with_defaults(cfg)["coverage"]
    assert "mixtures" not in coverage_job.with_defaults(cfg, mixtures=False)["coverage"]
    assert "mixtures" in cfg["coverage"], "the caller's dict is not mutated"


def test_with_defaults_adds_the_feed_plus_h_grid_when_supported() -> None:
    cov = coverage_job.with_defaults(CONFIG, mixtures=True)["coverage"]
    assert cov["mixtures"] == [
        {
            "species": ["NO", "H"],
            "ratios": [[3, 1], [1, 1], [1, 3]],
            "coverages": [0.5, 0.75],
        }
    ]


def test_with_defaults_keeps_a_caller_mixtures_list_verbatim() -> None:
    cfg = {**CONFIG, "coverage": {"mixtures": [_ASKED_MIXTURE]}}
    cov = coverage_job.with_defaults(cfg, mixtures=True)["coverage"]
    assert cov["mixtures"] == [_ASKED_MIXTURE]


@pytest.mark.parametrize(
    "substrate", ["H2O", None], ids=["no-feed-gas", "no-substrate"]
)
def test_with_defaults_gives_no_mixtures_without_a_feed_reservoir(
    substrate: str | None,
) -> None:
    cfg = {k: v for k, v in CONFIG.items() if k != "substrate"}
    if substrate is not None:
        cfg["substrate"] = substrate
    cov = coverage_job.with_defaults(cfg, mixtures=True)["coverage"]
    assert "mixtures" not in cov


def _mixture_dispatch(
    store: Store, monkeypatch: pytest.MonkeyPatch, *, supported: bool, config: dict
) -> tuple[_FakeCtx, dict[str, Any]]:
    seen: dict[str, Any] = {}

    def _fake_scan(cfg: dict[str, Any], model_index: int, **kw: Any) -> dict[str, Any]:
        seen["config"] = cfg
        return {
            "model": "emt",
            "model_index": model_index,
            "coverage": _coverage("emt", -0.02),
            "engine_version": "0.24.1",
            "corrections": None,
        }

    monkeypatch.setattr(runner, "run_coverage_scan_subprocess", _fake_scan)
    monkeypatch.setattr(runner, "coverage_mixtures_supported", lambda: supported)
    if supported:
        # the installed engine may predate coverage.mixtures; its Config
        # parser (behind the anchor key) would refuse the key we pretend
        # it takes
        monkeypatch.setattr(
            runner, "coverage_anchor_key", lambda config, **kw: "k" * 64
        )
    ctx = _FakeCtx(store=store, params={"config": config})
    coverage_job._dispatch(ctx, coverage_job.SPEC)
    return ctx, seen


def test_dispatch_sends_default_mixtures_to_a_supporting_engine(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, seen = _mixture_dispatch(store, monkeypatch, supported=True, config=CONFIG)
    assert ctx.status == "succeeded"
    assert seen["config"]["coverage"]["mixtures"][0]["species"] == ["NO", "H"]
    event = ctx.chunks[0][1]
    assert " mixtures=NO+H" in event
    assert "mixtures dropped" not in event


def test_dispatch_drops_asked_mixtures_for_an_older_engine_and_says_so(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = {**CONFIG, "coverage": {"mixtures": [_ASKED_MIXTURE]}}
    ctx, seen = _mixture_dispatch(store, monkeypatch, supported=False, config=asked)
    assert ctx.status == "succeeded"
    assert "mixtures" not in seen["config"]["coverage"]
    event = ctx.chunks[0][1]
    assert "mixtures dropped" in event
    assert " mixtures=" not in event


def test_dispatch_is_silent_when_an_older_engine_was_never_asked(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, seen = _mixture_dispatch(store, monkeypatch, supported=False, config=CONFIG)
    assert ctx.status == "succeeded"
    assert "mixtures" not in seen["config"]["coverage"]
    event = ctx.chunks[0][1]
    assert "mixtures" not in event


def test_runner_mixtures_support_matches_the_engine_config_fields() -> None:
    import dataclasses

    from autocatpath.config import CoverageConfig

    got = runner.coverage_mixtures_supported()
    assert isinstance(got, bool)
    assert got == ("mixtures" in {f.name for f in dataclasses.fields(CoverageConfig)})
