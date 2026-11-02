"""kinetics v2 glue that needs no engine — the autocatpath_kinetics mint,
the kinetics-sweep view and the harvest lift (slices 4-6, D7/D8/D9).

Split out of ``test_pathway_kinetics_sweep.py``, which skips without the
``catalyst`` extra (CI runs ``--no-extra catalyst``): nothing here imports
``autocatpath``.  The payloads are REAL engine output checked in as
numbers-only JSON; the job's runner is a stand-in where it would pull the
engine.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _sweep_payload() -> dict[str, Any]:
    """A REAL ``kinetics.sweep`` payload, captured from
    ``mkm.run_sweep`` on the single-site fixture — checked in as
    numbers-only JSON.  Regenerate in the catpath tree:

      uv run python -c "import sys, json; sys.path.insert(0,'tests');
        from types import SimpleNamespace;
        from test_mkm import _run_fixture_sweep;
        from autocatpath.environment import Environment;
        env = Environment(T=300.0, inputs={'NO': 0.1, 'H2': 1.0}, preset='far');
        obj = SimpleNamespace(maximise='tof', competing=['NO@N','NOH'], subject_to={});
        json.dump(_run_fixture_sweep(env, sweep={'n': 4}, objective=obj),
                  open('tests/fixtures/kinetics_sweep.json','w'))"
    """
    return json.loads((FIXTURES / "kinetics_sweep.json").read_text(encoding="utf-8"))


def _mint_ctx(tier: str = "verify") -> MagicMock:
    """A MagicMock ctx whose pathway reads ``tier`` and whose store has
    no settings DB — ``pool.connection`` raising makes the flag resolve
    env -> compiled default only."""
    ctx = MagicMock()
    ctx.store.get_ref.return_value = MagicMock(meta={"tier": tier})
    ctx.store.pool.connection.side_effect = Exception("no app_settings")
    return ctx


def test_mint_reaches_the_real_job_handler_from_a_worker_pass(store, monkeypatch):
    """The aggregate mints from a worker pass, whose bare ``Hub`` has no
    plugin kinds: the pathway parent must still be accepted. The mocks below
    stub ``JobHandler`` and hid this on prod (every mint raised BadInput)."""
    from types import SimpleNamespace

    from precis_pathway import aggregate_job

    monkeypatch.setenv("PRECIS_PATHWAY_KINETICS_MINT_ENABLED", "1")
    pathway = store.insert_ref(
        kind="pathway",
        slug="kin-mint-real",
        title="kin-mint-real pathway",
        meta={"tier": "verify", "results": {"nodes": {"A": 0.0}, "edges": []}},
    )

    job_id = aggregate_job._mint_kinetics_job(
        SimpleNamespace(store=store), {"config": {"name": "x"}}, pathway.id
    )

    assert job_id is not None
    job = store.get_ref(kind="job", id=job_id)
    assert job.parent_id == pathway.id
    assert job.meta["job_type"] == "autocatpath_kinetics"


def test_verify_aggregate_mints_kinetics_job(monkeypatch):
    """With the mint flag on, a verify-tier pathway's aggregate mints
    autocatpath_kinetics; other tiers don't, and the mint carries no
    GPU-lane pin (D9)."""
    from precis_pathway import aggregate_job

    monkeypatch.setenv("PRECIS_PATHWAY_KINETICS_MINT_ENABLED", "1")

    minted: list[dict] = []

    class _Resp:
        ref_id = 777

    class _JH:
        def __init__(self, hub=None):
            pass

        def put(self, **kw):
            minted.append(kw)
            return _Resp()

    ctx = _mint_ctx()

    import precis.handlers.job as jh_mod

    monkeypatch.setattr(jh_mod, "JobHandler", _JH)
    out = aggregate_job._mint_kinetics_job(
        ctx,
        {
            "pathway_slug": "pw1",
            "config": {"name": "x"},
            "target_node": "gpu-node-1",
        },
        42,
    )
    assert out == 777
    assert len(minted) == 1
    assert minted[0]["job_type"] == "autocatpath_kinetics"
    assert minted[0]["executor"] == "ssh_node"  # CPU worker lane
    assert minted[0]["params"]["pathway_ref_id"] == 42
    assert "target_node" not in minted[0]["params"]
    # D9: no CPU-lane mechanism exists — no phantom requires either;
    # the GPU-node routing question is flagged needs-ruling in .claude/purpose
    assert minted[0]["requires"] == {}

    # non-verify tier: no mint
    minted.clear()
    ctx.store.get_ref.return_value = MagicMock(meta={"tier": "screen"})
    out = aggregate_job._mint_kinetics_job(
        ctx, {"pathway_slug": "pw1", "config": {}}, 42
    )
    assert out is None and not minted


def test_kinetics_mint_dark_without_flag(monkeypatch):
    """Default off — gated until the CPU-lane ruling (D9): flag unset, a
    verify-tier aggregate mints NO autocatpath_kinetics job."""
    from precis_pathway import aggregate_job

    monkeypatch.delenv("PRECIS_PATHWAY_KINETICS_MINT_ENABLED", raising=False)

    minted: list[dict] = []

    class _JH:
        def __init__(self, hub=None):
            pass

        def put(self, **kw):
            minted.append(kw)
            raise AssertionError("mint must not reach JobHandler.put")

    ctx = _mint_ctx()

    import precis.handlers.job as jh_mod

    monkeypatch.setattr(jh_mod, "JobHandler", _JH)
    out = aggregate_job._mint_kinetics_job(
        ctx, {"pathway_slug": "pw1", "config": {}}, 42
    )
    assert out is None
    assert not minted


def test_kinetics_mint_flag_on_mints_as_before(monkeypatch):
    """Flag on: the verify-tier mint happens exactly as slice 4 wrote it
    — and the flag is what armed it."""
    from precis import settings as psettings
    from precis_pathway import aggregate_job

    calls: list[str] = []
    real_get_bool = psettings.get_bool

    def _spy(key, **kw):
        calls.append(key)
        return real_get_bool(key, **kw)

    monkeypatch.setattr(psettings, "get_bool", _spy)
    monkeypatch.setenv("PRECIS_PATHWAY_KINETICS_MINT_ENABLED", "1")

    minted: list[dict] = []

    class _Resp:
        ref_id = 777

    class _JH:
        def __init__(self, hub=None):
            pass

        def put(self, **kw):
            minted.append(kw)
            return _Resp()

    ctx = _mint_ctx()

    import precis.handlers.job as jh_mod

    monkeypatch.setattr(jh_mod, "JobHandler", _JH)
    out = aggregate_job._mint_kinetics_job(
        ctx, {"pathway_slug": "pw1", "config": {}}, 42
    )
    assert out == 777
    assert len(minted) == 1
    assert minted[0]["job_type"] == "autocatpath_kinetics"
    assert aggregate_job.KINETICS_MINT_KEY in calls


def test_kinetics_sweep_view_renders_optimum_and_competing():
    """view='kinetics-sweep' renders the grid, names the optimum's
    objective+value, and shows the competitive coverage map (D8)."""
    from precis_pathway.toon_views import kinetics_sweep_text

    meta = {"results": {"kinetics_sweep": _sweep_payload()}}
    text = kinetics_sweep_text(meta)
    assert "mean-field sweep" in text
    assert "Optimum:" in text and "T = 4.50e+02" in text
    assert "tof" in text  # the real record's objective name
    assert "Competitive coverage (NO@N vs NOH)" in text
    # no record -> legible miss, not an exception
    assert "No kinetics_sweep" in kinetics_sweep_text({"results": {}})


def test_kinetics_scalars_lift_v2_fields():
    """The harvest lane lifts tof_mf, orders and the sweep optimum off
    the v2 record — drc is flat (step -> X_RC), records.py."""
    from precis_pathway._dispatch_common import _kinetics_scalars

    results = {
        "kinetics": {"tof": 1.0e-4, "drc": {"X_RC": {"s1": 0.9}}},
        "kinetics_v2": {
            "tof_mf": 9.1e-3,
            "drc": {"NO@N->NOH": 0.82, "NOH->NH3": 0.15},
            "orders": {"NO": 0.5, "NH3": -0.1},
        },
        "kinetics_sweep": _sweep_payload(),
    }
    out = _kinetics_scalars(results)
    assert out["tof"] == 9.1e-3
    assert out["tof_mf"] == 9.1e-3
    assert out["drc_top"] == "NO@N->NOH"
    assert out["order_NO"] == 0.5 and out["order_NH3"] == -0.1
    opt = _sweep_payload()["optimum"]
    assert out["optimum_T"] == opt["T"]
    assert out["optimum_p_total"] == opt["p_total"]


# --- kinetics v2 slice 5: the lattice KMC at kmc_points (D7/D8) --------------


def _kmc_payload() -> dict[str, Any]:
    """A REAL catpath 0.36 ``kinetics.sweep`` payload with ``kmc_points``
    (single-site fixture, numbers only): regenerate with the catpath tree's
    test_kmc.py::test_correlation_effect_only_at_kmc_points setup plus
    ``objective: {maximise: tof}``, ``kmc: {L: 8, replicas: 4,
    n_events: 400000, seed: 4}`` and no explicit ``kmc_points``."""
    return json.loads(
        (FIXTURES / "kinetics_sweep_kmc.json").read_text(encoding="utf-8")
    )


def test_kinetics_sweep_view_renders_kmc_points():
    """view='kinetics-sweep' lists every kmc_point with its MF TOF, the KMC
    TOF and 90 % interval, and correlation_effect — and the grid table
    carries no KMC column (the KMC ran only at kmc_points)."""
    from precis_pathway.toon_views import kinetics_sweep_text

    payload = _kmc_payload()
    text = kinetics_sweep_text({"results": {"kinetics_sweep": payload}})
    assert "Lattice KMC at kmc_points" in text
    head, _, kmc_part = text.partition("Lattice KMC at kmc_points")
    assert "tof_kmc" not in head
    opt = next(p for p in payload["kmc_points"] if p["label"] == "optimum")
    assert f"{opt['tof_kmc']['median']:.2e}" in kmc_part
    assert f"{opt['tof_kmc']['p5']:.2e}" in kmc_part
    assert f"{opt['correlation_effect']:+.2f}" in kmc_part
    for label in ("corner", "centre", "optimum"):
        assert label in kmc_part


def test_sweep_zero_turnover_is_not_a_measured_zero_rate():
    """gr543723: a spent KMC budget is not evidence of zero activity."""
    from precis_pathway._dispatch_common import _kinetics_scalars
    from precis_pathway.toon_views import kinetics_sweep_text

    payload = _kmc_payload()
    for point in payload["kmc_points"]:
        point["tof_kmc"].update(median=0.0, p5=0.0, p95=0.0)
        point["correlation_effect"] = None
    results = {"kinetics_v2": {"tof_mf": 10.0}, "kinetics_sweep": payload}
    text = kinetics_sweep_text({"results": results})
    kmc_part = text.partition("Lattice KMC at kmc_points")[2]
    assert "no measurement" in kmc_part
    assert "not a measured zero rate" in kmc_part
    assert "correlation_effect is unavailable" in kmc_part
    assert "0.00e+00" not in kmc_part
    scalars = _kinetics_scalars(results)
    assert "optimum_tof_kmc" not in scalars
    assert "optimum_correlation_effect" not in scalars


def test_sweep_zero_median_with_a_nonzero_interval_keeps_the_measurement():
    from precis_pathway.toon_views import kinetics_sweep_text

    payload = _kmc_payload()
    for point in payload["kmc_points"]:
        point["tof_kmc"].update(median=0.0, p5=-1.0, p95=1.0)
        point["correlation_effect"] = None
    text = kinetics_sweep_text({"results": {"kinetics_sweep": payload}})
    assert "no measurement" not in text
    assert "0.00e+00" in text
    assert "unavailable" in text


def test_stored_null_objective_is_not_rendered_or_harvested_as_an_optimum():
    from precis_pathway._dispatch_common import _kinetics_scalars
    from precis_pathway.toon_views import kinetics_sweep_text

    payload = _sweep_payload()
    payload["optimum"]["value"] = None
    results = {"kinetics_v2": {"tof_mf": 1.0}, "kinetics_sweep": payload}
    assert "Optimum:" not in kinetics_sweep_text({"results": results})
    scalars = _kinetics_scalars(results)
    assert "optimum_T" not in scalars
    assert "optimum_p_total" not in scalars
    # An objective value of zero is still a measured objective.
    payload["optimum"]["value"] = 0.0
    assert "Optimum:" in kinetics_sweep_text({"results": results})
    assert _kinetics_scalars(results)["optimum_T"] == payload["optimum"]["T"]


def test_kinetics_scalars_lift_kmc():
    """D8: a tof_kmc on the record outranks tof_mf (kinetics_method='kmc',
    its interval and correlation_effect lifted); without one the method is
    'mf'; the sweep's optimum kmc_point lifts its KMC TOF and effect."""
    from precis_pathway._dispatch_common import _kinetics_scalars

    payload = _kmc_payload()
    v2 = {
        "tof_mf": 100.0,
        "tof_kmc": {
            "median": 98.7,
            "p5": 95.1,
            "p95": 102.3,
            "n_events": 1,
            "replicas": 4,
            "accelerator": "none",
        },
        "correlation_effect": -0.0057,
    }
    out = _kinetics_scalars(
        {"kinetics": {"tof": 1.0}, "kinetics_v2": v2, "kinetics_sweep": payload}
    )
    assert out["tof"] == 98.7 and out["tof_kmc"] == 98.7
    assert (out["tof_kmc_p5"], out["tof_kmc_p95"]) == (95.1, 102.3)
    assert out["kinetics_method"] == "kmc"
    assert out["correlation_effect"] == -0.0057
    assert out["tof_mf"] == 100.0
    opt = next(p for p in payload["kmc_points"] if p["label"] == "optimum")
    assert out["optimum_tof_kmc"] == opt["tof_kmc"]["median"]
    assert out["optimum_correlation_effect"] == opt["correlation_effect"]
    assert out["n_kmc_points"] == len(payload["kmc_points"])

    mf_only = _kinetics_scalars(
        {"kinetics": {"tof": 1.0}, "kinetics_v2": {"tof_mf": 100.0}}
    )
    assert mf_only["tof"] == 100.0 and mf_only["kinetics_method"] == "mf"
    assert "tof_kmc" not in mf_only and "correlation_effect" not in mf_only
    chain = _kinetics_scalars({"kinetics": {"tof": 1.0}})
    assert chain["kinetics_method"] == "chain"


