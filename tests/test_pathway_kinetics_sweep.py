"""kinetics v2 slice 4 acceptance — autocatpath_kinetics job +
kinetics-sweep view (D7/D8/D9).

Every check runs in a real solve/mint: a verify-tier aggregate mints the
job, the job runs the grid on a CPU worker and persists through the real
store, the view renders the optimum and the competitive coverage map.
The sweep fixture mirrors the engine's ``mkm.run_sweep`` payload shape
(``points[].tof_mf`` + ``coverages``, ``optimum{value,objective}``,
``competing{pair,points}``) — the same dict ``kinetics.sweep`` returns.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

# precis_pathway imports autocatpath; CI's default env has no catalyst extra.
pytest.importorskip("autocatpath")


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
    import json
    from pathlib import Path

    return json.loads(
        (Path(__file__).parent / "fixtures" / "kinetics_sweep.json").read_text(
            encoding="utf-8"
        )
    )


def _mint_ctx(tier: str = "verify") -> MagicMock:
    """A MagicMock ctx whose pathway reads ``tier`` and whose store has
    no settings DB — ``pool.connection`` raising makes the flag resolve
    env -> compiled default only."""
    ctx = MagicMock()
    ctx.store.get_ref.return_value = MagicMock(meta={"tier": tier})
    ctx.store.pool.connection.side_effect = Exception("no app_settings")
    return ctx


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


def test_kinetics_job_dispatch_runs_grid_and_persists(store, monkeypatch):
    """The dispatch runs the sweep subprocess and stamps
    kinetics_sweep + kinetics_v2 onto the pathway ref — through the real
    store, not a mock."""
    from precis.store import Store  # noqa: F401 - documents the fixture type
    from precis_pathway import kinetics_job

    with store.tx() as conn:
        ref = store.insert_ref(
            kind="pathway",
            slug="kin-sweep-dispatch",
            title="kin-sweep-dispatch pathway",
            meta={
                "results": {
                    "nodes": {"A": -1.0, "B": -0.5},
                    "edges": [{"a": "A", "b": "B"}],
                }
            },
            conn=conn,
        )
    payload = _sweep_payload()

    def _fake_sweep(config, res, *, timeout=None):
        res["kinetics_sweep"] = payload
        res["kinetics_v2"] = {
            "tof_mf": 9.1e-3,
            "drc": {"NO@N->NOH": 0.82},
            "orders": {"NO": 0.5, "NH3": -0.1},
        }

    import precis_pathway.runner as runner_mod

    monkeypatch.setattr(runner_mod, "run_kinetics_sweep_subprocess", _fake_sweep)
    ctx = MagicMock()
    ctx.meta = {"params": {"pathway_ref_id": ref.id, "config": {}}}
    ctx.store = store
    kinetics_job._dispatch(ctx, kinetics_job.SPEC)

    got = store.get_ref(kind="pathway", id=ref.id)
    sw = got.meta["results"]["kinetics_sweep"]
    opt = payload["optimum"]
    assert sw["optimum"]["T"] == opt["T"]
    assert sw["optimum"]["value"] == opt["value"]
    assert sw["competing"]["pair"] == ["NO@N", "NOH"]
    assert len(sw["competing"]["points"]) == len(payload["competing"]["points"])
    assert got.meta["results"]["kinetics_v2"]["tof_mf"] == 9.1e-3


def test_kinetics_job_records_failure_on_sweep_error(store, monkeypatch):
    """A sweep error is saved onto the pathway AND fails the job — a
    failed sweep must not end as a success."""
    from precis_pathway import kinetics_job

    with store.tx() as conn:
        ref = store.insert_ref(
            kind="pathway",
            slug="kin-sweep-error",
            title="kin-sweep-error pathway",
            meta={"results": {"nodes": {"A": -1.0}, "edges": [{"a": "A", "b": "A"}]}},
            conn=conn,
        )

    def _fail_sweep(config, res, *, timeout=None):
        res["kinetics_sweep_error"] = "kinetics sweep timed out"

    import precis_pathway.runner as runner_mod

    monkeypatch.setattr(runner_mod, "run_kinetics_sweep_subprocess", _fail_sweep)
    ctx = MagicMock()
    ctx.meta = {"params": {"pathway_ref_id": ref.id, "config": {}}}
    ctx.store = store
    kinetics_job._dispatch(ctx, kinetics_job.SPEC)

    got = store.get_ref(kind="pathway", id=ref.id)
    assert got.meta["results"]["kinetics_sweep_error"] == "kinetics sweep timed out"
    ctx.record_failure.assert_called_once()


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
