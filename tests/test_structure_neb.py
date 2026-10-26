"""``structure/neb.py`` helpers + the ``struct_neb`` job_type (gr512126,
slice 3): params normalization, the content address, the dopant-safe
slab-first atom order the autocatpath engine requires (``index <
n_slab`` = slab), and the dispatch → write-back path against a stubbed
``struct_neb.NEB_RUNNER`` (no catalyst extra in the test image).

Scenes reuse the slice-1 acceptance pair fixtures
(``_endpoint_is``/``_fs_ops``) so the cache-key and slab-split tests run
on the same Cu(111)/O/CH₄ edge the spec was written against, plus its
Pd+Cu-dopant variant (slice-3 acceptance: "a doped slab is split with Cu
on the slab side").
"""

from __future__ import annotations

import copy
import threading
import time
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, Internal, NotFound
from precis.handlers.job import _idem_lock_key
from precis.handlers.structure import StructureHandler
from precis.store.types import Tag
from precis.structure import apply_ops
from precis.structure.neb import (
    NEB_CODE_VERSION,
    NEB_DEFAULTS,
    neb_atoms,
    neb_cache_key,
    normalize_neb_params,
    slab_first_order,
    slab_labels,
)
from precis.workers.executors._context import DispatchContext
from precis.workers.job_types import get_job_type, struct_neb
from tests.test_structure_endpoints import _endpoint_is, _fs_ops
from tests.test_structure_handler import _child_jobs2
from tests.test_structure_pair_check import _put_is_fs


def _pair(element: str = "Cu"):
    pytest.importorskip("ase")
    scene_is, mg_site, spare = _endpoint_is(element)
    scene_fs = copy.deepcopy(scene_is)
    apply_ops(scene_fs, _fs_ops(mg_site))
    return scene_is, scene_fs, spare


# -- params normalization ---------------------------------------------------


def test_normalize_defaults_fill_the_spec_knob_set() -> None:
    assert normalize_neb_params(None) == NEB_DEFAULTS
    assert normalize_neb_params({}) == NEB_DEFAULTS


def test_normalize_coerces_and_keeps_overrides() -> None:
    out = normalize_neb_params(
        {
            "images": "9",
            "path_seeds": 3,
            "fmax": 0.1,
            "max_steps": 150,
            "retries": 0,
            "tether": "false",
            "optimizer": "fire",
        }
    )
    assert out["images"] == 9 and out["path_seeds"] == 3
    assert out["fmax"] == 0.1 and out["max_steps"] == 150
    assert out["retries"] == 0 and out["tether"] is False
    assert out["optimizer"] == "fire"


def test_normalize_rejects_unknown_and_out_of_bounds_keys() -> None:
    with pytest.raises(ValueError, match="imagez"):
        normalize_neb_params({"imagez": 7})
    for bad in (
        {"images": 0},
        {"path_seeds": 0},
        {"max_steps": 0},
        {"retries": -1},
        {"fmax": 0.0},
        {"fmax": "high"},
        {"tether": "maybe"},
    ):
        with pytest.raises(ValueError, match="neb params"):
            normalize_neb_params(bad)


# -- the content address ------------------------------------------------------


def test_cache_key_is_deterministic_and_param_sensitive() -> None:
    scene_is, scene_fs, _spare = _pair()
    base = normalize_neb_params({})
    k1 = neb_cache_key(scene_is, scene_fs, fidelity="ml", model="mace_mp", params=base)
    k2 = neb_cache_key(scene_is, scene_fs, fidelity="ml", model="mace_mp", params=base)
    assert k1 == k2 and len(k1) == 64

    assert (
        neb_cache_key(scene_is, scene_fs, fidelity="ml", model="other", params=base)
        != k1
    )
    assert (
        neb_cache_key(
            scene_is,
            scene_fs,
            fidelity="ml",
            model="mace_mp",
            params=normalize_neb_params({"images": 9}),
        )
        != k1
    )
    assert (
        neb_cache_key(
            scene_is,
            scene_fs,
            fidelity="ml",
            model="mace_mp",
            params=base,
            code_version="2",
        )
        != k1
    )


def test_cache_key_direction_and_endpoint_geometry_matter() -> None:
    scene_is, scene_fs, _spare = _pair()
    base = normalize_neb_params({})
    fwd = neb_cache_key(scene_is, scene_fs, fidelity="ml", model="mace_mp", params=base)
    # A reversed request is a different edge (fwd/rev barriers swap).
    rev = neb_cache_key(scene_fs, scene_is, fidelity="ml", model="mace_mp", params=base)
    assert fwd != rev
    # IS vs itself (same-basin pair) is its own address too.
    same = neb_cache_key(
        scene_is, scene_is, fidelity="ml", model="mace_mp", params=base
    )
    assert same != fwd


def test_cache_key_ignores_labels_and_request_stamps() -> None:
    scene_is, scene_fs, _spare = _pair()
    base = normalize_neb_params({})
    k = neb_cache_key(scene_is, scene_fs, fidelity="ml", model="mace_mp", params=base)
    # Request-context stamps merged into the stored params must not fork
    # the address — two quests asking the same edge share the cache row.
    stamped = {**base, "quest_ref_id": "42"}
    assert (
        neb_cache_key(
            scene_is, scene_fs, fidelity="ml", model="mace_mp", params=stamped
        )
        == k
    )


# -- the slab-first order -----------------------------------------------------


def test_slab_first_order_puts_all_metal_first() -> None:
    scene_is, _fs, _spare = _pair("Cu")
    order, n_slab = slab_first_order(scene_is)
    assert n_slab == 36  # [3,3,4] fcc slab
    assert len(order) == len(scene_is.atoms)
    slab = slab_labels(scene_is)
    assert set(order[:n_slab]) == slab
    # Adsorbate atoms (O, C, H's) all sit after the slab block.
    for la in order[n_slab:]:
        assert scene_is.atoms[la].element in {"O", "C", "H"}


def test_slab_split_counts_a_surface_dopant_as_slab() -> None:
    """Slice-3 acceptance: Pd slab with one surface Cu — the Cu chains in
    through inferred metal contacts and lands on the slab side."""
    scene_is, _fs, spare = _pair("Pd")
    apply_ops(scene_is, [{"op": "set_element", "atom": spare, "element": "Cu"}])
    assert scene_is.atoms[spare].element == "Cu"
    slab = slab_labels(scene_is)
    assert spare in slab
    order, n_slab = slab_first_order(scene_is)
    assert order.index(spare) < n_slab


def test_neb_atoms_builds_in_order_and_pins_fixed() -> None:
    scene_is, _fs, _spare = _pair("Cu")
    order, n_slab = slab_first_order(scene_is)
    atoms = neb_atoms(scene_is, order)
    assert len(atoms) == len(order)
    assert list(atoms.symbols[:n_slab]) == [
        scene_is.atoms[la].element for la in order[:n_slab]
    ]
    # fix_layers: 2 pinned the bottom two layers — indices inside the slab
    # block, matching the scene's fixed flags.
    fixed_idx = {i for i, la in enumerate(order) if scene_is.atoms[la].fixed}
    assert fixed_idx and max(fixed_idx) < n_slab
    constraint = atoms.constraints[0]
    assert set(constraint.get_indices()) == fixed_idx
    # positions round-trip through fractional coordinates
    np.testing.assert_allclose(
        atoms.get_scaled_positions()[0],
        np.asarray(scene_is.atoms[order[0]].frac, dtype=float),
        atol=1e-9,
    )


def test_neb_atoms_rejects_an_order_that_does_not_cover_the_scene() -> None:
    scene_is, _fs, _spare = _pair("Cu")
    order, _n_slab = slab_first_order(scene_is)
    with pytest.raises(ValueError, match="does not cover"):
        neb_atoms(scene_is, order[:-1])


def test_code_version_constant_is_stamped() -> None:
    assert NEB_CODE_VERSION == "1"


# -- the struct_neb job_type (stub engine) --------------------------------------


def _get(store: Any, run_id: int) -> dict:
    row = store.struct_neb_get(run_id)
    assert row is not None, run_id
    return row