class _MetaStore:
    """The store calls the job makes, over one in-memory pathway ref
    (the persist path itself is covered through the real store in
    test_pathway_kinetics_sweep.py)."""

    def __init__(self, meta: dict[str, Any]):
        from types import SimpleNamespace

        self.ref = SimpleNamespace(id=4242, meta=meta)
        self.events: list[dict[str, Any]] = []

    def get_ref(self, *, kind: str, id: int) -> Any:
        return self.ref if (kind, id) == ("pathway", self.ref.id) else None

    def stamp_ref_meta(self, ref_id: int, meta: dict[str, Any]) -> None:
        assert ref_id == self.ref.id
        self.ref.meta = meta

    def append_event(
        self, ref_id: int, *, source: str, event: str, payload: Any = None
    ) -> int:
        self.events.append(
            {
                "ref_id": ref_id,
                "source": source,
                "event": event,
                "payload": payload,
            }
        )
        return len(self.events)


def test_kinetics_job_reports_kmc_points(monkeypatch):
    """The job's event log names how many kmc_points ran the lattice KMC,
    and its meta stamp carries n_kmc_points and the sweep lift."""
    from precis_pathway import kinetics_job

    store = _MetaStore(
        {
            "results": {
                "nodes": {"A": -1.0},
                "edges": [{"a": "A", "b": "A"}],
                "kinetics": {"tof": 1.0},  # the aggregate's v1 block
            }
        }
    )
    ref = store.ref
    payload = _kmc_payload()

    def _fake_sweep(config, res, *, timeout=None):
        res["kinetics_sweep"] = payload

    # a stand-in runner module: the job imports it lazily, and the real one
    # pulls the catalyst engine (absent on a torch-free host)
    import sys
    from types import SimpleNamespace

    import precis_pathway

    # an engine without the slice-9 TPD check: the job goes straight to the sweep
    def _fake_solve(config, artifact, *, timeout=None, tpd=None):
        artifact["results_json"]["kinetics_v2"] = {"tof_mf": 2.0}

    fake_runner = SimpleNamespace(
        run_kinetics_sweep_subprocess=_fake_sweep,
        run_kinetics_subprocess=_fake_solve,
        has_tpd=lambda: False,
    )
    monkeypatch.setitem(sys.modules, "precis_pathway.runner", fake_runner)
    monkeypatch.setattr(precis_pathway, "runner", fake_runner, raising=False)
    ctx = MagicMock()
    ctx.meta = {
        "params": {
            "pathway_ref_id": ref.id,
            "config": {"conditions": {"pressures": {"NO": 0.5}}},
        }
    }
    ctx.store = store
    kinetics_job._dispatch(ctx, kinetics_job.SPEC)
    events = [c.args[1] for c in ctx.append_chunk.call_args_list]
    n = len(payload["kmc_points"])
    assert f"autocatpath_kinetics: lattice KMC at {n} of {n} kmc_points" in events
    stamped = ctx.set_meta.call_args.args[0]
    assert stamped["n_kmc_points"] == n
    assert (
        stamped["optimum_T"] == payload["optimum"]["T"]
    )  # the sweep lift reaches the stamp
    opt = next(p for p in payload["kmc_points"] if p["label"] == "optimum")
    assert stamped["optimum_correlation_effect"] == opt["correlation_effect"]
    got = store.get_ref(kind="pathway", id=ref.id)
    assert len(got.meta["results"]["kinetics_sweep"]["kmc_points"]) == n


# --- finalize: *_previous preservation + the degradation guard ---------------


def _stored_results() -> dict[str, Any]:
    """A pathway results block holding a good kinetics record (real tof
    + an optimum-bearing sweep), as the aggregate stamped it."""
    return {
        "nodes": {"A": -1.0},
        "edges": [{"a": "A", "b": "A"}],
        "kinetics_v2": {"tof_mf": 9.1e-3, "warnings": []},
        "kinetics_sweep": {
            "points": [{"T": 300.0}, {"T": 400.0}],
            "optimum": {"T": 400.0, "p_total": 1.0, "value": 9.1e-3},
        },
    }


def _finalize_ctx(store: _MetaStore, ref_id: int) -> MagicMock:
    ctx = MagicMock()
    ctx.meta = {"params": {"pathway_ref_id": ref_id, "config": {}}}
    ctx.store = store
    return ctx


def test_finalize_rejects_a_degraded_resolved_record():
    """gr548810/pathway 527720: a re-solve with no tof_mf and no optimum
    must NOT replace a stored record that has them — each degraded key
    lands under ``<key>_rejected`` with its reason, the job fails, and a
    ref_events row marks the rejection."""
    from precis_pathway import kinetics_job

    store = _MetaStore({"results": _stored_results()})
    ref = store.ref
    results = {
        "nodes": {"A": -1.0},
        "edges": [{"a": "A", "b": "A"}],
        "kinetics_v2": {"tof_mf": None, "warnings": ["no backend"]},
        "kinetics_sweep": {"points": [{"T": 300.0}], "optimum": None},
    }
    ctx = _finalize_ctx(store, ref.id)
    ok = kinetics_job._finalize(ctx, ref.id, {}, ref, results)

    assert ok is False
    ctx.record_failure.assert_called_once()
    fail = ctx.record_failure.call_args.args[0]
    assert "kinetics_v2 degraded" in fail and "kinetics_sweep degraded" in fail
    res = ref.meta["results"]
    assert res["kinetics_v2"] == {"tof_mf": 9.1e-3, "warnings": []}
    assert res["kinetics_sweep"]["optimum"]["value"] == 9.1e-3
    rej_v2 = res["kinetics_v2_rejected"]
    assert rej_v2["reason"] == (
        "kinetics_v2 degraded: no finite tof_mf; stored record kept"
    )
    assert rej_v2["kinetics_v2"]["tof_mf"] is None
    rej_sweep = res["kinetics_sweep_rejected"]
    assert rej_sweep["reason"] == (
        "kinetics_sweep degraded: no optimum; stored record kept"
    )
    assert len(rej_sweep["kinetics_sweep"]["points"]) == 1
    assert store.events and store.events[0]["event"] == "kinetics-rejected"
    assert store.events[0]["payload"]["keys"] == [
        "kinetics_sweep",
        "kinetics_v2",
    ]


def test_finalize_judges_each_record_key_separately():
    """A finite tof with a one-point, optimum-less sweep is a MIXED run:
    the v2 record replaces the stored one (stored kept under
    ``*_previous``) while the stored sweep stands and the new sweep lands
    under ``kinetics_sweep_rejected``."""
    from precis_pathway import kinetics_job

    store = _MetaStore({"results": _stored_results()})
    ref = store.ref
    results = {
        "nodes": {"A": -1.0},
        "edges": [{"a": "A", "b": "A"}],
        "kinetics_v2": {"tof_mf": 1.2e-2, "warnings": []},
        "kinetics_sweep": {"points": [{"T": 300.0}], "optimum": None},
    }
    ctx = _finalize_ctx(store, ref.id)
    ok = kinetics_job._finalize(ctx, ref.id, {}, ref, results)

    assert ok is False
    fail = ctx.record_failure.call_args.args[0]
    assert "kinetics_sweep degraded" in fail and "kinetics_v2 degraded" not in fail
    res = ref.meta["results"]
    # the good half landed
    assert res["kinetics_v2"] == {"tof_mf": 1.2e-2, "warnings": []}
    assert res["kinetics_v2_previous"] == {"tof_mf": 9.1e-3, "warnings": []}
    # the bad half did not
    assert res["kinetics_sweep"]["optimum"]["value"] == 9.1e-3
    assert res["kinetics_sweep_rejected"]["reason"] == (
        "kinetics_sweep degraded: no optimum; stored record kept"
    )
    assert "kinetics_v2_rejected" not in res


def test_accepted_finalize_pops_the_stale_rejected():
    """A later accepted finalize of a previously rejected key clears its
    ``*_rejected`` marker — the good answer retiring the bad run."""
    from precis_pathway import kinetics_job

    store = _MetaStore({"results": _stored_results()})
    ref = store.ref
    ctx = _finalize_ctx(store, ref.id)
    bad = {
        "nodes": {"A": -1.0},
        "edges": [{"a": "A", "b": "A"}],
        "kinetics_v2": {"tof_mf": None},
    }
    assert kinetics_job._finalize(ctx, ref.id, {}, ref, dict(bad)) is False
    assert "kinetics_v2_rejected" in ref.meta["results"]

    good = {
        "nodes": {"A": -1.0},
        "edges": [{"a": "A", "b": "A"}],
        "kinetics_v2": {"tof_mf": 1.2e-2, "warnings": []},
    }
    ctx = _finalize_ctx(store, ref.id)
    assert kinetics_job._finalize(ctx, ref.id, {}, ref, dict(good)) is True
    res = ref.meta["results"]
    assert res["kinetics_v2"] == {"tof_mf": 1.2e-2, "warnings": []}
    assert res["kinetics_v2_previous"] == {"tof_mf": 9.1e-3, "warnings": []}
    assert "kinetics_v2_rejected" not in res


