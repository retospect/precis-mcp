"""kinetics v2 slice 4 acceptance — the autocatpath_kinetics job's
dispatch through the real store (D7/D8/D9).

These two import ``precis_pathway.runner`` (which imports the engine), so
the module skips without the ``catalyst`` extra; the engine-free glue —
the mint, the kinetics-sweep view, the harvest lift — lives in
``test_pathway_kinetics_glue.py``, which CI runs.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

# precis_pathway imports autocatpath; CI's default env has no catalyst extra.
pytest.importorskip("autocatpath")


def _sweep_payload() -> dict[str, Any]:
    """The REAL ``kinetics.sweep`` payload of the single-site fixture (see
    ``test_pathway_kinetics_glue._sweep_payload`` for the recipe)."""
    import json
    from pathlib import Path

    return json.loads(
        (Path(__file__).parent / "fixtures" / "kinetics_sweep.json").read_text(
            encoding="utf-8"
        )
    )


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
    monkeypatch.setattr(runner_mod, "has_tpd", lambda: False)  # the sweep path only
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
    monkeypatch.setattr(runner_mod, "has_tpd", lambda: False)  # the sweep path only
    ctx = MagicMock()
    ctx.meta = {"params": {"pathway_ref_id": ref.id, "config": {}}}
    ctx.store = store
    kinetics_job._dispatch(ctx, kinetics_job.SPEC)

    got = store.get_ref(kind="pathway", id=ref.id)
    assert got.meta["results"]["kinetics_sweep_error"] == "kinetics sweep timed out"
    ctx.record_failure.assert_called_once()