def _fake_ctx(store: Any, params: dict[str, Any]) -> tuple[DispatchContext, list[Any]]:
    events: list[tuple[str, Any]] = []
    ctx = DispatchContext(
        store=store,
        ref_id=7777,
        title="neb",
        meta={"params": params},
        set_status=lambda s: events.append(("status", s)),
        append_chunk=lambda k, t: events.append((k, t)),
        set_meta=lambda **kw: events.append(("meta", kw)),
        record_failure=lambda r, **kw: events.append(("fail", {"reason": r, **kw})),
        is_cancel_requested=lambda: False,
    )
    return ctx, events


def _endpoint_refs(store: Any) -> tuple[Any, Any, list[str]]:
    """IS/FS structure refs saved from the fixture pair; returns the refs
    and the band atom order (computed on the IS)."""
    scene_is, scene_fs, _spare = _pair("Cu")
    is_ref, _ = store.structure_save(
        slug="neb_is", title="IS", scene=scene_is, version=1, card_text="IS"
    )
    fs_ref, _ = store.structure_save(
        slug="neb_fs", title="FS", scene=scene_fs, version=1, card_text="FS"
    )
    order, _n_slab = slab_first_order(scene_is)
    return is_ref, fs_ref, order


def _job_params(store: Any, is_ref: Any, fs_ref: Any, run_id: int, **over: Any) -> dict:
    params: dict[str, Any] = {
        "neb_run_id": run_id,
        "is_ref_id": is_ref.id,
        "fs_ref_id": fs_ref.id,
        "is_version": store.structure_version(is_ref.id),
        "fs_version": store.structure_version(fs_ref.id),
        "is_run_id": None,
        "fs_run_id": None,
        "is_slug": "neb_is",
        "fidelity": "ml",
        "model": "mace_mp",
        "params": normalize_neb_params({}),
        "cache_key": "k-job-test",
        "target_node": None,
    }
    params.update(over)
    return params


def _outcome(scene: Any, order: list[str], **over: Any) -> dict[str, Any]:
    """A stubbed engine outcome: 9-image band peaking at idx 4, both seeds
    agreeing — the shape :func:`struct_neb._record_outcome` consumes."""
    fracs = [list(map(float, scene.atoms[la].frac)) for la in order]
    lat = [list(map(float, row)) for row in scene.cell.lattice]
    energies = [-100.0, -99.6, -99.3, -99.2, -99.1, -99.3, -99.7, -100.2, -100.5]
    images = [
        {
            "idx": i,
            "energy_ev": e,
            "max_force": 0.04,
            "positions": {"frac": fracs, "lattice": lat},
        }
        for i, e in enumerate(energies)
    ]
    outcome: dict[str, Any] = {
        "converged": True,
        "collapsed_images": 0,
        "desorbed_images": 0,
        "e_reactant": -100.0,
        "e_product": -100.5,
        "e_ts": -99.1,
        "ts_index": 4,
        "ts_fmax": 0.04,
        "images": images,
        # Real autocatpath neb_barrier multistart shape (engine pin
        # ca54d7e): summary counters + one starts[] entry per path init.
        "multistart": {
            "n_seeds": 2,
            "n_valid": 2,
            "n_distinct_paths": 1,
            "n_at_min": 2,
            "selected_seed": 0,
            "init": "idpp+jitter",
            "jitter_A": 0.05,
            "starts": [
                {
                    "init_seed": 0,
                    "barrier_eV": 0.9,
                    "converged": True,
                    "valid": True,
                    "desorbed_images": 0,
                    "ts_image": 5,
                },
                {
                    "init_seed": 1,
                    "barrier_eV": 0.91,
                    "converged": True,
                    "valid": True,
                    "desorbed_images": 0,
                    "ts_image": 4,
                },
            ],
        },
        # slice 5: the TS-image Hessian — one imaginary mode past the 50i
        # cm⁻¹ floor = a first-order saddle (the frequency check passes).
        "n_imag": 1,
        "imag_cm1": -121.4,
        "records": [
            {
                "id": "eng-sad-0",
                "check": "saddle_verified",
                "verdict": "pass",
                "severity": "info",
                "seed": 0,
                "step": None,
                "evidence": {"ts_index": 4},
            },
            {
                "id": "eng-det-0",
                "check": "detachment",
                "verdict": "pass",
                "severity": "info",
                "seed": 0,
                "step": None,
                "evidence": {"desorbed": 0},
            },
        ],
        "engine": "autocatpath-test-1",
        "wall_s": 12.0,
    }
    outcome.update(over)
    return outcome


def _scene_of(store: Any, ref_id: int) -> Any:
    scene, _ = store.structure_load(ref_id)
    return scene