def test_finalize_keeps_the_prior_record_under_previous():
    """A better/equal re-solve replaces the stored record but moves it
    under ``kinetics_v2_previous``/``kinetics_sweep_previous`` first."""
    from precis_pathway import kinetics_job

    store = _MetaStore({"results": _stored_results()})
    ref = store.ref
    new_v2 = {"tof_mf": 1.2e-2, "warnings": []}
    new_sweep = {
        "points": [{"T": 300.0}, {"T": 400.0}, {"T": 500.0}],
        "optimum": {"T": 500.0, "p_total": 1.0, "value": 1.2e-2},
    }
    results = {
        "nodes": {"A": -1.0},
        "edges": [{"a": "A", "b": "A"}],
        "kinetics_v2": new_v2,
        "kinetics_sweep": new_sweep,
    }
    ctx = _finalize_ctx(store, ref.id)
    ok = kinetics_job._finalize(ctx, ref.id, {}, ref, results)

    assert ok is True
    res = ref.meta["results"]
    assert res["kinetics_v2"] == new_v2
    assert res["kinetics_sweep"] == new_sweep
    assert res["kinetics_v2_previous"] == {"tof_mf": 9.1e-3, "warnings": []}
    assert res["kinetics_sweep_previous"]["optimum"]["value"] == 9.1e-3
    assert "kinetics_v2_rejected" not in res
    assert not store.events


def test_finalize_first_record_writes_no_previous():
    """A first-ever kinetics record is stored as today — no *_previous,
    no rejection, nothing to preserve."""
    from precis_pathway import kinetics_job

    store = _MetaStore(
        {"results": {"nodes": {"A": -1.0}, "edges": [{"a": "A", "b": "A"}]}}
    )
    ref = store.ref
    new_v2 = {"tof_mf": 9.1e-3, "warnings": []}
    results = {
        "nodes": {"A": -1.0},
        "edges": [{"a": "A", "b": "A"}],
        "kinetics_v2": new_v2,
    }
    ctx = _finalize_ctx(store, ref.id)
    ok = kinetics_job._finalize(ctx, ref.id, {}, ref, results)

    assert ok is True
    res = ref.meta["results"]
    assert res["kinetics_v2"] == new_v2
    assert "kinetics_v2_previous" not in res
    assert "kinetics_sweep_previous" not in res
    assert "kinetics_v2_rejected" not in res


# --- kinetics v2 slice 6: accelerated KMC (D6/D8) ----------------------------


def _accel_payload() -> dict[str, Any]:
    """A REAL catpath 0.37 ``kinetics.sweep`` payload whose kmc_points ran
    channel scaling (single-site fixture, numbers only): the slice-5 recipe
    with ``axes: {T: [300, 300], p_total: [1, 2]}`` and ``kmc: {L: 8,
    replicas: 4, n_events: 400000, seed: 4, accelerator: channel_scaling}``."""
    return json.loads(
        (FIXTURES / "kinetics_sweep_kmc_accel.json").read_text(encoding="utf-8")
    )


def test_kinetics_sweep_view_names_the_accelerator():
    """view='kinetics-sweep' shows, per kmc_point, which accelerator ran and
    its tof_scaling_drift (amc_leak a dash where the accelerator has none);
    a slice-5 payload without acceleration renders dashes."""
    from precis_pathway.toon_views import kinetics_sweep_text

    payload = _accel_payload()
    text = kinetics_sweep_text({"results": {"kinetics_sweep": payload}})
    _, _, kmc_part = text.partition("Lattice KMC at kmc_points")
    assert "accelerator" in kmc_part and "drift_dec" in kmc_part
    for p in payload["kmc_points"]:
        acc = p["acceleration"]
        assert acc["accelerator"] == "channel_scaling"
        assert f"{acc['tof_scaling_drift']:.2e}" in kmc_part
    assert kmc_part.count("channel_scaling") == len(payload["kmc_points"])
    old = kinetics_sweep_text({"results": {"kinetics_sweep": _kmc_payload()}})
    assert "channel_scaling" not in old and "none" in old.partition("Lattice KMC")[2]


def _kmc_v2(accelerator: str, *, drift=None, leak=None) -> dict[str, Any]:
    return {
        "tof_mf": 100.0,
        "tof_kmc": {
            "median": 98.7,
            "p5": 95.1,
            "p95": 102.3,
            "n_events": 1,
            "replicas": 4,
            "accelerator": accelerator,
        },
        "acceleration": {
            "accelerator": accelerator,
            "amc_leak": leak,
            "tof_scaling_drift": drift,
            "events_per_second_sim": 1.5e6,
            "speedup_vs_none": None,
        },
    }


def test_kinetics_scalars_gate_trust_on_acceleration():
    """D8 trust gate, slice 6: a channel-scaled KMC TOF whose drift between
    (δ, w) and (δ/2, w/2) exceeds 0.3 decades — or an mcamc_bfs record
    leaking > 1e-3 fast events (the fallback was off) — is untrusted with
    the reason named and no log_tof; below the thresholds, and for the
    s = 2 pair whose leak is structural, the solve stays trusted.  The
    accelerator, drift, leak and event rate are lifted either way."""
    from precis_pathway._dispatch_common import _kinetics_scalars

    v1 = {"tof": 1.0}
    ok = _kinetics_scalars(
        {"kinetics": v1, "kinetics_v2": _kmc_v2("channel_scaling", drift=0.29)}
    )
    assert ok["kinetics_trusted"] is True and "log_tof" in ok
    assert ok["kmc_accelerator"] == "channel_scaling"
    assert ok["tof_scaling_drift"] == 0.29
    assert ok["kmc_events_per_second_sim"] == 1.5e6
    bad = _kinetics_scalars(
        {"kinetics": v1, "kinetics_v2": _kmc_v2("channel_scaling", drift=0.31)}
    )
    assert bad["kinetics_trusted"] is False
    assert bad["kinetics_note"] == "tof_scaling_drift=0.31"
    assert "log_tof" not in bad and bad["tof"] == 98.7
    leaky = _kinetics_scalars(
        {"kinetics": v1, "kinetics_v2": _kmc_v2("mcamc_bfs", leak=2e-3)}
    )
    assert leaky["kinetics_trusted"] is False
    assert leaky["kinetics_note"] == "amc_leak=0.002" and leaky["amc_leak"] == 2e-3
    closed = _kinetics_scalars(
        {"kinetics": v1, "kinetics_v2": _kmc_v2("mcamc_bfs", leak=5e-4)}
    )
    assert closed["kinetics_trusted"] is True and "log_tof" in closed
    pair = _kinetics_scalars(
        {"kinetics": v1, "kinetics_v2": _kmc_v2("mcamc_s2", leak=0.5)}
    )
    assert pair["kinetics_trusted"] is True and pair["amc_leak"] == 0.5


def test_undefined_scaling_drift_is_untrusted():
    """A channel-scaled KMC TOF whose drift the engine reports undefined
    (no target turnover in one of the two scaled solves) is untrusted with
    that reason; brute force and MCAMC records carry no drift and are not
    gated by its absence."""
    from precis_pathway._dispatch_common import _kinetics_scalars

    v1 = {"tof": 1.0}
    out = _kinetics_scalars(
        {"kinetics": v1, "kinetics_v2": _kmc_v2("channel_scaling", drift=None)}
    )
    assert out["kinetics_trusted"] is False
    assert out["kinetics_note"] == "tof_scaling_drift undefined"
    assert "log_tof" not in out and "tof_scaling_drift" not in out
    for acc in ("none", "mcamc_bfs"):
        ok = _kinetics_scalars({"kinetics": v1, "kinetics_v2": _kmc_v2(acc)})
        assert ok["kinetics_trusted"] is True, acc


def test_kinetics_scalars_lift_the_optimum_acceleration():
    """The sweep's optimum kmc_point lifts its accelerator and drift under
    the optimum_ prefix, with optimum_kmc_trusted from the same gate."""
    from precis_pathway._dispatch_common import _kinetics_scalars

    payload = _accel_payload()
    out = _kinetics_scalars({"kinetics": {"tof": 1.0}, "kinetics_sweep": payload})
    opt = next(p for p in payload["kmc_points"] if p["label"] == "optimum")
    acc = opt["acceleration"]
    assert out["optimum_kmc_accelerator"] == "channel_scaling"
    assert out["optimum_tof_scaling_drift"] == acc["tof_scaling_drift"]
    assert out["optimum_kmc_events_per_second_sim"] == acc["events_per_second_sim"]
    assert out["optimum_kmc_trusted"] is True
    drifted = json.loads(json.dumps(payload))
    for p in drifted["kmc_points"]:
        if p["label"] == "optimum":
            p["acceleration"]["tof_scaling_drift"] = 0.4
    out2 = _kinetics_scalars({"kinetics": {"tof": 1.0}, "kinetics_sweep": drifted})
    assert out2["optimum_kmc_trusted"] is False


# --- D8 reader flip: the v2 record is a complete input, v1 the fallback ----


def _v2_record() -> dict[str, Any]:
    """A D8 record shaped like the engine's ``KineticsResult.as_dict()`` on
    the current pin — the chain/MF scalars plus band, orders and optimum."""
    return {
        "schema": "kinetics",
        "schema_version": 2,
        "tof_chain": 1.0e-4,
        "tof_mf": 9.1e-3,
        "drc": {"NO@N->NOH": 0.82, "NOH->NH3": 0.15},
        "trc": {"NO@N": -0.7},
        "orders": {"NO": 0.5, "NH3": -0.1},
        "q_over_k": 1e-31,
        "bep_flux_share": 0.0,
        "uncertainty": {"log_tof_p5": -4.0, "log_tof_p95": -2.0},
        "optimum": {"T": 450.0, "p_total": 8.0},
        "warnings": [],
    }


def test_kinetics_scalars_v2_only_yields_the_v1_plus_v2_lift():
    """D8 reader flip (the spec's migration paragraph): a results payload
    carrying ONLY ``kinetics_v2`` — no ``kinetics`` block, the shape prod
    takes once the engine stops writing v1 — yields the same scalars as
    the equivalent v1+v2 payload, minus the v1-only ``environment_basis``
    (the record carries the resolved environment, never the hash)."""
    from precis_pathway._dispatch_common import _kinetics_scalars

    v2 = _v2_record()
    v1 = {
        "tof": v2["tof_chain"],  # the record copies the chain TOF through
        "drc": {"X_RC": {"s1": 0.9}},
        "q_over_k": v2["q_over_k"],
        "environment_basis": "ab" * 32,
        "warnings": [],
    }
    both = _kinetics_scalars({"kinetics": v1, "kinetics_v2": v2})
    only = _kinetics_scalars({"kinetics_v2": v2})
    assert only == {k: v for k, v in both.items() if k != "environment_basis"}
    assert "environment_basis" not in only
    # spot-check the lift itself: ranked MF tof gates the log band
    assert only["tof"] == 9.1e-3 and only["kinetics_method"] == "mf"
    assert only["log_tof"] == pytest.approx(math.log10(9.1e-3))
    assert only["optimum_T"] == 450.0 and only["optimum_p_total"] == 8.0


def test_kinetics_scalars_v2_only_falls_back_through_the_tof_chain():
    """v2-only ranking: tof_kmc.median outranks tof_mf outranks tof_chain;
    a record whose MF leg failed still reports the chain TOF as 'chain'."""
    from precis_pathway._dispatch_common import _kinetics_scalars

    v2 = {
        **_v2_record(),
        "tof_kmc": {
            "median": 98.7,
            "p5": 95.1,
            "p95": 102.3,
            "n_events": 1,
            "replicas": 4,
            "accelerator": "none",
        },
        "correlation_effect": -0.0057,
    }
    out = _kinetics_scalars({"kinetics_v2": v2})
    assert out["tof"] == 98.7 and out["kinetics_method"] == "kmc"
    assert (out["tof_kmc_p5"], out["tof_kmc_p95"]) == (95.1, 102.3)
    assert out["correlation_effect"] == -0.0057 and out["tof_mf"] == 9.1e-3

    chain_only = _kinetics_scalars(
        {"kinetics_v2": {"tof_chain": 1.0e-4, "warnings": []}}
    )
    assert chain_only["tof"] == 1.0e-4
    assert chain_only["kinetics_method"] == "chain"
    assert chain_only["kinetics_trusted"] is True


