"""``surface_coverage_scan`` job (surface-Pourbaix slice 1): params scope,
the anchor key, sibling pooling across models, and the dispatch tail that
stamps the footing + the sweep onto the job's own meta.

The engine call itself (``runner.run_coverage_scan_subprocess``) is stubbed
with a synthetic ``coverage.json`` payload — the scan's physics is
catpath's; what is under test is the precis glue around it.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("autocatpath")

from precis.store import Store
from precis.store.types import Tag
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
            {"config": CONFIG, "slab_extxyz": "2\nLattice\nPd 0 0 0\nPd 1 1 1\n"},
            "prebuilt slab",
        ),
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


def test_anchor_key_pools_models_and_separates_anchor_sets() -> None:
    """The key fixes slab + adsorbates + coverages + footing; the MLIP block
    is excluded so several models pool under one key."""
    base = coverage_job.with_defaults(CONFIG)
    other_model = {**base, "mlip": {"backend": "mace", "model": "medium"}}
    other_set = coverage_job.with_defaults({**CONFIG, "coverage": {"coverages": [0.5]}})
    assert runner.coverage_anchor_key(base) == runner.coverage_anchor_key(other_model)
    assert runner.coverage_anchor_key(base) != runner.coverage_anchor_key(other_set)


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
    assert seen["kw"] == {"force_backend": None, "timeout": 600, "cpuset": "0-3"}
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


def test_dispatch_refuses_out_of_scope_params_before_any_compute(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*a: Any, **k: Any) -> dict[str, Any]:
        raise AssertionError("the engine must not be called")

    monkeypatch.setattr(runner, "run_coverage_scan_subprocess", _boom)
    ctx = _FakeCtx(store=store, params={"config": CONFIG, "slab_extxyz": "x"})
    coverage_job._dispatch(ctx, coverage_job.SPEC)
    assert ctx.status == "failed" and ctx.failure_class == "input"
    assert ctx.failure is not None and "prebuilt slab" in ctx.failure


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