def test_job_writes_band_trust_and_ts_design(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    is_ref, fs_ref, order = _endpoint_refs(store)
    scene_is = _scene_of(store, is_ref.id)
    run_id = store.struct_neb_record_run(
        is_ref_id=is_ref.id,
        fs_ref_id=fs_ref.id,
        fidelity="ml",
        model="mace_mp",
        params=normalize_neb_params({}),
        cache_key="k-job-test",
        job_ref_id=None,
    )
    monkeypatch.setattr(
        struct_neb,
        "NEB_RUNNER",
        lambda **kw: _outcome(scene_is, order),
    )
    ctx, events = _fake_ctx(store, _job_params(store, is_ref, fs_ref, run_id))
    struct_neb._dispatch(ctx, struct_neb.SPEC)

    assert ("status", "succeeded") in events
    row = _get(store, run_id)
    assert row["status"] == "succeeded" and row["converged"] is True
    assert row["barrier_fwd_ev"] == pytest.approx(0.9)
    assert row["barrier_rev_ev"] == pytest.approx(1.4)
    assert row["delta_e_ev"] == pytest.approx(-0.5)
    assert row["ts_index"] == 4 and row["wall_s"] == 12.0
    assert row["trusted"] is True
    assert all(r["verdict"] == "pass" for r in row["trust"])
    assert all(r.get("engine") == "autocatpath-test-1" for r in row["trust"])
    checks = {r["check"] for r in row["trust"]}
    assert {
        "saddle_verified",
        "neb_convergence",
        "collapsed_band",
        "multistart",
        "absurd_magnitude",
    } <= checks

    # The band: n+2 rows; nothing lands in struct_runs (spec).
    assert len(store.struct_neb_images(run_id)) == 9
    assert store.structure_runs(is_ref.id) == []

    # The TS design, derived-from the IS with labels preserved.
    ts_ref_id = row["ts_ref_id"]
    assert ts_ref_id is not None
    ts_ref = store.get_ref(kind="structure", id=ts_ref_id)
    assert ts_ref.slug == f"neb_is-ts-r{run_id}"
    ts_scene = _scene_of(store, ts_ref_id)
    assert set(ts_scene.atoms) == set(scene_is.atoms)
    links = store.links_for(ts_ref_id, direction="out", relation="derived-from")
    assert any(lk.dst_ref_id == is_ref.id for lk in links)


def test_job_one_seed_is_untrusted_not_failed(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr517804: fewer than 2 independent same-version seeds keeps the
    barrier but never trusts it."""
    is_ref, fs_ref, order = _endpoint_refs(store)
    scene_is = _scene_of(store, is_ref.id)
    run_id = store.struct_neb_record_run(
        is_ref_id=is_ref.id,
        fs_ref_id=fs_ref.id,
        fidelity="ml",
        model="mace_mp",
        params=normalize_neb_params({}),
        cache_key="k-job-test",
        job_ref_id=None,
    )
    outcome = _outcome(
        scene_is,
        order,
        multistart={
            "n_seeds": 2,
            "n_valid": 1,
            "n_distinct_paths": 1,
            "n_at_min": 1,
            "selected_seed": 0,
            "init": "idpp+jitter",
            "jitter_A": 0.05,
            "starts": [
                {
                    "init_seed": 0,
                    "barrier_eV": 0.9,
                    "converged": True,
                    "valid": True,
                    "desorbed_images": 0,
                    "ts_image": 5,
                },
                {
                    "init_seed": 1,
                    "barrier_eV": 1.4,
                    "converged": False,
                    "valid": False,
                    "desorbed_images": 1,
                    "ts_image": None,
                },
            ],
        },
    )
    monkeypatch.setattr(struct_neb, "NEB_RUNNER", lambda **kw: outcome)
    ctx, events = _fake_ctx(store, _job_params(store, is_ref, fs_ref, run_id))
    struct_neb._dispatch(ctx, struct_neb.SPEC)

    assert ("status", "succeeded") in events
    row = _get(store, run_id)
    assert row["status"] == "succeeded"
    assert row["barrier_fwd_ev"] == pytest.approx(0.9)
    assert row["trusted"] is False
    ms = next(r for r in row["trust"] if r["check"] == "multistart")
    assert ms["verdict"] == "marginal"


# -- slice 5: the TS-image saddle (frequency) check ---------------------------


def test_count_imaginary_reads_both_ase_conventions() -> None:
    """ASE surfaces imaginary modes as a complex 1j·f (newer) or a
    negative real (older); both count past the 50i cm⁻¹ noise floor, and
    ``imag_cm1`` is the largest magnitude stored negative."""
    assert struct_neb._count_imaginary([1j * 120.0, 200.0, 35.0]) == (1, -120.0)
    assert struct_neb._count_imaginary([-140.0, 80.0, 20.0]) == (1, -140.0)
    assert struct_neb._count_imaginary([1j * 90.0, -60.0, 100.0]) == (2, -90.0)
    # under the floor and ordinary real modes count for nothing
    assert struct_neb._count_imaginary([1j * 30.0, 110.0]) == (0, None)
    assert struct_neb._count_imaginary([200.0, 150.0, 80.0]) == (0, None)


def test_job_two_imaginary_modes_is_untrusted(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 5 acceptance: a higher-order saddle keeps its barrier but is
    never ``trusted`` — reason ``n_imag=2``."""
    is_ref, fs_ref, order = _endpoint_refs(store)
    scene_is = _scene_of(store, is_ref.id)
    run_id = store.struct_neb_record_run(
        is_ref_id=is_ref.id,
        fs_ref_id=fs_ref.id,
        fidelity="ml",
        model="mace_mp",
        params=normalize_neb_params({}),
        cache_key="k-job-test",
        job_ref_id=None,
    )
    outcome = _outcome(scene_is, order, n_imag=2, imag_cm1=-88.0)
    monkeypatch.setattr(struct_neb, "NEB_RUNNER", lambda **kw: outcome)
    ctx, events = _fake_ctx(store, _job_params(store, is_ref, fs_ref, run_id))
    struct_neb._dispatch(ctx, struct_neb.SPEC)

    assert ("status", "succeeded") in events
    row = _get(store, run_id)
    assert row["status"] == "succeeded" and row["barrier_fwd_ev"] == pytest.approx(0.9)
    assert row["trusted"] is False
    assert row["n_imag"] == 2 and row["imag_cm1"] == -88.0
    freq = next(r for r in row["trust"] if r["check"] == "frequency")
    assert freq["verdict"] == "marginal"
    assert freq["evidence"]["reason"] == "n_imag=2"


def test_job_one_imaginary_mode_stays_trusted(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The passing arm: ``n_imag == 1`` emits a ``pass`` record and the
    columns round-trip."""
    is_ref, fs_ref, order = _endpoint_refs(store)
    scene_is = _scene_of(store, is_ref.id)
    run_id = store.struct_neb_record_run(
        is_ref_id=is_ref.id,
        fs_ref_id=fs_ref.id,
        fidelity="ml",
        model="mace_mp",
        params=normalize_neb_params({}),
        cache_key="k-job-test",
        job_ref_id=None,
    )
    monkeypatch.setattr(
        struct_neb,
        "NEB_RUNNER",
        lambda **kw: _outcome(scene_is, order),
    )
    ctx, events = _fake_ctx(store, _job_params(store, is_ref, fs_ref, run_id))
    struct_neb._dispatch(ctx, struct_neb.SPEC)

    assert ("status", "succeeded") in events
    row = _get(store, run_id)
    assert row["trusted"] is True
    assert row["n_imag"] == 1 and row["imag_cm1"] == -121.4
    freq = next(r for r in row["trust"] if r["check"] == "frequency")
    assert freq["verdict"] == "pass" and freq["evidence"]["n_imag"] == 1


def test_job_no_frequencies_is_untrusted_not_checked(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run whose Hessian never ran is ``not-run``, never a pass — the
    column stays NULL and the read renders 'frequency: not checked'
    (the pre-slice-5 rows' honest shape)."""
    is_ref, fs_ref, order = _endpoint_refs(store)
    scene_is = _scene_of(store, is_ref.id)
    run_id = store.struct_neb_record_run(
        is_ref_id=is_ref.id,
        fs_ref_id=fs_ref.id,
        fidelity="ml",
        model="mace_mp",
        params=normalize_neb_params({}),
        cache_key="k-job-test",
        job_ref_id=None,
    )
    outcome = _outcome(scene_is, order, n_imag=None, imag_cm1=None)
    monkeypatch.setattr(struct_neb, "NEB_RUNNER", lambda **kw: outcome)
    ctx, events = _fake_ctx(store, _job_params(store, is_ref, fs_ref, run_id))
    struct_neb._dispatch(ctx, struct_neb.SPEC)

    assert ("status", "succeeded") in events
    row = _get(store, run_id)
    assert row["status"] == "succeeded" and row["trusted"] is False
    assert row["n_imag"] is None and row["imag_cm1"] is None
    freq = next(r for r in row["trust"] if r["check"] == "frequency")
    assert freq["verdict"] == "not-run"


class _RaisingCalc:
    """A duck-typed ASE-calculator stand-in whose first force call raises
    — the deadline case the budget reason covers (ASE stores any object
    on ``atoms.calc``; ``get_forces`` is the whole contract here)."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def set_atoms(self, atoms: Any) -> None:
        pass

    def get_forces(self, atoms: Any = None) -> Any:
        raise self._exc


def test_ts_frequencies_reuses_the_band_calc_and_reports_budget() -> None:
    """The saddle check rides the TS image's own (deadline-wrapped)
    calculator rather than loading a second model, and a ``NebTimeout``
    out of it is named ``freq_check_budget_exhausted`` — not the generic
    'did not run'."""
    ase = pytest.importorskip("ase")
    img = ase.Atoms("HCu2", positions=[[0, 0, 0], [0, 0, 1.5], [0, 0, 3.0]])
    img.calc = _RaisingCalc(struct_neb.NebTimeout("cap"))
    bar = SimpleNamespace(ts_index=0, images=[img])
    made: list[int] = []

    def _make_calc() -> Any:
        made.append(1)
        return _RaisingCalc(AssertionError("the band calc was not reused"))

    assert struct_neb._ts_frequencies(bar, 1, _make_calc) == (
        None,
        None,
        "freq_check_budget_exhausted",
    )
    assert made == []


def test_ts_frequencies_make_calc_when_the_band_detached_it() -> None:
    """No calculator left on the band image → ``make_calc`` supplies one;
    the same deadline timeout still names the budget reason."""
    ase = pytest.importorskip("ase")
    img = ase.Atoms("HCu2", positions=[[0, 0, 0], [0, 0, 1.5], [0, 0, 3.0]])
    bar = SimpleNamespace(ts_index=0, images=[img])
    made: list[int] = []

    def _make_calc() -> Any:
        made.append(1)
        return _RaisingCalc(struct_neb.NebTimeout("cap"))

    assert struct_neb._ts_frequencies(bar, 1, _make_calc) == (
        None,
        None,
        "freq_check_budget_exhausted",
    )
    assert made == [1]


def test_job_freq_budget_reason_lands_on_the_record(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner's ``freq_skip_reason`` reaches the stored frequency
    record — 'freq_check_budget_exhausted', not the generic
    'the Hessian did not run'."""
    is_ref, fs_ref, order = _endpoint_refs(store)
    scene_is = _scene_of(store, is_ref.id)
    run_id = store.struct_neb_record_run(
        is_ref_id=is_ref.id,
        fs_ref_id=fs_ref.id,
        fidelity="ml",
        model="mace_mp",
        params=normalize_neb_params({}),
        cache_key="k-job-test",
        job_ref_id=None,
    )
    outcome = _outcome(
        scene_is,
        order,
        n_imag=None,
        imag_cm1=None,
        freq_skip_reason="freq_check_budget_exhausted",
    )
    monkeypatch.setattr(struct_neb, "NEB_RUNNER", lambda **kw: outcome)
    ctx, events = _fake_ctx(store, _job_params(store, is_ref, fs_ref, run_id))
    struct_neb._dispatch(ctx, struct_neb.SPEC)

    assert ("status", "succeeded") in events
    row = _get(store, run_id)
    assert row["status"] == "succeeded" and row["trusted"] is False
    freq = next(r for r in row["trust"] if r["check"] == "frequency")
    assert freq["verdict"] == "not-run"
    assert freq["evidence"]["reason"] == "freq_check_budget_exhausted"


def test_job_collapsed_band_fails_not_priced(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    is_ref, fs_ref, order = _endpoint_refs(store)
    scene_is = _scene_of(store, is_ref.id)
    run_id = store.struct_neb_record_run(
        is_ref_id=is_ref.id,
        fs_ref_id=fs_ref.id,
        fidelity="ml",
        model="mace_mp",
        params=normalize_neb_params({}),
        cache_key="k-job-test",
        job_ref_id=None,
    )
    outcome = _outcome(scene_is, order, collapsed_images=2, e_ts=None, converged=False)
    monkeypatch.setattr(struct_neb, "NEB_RUNNER", lambda **kw: outcome)
    ctx, events = _fake_ctx(store, _job_params(store, is_ref, fs_ref, run_id))
    struct_neb._dispatch(ctx, struct_neb.SPEC)

    fails = [p for k, p in events if k == "fail"]
    assert len(fails) == 1 and fails[0]["failure_class"] == "non-convergence"
    row = _get(store, run_id)
    assert row["status"] == "failed"
    assert row["barrier_fwd_ev"] is None and row["ts_index"] is None
    assert row["trusted"] is False


def test_job_endpoint_drift_fails_input_and_runner_never_runs(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    is_ref, fs_ref, _order = _endpoint_refs(store)
    run_id = store.struct_neb_record_run(
        is_ref_id=is_ref.id,
        fs_ref_id=fs_ref.id,
        fidelity="ml",
        model="mace_mp",
        params=normalize_neb_params({}),
        cache_key="k-job-test",
        job_ref_id=None,
    )
    called: list[Any] = []

    def _never(**kw: Any) -> dict[str, Any]:
        called.append(kw)
        raise AssertionError("runner must not run on a drifted endpoint")

    monkeypatch.setattr(struct_neb, "NEB_RUNNER", _never)
    params = _job_params(
        store, is_ref, fs_ref, run_id, fs_version=store.structure_version(fs_ref.id) + 1
    )
    ctx, events = _fake_ctx(store, params)
    struct_neb._dispatch(ctx, struct_neb.SPEC)

    assert not called
    fails = [p for k, p in events if k == "fail"]
    assert len(fails) == 1 and fails[0]["failure_class"] == "input"
    assert _get(store, run_id)["status"] == "failed"


def test_job_missing_engine_is_infra(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    is_ref, fs_ref, _order = _endpoint_refs(store)
    run_id = store.struct_neb_record_run(
        is_ref_id=is_ref.id,
        fs_ref_id=fs_ref.id,
        fidelity="ml",
        model="mace_mp",
        params=normalize_neb_params({}),
        cache_key="k-job-test",
        job_ref_id=None,
    )

    def _no_engine(**kw: Any) -> dict[str, Any]:
        raise struct_neb.NebUnsupported("autocatpath not importable")

    monkeypatch.setattr(struct_neb, "NEB_RUNNER", _no_engine)
    ctx, events = _fake_ctx(store, _job_params(store, is_ref, fs_ref, run_id))
    struct_neb._dispatch(ctx, struct_neb.SPEC)

    fails = [p for k, p in events if k == "fail"]
    assert len(fails) == 1 and fails[0]["failure_class"] == "infra"
    assert "[catalyst]" in fails[0]["reason"]
    assert _get(store, run_id)["status"] == "failed"


def test_struct_neb_is_registered() -> None:
    from precis.workers.registry import SERVICES

    assert get_job_type("struct_neb") is struct_neb.SPEC
    assert "struct_neb" in {s.name for s in SERVICES}


# -- the mode='neb' mint + view='neb' render (handler level) ------------------
#
# Endpoints reuse the slice-2 handler fixture: ``_put_is_fs`` puts the tiny
# CH4 + O cell as ``ch_is`` and derives ``ch_fs`` by ``transfer`` — the pair
# whose stored bond graphs differ by exactly one break + one form.


@pytest.fixture
def structure(store: Any) -> StructureHandler:
    return StructureHandler(hub=Hub(store=store))


def _ref(store: Any, slug: str) -> Any:
    ref = store.get_ref(kind="structure", id=slug)
    assert ref is not None, slug
    return ref


def _relax_both(store: Any, model: str = "mace_mp") -> None:
    """A converged ``ml`` relax stubbed on each endpoint's current
    version — the ``relaxed`` gate's pass condition (and the model the
    mint prices at)."""
    for slug in ("ch_is", "ch_fs"):
        ref = _ref(store, slug)
        store.structure_record_run(
            ref.id,
            fidelity="ml",
            on_version=store.structure_version(ref.id),
            converged=True,
            n_steps=120,
            max_disp=0.0,
            model=model,
            energy=-12.3,
        )


def _pin_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ml rung pins into the compute group — deterministic here."""
    monkeypatch.setenv("PRECIS_MLIP_NODES", "alpha,beta")
    for env in ("PRECIS_AUTOCATPATH_ROUTE_NODE", "PRECIS_DFT_NODE"):
        monkeypatch.delenv(env, raising=False)


def _neb(structure: Any, **over: Any) -> Any:
    args: dict[str, Any] = {"final": "ch_fs"}
    args.update(over)
    return structure.put(id="ch_is", mode="neb", args=args)


def _only_run(store: Any, is_ref_id: int) -> dict[str, Any]:
    runs = store.struct_neb_runs_for(is_ref_id)
    assert len(runs) == 1
    return runs[0]


def test_put_neb_blocked_pair_refuses_with_the_gate_next(
    structure: Any, store: Any
) -> None:
    """No converged ml runs → the relaxed gate's own ``next=`` surfaces;
    nothing is minted."""
    _put_is_fs(structure)
    with pytest.raises(BadInput) as exc:
        _neb(structure)
    assert "blocked" in str(exc.value)
    assert "'op': 'relax'" in str(exc.value.next)
    is_ref = _ref(store, "ch_is")
    assert store.struct_neb_runs_for(is_ref.id) == []
    assert _child_jobs2(store, is_ref.id) == []


def test_put_neb_arg_gates(structure: Any, store: Any) -> None:
    _put_is_fs(structure)
    with pytest.raises(BadInput, match="requires id"):
        structure.put(mode="neb", args={"final": "ch_fs"})
    with pytest.raises(BadInput, match="final"):
        structure.put(id="ch_is", mode="neb", args={})
    with pytest.raises(BadInput, match="unknown key"):
        _neb(structure, bogus_knob=1)
    with pytest.raises(BadInput, match="neb params"):
        _neb(structure, images=0)
    with pytest.raises(BadInput, match="same design"):
        _neb(structure, final="ch_is")
    assert store.struct_neb_runs_for(_ref(store, "ch_is").id) == []


def test_put_neb_mints_the_run_row_and_job(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put_is_fs(structure)
    _relax_both(store)
    _pin_nodes(monkeypatch)
    resp = _neb(structure, images=9)
    assert "queued struct_neb" in resp.body
    assert "9 images × 2 seeds" in resp.body

    is_ref = _ref(store, "ch_is")
    run = _only_run(store, is_ref.id)
    assert run["status"] == "queued" and run["model"] == "mace_mp"
    assert run["params"]["images"] == 9
    assert "quest_ref_id" not in run["params"]
    assert run["fs_ref_id"] == _ref(store, "ch_fs").id
    assert run["is_run_id"] is not None and run["fs_run_id"] is not None
    assert run["job_ref_id"] is not None

    jobs = _child_jobs2(store, is_ref.id)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["job_type"] == "struct_neb" and job["executor"] == "ssh_node"
    params = job["params"]
    assert params["neb_run_id"] == run["id"]
    assert params["fidelity"] == "ml" and params["model"] == "mace_mp"
    assert params["target_node"] in {"alpha", "beta"}
    assert params["resources"]["wall_seconds"] >= 7200
    assert job["idem_key"] == f"struct_neb:{run['cache_key']}"
    # The minted job is the run row's backfill — same ref, parented on IS.
    job_ref = store.get_ref(kind="job", id=run["job_ref_id"])
    assert job_ref is not None


def test_put_neb_identical_request_collapses_inflight(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put_is_fs(structure)
    _relax_both(store)
    _pin_nodes(monkeypatch)
    _neb(structure)
    is_ref = _ref(store, "ch_is")
    resp = _neb(structure)
    assert "already" in resp.body and "in flight" in resp.body
    assert len(store.struct_neb_runs_for(is_ref.id)) == 1
    assert len(_child_jobs2(store, is_ref.id)) == 1


def test_put_neb_identical_request_is_a_cache_hit(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A succeeded twin serves the stored band — zero compute."""
    _put_is_fs(structure)
    _relax_both(store)
    _pin_nodes(monkeypatch)
    _neb(structure)
    is_ref = _ref(store, "ch_is")
    run = _only_run(store, is_ref.id)
    store.struct_neb_update_run(
        run["id"],
        status="succeeded",
        converged=True,
        barrier_fwd_ev=0.9,
        barrier_rev_ev=1.4,
        delta_e_ev=-0.5,
        trusted=True,
    )
    resp = _neb(structure)
    assert "cached struct_neb run" in resp.body
    assert "E_a fwd 0.900 eV" in resp.body and "trusted" in resp.body
    assert len(store.struct_neb_runs_for(is_ref.id)) == 1
    assert len(_child_jobs2(store, is_ref.id)) == 1


def test_put_neb_quest_cap_counts_attributed_mints(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``requested_by=<todo>`` wires the wait and attributes the mint to
    the todo's served quest; the rolling-24h cap refuses with a next=."""
    _put_is_fs(structure)
    _relax_both(store)
    _pin_nodes(monkeypatch)
    monkeypatch.setenv("PRECIS_STRUCT_NEB_QUEST_DAILY", "1")
    todo_id = int(store.insert_ref(kind="todo", slug=None, title="ask").id)
    quest_id = int(store.insert_ref(kind="quest", slug=None, title="q").id)
    store.add_link(src_ref_id=todo_id, dst_ref_id=quest_id, relation="serves")

    resp = _neb(structure, requested_by=todo_id)
    assert f"todo #{todo_id} will block on it" in resp.body
    is_ref = _ref(store, "ch_is")
    run = _only_run(store, is_ref.id)
    assert run["params"]["quest_ref_id"] == str(quest_id)
    edge = store.links_for(todo_id, direction="out", relation="requested")
    assert [lk.dst_ref_id for lk in edge] == [run["job_ref_id"]]
    todo = store.get_ref(kind="todo", id=todo_id)
    assert todo is not None and todo.meta is not None
    assert todo.meta["auto_check"]["type"] == "derived_job_succeeded"

    # A different edge (images=9 → a different content address) from the
    # same quest reaches the cap and refuses with the reopen next=.
    with pytest.raises(BadInput, match="cap") as exc:
        _neb(structure, requested_by=todo_id, images=9)
    assert exc.value.next and "reopens" in exc.value.next
    assert store.struct_neb_quest_mints_24h(quest_id) == 1
    assert len(_child_jobs2(store, is_ref.id)) == 1


def test_put_neb_requester_must_be_a_live_todo(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put_is_fs(structure)
    _relax_both(store)
    _pin_nodes(monkeypatch)
    with pytest.raises(NotFound):
        _neb(structure, requested_by=99999999)
    with pytest.raises(BadInput):
        _neb(structure, requested_by=_ref(store, "ch_is").id)
    assert store.struct_neb_runs_for(_ref(store, "ch_is").id) == []


def test_put_neb_caller_model_must_cover_both_endpoints(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put_is_fs(structure)
    _relax_both(store)  # mace_mp on both endpoints
    _pin_nodes(monkeypatch)
    with pytest.raises(BadInput, match="chgnet") as exc:
        _neb(structure, model="chgnet")
    assert "'op': 'relax'" in str(exc.value.next)
    assert store.struct_neb_runs_for(_ref(store, "ch_is").id) == []


def test_put_neb_refuses_without_a_compute_node(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put_is_fs(structure)
    _relax_both(store)
    for env in (
        "PRECIS_MLIP_NODES",
        "PRECIS_AUTOCATPATH_ROUTE_NODE",
        "PRECIS_DFT_NODE",
    ):
        monkeypatch.delenv(env, raising=False)
    with pytest.raises(Internal, match="no compute node"):
        _neb(structure)
    is_ref = _ref(store, "ch_is")
    assert store.struct_neb_runs_for(is_ref.id) == []
    assert _child_jobs2(store, is_ref.id) == []


def test_put_neb_warn_rides_the_ack(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 'no cross-cell drag' warn never blocks — it rides the mint ack."""
    _put_is_fs(structure)
    # aC1 is bond-free in the FS (aH1 transferred onto aO1) — a 6 Å hop
    # trips 'no cross-cell drag' without tearing a declared bond.
    structure.edit(
        id="ch_fs",
        ops=[{"op": "displace", "atom": "aC1", "vector": [0, 0, 6]}],
    )
    _relax_both(store)
    _pin_nodes(monkeypatch)
    resp = _neb(structure)
    assert "# warn no cross-cell drag" in resp.body
    assert len(store.struct_neb_runs_for(_ref(store, "ch_is").id)) == 1


def test_view_neb_empty_then_lists_the_minted_run(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put_is_fs(structure)
    resp = structure.get(id="ch_is", view="neb")
    assert "no NEB runs" in resp.body and "view='pair'" in resp.body

    _relax_both(store)
    _pin_nodes(monkeypatch)
    _neb(structure)
    body = structure.get(id="ch_is", view="neb").body
    assert "1 NEB run(s)" in body
    assert "ch_is→ch_fs" in body and "queued" in body
    fs_body = structure.get(id="ch_fs", view="neb").body
    assert "ch_is→ch_fs" in fs_body and "FS" in fs_body


def test_view_neb_run_renders_scalars_trust_and_band(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put_is_fs(structure)
    _relax_both(store)
    _pin_nodes(monkeypatch)
    _neb(structure)
    is_ref = _ref(store, "ch_is")
    run = _only_run(store, is_ref.id)
    store.struct_neb_update_run(
        run["id"],
        status="succeeded",
        converged=True,
        barrier_fwd_ev=0.9,
        barrier_rev_ev=1.4,
        delta_e_ev=-0.5,
        ts_index=4,
        ts_fmax=0.04,
        trusted=True,
        wall_s=12.0,
        trust=[
            {
                "id": "r1",
                "check": "saddle_verified",
                "verdict": "pass",
                "severity": "info",
                "engine": "autocatpath-test-1",
                "evidence": {"ts_index": 4},
            },
            {
                "id": "r2",
                "check": "neb_convergence",
                "verdict": "pass",
                "severity": "info",
                # Stamped to an older engine → renders stale (gr517804).
                "engine": "autocatpath-old",
                "evidence": {"reason": "converged"},
            },
        ],
    )
    is_scene, _ = store.structure_load(is_ref.id)
    order, _n_slab = slab_first_order(is_scene)
    fracs = [list(map(float, is_scene.atoms[la].frac)) for la in order]
    lat = [list(map(float, row)) for row in is_scene.cell.lattice]
    energies = [-100.0, -99.6, -99.3, -99.2, -99.1, -99.3, -99.7, -100.2, -100.5]
    store.struct_neb_record_images(
        run["id"],
        [
            {
                "idx": i,
                "energy_ev": e,
                "max_force": 0.04,
                "positions": {"frac": fracs, "lattice": lat},
            }
            for i, e in enumerate(energies)
        ],
    )

    body = structure.get(id="ch_is", view="neb", args={"run": run["id"]}).body
    assert f"# neb run {run['id']}" in body and "succeeded" in body
    assert "E_a fwd 0.900 eV" in body and "rev 1.400 eV" in body
    assert "ΔE -0.500 eV" in body and "trusted" in body
    assert "ml/mace_mp" in body and "wall 12 s" in body
    assert "saddle_verified" in body and "neb_convergence" in body
    assert "stale, rerun on current" in body
    assert "frequency" in body and "not checked" in body
    assert "TS at image 4" in body
    # The changed bonds: aC1–aH1 broken, aH1–aO1 formed by the transfer.
    assert "aC1–aH1" in body and "aH1–aO1" in body


def test_view_neb_failed_run_shows_reason_and_next(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put_is_fs(structure)
    _relax_both(store)
    _pin_nodes(monkeypatch)
    _neb(structure)
    is_ref = _ref(store, "ch_is")
    run = _only_run(store, is_ref.id)
    store.struct_neb_update_run(
        run["id"],
        status="failed",
        trust=[
            {
                "id": "r1",
                "check": "collapsed_band",
                "verdict": "fail",
                "severity": "error",
                "engine": "autocatpath-test-1",
                "evidence": {"reason": "2 images collapsed into endpoints"},
            }
        ],
    )
    body = structure.get(id="ch_is", view="neb", args={"run": run["id"]}).body
    assert "failed: 2 images collapsed into endpoints" in body
    assert "next:" in body and "trust: untrusted" in body


def test_view_neb_run_arg_validates(structure: Any) -> None:
    _put_is_fs(structure)
    with pytest.raises(BadInput, match="run id"):
        structure.get(id="ch_is", view="neb", args={"run": "abc"})
    with pytest.raises(BadInput, match="doesn't touch"):
        structure.get(id="ch_is", view="neb", args={"run": 424242})


# -- GPU-time ceilings + the engine-token rekey --------------------------------


def test_normalize_gpu_ceilings() -> None:
    """The review-#5 ceilings: an unbounded knob mints an unbounded GPU
    job. The boundary values still pass."""
    for bad in (
        {"images": 17},
        {"path_seeds": 6},
        {"max_steps": 2001},
        {"retries": 4},
        {"fmax": 0.005},
    ):
        with pytest.raises(ValueError, match="neb params"):
            normalize_neb_params(bad)
    out = normalize_neb_params(
        {
            "images": 16,
            "path_seeds": 5,
            "max_steps": 2000,
            "retries": 3,
            "fmax": 0.01,
        }
    )
    assert out["images"] == 16 and out["path_seeds"] == 5
    assert out["max_steps"] == 2000 and out["fmax"] == 0.01
    assert out["retries"] == 3


def test_cache_key_rekeys_on_an_engine_bump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr517804 strict-engine ruling: only same-engine results are a cache
    hit, so the token folds into the address — bumping
    ``PRECIS_AUTOCATPATH_VERSION`` re-keys an identical edge."""
    scene_is, scene_fs, _spare = _pair()
    params = normalize_neb_params({})
    monkeypatch.setenv("PRECIS_AUTOCATPATH_VERSION", "ac-old")
    old = neb_cache_key(scene_is, scene_fs, fidelity="ml", model="m", params=params)
    monkeypatch.setenv("PRECIS_AUTOCATPATH_VERSION", "ac-new")
    new = neb_cache_key(scene_is, scene_fs, fidelity="ml", model="m", params=params)
    assert old != new


# -- _DeadlineCalc (the wall-clock cap inside the band's own calls) ------------


class _InnerCalc:
    """The smallest duck-typed inner calculator ``_DeadlineCalc`` wraps —
    a results dict, the evaluative entry points, and ``set_atoms`` so
    ASE's ``atoms.calc =`` assignment lands."""

    implemented_properties = ["energy", "forces"]

    def __init__(self) -> None:
        self.results: dict[str, Any] = {"energy": -1.0}
        self.atoms_seen: Any = None
        self.calculated = False

    def set_atoms(self, atoms: Any) -> None:
        self.atoms_seen = atoms

    def get_potential_energy(
        self, atoms: Any = None, force_consistent: bool = False
    ) -> float:
        return -1.0

    def get_forces(self, atoms: Any = None) -> Any:
        return np.zeros((1, 3))

    def get_stress(self, atoms: Any = None) -> Any:
        return np.zeros(6)

    def get_property(
        self, name: str, atoms: Any = None, allow_calculation: bool = True
    ) -> str:
        return name

    def calculate(
        self,
        atoms: Any = None,
        properties: Any = ("energy",),
        system_changes: Any = None,
    ) -> None:
        self.calculated = True


def test_deadline_calc_delegates_and_assigns_as_ase_calc() -> None:
    inner = _InnerCalc()
    calc = struct_neb._DeadlineCalc(inner, time.monotonic() + 60.0, 60.0)
    assert calc.get_potential_energy() == -1.0
    assert calc.get_property("energy") == "energy"
    calc.calculate()
    assert inner.calculated
    assert calc.results is inner.results
    assert calc.implemented_properties == ["energy", "forces"]

    pytest.importorskip("ase")
    from ase import Atoms

    atoms = Atoms("H")
    atoms.calc = calc  # ASE's setter calls set_atoms — delegated through
    assert inner.atoms_seen is atoms


def test_deadline_calc_raises_past_the_deadline() -> None:
    dead = struct_neb._DeadlineCalc(_InnerCalc(), time.monotonic() - 1.0, 60.0)
    with pytest.raises(struct_neb.NebTimeout, match="wall-clock cap"):
        dead.get_forces()
    with pytest.raises(struct_neb.NebTimeout):
        dead.calculate()


# -- more job_type write-back paths --------------------------------------------


def test_job_runner_timeout_fails_with_timeout_class(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    is_ref, fs_ref, _order = _endpoint_refs(store)
    run_id = store.struct_neb_record_run(
        is_ref_id=is_ref.id,
        fs_ref_id=fs_ref.id,
        fidelity="ml",
        model="mace_mp",
        params=normalize_neb_params({}),
        cache_key="k-job-test",
        job_ref_id=None,
    )

    def _slow(**kw: Any) -> dict[str, Any]:
        raise struct_neb.NebTimeout("edge exceeded the 7200s wall-clock cap")

    monkeypatch.setattr(struct_neb, "NEB_RUNNER", _slow)
    ctx, events = _fake_ctx(store, _job_params(store, is_ref, fs_ref, run_id))
    struct_neb._dispatch(ctx, struct_neb.SPEC)

    fails = [p for k, p in events if k == "fail"]
    assert len(fails) == 1 and fails[0]["failure_class"] == "timeout"
    assert _get(store, run_id)["status"] == "failed"


def test_job_unpriced_band_keeps_every_priced_scalar_null(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A converged band with no TS is "not priced": status fails, and
    ``delta_e_ev`` stays NULL even though E(FS)−E(IS) is computable —
    the priced gate guards all three scalars."""
    is_ref, fs_ref, order = _endpoint_refs(store)
    scene_is = _scene_of(store, is_ref.id)
    run_id = store.struct_neb_record_run(
        is_ref_id=is_ref.id,
        fs_ref_id=fs_ref.id,
        fidelity="ml",
        model="mace_mp",
        params=normalize_neb_params({}),
        cache_key="k-job-test",
        job_ref_id=None,
    )
    outcome = _outcome(scene_is, order, e_ts=None, ts_index=None)
    monkeypatch.setattr(struct_neb, "NEB_RUNNER", lambda **kw: outcome)
    ctx, events = _fake_ctx(store, _job_params(store, is_ref, fs_ref, run_id))
    struct_neb._dispatch(ctx, struct_neb.SPEC)

    fails = [p for k, p in events if k == "fail"]
    assert len(fails) == 1 and fails[0]["failure_class"] == "non-convergence"
    row = _get(store, run_id)
    assert row["status"] == "failed" and row["converged"] is True
    for col in ("barrier_fwd_ev", "barrier_rev_ev", "delta_e_ev", "ts_index"):
        assert row[col] is None, col
    assert row["trusted"] is False


def test_job_missing_engine_grades_is_untrusted_not_passed(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr512126 review #3: the engine grader may return ``None`` for
    ``saddle_verified``/``detachment`` — a check that emitted no record is
    ``not-run``, never a pass, so the run is not ``trusted``."""
    is_ref, fs_ref, order = _endpoint_refs(store)
    scene_is = _scene_of(store, is_ref.id)
    run_id = store.struct_neb_record_run(
        is_ref_id=is_ref.id,
        fs_ref_id=fs_ref.id,
        fidelity="ml",
        model="mace_mp",
        params=normalize_neb_params({}),
        cache_key="k-job-test",
        job_ref_id=None,
    )
    # Grader-None for both engine checks: the outcome carries no records.
    outcome = _outcome(scene_is, order, records=[])
    monkeypatch.setattr(struct_neb, "NEB_RUNNER", lambda **kw: outcome)
    ctx, events = _fake_ctx(store, _job_params(store, is_ref, fs_ref, run_id))
    struct_neb._dispatch(ctx, struct_neb.SPEC)

    assert ("status", "succeeded") in events
    row = _get(store, run_id)
    assert row["status"] == "succeeded" and row["trusted"] is False
    checks = {r["check"] for r in row["trust"]}
    assert "saddle_verified" not in checks and "detachment" not in checks

    # The run view renders the missing required checks as not-run rows.
    handler = StructureHandler(hub=Hub(store=store))
    body = handler.get(id="neb_is", view="neb", args={"run": run_id}).body
    assert "untrusted" in body and "not-run" in body
    assert "saddle_verified" in body and "detachment" in body


def test_job_second_priced_run_of_one_key_fails_the_duplicate(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two priced runs of one cache_key: the partial unique index refuses
    the second — the row fails honestly ("the earlier run is the
    answer"), the job terminalizes, and run 1 is untouched."""
    is_ref, fs_ref, order = _endpoint_refs(store)
    scene_is = _scene_of(store, is_ref.id)
    first = store.struct_neb_record_run(
        is_ref_id=is_ref.id,
        fs_ref_id=fs_ref.id,
        fidelity="ml",
        model="mace_mp",
        params=normalize_neb_params({}),
        cache_key="dup-key",
        job_ref_id=None,
    )
    store.struct_neb_update_run(first, status="succeeded", barrier_fwd_ev=0.9)
    second = store.struct_neb_record_run(
        is_ref_id=is_ref.id,
        fs_ref_id=fs_ref.id,
        fidelity="ml",
        model="mace_mp",
        params=normalize_neb_params({}),
        cache_key="dup-key",
        job_ref_id=None,
    )
    monkeypatch.setattr(
        struct_neb, "NEB_RUNNER", lambda **kw: _outcome(scene_is, order)
    )
    ctx, events = _fake_ctx(store, _job_params(store, is_ref, fs_ref, second))
    struct_neb._dispatch(ctx, struct_neb.SPEC)

    fails = [p for k, p in events if k == "fail"]
    assert len(fails) == 1 and "duplicate" in fails[0]["reason"]
    assert _get(store, second)["status"] == "failed"
    first_row = _get(store, first)
    assert first_row["status"] == "succeeded"
    assert first_row["barrier_fwd_ev"] == 0.9


# -- the mint lock, caps and reaping at the handler level ----------------------


def test_put_neb_ceiling_knobs_refuse_before_any_mint(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put_is_fs(structure)
    _relax_both(store)
    _pin_nodes(monkeypatch)
    for knob in (
        {"images": 17},
        {"path_seeds": 6},
        {"max_steps": 2001},
        {"retries": 4},
        {"fmax": 0.005},
    ):
        with pytest.raises(BadInput, match="neb params"):
            _neb(structure, **knob)
    assert store.struct_neb_runs_for(_ref(store, "ch_is").id) == []


def test_put_neb_global_cap_meters_unstamped_mints(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calls with no quest to charge draw down ``PRECIS_STRUCT_NEB_GLOBAL_DAILY``;
    a quest-stamped mint is a different lane and still goes through."""
    _put_is_fs(structure)
    _relax_both(store)
    _pin_nodes(monkeypatch)
    monkeypatch.setenv("PRECIS_STRUCT_NEB_GLOBAL_DAILY", "1")
    _neb(structure)  # spends the day's one unstamped slot

    # A different edge (images=9 → a different content address) reaches
    # the count and refuses with the requested_by= next=.
    with pytest.raises(BadInput, match="daily struct_neb cap") as exc:
        _neb(structure, images=9)
    assert exc.value.next and "requested_by" in exc.value.next
    is_ref = _ref(store, "ch_is")
    assert len(store.struct_neb_runs_for(is_ref.id)) == 1
    assert len(_child_jobs2(store, is_ref.id)) == 1
    assert store.struct_neb_nonquest_mints_today() == 1

    # Stamped to a quest: the quest lane's own cap (default 4) applies.
    todo_id = int(store.insert_ref(kind="todo", slug=None, title="ask").id)
    quest_id = int(store.insert_ref(kind="quest", slug=None, title="q").id)
    store.add_link(src_ref_id=todo_id, dst_ref_id=quest_id, relation="serves")
    resp = _neb(structure, requested_by=todo_id, images=9)
    assert "queued struct_neb" in resp.body
    assert len(store.struct_neb_runs_for(is_ref.id)) == 2


def test_put_neb_concurrent_mint_collapses_onto_committed_row(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two racing mints serialize on ``struct_neb_mint:<cache_key>``: the
    loser blocks on the advisory lock, then sees the winner's committed
    row on the locked re-check and collapses — one run row, zero jobs."""
    _put_is_fs(structure)
    _relax_both(store)
    _pin_nodes(monkeypatch)
    is_ref = _ref(store, "ch_is")
    fs_ref = _ref(store, "ch_fs")

    # The content address the mint computes (same inputs as _put_neb:
    # mace_mp is the shared converged model from _relax_both).
    is_scene, _ = store.structure_load(is_ref.id)
    fs_scene, _ = store.structure_load(fs_ref.id)
    ck = neb_cache_key(
        is_scene,
        fs_scene,
        fidelity="ml",
        model="mace_mp",
        params=normalize_neb_params({}),
    )

    done: list[tuple[str, Any]] = []

    def _mint() -> None:
        try:
            done.append(("ok", _neb(structure)))
        except Exception as exc:  # surfaced by the asserts below
            done.append(("err", exc))

    # The "winner": holds the mint lock with the run row staged but still
    # uncommitted — exactly where a losing mint must block.
    with store.pool.connection() as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (_idem_lock_key(f"struct_neb_mint:{ck}"),),
        )
        staged_id = store.struct_neb_record_run(
            is_ref_id=is_ref.id,
            fs_ref_id=fs_ref.id,
            fidelity="ml",
            model="mace_mp",
            params=normalize_neb_params({}),
            cache_key=ck,
            job_ref_id=None,
            conn=conn,
        )
        t = threading.Thread(target=_mint, daemon=True)
        t.start()
        blocked = False
        deadline = time.monotonic() + 15.0
        while t.is_alive() and time.monotonic() < deadline:
            waiting = conn.execute(
                "SELECT 1 FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND wait_event = 'advisory' "
                "AND pid <> pg_backend_pid() LIMIT 1"
            ).fetchone()
            if waiting:
                blocked = True
                break
            time.sleep(0.05)
        assert blocked, "the losing mint never blocked on the advisory lock"
        conn.commit()  # the winner lands: lock releases, row publishes
        t.join(timeout=20.0)

    assert not t.is_alive(), "mint never returned after the lock released"
    assert done and done[0][0] == "ok", done
    resp = done[0][1]
    assert "already" in resp.body and "queued" in resp.body
    runs = store.struct_neb_runs_for(is_ref.id)
    assert [r["id"] for r in runs] == [staged_id]
    assert _child_jobs2(store, is_ref.id) == []


def test_put_neb_dead_job_row_is_reaped_and_the_edge_remints(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker kill leaves the row lying 'queued' while its job went
    terminal: the inflight join stops counting it, the mint's reap fails
    it, and a fresh run + job mint in its place."""
    _put_is_fs(structure)
    _relax_both(store)
    _pin_nodes(monkeypatch)
    _neb(structure)
    is_ref = _ref(store, "ch_is")
    dead_run = _only_run(store, is_ref.id)
    assert dead_run["job_ref_id"] is not None
    store.add_tag(
        dead_run["job_ref_id"],
        Tag.closed("STATUS", "failed"),
        set_by="system",
        replace_prefix=True,
    )
    # The zombie row still reads 'queued' — its status is job-written.
    assert _get(store, dead_run["id"])["status"] == "queued"

    resp = _neb(structure)
    assert "queued struct_neb" in resp.body
    runs = {r["id"]: r for r in store.struct_neb_runs_for(is_ref.id)}
    assert len(runs) == 2
    assert runs[dead_run["id"]]["status"] == "failed"  # reaped
    new = next(r for r in runs.values() if r["id"] != dead_run["id"])
    assert new["status"] == "queued" and new["job_ref_id"] is not None
    assert new["job_ref_id"] != dead_run["job_ref_id"]
    assert len(_child_jobs2(store, is_ref.id)) == 2


def test_put_neb_engine_bump_remints_and_renders_the_old_run_stale(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An engine bump re-keys the edge: the same request mints a fresh
    run instead of hitting the old-engine row, and the old row renders
    ``stale, rerun on current`` (gr517804)."""
    _put_is_fs(structure)
    _relax_both(store)
    _pin_nodes(monkeypatch)
    monkeypatch.setenv("PRECIS_AUTOCATPATH_VERSION", "ac-1")
    resp = _neb(structure)
    assert "queued struct_neb" in resp.body
    is_ref = _ref(store, "ch_is")
    run1 = _only_run(store, is_ref.id)
    assert run1["engine"] == "ac-1"

    monkeypatch.setenv("PRECIS_AUTOCATPATH_VERSION", "ac-2")
    resp = _neb(structure)
    assert "queued struct_neb" in resp.body  # not a cache hit — new address
    runs = store.struct_neb_runs_for(is_ref.id)
    assert len(runs) == 2
    assert {r["engine"] for r in runs} == {"ac-1", "ac-2"}
    assert len({r["cache_key"] for r in runs}) == 2
    assert len(_child_jobs2(store, is_ref.id)) == 2

    body = structure.get(id="ch_is", view="neb").body
    assert "ac-1 (stale)" in body and "ac-2" in body
    run_body = structure.get(id="ch_is", view="neb", args={"run": run1["id"]}).body
    assert "engine ac-1 — stale, rerun on current" in run_body


def test_view_neb_band_reads_endpoints_at_the_minted_version(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The band's changed-bond columns come from the endpoints as minted
    (``is_version``/``fs_version`` on the row) — a later edit must not
    relabel the band's atoms."""
    _put_is_fs(structure)
    _relax_both(store)
    _pin_nodes(monkeypatch)
    _neb(structure)
    is_ref = _ref(store, "ch_is")
    run = _only_run(store, is_ref.id)
    assert run["is_version"] == 1 and run["fs_version"] == 1
    store.struct_neb_update_run(
        run["id"], status="succeeded", converged=True, barrier_fwd_ev=0.9
    )
    is_scene, _ = store.structure_load(is_ref.id)
    order, _n_slab = slab_first_order(is_scene)
    fracs = [list(map(float, is_scene.atoms[la].frac)) for la in order]
    lat = [list(map(float, row)) for row in is_scene.cell.lattice]
    store.struct_neb_record_images(
        run["id"],
        [
            {
                "idx": i,
                "energy_ev": -10.0 + 0.1 * i,
                "max_force": 0.04,
                "positions": {"frac": fracs, "lattice": lat},
            }
            for i in range(3)
        ],
    )

    # Edit the IS past the minted version: the live scene no longer has
    # the aC1–aH1 bond, so a live-version diff would drop that column.
    structure.edit(id="ch_is", ops=[{"op": "remove_bond", "i": "aC1", "j": "aH1"}])
    assert store.structure_version(is_ref.id) == 2

    body = structure.get(id="ch_is", view="neb", args={"run": run["id"]}).body
    assert "aC1–aH1" in body and "aH1–aO1" in body


# -- gr518100: atomic mint, timeout surfacing, dedupe-free caps ---------------


def test_job_unpriced_after_full_cap_burn_fails_as_timeout(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine's retry loop can swallow the per-attempt NebTimeout
    and return an unpriced band — a burn that consumed the whole
    wall-clock cap records 'timeout', not 'non-convergence'
    (``test_job_unpriced_band_keeps_every_priced_scalar_null`` covers the
    fast-unpriced converse)."""
    is_ref, fs_ref, order = _endpoint_refs(store)
    scene_is = _scene_of(store, is_ref.id)
    run_id = store.struct_neb_record_run(
        is_ref_id=is_ref.id,
        fs_ref_id=fs_ref.id,
        fidelity="ml",
        model="mace_mp",
        params=normalize_neb_params({}),
        cache_key="k-job-test",
        job_ref_id=None,
    )
    outcome = _outcome(scene_is, order, e_ts=None, ts_index=None)
    monkeypatch.setattr(struct_neb, "NEB_RUNNER", lambda **kw: outcome)
    # The cap is already spent when the dispatch starts.
    monkeypatch.setattr(struct_neb, "_timeout_s", lambda: 0.0)
    ctx, events = _fake_ctx(store, _job_params(store, is_ref, fs_ref, run_id))
    struct_neb._dispatch(ctx, struct_neb.SPEC)

    fails = [p for k, p in events if k == "fail"]
    assert len(fails) == 1 and fails[0]["failure_class"] == "timeout"
    assert "wall-clock cap" in fails[0]["reason"]
    row = _get(store, run_id)
    assert row["status"] == "failed" and row["barrier_fwd_ev"] is None


def test_put_neb_mint_rollback_takes_the_job_with_it(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mint is one ``store.atomic()`` — job.put's own ``tx()`` joins
    as a savepoint, so a failure after job.put rolls the run row AND the
    job ref back together: no GPU job orphaned onto a rolled-back run."""
    _put_is_fs(structure)
    _relax_both(store)
    _pin_nodes(monkeypatch)

    real_update = store.struct_neb_update_run

    def _explode(run_id: int, **kw: Any) -> None:
        if "job_ref_id" in kw:  # the post-put backfill — last write
            raise RuntimeError("staged: post-put write fails")
        real_update(run_id, **kw)

    monkeypatch.setattr(store, "struct_neb_update_run", _explode)
    with pytest.raises(RuntimeError, match="staged"):
        _neb(structure)
    is_ref = _ref(store, "ch_is")
    assert store.struct_neb_runs_for(is_ref.id) == []
    assert _child_jobs2(store, is_ref.id) == []


def test_put_neb_dedupe_collapse_skips_the_daily_cap(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mint that collapses onto a live job's idem_key spent no compute —
    its dedupe-failed run row must not draw down the day's cap: a fresh
    edge mints under the same cap a genuine failure would have spent."""
    _put_is_fs(structure)
    _relax_both(store)
    _pin_nodes(monkeypatch)
    monkeypatch.setenv("PRECIS_STRUCT_NEB_GLOBAL_DAILY", "2")
    _neb(structure)
    is_ref = _ref(store, "ch_is")
    run1 = _only_run(store, is_ref.id)
    # run1's row went terminal while its job is still live — the next
    # identical mint passes the row probes, then job.put's idem_key
    # dedupe collapses it onto jo1.
    store.struct_neb_update_run(run1["id"], status="failed")

    resp = _neb(structure)
    assert "already owned by jo" in resp.body
    runs = {r["id"]: r for r in store.struct_neb_runs_for(is_ref.id)}
    assert len(runs) == 2
    collapsed = runs[next(i for i in runs if i != run1["id"])]
    assert collapsed["status"] == "failed"
    assert collapsed["trust"][0]["check"] == "dedupe"
    assert len(_child_jobs2(store, is_ref.id)) == 1

    # One spent mint + one free collapse: the day's second slot is still
    # open, so a different edge (images=9 → a different address) mints.
    assert store.struct_neb_nonquest_mints_today() == 1
    resp = _neb(structure, images=9)
    assert "queued struct_neb" in resp.body
    assert store.struct_neb_nonquest_mints_today() == 2