def test_kinetics_scalars_v1_only_is_unchanged():
    """Old engine (pre-0.33, no v2 record): the v1 block behaves exactly as
    before the flip — chain tof, sensitivity band, nested drc."""
    from precis_pathway._dispatch_common import _kinetics_scalars

    out = _kinetics_scalars(
        {
            "kinetics": {
                "tof": 2.0,
                "q_over_k": 1e-31,
                "environment_basis": "ab" * 32,
                "sensitivity": {"tof": {"p5": 1.0, "p95": 4.0}},
                "drc": {"X_RC": {"s1": 0.9}},
            }
        }
    )
    assert out["tof"] == 2.0 and out["kinetics_method"] == "chain"
    assert out["log_tof_p5"] == pytest.approx(0.0)
    assert out["log_tof_p95"] == pytest.approx(math.log10(4.0))
    assert out["drc_top"] == "s1" and out["environment_basis"] == "ab" * 32
    # and a missing-v1 error string still reports the engine's reason
    err = _kinetics_scalars({"kinetics_error": "engine 0.13.0 lacks kinetics"})
    assert err == {
        "kinetics_trusted": False,
        "kinetics_note": "engine 0.13.0 lacks kinetics",
    }


# --- D8 reader flip: the trust gate on a v2-only payload ----------------------


def test_v2_only_gate_trips_on_each_reason():
    """Every trust-gate reason trips from a payload with NO v1 block:
    ranked tof not finite / non-positive, bep_flux_share over the
    threshold, an uncited sticking coefficient in the record's own
    warnings, a drifted/leaky accelerator, and a failed-not-corrected
    TPD check.  The v1-only ``tof_bracket`` guard has no v2 field —
    what it bracketed as excluded is BEP-priced on the record, which is
    what ``bep_flux_share`` measures (the spec's gate set, D8)."""
    from precis_pathway._dispatch_common import _kinetics_scalars

    # tof anomalies, off the ranked (MF) source
    out = _kinetics_scalars({"kinetics_v2": {"tof_mf": float("nan")}})
    assert out["kinetics_trusted"] is False
    assert out["kinetics_note"] == "tof not finite" and "tof" not in out
    for bad in (0.0, -3.5):
        out = _kinetics_scalars({"kinetics_v2": {"tof_mf": bad}})
        assert out["kinetics_trusted"] is False
        assert out["tof"] == bad and "log_tof" not in out
        assert out["kinetics_note"] == "tof non-positive (solver anomaly)"
    # no tof-bearing field at all
    out = _kinetics_scalars({"kinetics_v2": {"bep_flux_share": 0.1}})
    assert out["kinetics_trusted"] is False
    assert out["kinetics_note"] == "tof not finite"

    # BEP-heavy
    out = _kinetics_scalars({"kinetics_v2": {"tof_mf": 1e-3, "bep_flux_share": 0.9}})
    assert out["kinetics_trusted"] is False
    assert out["kinetics_note"] == "bep_flux_share=0.90"

    # uncited sticking, read off the record's own warnings
    out = _kinetics_scalars(
        {
            "kinetics_v2": {
                "tof_mf": 5.0,
                "warnings": [
                    "kinetics: no barrier carries a spread",
                    "sticking uncited: H2",
                ],
            }
        }
    )
    assert out["kinetics_trusted"] is False
    assert out["kinetics_note"] == "sticking uncited: H2"

    # the accelerator gates — same reasons, no v1 block
    drifted = _kinetics_scalars({"kinetics_v2": _kmc_v2("channel_scaling", drift=0.31)})
    assert drifted["kinetics_trusted"] is False
    assert drifted["kinetics_note"] == "tof_scaling_drift=0.31"
    assert "log_tof" not in drifted
    leaky = _kinetics_scalars({"kinetics_v2": _kmc_v2("mcamc_bfs", leak=2e-3)})
    assert leaky["kinetics_trusted"] is False
    assert leaky["kinetics_note"] == "amc_leak=0.002"

    # TPD failed and not corrected
    tpd = {
        "CO": {
            "applied": False,
            "status": "mismatch",
            "offset_eV": None,
        }
    }
    out = _kinetics_scalars({"kinetics_v2": {"tof_mf": 2.0e-3, "tpd": tpd}})
    assert out["kinetics_trusted"] is False
    assert out["kinetics_note"] == "tpd check failed, not corrected: CO mismatch"
    applied = _kinetics_scalars(
        {
            "kinetics_v2": {
                "tof_mf": 2.0e-3,
                "tpd": {
                    "CO": {
                        "applied": True,
                        "status": "corrected",
                        "offset_eV": -0.12,
                    }
                },
            }
        }
    )
    assert applied["kinetics_trusted"] is True
    assert applied["tpd_max_offset_eV"] == pytest.approx(0.12)


def test_v2_only_warning_union_dedupes_v1_and_v2():
    """Both records carry the same engine warnings list; with both present
    the uncited note reads once per gas (v1's order), not twice."""
    from precis_pathway._dispatch_common import _kinetics_scalars

    warns = ["sticking uncited: H2", "sticking uncited: NO"]
    out = _kinetics_scalars(
        {
            "kinetics": {"tof": 1.0e-4, "warnings": list(warns)},
            "kinetics_v2": {"tof_mf": 2.0e-3, "warnings": list(warns)},
        }
    )
    assert out["kinetics_trusted"] is False
    assert out["kinetics_note"] == "sticking uncited: H2; sticking uncited: NO"


_GRAPH: dict[str, Any] = {
    "directed": True,
    "nodes": [{"id": "A"}, {"id": "B"}],
    "links": [
        {
            "source": "A",
            "target": "B",
            "barrier": 0.5,
            "delta_e": -0.1,
            "low_confidence": False,
            "kind": "reaction",
        }
    ],
}


def test_kinetics_view_reads_the_v2_record():
    """view='kinetics' lifts environment/Q-K/reversibility off the v2
    record alone (D8 reader flip); the pathway target names the product
    the record does not carry, and v1-only keys (the basis hash) fall back
    through the merge when a v1 block rides along."""
    from precis_pathway.toon_views import kinetics_text

    v2 = {
        "environment": {
            "T": 500.0,
            "p_total": 1.05,
            "p_total_stated": False,
            "gases": {
                "inputs": {"NO": 0.05},
                "outputs": {"NH3": 1e-3},
                "unlisted": "refuse",
            },
        },
        "q_over_k": 1e-31,
        "reversibility": {"NO*->NOH*": 0.01},
    }
    meta = {
        "graph": _GRAPH,
        "results": {"pathway": ["A", "B"], "target": "NH3", "kinetics_v2": v2},
    }
    body = kinetics_text(meta)
    assert "Environment: T = 500.0 K" in body
    assert "Q/K (overall NH3 formation) = 1.00e-31" in body
    assert "NO*->NOH*" in body  # reversibility off the record
    # v1 block beside it: the record's fields win, the v1-only basis hash
    # survives the merge
    meta2 = {
        "graph": _GRAPH,
        "results": {
            "pathway": ["A", "B"],
            "target": "NH3",
            "kinetics": {
                "q_over_k": 5.0,
                "environment_basis": "ab" * 32,
            },
            "kinetics_v2": v2,
        },
    }
    body2 = kinetics_text(meta2)
    assert "Q/K (overall NH3 formation) = 1.00e-31" in body2
    assert f"basis {('ab' * 32)[:12]}" in body2  # _environment_line truncates


def test_sweep_path_names_no_kinetics_v2_dead_branch():
    """gr514391: ``kinetics.sweep`` returns the sweep payload only — it
    never sets ``kinetics_v2``, so a read of that key in the sweep path is
    a dead branch with no producer (the D8 record reaches ``results_json``
    from ``run_kinetics``'s solve and the TPD leg, not the sweep).  Pins
    the removal: the three sweep functions must not name the key at all.
    Since gr514717 ``precis_pathway.runner`` imports engine-free, this
    reads the live functions rather than parsing the file."""
    import inspect

    from precis_pathway import runner

    for name in (
        "run_kinetics_sweep_subprocess",
        "_run_kinetics_sweep",
        "_kinetics_sweep_child",
    ):
        fn = getattr(runner, name, None)
        assert fn is not None, f"{name} renamed or removed"
        assert "kinetics_v2" not in inspect.getsource(fn), (
            f"{name} names kinetics_v2 — the engine sweep never "
            "produces that key; the branch is dead (gr514391)"
        )


# --- gr514717: the sweep internals run against a fake engine ----------------


def _fake_engine(monkeypatch: pytest.MonkeyPatch, sweep: Any) -> dict[str, Any]:
    """Stand-in ``autocatpath`` modules in ``sys.modules`` — enough of the
    engine surface for :func:`precis_pathway.runner._run_kinetics_sweep`
    (config/net/ledgers/refs/table + ``kinetics.sweep``).  Returns the call
    log each fake writes into.  Works with the engine absent (CI
    ``--no-extra catalyst``) or installed (the fakes shadow it)."""
    import sys
    from types import ModuleType

    calls: dict[str, Any] = {}

    class _Config:
        @classmethod
        def from_dict(cls, d: dict[str, Any]) -> Any:
            calls["config_dict"] = d
            return "CFG"

    config_mod: Any = ModuleType("autocatpath.config")
    config_mod.Config = _Config

    def _build_net(cfg: Any) -> Any:
        calls["build_net"] = cfg
        return "NET"

    def _ledger_refs(cfg: Any, net: Any, ledgers: Any, *, log: Any) -> Any:
        calls["refs_args"] = (cfg, net, ledgers)
        return {"r": 1.0}

    pipeline_mod: Any = ModuleType("autocatpath.pipeline")
    pipeline_mod._build_net = _build_net
    pipeline_mod.ledger_refs = _ledger_refs

    def _compute_ledgers(net: Any) -> Any:
        calls["ledgers_net"] = net
        return {"L": 2.0}, ["w1"]

    ledger_mod: Any = ModuleType("autocatpath.ledger")
    ledger_mod.compute_ledgers = _compute_ledgers

    class _Table:
        def corrected_refs(self, refs: Any) -> Any:
            calls["corrected_refs_in"] = refs
            return {"r": 1.5}

    def _build_table(cfg: Any) -> Any:
        calls["table_cfg"] = cfg
        return _Table()

    thermo_mod: Any = ModuleType("autocatpath.thermo")
    thermo_mod.build_table = _build_table

    kinetics_mod: Any = ModuleType("autocatpath.kinetics")
    kinetics_mod.sweep = sweep

    parent: Any = ModuleType("autocatpath")
    parent.kinetics = kinetics_mod

    monkeypatch.setitem(sys.modules, "autocatpath", parent)
    monkeypatch.setitem(sys.modules, "autocatpath.kinetics", kinetics_mod)
    monkeypatch.setitem(sys.modules, "autocatpath.config", config_mod)
    monkeypatch.setitem(sys.modules, "autocatpath.pipeline", pipeline_mod)
    monkeypatch.setitem(sys.modules, "autocatpath.ledger", ledger_mod)
    monkeypatch.setitem(sys.modules, "autocatpath.thermo", thermo_mod)
    return calls


