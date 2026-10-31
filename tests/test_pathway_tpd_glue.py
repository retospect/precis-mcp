"""Kinetics v2 slice 9 glue that needs NO engine (D11): ``view='tpd'``, the
TPD trust gate and ``tpd_max_offset_eV`` in the harvest lift, the TPD
basis that keeps margins across an applied offset apart, the TPD leg of
the ``autocatpath_kinetics`` job and the re-runnable TPD leg of the
validation manifest.

CI runs without the ``catalyst`` extra, so nothing here imports
``autocatpath``.  ``tests/fixtures/kinetics_tpd.json`` is a REAL catpath
0.39 ``tpd.run`` payload (numbers, ids and short labels only), captured
from the fixture models of catpath ``tests/test_tpd.py``::

  uv run --extra dev python -c "import sys, json; sys.path.insert(0, 'tests');
    from test_tpd import _co, _h, _n, _no; from autocatpath import tpd;
    from autocatpath.uncertainty import json_safe;
    p = tpd.run({'CO': _co(), 'H': _h(), 'NO': _no(), 'N': _n()},
                family='Pd(111)', adsorbate_mode='hindered',
                reference_state='site', log=lambda *a: None);
    json.dump(json_safe(p), open('kinetics_tpd.json', 'w'), indent=1, sort_keys=True)"
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from precis.quest.compute import _network_basis
from precis.quest.frontier import same_network_basis
from precis_pathway import manifest as vm
from precis_pathway._dispatch_common import _kinetics_scalars
from precis_pathway.toon_views import tpd_text

FIXTURES = Path(__file__).parent / "fixtures"


def _payload() -> dict[str, Any]:
    return json.loads((FIXTURES / "kinetics_tpd.json").read_text(encoding="utf-8"))


# --- view='tpd' --------------------------------------------------------------------


def test_tpd_view_renders_comparisons_refusal_and_offsets() -> None:
    p = _payload()
    text = tpd_text({"results": {"kinetics_tpd": p}})
    assert "reference family Pd(111)" in text and "vacuum+tpd" in text
    assert p["corrections_version"] in text
    rows = {ln.split("\t")[0]: ln.split("\t") for ln in text.splitlines() if "\t" in ln}
    assert set(rows) >= {"CO", "H", "NO", "N"}
    co_rec = p["tpd"]["CO"]
    assert rows["CO"][1:4] == ["pa498702", "0.5", "0.36"]
    assert rows["CO"][4:8] == [
        f"{co_rec['tp_sim_K']:.1f}",
        "530.0",
        f"{co_rec['tp_sim_K'] - 530.0:.1f}",
        f"{co_rec['tp_sim_corrected_K']:.1f}",
    ]
    assert (
        rows["N"][1] == "null"
        and rows["N"][5] == "—"
        and rows["N"][-1] == "no_reference"
    )
    assert rows["NO"][-1] == "refused"
    assert "NO: refused — reference refused: dissociation" in text
    co = p["offsets"]["CO"]
    assert f"CO: offset {co['offset_eV']:+.3f} ± {co['offset_sigma_eV']:.3f} eV" in text
    assert "tpd_corrected over mlip:mace" in text
    assert "pa498702" in text


def test_tpd_view_without_a_record_or_with_an_error() -> None:
    assert "No TPD record" in tpd_text({"results": {}})
    assert (
        tpd_text({"results": {"kinetics_tpd_error": "boom"}})
        == "TPD check failed: boom"
    )


def test_tpd_is_a_declared_view() -> None:
    from precis_pathway.handler import PathwayHandler

    assert "tpd" in PathwayHandler.spec.views


# --- the harvest lift (D8) ---------------------------------------------------------


def _scalar_input(tpd: dict[str, Any]) -> dict[str, Any]:
    return {
        "kinetics": {"tof": 1.0e-3, "warnings": []},
        "kinetics_v2": {"tof_mf": 2.0e-3, "tpd": tpd},
    }


def test_refused_tpd_check_trips_the_trust_gate() -> None:
    """D11: a refused check (NO, dissociation) and a fitted offset that
    still misses (H: mismatch, not applied) are failed checks that were not
    corrected — the solve is untrusted and the note names both."""
    out = _kinetics_scalars(_scalar_input(_payload()["tpd"]))
    assert out["kinetics_trusted"] is False
    assert (
        out["kinetics_note"]
        == "tpd check failed, not corrected: H mismatch, NO refused"
    )
    assert "log_tof" not in out
    applied = [abs(r["offset_eV"]) for r in _payload()["tpd"].values() if r["applied"]]
    assert out["tpd_max_offset_eV"] == pytest.approx(max(applied))


def test_corrected_matched_and_null_reference_pass_the_gate() -> None:
    tpd = {k: v for k, v in _payload()["tpd"].items() if k not in ("NO", "H")}
    out = _kinetics_scalars(_scalar_input(tpd))
    assert out["kinetics_trusted"] is True and "log_tof" in out
    mismatch = copy.deepcopy(tpd)
    mismatch["CO"].update(status="mismatch", applied=False, offset_eV=None)
    out = _kinetics_scalars(_scalar_input(mismatch))
    assert out["kinetics_trusted"] is False and "CO mismatch" in out["kinetics_note"]


# --- margins across an applied offset (D11 / D3) -----------------------------------


def _pathway(tpd: dict[str, Any] | None) -> dict[str, Any]:
    results: dict[str, Any] = {"network_digest": "same-topology"}
    if tpd is not None:
        results["kinetics_tpd"] = tpd
    return {
        "config": {"network": "ammonia"},
        "autocatpath_version": "0.39.0",
        "results": results,
    }


def test_margins_with_and_without_an_offset_do_not_compare() -> None:
    p = _payload()
    with_off = _network_basis(_pathway(p))
    unapplied = {**p, "offsets": {}, "tpd_set_sha256": None}
    without = _network_basis(_pathway(unapplied))
    never_ran = _network_basis(_pathway(None))
    assert with_off is not None and without is not None and never_ran is not None
    assert with_off["tpd_basis"] == p["tpd_set_sha256"]
    assert without["tpd_basis"] == never_ran["tpd_basis"] == "none"
    assert not same_network_basis(with_off, without)
    assert not same_network_basis(without, with_off)
    assert same_network_basis(without, never_ran)
    # a different applied offset is a different basis
    moved = _network_basis(_pathway({**p, "tpd_set_sha256": "f" * 64}))
    assert moved is not None and not same_network_basis(with_off, moved)
    # a stamp from before slice 9 (no key) stays compatible
    legacy = {k: v for k, v in with_off.items() if k != "tpd_basis"}
    assert same_network_basis(legacy, with_off) and same_network_basis(legacy, without)


# --- the job's TPD leg -------------------------------------------------------------------


class _MetaStore:
    def __init__(self, meta: dict[str, Any]) -> None:
        self.ref = SimpleNamespace(id=7, meta=meta)

    def get_ref(self, *, kind: str, id: int) -> Any:
        return self.ref

    def stamp_ref_meta(self, ref_id: int, meta: dict[str, Any]) -> None:
        self.ref.meta = meta


def _run_job(
    monkeypatch: pytest.MonkeyPatch, tpd: dict[str, Any]
) -> tuple[Any, Any, list]:
    from precis_pathway import kinetics_job

    store = _MetaStore(
        {
            "results": {
                "nodes": {"A": -1.0},
                "edges": [{"a": "A"}],
                "kinetics": {"tof": 1.0},
                "kinetics_v2": {"tof_mf": 1.0, "tpd": None},
            }
        }
    )
    calls: list = []

    def _tpd(config, res, *, timeout=None):
        res["kinetics_tpd"] = copy.deepcopy(tpd)

    def _kin(config, artifact, *, timeout=None, tpd=None):
        calls.append(tpd)
        # the compute starts from the inputs only: a stale record is never seen
        assert "kinetics_v2" not in artifact["results_json"]
        artifact["results_json"]["kinetics_v2"] = {
            "tof_mf": 3.0,
            "provider": "vacuum+tpd" if tpd else "vacuum",
            "tpd": tpd["tpd"] if tpd else None,
        }

    def _sweep(config, res, *, timeout=None):
        calls.append(("sweep", res.get("kinetics_tpd") is not None))
        res["kinetics_sweep"] = {"points": []}

    def _fold(res):
        if not res["kinetics_tpd"].get("offsets"):
            res["kinetics_v2"]["tpd"] = res["kinetics_tpd"]["tpd"]

    import precis_pathway

    fake = SimpleNamespace(
        has_tpd=lambda: True,
        run_kinetics_tpd_subprocess=_tpd,
        run_kinetics_subprocess=_kin,
        run_kinetics_sweep_subprocess=_sweep,
        fold_tpd=_fold,
    )
    monkeypatch.setitem(sys.modules, "precis_pathway.runner", fake)
    monkeypatch.setattr(precis_pathway, "runner", fake, raising=False)
    ctx = MagicMock()
    # a family is required: with none the job skips the TPD leg (8a75d4bfb)
    config = {"kinetics": {"tpd": {"family": "Pd(111)"}}}
    ctx.meta = {"params": {"pathway_ref_id": 7, "config": config}}
    ctx.store = store
    kinetics_job._dispatch(ctx, kinetics_job.SPEC)
    return ctx, store, calls


def test_job_resolves_on_the_corrected_states_when_an_offset_applies(
    monkeypatch,
) -> None:
    p = _payload()
    ctx, store, calls = _run_job(monkeypatch, p)
    assert calls[0] is not None and calls[0]["offsets"].keys() == {"CO"}
    assert calls[1] == ("sweep", True)  # the sweep sees the TPD payload
    res = store.ref.meta["results"]
    assert res["kinetics_tpd"]["tpd_set_sha256"] == p["tpd_set_sha256"]
    assert res["kinetics_v2"]["provider"] == "vacuum+tpd"
    events = [c.args[1] for c in ctx.append_chunk.call_args_list]
    assert any(
        e.startswith("autocatpath_kinetics: TPD (Pd(111)): CO corrected")
        for e in events
    )


def test_job_folds_an_uncorrected_check_without_resolving(monkeypatch) -> None:
    p = {**_payload(), "offsets": {}, "tpd_set_sha256": None, "provider": "vacuum"}
    _ctx, store, calls = _run_job(monkeypatch, p)
    # one plain re-solve (no tpd=) ahead of the sweep, then the check folds on
    assert calls == [None, ("sweep", True)]
    v2 = store.ref.meta["results"]["kinetics_v2"]
    # refreshed (the stored 1.0 is replaced), uncorrected provider, check folded
    assert v2["tof_mf"] == 3.0 and v2["provider"] == "vacuum"
    assert v2["tpd"]["NO"]["status"] == "refused"


# --- the validation manifest leg ------------------------------------------------------


def _rerun_with(monkeypatch: pytest.MonkeyPatch, tpd: dict[str, Any]) -> list:
    """``rerun_cpu_legs`` against a stand-in runner; returns the ``tpd=``
    the kinetics leg was called with."""
    import precis_pathway
    from precis_pathway import revalidate_job

    seen: list = []

    def _tpd(config, res, *, timeout=None):
        res["kinetics_tpd"] = copy.deepcopy(tpd)

    def _kin(config, artifact, *, timeout=None, tpd=None):
        seen.append(tpd)
        artifact["results_json"]["kinetics_v2"] = {"tof_mf": 1.0}

    fake = SimpleNamespace(
        run_kinetics_tpd_subprocess=_tpd,
        run_kinetics_subprocess=_kin,
        run_kinetics_sweep_subprocess=lambda *a, **k: None,
        fold_tpd=lambda res: None,
    )
    monkeypatch.setitem(sys.modules, "precis_pathway.runner", fake)
    monkeypatch.setattr(precis_pathway, "runner", fake, raising=False)
    now, failed = revalidate_job.rerun_cpu_legs(
        {"kinetics": {}}, {"nodes": {"A": 0.0}, "edges": []}, ("tpd", "kinetics")
    )
    assert failed == () and now["kinetics_tpd"]["tpd"]
    return seen


def test_rerun_resolves_a_corrected_result_on_the_corrected_states(monkeypatch) -> None:
    """A corrected result re-runs its kinetics leg WITH the re-run TPD
    offsets — re-solving uncorrected would read as false drift."""
    p = _payload()
    assert _rerun_with(monkeypatch, p) == [p]
    plain = {**p, "offsets": {}, "tpd_set_sha256": None}
    assert _rerun_with(monkeypatch, plain) == [None]


def test_tpd_is_a_cpu_leg_and_leaves_the_not_run_list() -> None:
    """Slice 9's TPD leg is CPU-only (MF ramp over the stored nodes/edges),
    so the re-validation re-runs it — first, its offsets feed the kinetics
    leg — and only the GPU leg is reported not run by default."""
    assert vm.CPU_LEGS[0] == "tpd"
    assert set(vm.NOT_RUN_LEGS) == {"energies"}
    assert "kinetics_tpd" in vm.NO_TPD_RECORD