def test_run_kinetics_sweep_normalizes_g_target_tpd(monkeypatch):
    """gr514717: ``_run_kinetics_sweep`` runs engine-free — ``g`` flattens
    Estimate/dict node records to floats, ``target`` reads score → config →
    ``"NH3"``, and ``kinetics_tpd`` is forwarded only when it carries
    ``offsets``."""
    from precis_pathway import runner

    seen: dict[str, Any] = {}

    def _sweep(cfg, nodes, edges, net, ledgers, refs, **kw):
        seen.update(
            cfg=cfg,
            nodes=nodes,
            edges=edges,
            net=net,
            ledgers=ledgers,
            refs=refs,
            **kw,
        )
        return {"points": [{"T": 500.0}]}

    calls = _fake_engine(monkeypatch, _sweep)

    results: dict[str, Any] = {
        "nodes": {"A": {"mean": -1.25}, "B": -0.5, "C": "0.75"},
        "edges": [{"a": "A", "b": "B"}],
        "score": {"target": "NO"},
        "thermo": {"basis": "x"},
        "kinetics_tpd": {"offsets": {"CO": {"offset_eV": -0.1}}},
    }
    runner._run_kinetics_sweep({"target": "CH4"}, results)

    assert calls["config_dict"] == {"target": "CH4"}
    assert seen["cfg"] == "CFG" and seen["net"] == "NET"
    assert seen["ledgers"] == {"L": 2.0}
    assert seen["refs"] == {"r": 1.5}  # the table-corrected refs, not raw
    assert seen["g"] == {"A": -1.25, "B": -0.5, "C": 0.75}
    assert seen["target"] == "NO"  # score.target outranks config.target
    assert seen["thermo_record"] == {"basis": "x"}
    assert seen["tpd"] == {"offsets": {"CO": {"offset_eV": -0.1}}}
    assert results["kinetics_sweep"] == {"points": [{"T": 500.0}]}

    # target falls back through config to "NH3"; tpd without offsets is
    # not forwarded at all
    results2: dict[str, Any] = {"nodes": {}, "edges": []}
    seen.clear()
    runner._run_kinetics_sweep({}, results2)
    assert seen["target"] == "NH3"
    assert "tpd" not in seen
    results3: dict[str, Any] = {
        "nodes": {},
        "edges": [],
        "kinetics_tpd": {"tpd": {"CO": {"status": "mismatch"}}},
    }
    seen.clear()
    runner._run_kinetics_sweep({}, results3)
    assert seen["target"] == "NH3" and "tpd" not in seen


def test_run_kinetics_sweep_records_error_never_raises(monkeypatch):
    """gr514717: any engine-side failure lands as ``kinetics_sweep_error``
    on the results dict — the job turns it into a note, never a
    pathway-fatal."""
    from precis_pathway import runner

    def _boom(*a, **k):
        raise RuntimeError("engine exploded")

    _fake_engine(monkeypatch, _boom)
    results: dict[str, Any] = {"nodes": {}, "edges": []}
    runner._run_kinetics_sweep({}, results)
    assert results["kinetics_sweep_error"] == "engine exploded"
    assert "kinetics_sweep" not in results


def test_kinetics_view_leads_with_the_v2_solve_status():
    """A failed MF solve (prod 527720 shape) is stated before the v1 Eyring
    table: rung, failure reason, non-positive TOF, legacy flag, BEP share,
    and the target's missing selectivity."""
    from precis_pathway.toon_views import kinetics_text

    v2 = {
        "tof_mf": None,
        "tof_kmc": None,
        "tof_chain": -6.77e-9,
        "coverages": None,
        "drc": {},
        "bep_flux_share": 1.0,
        "environment": {"legacy": True},
        "selectivity": {"H2O": 1.0},
        "warnings": [
            "kinetics: mean-field MKM failed (MKMError: mkm: Newton did not converge)"
        ],
    }
    meta = {
        "graph": _GRAPH,
        "results": {"pathway": ["A", "B"], "target": "NH3", "kinetics_v2": v2},
    }
    body = kinetics_text(meta)
    assert body.startswith(
        "v2 solve: mean-field solve failed: Newton did not converge"
        " — showing chain estimate"
    )
    assert "no positive steady state" in body
    assert "no pressures stated — gases priced at 1 bar reference" in body
    assert "bep_flux_share = 1" in body
    assert "selectivity to NH3: unknown" in body
    assert body.index("v2 solve:") < body.index("microkinetics (Eyring TST")


# --- mf_status display (engine >= the one stamping it) ----------------------------


def _mf_v2(**status: Any) -> dict[str, Any]:
    return {
        "tof_mf": 2.5e-3,
        "tof_kmc": None,
        "tof_chain": 1.0e-3,
        "environment": {},
        "selectivity": {"NH3": 0.9},
        "mf_status": {
            "ok": True,
            "stage": "steady",
            "reason": None,
            "state_err": 0.0,
            "failed_observables": [],
            **status,
        },
        "warnings": [],
    }


def test_toon_status_names_an_unsettled_state_and_failed_observables() -> None:
    from precis_pathway.toon_views import _v2_status_lines

    v2 = _mf_v2(
        state_err=0.37,
        failed_observables=[{"name": "drc", "reason": "singular Jacobian"}],
    )
    lines = _v2_status_lines(v2, "NH3")
    assert (
        "state not fully settled: ln-coverage step 0.37 — "
        "treat TOF as order-of-magnitude" in lines
    )
    assert "failed observables: drc (singular Jacobian)" in lines
    # a settled, clean record says neither
    clean = _v2_status_lines(_mf_v2(state_err=1e-12), "NH3")
    assert not any("not fully settled" in x or "failed observables" in x for x in clean)


def test_toon_status_prefers_mf_status_over_the_warning() -> None:
    from precis_pathway.toon_views import _v2_status_lines

    v2 = _mf_v2(ok=False, stage="newton", reason="MKMError: mkm: stalled")
    v2["tof_mf"] = None
    v2["warnings"] = ["kinetics: mean-field MKM failed (MKMError: mkm: other)"]
    head = _v2_status_lines(v2, None)[0]
    assert "stalled" in head and "other" not in head


def test_toon_status_falls_back_to_the_warnings_for_stored_records() -> None:
    from precis_pathway.toon_views import _v2_status_lines

    v2 = _mf_v2()
    del v2["mf_status"]
    v2["warnings"] = [
        "kinetics: mean-field observable drc failed (singular)",
    ]
    assert "failed observables: drc (singular)" in _v2_status_lines(v2, None)


# --- the job: tiers, idempotency, detached protocol, recompute -------------------------


def _mint_capture(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    import precis.handlers.job as jh_mod

    minted: list[dict] = []

    class _JH:
        def __init__(self, hub=None):
            pass

        def put(self, **kw):
            minted.append(kw)
            return MagicMock(ref_id=777, body="queued")

    monkeypatch.setattr(jh_mod, "JobHandler", _JH)
    monkeypatch.setenv("PRECIS_PATHWAY_KINETICS_MINT_ENABLED", "1")
    return minted


@pytest.mark.parametrize(
    ("tier", "mints"), [("verify", True), ("neb", True), ("screen", False)]
)
def test_mint_tiers(monkeypatch, tier: str, mints: bool) -> None:
    from precis_pathway import aggregate_job

    minted = _mint_capture(monkeypatch)
    out = aggregate_job._mint_kinetics_job(_mint_ctx(tier), {"config": {}}, 42)
    assert (out == 777) is mints
    assert len(minted) == (1 if mints else 0)


def test_idem_key_tracks_engine_environment_and_aggregate() -> None:
    from precis_pathway.aggregate_job import kinetics_idem_key

    env_a = {"environment": {"T": 300.0, "gases": {"inputs": {"NO": 0.1}}}}
    env_b = {"environment": {"T": 300.0, "gases": {"inputs": {"NO": 0.2}}}}
    base = kinetics_idem_key(42, env_a, engine="0.40.0")
    assert base == kinetics_idem_key(42, dict(env_a), engine="0.40.0")  # unchanged
    assert base != kinetics_idem_key(42, env_a, engine="0.41.0")  # new engine
    assert base != kinetics_idem_key(42, env_b, engine="0.40.0")  # new environment
    assert base != kinetics_idem_key(43, env_a, engine="0.40.0")
    sweep = {"kinetics": {"sweep": {"n": 4}}}
    assert kinetics_idem_key(42, {**env_a, **sweep}, engine="0.40.0") != base
    assert base.startswith("autocatpath_kinetics:42:0.40.0:")
    # a promoted aggregate is new work; a re-solve's kinetics blocks are not
    neb = {"nodes": [1], "edges": [{"tier": "neb"}], "kinetics": {"tof": 1.0}}
    verify = {**neb, "edges": [{"tier": "verify"}]}
    key = kinetics_idem_key(42, env_a, neb, engine="0.40.0")
    assert key != kinetics_idem_key(42, env_a, verify, engine="0.40.0")
    assert key == kinetics_idem_key(
        42, env_a, {**neb, "kinetics": {"tof": 2.0}}, engine="0.40.0"
    )


def test_mint_skips_when_the_same_key_already_succeeded(monkeypatch) -> None:
    from precis_pathway import aggregate_job

    minted = _mint_capture(monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(aggregate_job, "_engine_version", lambda: "0.42.0")

    def _succeeded(_store: Any, key: str) -> int:
        seen.append(key)
        return 5

    monkeypatch.setattr(aggregate_job, "_succeeded_job_id", _succeeded)
    out = aggregate_job._mint_kinetics_job(_mint_ctx(), {"config": {}}, 42)
    assert out is None and not minted and seen[0].startswith("autocatpath_kinetics:42:")


def test_mint_without_a_named_engine_always_mints(monkeypatch) -> None:
    from precis_pathway import aggregate_job

    minted = _mint_capture(monkeypatch)
    monkeypatch.setattr(aggregate_job, "_engine_version", lambda: "unknown")
    monkeypatch.setattr(aggregate_job, "_succeeded_job_id", lambda store, k: 5)
    aggregate_job._mint_kinetics_job(_mint_ctx(), {"config": {}}, 42)
    assert len(minted) == 1


def test_job_is_detached_and_sized_from_the_kinetics_env(monkeypatch) -> None:
    from precis_pathway import kinetics_job

    spec = kinetics_job.SPEC
    assert spec.submit and spec.poll and spec.kill and spec.reap and spec.dispatch
    assert spec.requires == frozenset()
    assert spec.wall_seconds is not None
    monkeypatch.delenv("PRECIS_AUTOCATPATH_KINETICS_SECONDS", raising=False)
    assert spec.wall_seconds() == 4800.0
    monkeypatch.setenv("PRECIS_AUTOCATPATH_KINETICS_SECONDS", "2400")
    # TPD and solve one step each, the sweep three
    assert spec.wall_seconds() == 5 * 2400 + 300
    assert kinetics_job._sweep_budget_s() == 7200
    monkeypatch.setenv("PRECIS_AUTOCATPATH_KINETICS_SECONDS", "0")
    assert spec.wall_seconds() == 4800.0  # unbounded steps: the deadline bounds it
    assert kinetics_job._sweep_budget_s() == 0
    monkeypatch.setenv("PRECIS_AUTOCATPATH_KINETICS_SECONDS", "junk")
    assert spec.wall_seconds() == 4800.0
    assert kinetics_job._sweep_budget_s() == 2700


def test_compute_skips_the_tpd_ramp_without_a_reference_family(monkeypatch) -> None:
    """No family means no comparison and no offset: the ramp timed out at 900 s
    on 511428 and 527720 to produce nothing."""
    from precis_pathway import kinetics_job

    ramps: list[dict] = []
    _fake_runner(
        monkeypatch,
        has_tpd=lambda: True,
        run_kinetics_tpd_subprocess=lambda cfg, res: ramps.append(cfg),
        run_kinetics_subprocess=lambda cfg, artifact, **k: None,
        run_kinetics_sweep_subprocess=lambda cfg, res, **k: None,
        fold_tpd=lambda res: None,
    )
    notes: list[str] = []
    legacy = {"name": "x", "conditions": {"pressures": {"NO": 0.5}}}
    kinetics_job.compute(legacy, {}, notes.append)
    assert ramps == []
    assert any("TPD skipped" in n for n in notes)

    with_family = {**legacy, "kinetics": {"tpd": {"family": "Pd111"}}}

    def _ramp(runner: Any, cfg: dict, res: dict, note: Any) -> None:
        ramps.append(cfg)

    monkeypatch.setattr(kinetics_job, "_tpd_check", _ramp)
    kinetics_job.compute(with_family, {}, notes.append)
    assert ramps == [with_family]


def test_failed_sweep_moves_the_stored_one_aside(monkeypatch) -> None:
    """Solved under another request, it must not read as this one's answer."""
    from precis_pathway import kinetics_job, seed_job

    store = _stored_pathway()
    store.ref.meta["results"]["kinetics_sweep"] = {"points": [{"T": 1.0}]}
    envelope = {
        "results": {
            "kinetics_v2": {"tof_mf": 1.0},
            "kinetics_sweep_error": "timed out",
        },
        "notes": [],
    }
    _fake_runner(
        monkeypatch,
        poll_seed_partial_detached=lambda h, **kw: {
            "state": "done",
            "result": envelope,
        },
    )
    monkeypatch.setattr(seed_job, "_reap_scratch", lambda ctx, h, *, failed: None)

    kinetics_job._poll(_job_ctx(store), {"dir": "/tmp/x"})

    res = store.ref.meta["results"]
    assert "kinetics_sweep" not in res
    assert res["kinetics_sweep_previous"] == {"points": [{"T": 1.0}]}
    assert res["kinetics_sweep_error"] == "timed out"


def test_overlay_replaces_environment_and_drops_its_priced_views() -> None:
    from precis_pathway.kinetics_job import overlay_recompute_config

    stored: dict[str, Any] = {
        "environment": {"T": 300.0},
        "conditions": {"temperature": 300.0, "pressures": {"NO": 0.1}},
        "kinetics": {"sticking": {"NO": 0.5}, "n_samples": 50},
        "objective": {"maximise": "tof"},
    }
    new_env = {"T": 300.0, "gases": {"inputs": {"NO": 0.5}}}
    out = overlay_recompute_config(stored, environment=new_env, sweep={"n": 6})
    assert out["environment"] == new_env
    assert "pressures" not in out["conditions"]
    assert out["conditions"]["temperature"] == 300.0
    assert "sticking" not in out["kinetics"] and out["kinetics"]["n_samples"] == 50
    assert out["kinetics"]["sweep"] == {"n": 6}
    assert out["objective"] == {"maximise": "tof"}  # untouched
    assert stored["conditions"]["pressures"] == {"NO": 0.1}  # input not mutated
    # no overrides: the stored config as it was
    assert overlay_recompute_config(stored) == stored


def _fake_runner(monkeypatch: pytest.MonkeyPatch, **fns: Any) -> Any:
    import sys
    from types import SimpleNamespace

    import precis_pathway

    fake = SimpleNamespace(**fns)
    monkeypatch.setitem(sys.modules, "precis_pathway.runner", fake)
    monkeypatch.setattr(precis_pathway, "runner", fake, raising=False)
    return fake


def test_run_compute_drops_a_stale_record_and_returns_an_envelope(monkeypatch) -> None:
    from precis_pathway import kinetics_job

    env = {"T": 300.0, "gases": {"inputs": {"NO": 0.5}}}

    def _solve(config, artifact, *, timeout=None, tpd=None):
        rj = artifact["results_json"]
        assert "kinetics_v2" not in rj  # the stale record never reaches the engine
        assert config == {"environment": env}  # params.config rides through
        rj["kinetics_v2"] = {"tof_mf": 9.0}

    def _sweep(config, res, *, timeout=None):
        res["kinetics_sweep"] = {"points": []}

    _fake_runner(
        monkeypatch,
        has_tpd=lambda: False,
        run_kinetics_subprocess=_solve,
        run_kinetics_sweep_subprocess=_sweep,
    )
    out = kinetics_job.run_compute(
        {"environment": env},
        {"nodes": {"A": 0}, "edges": [], "kinetics_v2": {"tof_mf": 1.0}},
    )
    assert out["results"] == {
        "kinetics_v2": {"tof_mf": 9.0},
        "kinetics_sweep": {"points": []},
    }
    assert any("kinetics solved (tof_mf=9.0)" in n for n in out["notes"])


def _job_ctx(store: _MetaStore, config: dict | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.meta = {"params": {"pathway_ref_id": store.ref.id, "config": config or {}}}
    ctx.store = store
    return ctx


def _stored_pathway() -> _MetaStore:
    return _MetaStore(
        {
            "results": {
                "nodes": {"A": -1.0},
                "edges": [{"a": "A", "b": "A"}],
                "kinetics_v2": {"tof_mf": 1.0},
                "kinetics_error": "old solve error",
            }
        }
    )


def test_submit_launches_the_child_with_inputs_only(monkeypatch) -> None:
    from precis_pathway import kinetics_job

    launched: list[tuple] = []

    def _launch(cfg, res, **kw):
        launched.append((cfg, res))
        return {"pid": 1, "pgid": 1, "dir": "/tmp/x", "started_at": 0.0}

    _fake_runner(monkeypatch, submit_kinetics_job_detached=_launch)
    store = _stored_pathway()
    env = {"T": 300.0, "gases": {"inputs": {"NO": 0.5}}}
    ctx = _job_ctx(store, {"environment": env})
    handle = kinetics_job._submit(ctx, kinetics_job.SPEC)
    assert handle and handle["pid"] == 1
    cfg, res = launched[0]
    assert cfg == {"environment": env}
    assert set(res) == {"nodes", "edges", "score", "thermo"}  # no stale kinetics_v2
    ctx.set_status.assert_not_called()
    ctx.record_failure.assert_not_called()
    # a pathway with nothing aggregated fails the job instead of launching
    store.ref.meta = {"results": {}}
    ctx2 = _job_ctx(store)
    assert kinetics_job._submit(ctx2, kinetics_job.SPEC) is None
    ctx2.record_failure.assert_called_once()


def test_poll_persists_on_done_and_refreshes_the_record(monkeypatch) -> None:
    from precis_pathway import kinetics_job, seed_job

    store = _stored_pathway()
    envelope = {
        "results": {
            "kinetics_v2": {"tof_mf": 9.0},
            "kinetics_sweep": {"points": [{"T": 500.0}]},
        },
        "notes": ["autocatpath_kinetics: kinetics solved (tof_mf=9.0)"],
    }
    states = iter([{"state": "running"}, {"state": "done", "result": envelope}])
    reaped: list[bool] = []
    _fake_runner(monkeypatch, poll_seed_partial_detached=lambda h, **kw: next(states))
    monkeypatch.setattr(
        seed_job, "_reap_scratch", lambda ctx, h, *, failed: reaped.append(failed)
    )
    ctx = _job_ctx(store)
    assert kinetics_job._poll(ctx, {"dir": "/tmp/x"}) is False
    ctx.set_status.assert_not_called()
    assert kinetics_job._poll(ctx, {"dir": "/tmp/x"}) is True
    res = store.ref.meta["results"]
    assert res["kinetics_v2"]["tof_mf"] == 9.0  # the stale 1.0 is replaced
    assert "kinetics_error" not in res  # the fresh success retires the old error
    ctx.set_status.assert_called_once_with("succeeded")
    ctx.record_failure.assert_not_called()
    assert reaped == [False]
    events = [c.args[1] for c in ctx.append_chunk.call_args_list]
    assert "autocatpath_kinetics: kinetics solved (tof_mf=9.0)" in events


def test_poll_fails_the_job_when_the_sweep_solved_no_point(monkeypatch) -> None:
    """Every point refused (542535): the record is saved, the job fails naming
    the first refusal instead of succeeding with an empty sweep."""
    from precis_pathway import kinetics_job, seed_job

    store = _stored_pathway()
    envelope = {
        "results": {
            "kinetics_v2": {"tof_mf": 9.0},
            "kinetics_sweep": {"points": [], "warnings": ["sweep: re-T refuses 400 K"]},
        },
        "notes": [],
    }
    _fake_runner(
        monkeypatch,
        poll_seed_partial_detached=lambda h, **kw: {
            "state": "done",
            "result": envelope,
        },
    )
    monkeypatch.setattr(seed_job, "_reap_scratch", lambda ctx, h, *, failed: None)
    ctx = _job_ctx(store)

    assert kinetics_job._poll(ctx, {"dir": "/tmp/x"}) is True

    assert store.ref.meta["results"]["kinetics_v2"]["tof_mf"] == 9.0
    ctx.set_status.assert_not_called()
    assert "re-T refuses 400 K" in ctx.record_failure.call_args.args[0]


def test_poll_does_not_call_the_first_grid_row_an_optimum(monkeypatch) -> None:
    from precis_pathway import kinetics_job, seed_job

    store = _stored_pathway()
    envelope = {
        "results": {
            "kinetics_v2": {"tof_mf": 1.0},
            "kinetics_sweep": {
                "points": [{"T": 500.0}],
                "optimum": {"T": 500.0, "value": None, "objective": "selectivity.NH3"},
            },
        },
        "notes": [],
    }
    _fake_runner(
        monkeypatch,
        poll_seed_partial_detached=lambda h, **kw: {
            "state": "done",
            "result": envelope,
        },
    )
    monkeypatch.setattr(seed_job, "_reap_scratch", lambda ctx, h, *, failed: None)
    ctx = _job_ctx(store)

    kinetics_job._poll(ctx, {"dir": "/tmp/x"})

    events = [c.args[1] for c in ctx.append_chunk.call_args_list]
    assert any("selectivity.NH3 had no value" in e for e in events)
    assert not any("optimum T=" in e for e in events)
    sweep = store.ref.meta["results"]["kinetics_sweep"]
    assert sweep["optimum"] is None
    assert any("no optimum" in w for w in sweep["warnings"])
    assert "optimum_T" not in ctx.set_meta.call_args.args[0]


def test_poll_persists_a_nan_as_null(monkeypatch) -> None:
    """A solve with no positive TOF reports its rate-control sum as NaN, which
    a jsonb write refuses (job 543712)."""
    from precis_pathway import kinetics_job, seed_job

    store = _stored_pathway()
    envelope = {
        "results": {"kinetics_v2": {"tof_mf": 1e-21, "drc": {"sum": float("nan")}}},
        "notes": [],
    }
    _fake_runner(
        monkeypatch,
        poll_seed_partial_detached=lambda h, **kw: {
            "state": "done",
            "result": envelope,
        },
    )
    monkeypatch.setattr(seed_job, "_reap_scratch", lambda ctx, h, *, failed: None)

    assert kinetics_job._poll(_job_ctx(store), {"dir": "/tmp/x"}) is True

    stored = store.ref.meta["results"]["kinetics_v2"]
    assert stored["tof_mf"] == 1e-21
    assert stored["drc"]["sum"] is None
    json.dumps(store.ref.meta, allow_nan=False)


def test_poll_manifest_names_the_overriding_environment(monkeypatch) -> None:
    from precis_pathway import kinetics_job, seed_job
    from precis_pathway import manifest as vm

    store = _stored_pathway()
    store.ref.meta["config"] = {"name": "stored"}
    store.ref.meta["config_snapshot_yaml"] = "name: stored\n"
    seen: list[dict] = []

    def _build(**kw):
        seen.append(kw["config"])

    monkeypatch.setattr(vm, "build_safely", _build)
    envelope = {"results": {"kinetics_v2": {"tof_mf": 9.0}}, "notes": []}
    _fake_runner(
        monkeypatch,
        poll_seed_partial_detached=lambda h, **kw: {
            "state": "done",
            "result": envelope,
        },
    )
    monkeypatch.setattr(seed_job, "_reap_scratch", lambda ctx, h, *, failed: None)
    env = {"T": 298.15, "gases": {"inputs": {"NO": 1e-3}}}
    ctx = _job_ctx(store, {"name": "stored", "environment": env})
    assert kinetics_job._poll(ctx, {"dir": "/tmp/x"}) is True
    assert seen == [{"name": "stored", "environment": env}]
    assert "environment" not in store.ref.meta["config"]  # stored config untouched


def test_poll_fails_the_job_when_the_resolve_failed(monkeypatch) -> None:
    from precis_pathway import kinetics_job, seed_job

    store = _stored_pathway()
    envelope = {
        "results": {"kinetics_error": "mkm: Newton did not converge"},
        "notes": [],
    }
    _fake_runner(
        monkeypatch,
        poll_seed_partial_detached=lambda h, **kw: {
            "state": "done",
            "result": envelope,
        },
    )
    monkeypatch.setattr(seed_job, "_reap_scratch", lambda *a, **k: None)
    ctx = _job_ctx(store)
    assert kinetics_job._poll(ctx, {"dir": "/tmp/x"}) is True
    ctx.set_status.assert_not_called()
    ctx.record_failure.assert_called_once()
    assert "Newton did not converge" in ctx.record_failure.call_args.args[0]
    # the stored record is untouched, the error rides on it
    assert store.ref.meta["results"]["kinetics_v2"]["tof_mf"] == 1.0
    assert store.ref.meta["results"]["kinetics_error"] == "mkm: Newton did not converge"


def test_poll_records_a_child_that_died(monkeypatch) -> None:
    from precis_pathway import kinetics_job, seed_job

    _fake_runner(
        monkeypatch,
        poll_seed_partial_detached=lambda h, **kw: {
            "state": "failed",
            "error": "child process exited without writing result.json",
            "infra": True,
        },
    )
    monkeypatch.setattr(seed_job, "_reap_scratch", lambda *a, **k: None)
    ctx = _job_ctx(_stored_pathway())
    assert kinetics_job._poll(ctx, {"dir": "/tmp/x"}) is True
    ctx.record_failure.assert_called_once()
    assert ctx.record_failure.call_args.kwargs["failure_class"] == "infra"


def test_poll_streams_progress_lines_once_and_heartbeats_when_quiet(
    monkeypatch, tmp_path
) -> None:
    """gr549807: the detached child's step notes land as job events while
    it still runs — each exactly once across polls — and a quiet poll
    writes one heartbeat with the child's pid and elapsed."""
    from precis_pathway import kinetics_job, seed_job

    progress = tmp_path / kinetics_job.PROGRESS_NAME
    progress.write_text(
        json.dumps("autocatpath_kinetics: TPD check") + "\n", encoding="utf-8"
    )
    handle = {"dir": str(tmp_path), "pid": 4242, "started_at": time.time() - 120}
    notes = [
        "autocatpath_kinetics: TPD check",
        "autocatpath_kinetics: kinetics solved (tof_mf=9.0)",
        "autocatpath_kinetics: sweeping (T, p_total)",
    ]
    envelope = {
        "results": {
            "kinetics_v2": {"tof_mf": 9.0},
            "kinetics_sweep": {"points": [{"T": 500.0}]},
        },
        "notes": notes,
    }
    states = iter(
        [
            {"state": "running"},
            {"state": "running"},
            {"state": "running"},
            {"state": "done", "result": envelope},
        ]
    )
    _fake_runner(
        monkeypatch,
        poll_seed_partial_detached=lambda h, **kw: next(states),
        _process_alive=lambda pid: True,
    )
    monkeypatch.setattr(seed_job, "_reap_scratch", lambda *a, **k: None)
    ctx = _job_ctx(_stored_pathway())

    assert kinetics_job._poll(ctx, handle) is False
    # a quiet poll (no new progress records) writes the heartbeat
    assert kinetics_job._poll(ctx, handle) is False
    with progress.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps("autocatpath_kinetics: kinetics solved (tof_mf=9.0)") + "\n"
        )
        fh.write(json.dumps("autocatpath_kinetics: sweeping (T, p_total)") + "\n")
    assert kinetics_job._poll(ctx, handle) is False
    assert kinetics_job._poll(ctx, handle) is True

    events = [c.args[1] for c in ctx.append_chunk.call_args_list]
    for line in notes:
        assert events.count(line) == 1
    heartbeats = [e for e in events if "heartbeat" in e]
    assert len(heartbeats) == 1
    assert "pid 4242 alive" in heartbeats[0]
    ctx.set_status.assert_called_once_with("succeeded")


def test_poll_records_a_dead_childs_last_progress(monkeypatch, tmp_path) -> None:
    """A child that wrote progress lines then died still fails as today —
    and the lines it did write land first."""
    from precis_pathway import kinetics_job, seed_job

    (tmp_path / kinetics_job.PROGRESS_NAME).write_text(
        json.dumps("autocatpath_kinetics: sweeping (T, p_total)") + "\n",
        encoding="utf-8",
    )
    _fake_runner(
        monkeypatch,
        poll_seed_partial_detached=lambda h, **kw: {
            "state": "failed",
            "error": "child process exited without writing result.json",
            "infra": True,
        },
    )
    monkeypatch.setattr(seed_job, "_reap_scratch", lambda *a, **k: None)
    ctx = _job_ctx(_stored_pathway())
    assert kinetics_job._poll(ctx, {"dir": str(tmp_path), "pid": 1}) is True
    ctx.record_failure.assert_called_once()
    assert ctx.record_failure.call_args.kwargs["failure_class"] == "infra"
    events = [c.args[1] for c in ctx.append_chunk.call_args_list]
    assert "autocatpath_kinetics: sweeping (T, p_total)" in events


def test_run_compute_mirrors_notes_to_the_progress_file(monkeypatch, tmp_path) -> None:
    """The child's note() mirror: every envelope note is also a flushed
    progress line, in order, so _poll can stream it."""
    from precis_pathway import kinetics_job

    _fake_runner(
        monkeypatch,
        has_tpd=lambda: False,
        run_kinetics_subprocess=lambda cfg, artifact, **kw: artifact[
            "results_json"
        ].__setitem__("kinetics_v2", {"tof_mf": 9.0}),
        run_kinetics_sweep_subprocess=lambda cfg, res, **kw: res.__setitem__(
            "kinetics_sweep", {"points": []}
        ),
    )
    path = tmp_path / kinetics_job.PROGRESS_NAME
    out = kinetics_job.run_compute(
        {"name": "x"},
        {"nodes": {"A": 0}, "edges": []},
        progress_path=str(path),
    )
    assert out["notes"]
    records = [
        json.loads(raw) for raw in path.read_text(encoding="utf-8").split("\n") if raw
    ]
    assert records == out["notes"]


def test_poll_streams_a_multi_line_note_once(monkeypatch, tmp_path) -> None:
    """A note carrying its own newlines (a ``kinetics_error`` reason with a
    stderr tail) is ONE JSON record: decoded whole, counted once — so the
    done-envelope dedup does not eat the notes after it."""
    from precis_pathway import kinetics_job, seed_job

    multi = (
        "autocatpath_kinetics: kinetics solve failed: boom\n"
        "--- stderr (warnings removed) ---\nTraceback: engine died"
    )
    notes = [
        multi,
        "autocatpath_kinetics: sweeping (T, p_total)",
        "autocatpath_kinetics: done",
    ]
    progress = tmp_path / kinetics_job.PROGRESS_NAME
    progress.write_text(json.dumps(multi) + "\n", encoding="utf-8")
    envelope = {
        "results": {
            "kinetics_v2": {"tof_mf": 9.0},
            "kinetics_sweep": {"points": [{"T": 500.0}]},
        },
        "notes": notes,
    }
    states = iter([{"state": "running"}, {"state": "done", "result": envelope}])
    _fake_runner(
        monkeypatch,
        poll_seed_partial_detached=lambda h, **kw: next(states),
        _process_alive=lambda pid: True,
    )
    monkeypatch.setattr(seed_job, "_reap_scratch", lambda *a, **k: None)
    ctx = _job_ctx(_stored_pathway())
    handle = {"dir": str(tmp_path), "pid": 4242, "started_at": time.time()}

    assert kinetics_job._poll(ctx, handle) is False  # drains the multi-line note
    with progress.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(notes[1]) + "\n")
        fh.write(json.dumps(notes[2]) + "\n")
    assert kinetics_job._poll(ctx, handle) is True

    events = [c.args[1] for c in ctx.append_chunk.call_args_list]
    for line in notes:
        assert events.count(line) == 1


class _Jobs:
    calls: list[dict[str, Any]] = []

    def __init__(self, *, hub: Any) -> None:
        pass

    def put(self, **kw: Any) -> Any:
        self.calls.append(kw)
        return MagicMock(body="job #99 queued", ref_id=99)


def _recompute_handler(meta: dict[str, Any]) -> Any:
    from types import SimpleNamespace

    from precis_pathway.handler import PathwayHandler

    ref = SimpleNamespace(id=11, slug="pw-a", meta=meta)
    store = SimpleNamespace(get_ref=lambda *, kind, id: ref)
    h = PathwayHandler.__new__(PathwayHandler)
    setattr(h, "hub", SimpleNamespace(live_store=store))
    return h


def test_recompute_kinetics_mode_mints_with_overridden_sweep(monkeypatch) -> None:
    import precis.handlers.job as job_mod

    _Jobs.calls = []
    monkeypatch.setattr(job_mod, "JobHandler", _Jobs)
    h = _recompute_handler(
        {
            "config": {
                "name": "x",
                "conditions": {"pressures": {"NO": 0.5, "H2": 1.0}},
                "kinetics": {"n_samples": 50},
            },
            "results": {"nodes": {"A": 0}, "edges": [{"a": "A"}]},
            "content_key": "ck",
        }
    )
    resp = h.put(id="pw-a", mode="recompute_kinetics", args={"sweep": {"n": 6}})
    assert "overriding sweep" in resp.body
    assert "environment (legacy conditions)" in resp.body
    (kw,) = _Jobs.calls
    assert kw["job_type"] == "autocatpath_kinetics" and kw["requires"] == {}
    assert kw["params"]["config"]["kinetics"] == {"n_samples": 50, "sweep": {"n": 6}}
    # the stored legacy env rides along untouched — only the sweep key moved
    assert kw["params"]["config"]["conditions"]["pressures"] == {
        "NO": 0.5,
        "H2": 1.0,
    }
    assert kw["params"]["pathway_ref_id"] == 11
    assert kw["params"]["pathway_slug"] == "pw-a"
    assert kw["idem_key"].startswith("autocatpath_kinetics:11:")


def test_recompute_kinetics_refusals_name_the_next_step() -> None:
    from precis.errors import BadInput

    h = _recompute_handler({"config": {"name": "x"}, "results": {}})
    with pytest.raises(BadInput, match="needs the pathway handle"):
        h.put(mode="recompute_kinetics")
    with pytest.raises(BadInput, match="no aggregated results") as e:
        h.put(id="pw-a", mode="recompute_kinetics")
    assert e.value.next
    h2 = _recompute_handler({"results": {"nodes": {"A": 0}, "edges": [1]}})
    with pytest.raises(BadInput, match="stores no config"):
        h2.put(id="pw-a", mode="recompute_kinetics")
    h3 = _recompute_handler(
        {"config": {"a": 1}, "results": {"nodes": {"A": 0}, "edges": [1]}}
    )
    with pytest.raises(BadInput, match="must be a mapping") as e:
        h3.put(id="pw-a", mode="recompute_kinetics", args={"sweep": "n=4"})
    assert e.value.next


#: A ``kinetics_v2.environment`` record as ``Environment.resolved()`` writes
#: it — resolved (bar-scaled) tables, stated p_total, derived views.
_RECORD_ENV = {
    "T": 298.15,
    "p_total": 1.1,
    "p_total_stated": True,
    "gases": {
        "inputs": {"NO": 0.1, "H2": 1.0},
        "outputs": {"NH3": 1e-6},
        "inerts": {},
        "unlisted": "reference",
    },
    "sticking": {"NO": {"s0": 0.5, "source": "pa498804"}},
    "electrochemical": None,
    "composition": {"NO": 0.0909, "H2": 0.909, "NH3": 1e-6},
}

_RECORD_SWEEP = {
    "axes": {"T": [298.0, 450.0], "p_total": [0.1, 30.0]},
    "design": "grid",
    "composition": "fixed",
    "competing": {"pair": ["NO@N", "NOH"], "points": []},
    "optimum": {"T": 450.0, "p_total": 30.0, "objective": "tof", "value": 234.9},
    "points": [{"T": 298.0}],
}


def _kinetics_meta(config: dict[str, Any]) -> dict[str, Any]:
    """Pathway meta with the gripe's shape: aggregated results, a kinetics
    record solved under an env the stored config never carried."""
    return {
        "config": config,
        "results": {
            "nodes": {"A": 0},
            "edges": [{"a": "A"}],
            "kinetics_v2": {"tof_mf": 1.0, "environment": _RECORD_ENV},
            "kinetics_sweep": _RECORD_SWEEP,
        },
        "content_key": "ck",
    }


def _stub_env_engine(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Stand-in ``autocatpath.environment`` for the door's ``from_dict``
    validation — the engine is absent on the glue host (CI
    ``--no-extra catalyst``); real refusals live in
    ``test_pathway_kinetics_sweep.py``. Returns the calls each fake saw."""
    import sys
    from types import ModuleType

    seen: dict[str, list] = {"environment": [], "objective": []}

    class _Environment:
        @classmethod
        def from_dict(cls, d: dict[str, Any]) -> Any:
            seen["environment"].append(d)
            # the one check the tests lean on: the engine requires a
            # numeric T — everything else is a pass-through
            if not isinstance(d.get("T"), (int, float)) or isinstance(d.get("T"), bool):
                raise ValueError("environment.T is required (K)")
            return d

    class _Objective:
        @classmethod
        def from_dict(cls, d: dict[str, Any]) -> Any:
            seen["objective"].append(d)
            return d

    env_mod: Any = ModuleType("autocatpath.environment")
    env_mod.Environment = _Environment
    env_mod.Objective = _Objective
    parent: Any = sys.modules.get("autocatpath")
    if parent is None:
        parent = ModuleType("autocatpath")
        monkeypatch.setitem(sys.modules, "autocatpath", parent)
    parent.environment = env_mod
    monkeypatch.setitem(sys.modules, "autocatpath.environment", env_mod)
    return seen


def test_recompute_kinetics_no_args_carries_the_stored_record(monkeypatch) -> None:
    """gr550976: a no-arg recompute mints the job under the env/objective/
    sweep the last solve stored, and the ack names what it reused."""
    import precis.handlers.job as job_mod

    _Jobs.calls = []
    monkeypatch.setattr(job_mod, "JobHandler", _Jobs)
    seen = _stub_env_engine(monkeypatch)
    h = _recompute_handler(_kinetics_meta({"name": "x", "kinetics": {"n_samples": 50}}))
    resp = h.put(id="pw-a", mode="recompute_kinetics")
    (kw,) = _Jobs.calls
    cfg = kw["params"]["config"]
    # the record env is de-resolved back into an environment: block — the
    # resolved gases are already bar-scaled so p_total is never restated,
    # and only keys the record actually carries are emitted
    assert cfg["environment"] == {
        "T": 298.15,
        "gases": {
            "inputs": {"NO": 0.1, "H2": 1.0},
            "outputs": {"NH3": 1e-6},
            "unlisted": "reference",
        },
        "sticking": {"NO": {"s0": 0.5, "source": "pa498804"}},
    }
    assert cfg["objective"] == {"maximise": "tof", "competing": ["NO@N", "NOH"]}
    assert cfg["kinetics"]["sweep"] == {
        "axes": {"T": [298.0, 450.0], "p_total": [0.1, 30.0]},
        "design": "grid",
        "composition": "fixed",
    }
    assert cfg["kinetics"]["n_samples"] == 50  # unrelated keys untouched
    # the reused env passed the engine's own validation at the door
    assert seen["environment"] == [cfg["environment"]]
    for what in (
        "environment (kinetics_v2 record)",
        "objective (kinetics_sweep record)",
        "sweep (kinetics_sweep record)",
    ):
        assert what in resp.body
    assert "overriding" not in resp.body


def test_recompute_kinetics_arg_overrides_only_its_own_key(monkeypatch) -> None:
    """An explicit arg replaces only its key; the others still come from
    the stored record/config."""
    import precis.handlers.job as job_mod

    _Jobs.calls = []
    monkeypatch.setattr(job_mod, "JobHandler", _Jobs)
    _stub_env_engine(monkeypatch)
    h = _recompute_handler(
        _kinetics_meta(
            {
                "name": "x",
                "objective": {"maximise": "selectivity.NH3"},
                "kinetics": {"sweep": {"n": 12}},
            }
        )
    )
    new_env = {"T": 300.0, "gases": {"inputs": {"NO": 0.5}}}
    resp = h.put(
        id="pw-a",
        mode="recompute_kinetics",
        args={"environment": new_env},
    )
    (kw,) = _Jobs.calls
    cfg = kw["params"]["config"]
    assert cfg["environment"] == new_env  # the override
    assert cfg["objective"] == {"maximise": "selectivity.NH3"}  # config's own
    assert cfg["kinetics"]["sweep"] == {"n": 12}  # config's own
    assert "overriding environment" in resp.body
    assert "objective (config)" in resp.body
    assert "sweep (config)" in resp.body
    assert "kinetics_v2 record" not in resp.body


def test_recompute_kinetics_prefers_the_record_env_over_the_config(
    monkeypatch,
) -> None:
    """The env the current record was priced under is the freshest stored
    env — it wins over a differing declared one. A legacy-flagged record
    instead keeps the config's legacy projection byte-faithful."""
    import precis.handlers.job as job_mod
    from precis_pathway.kinetics_job import stored_environment

    _Jobs.calls = []
    monkeypatch.setattr(job_mod, "JobHandler", _Jobs)
    _stub_env_engine(monkeypatch)
    config_env = {"T": 300.0, "gases": {"inputs": {"NO": 0.5}}}
    meta = _kinetics_meta({"name": "x", "environment": config_env})
    h = _recompute_handler(meta)
    resp = h.put(id="pw-a", mode="recompute_kinetics")
    (kw,) = _Jobs.calls
    # the record env's gases (NO+H2 at stated 1.1 bar), not the config's
    assert kw["params"]["config"]["environment"]["gases"]["inputs"] == {
        "NO": 0.1,
        "H2": 1.0,
    }
    assert "environment (kinetics_v2 record)" in resp.body

    # a legacy-flagged record is not de-resolved: the legacy conditions in
    # the config price the same env byte-faithfully
    legacy_meta: dict[str, Any] = {
        "config": {
            "name": "x",
            "conditions": {"pressures": {"NO": 0.5}},
            "kinetics": {"sticking": {"NO": 0.5}},
        },
        "results": {
            "kinetics_v2": {
                "environment": {**_RECORD_ENV, "legacy": True},
            }
        },
    }
    block, src = stored_environment(
        legacy_meta, legacy_meta["config"], legacy_meta["results"]
    )
    assert block is None and src == "legacy conditions"


def test_recompute_kinetics_refuses_without_any_stored_environment() -> None:
    """gr550976 part: no usable env anywhere — refuse at submit naming the
    missing environment, not after the solve burns a run."""
    from precis.errors import BadInput

    results = {"nodes": {"A": 0}, "edges": [{"a": "A"}]}
    for meta in (
        # no environment anywhere
        {"config": {"name": "x", "kinetics": {"n_samples": 50}}, "results": results},
        # a record env with an empty gas table is no env to sweep under
        {
            "config": {"name": "x"},
            "results": {
                **results,
                "kinetics_v2": {
                    "environment": {
                        "T": 298.15,
                        "p_total": 0.0,
                        "p_total_stated": False,
                        "gases": {
                            "inputs": {},
                            "outputs": {},
                            "inerts": {},
                            "unlisted": "reference",
                        },
                        "sticking": {},
                        "electrochemical": None,
                    }
                },
            },
        },
        # legacy keys but an empty pressures table
        {"config": {"name": "x", "conditions": {"pressures": {}}}, "results": results},
    ):
        h = _recompute_handler(meta)
        with pytest.raises(BadInput, match="stores no environment") as e:
            h.put(id="pw-a", mode="recompute_kinetics")
        assert "environment" in str(e.value.next)


def test_kinetics_job_fails_fast_only_on_a_stated_p_total(monkeypatch) -> None:
    """The job-side gate refuses the one env inconsistency that can never
    compute — a stated ``p_total`` on an empty gas table, which the
    engine's own ``Environment.from_dict`` rejects — before a child
    launches. Everything else runs: an aggregate-minted job with no env
    at all still solves at the fallback."""
    from precis_pathway import kinetics_job

    launched: list = []
    _fake_runner(
        monkeypatch,
        submit_kinetics_job_detached=lambda cfg, res, **kw: launched.append(cfg),
    )
    store = _stored_pathway()

    # stated p_total with nothing to scale — refused at submit AND at the
    # blocking dispatch fallback, before any child burns
    bad = _job_ctx(store, {"environment": {"T": 300.0, "p_total": 2.0}})
    assert kinetics_job._submit(bad, kinetics_job.SPEC) is None
    bad.record_failure.assert_called_once()
    assert "no composition to scale" in bad.record_failure.call_args.args[0]
    bad2 = _job_ctx(store, {"environment": {"T": 300.0, "p_total": 2.0}})
    kinetics_job._dispatch(bad2, kinetics_job.SPEC)
    bad2.record_failure.assert_called_once()
    assert launched == []

    # gas entries count toward the table by the engine's own acceptance —
    # a bare float(): numeric strings count; forms the engine itself
    # refuses (unit strings, dicts) don't, and the gate still catches them
    ok_env = {
        "T": 300.0,
        "p_total": 2.0,
        "gases": {"inputs": {"NO": "0.5"}},
    }
    str_ctx = _job_ctx(store, {"environment": ok_env})
    kinetics_job._submit(str_ctx, kinetics_job.SPEC)
    str_ctx.record_failure.assert_not_called()
    assert launched == [{"environment": ok_env}]
    for entry in ("0.5 bar", {"p": 0.5}):
        ctx = _job_ctx(
            store,
            {
                "environment": {
                    "T": 300.0,
                    "p_total": 2.0,
                    "gases": {"inputs": {"NO": entry}},
                }
            },
        )
        assert kinetics_job._submit(ctx, kinetics_job.SPEC) is None
        ctx.record_failure.assert_called_once()
    launched.clear()

    # never when the env is absent — and an empty table with null p_total
    # is engine-valid, so these launch (an empty-table env reaches the
    # sweep, where its rescale dies downstream as before)
    solved: list = []
    _fake_runner(
        monkeypatch,
        submit_kinetics_job_detached=lambda cfg, res, **kw: launched.append(cfg),
        run_kinetics_subprocess=lambda *a, **kw: solved.append(a),
        run_kinetics_sweep_subprocess=lambda *a, **kw: None,
    )
    ok_cfgs: list[dict[str, Any]] = [
        {},  # no environment at all — the aggregate-minted shape
        {"conditions": {"pressures": {}}},  # legacy empty
        {"environment": {"T": 300.0}},  # null p_total, empty table
    ]
    for cfg in ok_cfgs:
        ctx = _job_ctx(store, cfg)
        kinetics_job._submit(ctx, kinetics_job.SPEC)
        ctx.record_failure.assert_not_called()
    assert len(launched) == 3

    # the blocking fallback too: a no-env job still solves
    dispatch_ctx = _job_ctx(store, {})
    kinetics_job._dispatch(dispatch_ctx, kinetics_job.SPEC)
    dispatch_ctx.record_failure.assert_not_called()
    assert len(solved) == 1


def test_recompute_kinetics_names_a_stored_env_refusal(monkeypatch) -> None:
    """A reconstructed stored env the engine rejects is named as the STORED
    env being refused — the caller gave no args to blame; an args env
    refusal still says args."""
    import precis.handlers.job as job_mod
    from precis.errors import BadInput

    _Jobs.calls = []
    monkeypatch.setattr(job_mod, "JobHandler", _Jobs)
    _stub_env_engine(monkeypatch)  # refuses a non-numeric T
    meta = _kinetics_meta({"name": "x"})
    meta["results"]["kinetics_v2"]["environment"] = {**_RECORD_ENV, "T": "hot"}
    h = _recompute_handler(meta)
    with pytest.raises(BadInput, match="stored environment refused") as e:
        h.put(id="pw-a", mode="recompute_kinetics")
    assert "environment" in str(e.value.next)
    assert _Jobs.calls == []

    h2 = _recompute_handler(_kinetics_meta({"name": "x"}))
    with pytest.raises(BadInput, match="args environment refused"):
        h2.put(
            id="pw-a",
            mode="recompute_kinetics",
            args={"environment": {"T": "hot"}},
        )
