"""Stored rate-control provenance is explicit and unusable values rank no step."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

import pytest

from precis_pathway._dispatch_common import _kinetics_scalars
from precis_pathway.toon_views import kinetics_sweep_text, kinetics_text
from precis_web.pathway_kinetics import kinetics_payload, stored_kinetics_verdict
from precis_web.pathway_step_kinetics import attach_step_kinetics

CONTROL_CASES = [
    pytest.param({"A->B": 0.0, "B->C": None}, None, False, id="zero-and-null"),
    pytest.param({"A->B": 150.995}, None, False, id="sum-150.995"),
    pytest.param(
        {"A->B": 150.995, "B->C": -149.995}, "mf", False, id="ill-conditioned-unit-sum"
    ),
    pytest.param({"A->B": 2.0, "B->C": -1.0}, "mf", True, id="magnitude-edge"),
    pytest.param(
        {"A->B": math.nextafter(2.0, math.inf), "B->C": -1.0},
        "mf",
        False,
        id="above-magnitude-edge",
    ),
    pytest.param({"A->B": 0.8, "B->C": 0.2}, "mf", True, id="mf-unit-sum"),
    pytest.param({"A->B": 0.8, "B->C": 0.19995}, "chain", True, id="chain-0.99995"),
    pytest.param({"A->B": 1.0}, None, True, id="unit-sum-no-source"),
    pytest.param({"A->B": 0.95}, "mf", True, id="lower-edge"),
    pytest.param({"A->B": 1.05}, "chain", True, id="upper-edge"),
    pytest.param(
        {"A->B": math.nextafter(0.95, -math.inf)}, "mf", False, id="below-edge"
    ),
    pytest.param(
        {"A->B": math.nextafter(1.05, math.inf)}, "mf", False, id="above-edge"
    ),
    pytest.param({"A->B": 1.0, "B->C": None}, "mf", False, id="unit-sum-and-null"),
    pytest.param({}, "mf", False, id="empty"),
    pytest.param({"A->B": 0.0}, "mf", False, id="zero"),
    pytest.param({"A->B": 1.0, "B->C": math.nan}, "mf", False, id="nan"),
    pytest.param({"A->B": 1.0, "B->C": math.inf}, "mf", False, id="infinity"),
    pytest.param({"A->B": True}, "mf", False, id="boolean"),
    pytest.param({"A->B": "1.0"}, "mf", False, id="string"),
    pytest.param({"A->B": 1e308, "B->C": 1e308}, "mf", False, id="overflow"),
]


@pytest.mark.parametrize(("values", "source", "usable"), CONTROL_CASES)
def test_stored_verdict_gates_control_without_mutating_record(values, source, usable):
    record = {
        "tof": 1e-3,
        "drc": {"X_RC": values, "delta": 0.01},
        "drc_source": source,
    }
    before = copy.deepcopy(record)
    verdict = stored_kinetics_verdict(record)
    assert any("Rate control sits on" in line for line in verdict["lines"]) is usable
    assert ("No usable rate control." in verdict["lines"]) is (not usable)
    if usable:
        label = {"mf": "mean-field estimate", "chain": "chain estimate"}.get(
            source, "source was not recorded; the values are unreliable"
        )
        assert any(label in line for line in verdict["lines"])
    assert record == before


@pytest.mark.parametrize(("values", "source", "usable"), CONTROL_CASES)
@pytest.mark.parametrize("version", [1, 2])
def test_control_rule_across_stored_readers(values, source, usable, version):
    """No reader can turn a partial or unnormalised map into a top step."""
    record = {"tof_mf": 1e-3, "drc": values, "drc_source": source}
    meta = _meta(record)
    if version == 1:
        meta["results"] = {
            "kinetics": {"tof": 1e-3, "drc": {"X_RC": values}, "drc_source": source}
        }
    before = copy.deepcopy(meta)
    scalars = _kinetics_scalars(meta["results"])
    body = kinetics_text(meta)
    payload = kinetics_payload(meta["results"])
    assert payload is not None
    panel = payload["ml"]
    links: list[dict[str, Any]] = [{"source": "A", "target": "B"}]
    attach_step_kinetics(links, meta["results"])
    step = links[0]["step_kinetics"]
    assert ("X_RC" in step["quantities"]) is usable
    assert panel["drc_usable"] is usable
    assert ("drc_top" in scalars) is usable
    assert ("X_RC" in body) is usable
    assert any("Rate control sits on" in s for s in panel["verdict"]["lines"]) is usable
    if usable:
        label = {"mf": "mean-field estimate", "chain": "chain estimate"}.get(
            source, "source was not recorded"
        )
        assert label in scalars["drc_note"] and label in body
        assert label in panel["drc_note"]
        assert label in step["drc_note"]
        assert label in step["quantities"]["X_RC"]["solve"]
        assert scalars["drc_top"] == "A->B"
    else:
        assert scalars["drc_note"] == "No usable rate control."
        assert "No usable rate control." in body
        # The electronic-barrier summary is independent of rate control.
        assert "Rate-limiting step: A→B" in body
        assert panel["drc_note"] == "No usable rate control."
        assert step["drc_note"] == "No usable rate control."
    assert meta == before


@pytest.mark.parametrize(("values", "source", "usable"), CONTROL_CASES)
def test_sweep_requires_the_complete_control_map(values, source, usable):
    meta = _meta({})
    meta["results"]["kinetics_sweep"] = {
        "points": [{"T": 300.0}],
        "optimum": {
            "value": 1.0,
            "drc_top": "A->B",
            "drc": values,
            "drc_source": source,
        },
    }
    before = copy.deepcopy(meta)
    body = kinetics_sweep_text(meta)
    assert ("drc_top A->B" in body) is usable
    if not usable:
        assert "No usable rate control." in body
    assert meta == before


def test_sweep_top_without_values_keeps_engine_optimum_shape():
    meta = _meta({})
    sweep = json.loads(
        (Path(__file__).parent / "fixtures" / "kinetics_sweep.json").read_text(
            encoding="utf-8"
        )
    )
    assert "drc" not in sweep["optimum"]
    meta["results"]["kinetics_sweep"] = sweep
    before = copy.deepcopy(meta)
    body = kinetics_sweep_text(meta)
    assert "; drc_top NH3->*+NH3(g) (sum check not stored)" in body
    assert "source was not recorded; the values are unreliable" in body
    assert "No usable rate control." not in body
    assert meta == before


THERMODYNAMIC_CASES = [
    pytest.param({"NO@fcc": -0.7}, None, True, id="negative-no-source"),
    pytest.param({"NO@fcc": -0.7}, "mf", True, id="negative-mf"),
    pytest.param({"NO@fcc": -0.7}, "chain", True, id="negative-chain"),
    pytest.param({"A": 0.0}, "mf", True, id="zero"),
    pytest.param({"A": 150.995}, "mf", True, id="no-magnitude-guard"),
    pytest.param({"A": 1e308, "B": 1e308}, "mf", True, id="no-sum"),
    pytest.param({"A": -0.7, "B": None}, "mf", False, id="null"),
    pytest.param({}, "mf", False, id="empty"),
    pytest.param({"A": math.nan}, "mf", False, id="nan"),
    pytest.param({"A": math.inf}, "mf", False, id="infinity"),
    pytest.param({"A": True}, "mf", False, id="boolean"),
    pytest.param({"A": "1.0"}, "mf", False, id="string"),
]


@pytest.mark.parametrize(("values", "source", "usable"), THERMODYNAMIC_CASES)
@pytest.mark.parametrize("version", [1, 2])
def test_thermodynamic_control_requires_only_complete_finite_values(
    values, source, usable, version
):
    results = (
        {"kinetics_v2": {"tof_mf": 1e-3, "trc": values, "trc_source": source}}
        if version == 2
        else {
            "kinetics": {
                "tof": 1e-3,
                "thermodynamic_drc": {"X_TRC": values},
                "trc_source": source,
            }
        }
    )
    before = copy.deepcopy(results)
    payload = kinetics_payload(results)
    assert payload is not None
    panel = payload["ml"]
    assert panel["trc_usable"] is usable
    assert panel["trc_source_recorded"] == source
    if usable:
        label = {"mf": "mean-field estimate", "chain": "chain estimate"}.get(
            source, "source was not recorded; the values are unreliable"
        )
        assert label in panel["trc_note"]
    else:
        assert panel["trc_note"] == "No usable rate control (X_TRC)."
        assert not any("thermodynamic brake:" in s for s in panel["verdict"]["lines"])
    assert results == before


@pytest.mark.parametrize("extra", [{}, {"missing": None}, {"balancing": 1.7}])
def test_thermodynamic_brake_requires_usable_values(extra):
    record = {
        "tof": 1e-3,
        "thermodynamic_drc": {"X_TRC": {"NO@fcc": -0.7, **extra}},
        "trc_source": "chain",
    }
    before = copy.deepcopy(record)
    verdict = stored_kinetics_verdict(record)
    assert any("thermodynamic brake: NO@fcc" in s for s in verdict["lines"]) is (
        "missing" not in extra
    )
    assert record == before


@pytest.mark.parametrize("values", [{}, {"A->B": None}, {"A->B": 1, "B->C": None}])
def test_unusable_v2_map_never_falls_back_to_v1(values):
    meta = _meta({"tof_mf": 1e-3, "drc": values, "trc": values})
    meta["results"]["kinetics"] = {
        "tof": 1e-3,
        "drc": {"X_RC": {"old step": 1.0}},
        "drc_source": "chain",
        "thermodynamic_drc": {"X_TRC": {"old poison": -0.7, "balance": 1.7}},
        "trc_source": "chain",
    }
    assert "drc_top" not in _kinetics_scalars(meta["results"])
    assert "old step" not in kinetics_text(meta)
    payload = kinetics_payload(meta["results"])
    assert payload is not None
    assert payload["ml"]["drc_usable"] is False
    assert payload["ml"]["trc_usable"] is False
    assert payload["ml"]["drc_source_recorded"] is None
    assert payload["ml"]["trc_source_recorded"] is None
    links: list[dict[str, Any]] = [{"source": "A", "target": "B"}]
    attach_step_kinetics(links, meta["results"])
    assert "X_RC" not in links[0]["step_kinetics"]["quantities"]


def _meta(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "graph": {
            "nodes": [{"id": "A"}, {"id": "B"}],
            "links": [{"source": "A", "target": "B", "barrier": 0.5}],
        },
        "results": {"pathway": ["A", "B"], "kinetics_v2": record},
    }


@pytest.mark.parametrize(
    ("source", "label"),
    [
        (None, "source was not recorded; the values are unreliable"),
        ("mf", "mean-field estimate"),
        ("chain", "chain estimate"),
    ],
)
def test_rate_control_labels_in_scalar_and_kinetics_view(source, label):
    record: dict[str, Any] = {"tof_mf": 1e-3, "drc": {"A->B": 1.0}}
    if source is not None:
        record["drc_source"] = source
    meta = _meta(record)
    before = copy.deepcopy(meta)
    scalars = _kinetics_scalars(meta["results"])
    assert scalars["drc_top"] == "A->B"
    assert scalars["drc_source"] == source
    assert label in scalars["drc_note"]
    body = kinetics_text(meta)
    assert "X_RC" in body
    assert label in body
    assert meta == before


def test_zero_rate_control_does_not_rank_or_fall_back_to_an_old_step():
    meta = _meta({"tof_mf": 1e-3, "drc": {"A->B": 0.0}, "drc_source": "mf"})
    meta["results"]["kinetics"] = {
        "tof": 1e-3,
        "drc": {"X_RC": {"old step": 0.9}},
        "drc_source": "chain",
    }
    scalars = _kinetics_scalars(meta["results"])
    assert "drc_top" not in scalars
    assert "No usable rate control" in scalars["drc_note"]
    assert "No usable rate control" in kinetics_text(meta)
    assert "old step" not in kinetics_text(meta)


def test_v2_rate_control_does_not_inherit_v1_source():
    meta = _meta({"tof_mf": 1e-3, "drc": {"A->B": 1.0}})
    meta["results"]["kinetics"] = {
        "tof": 1e-3,
        "drc": {"X_RC": {"A->B": 1.0}},
        "drc_source": "chain",
    }
    assert "source was not recorded" in kinetics_text(meta)


@pytest.mark.parametrize("source", [None, "chain"])
def test_v1_rate_control_fallback_does_not_inherit_v2_source(source):
    meta = _meta({"tof_mf": 1e-3, "drc": None, "drc_source": "mf"})
    meta["results"]["kinetics"] = {
        "tof": 1e-3,
        "drc": {"X_RC": {"A->B": 1.0}},
        "drc_source": source,
    }
    before = copy.deepcopy(meta)
    body = kinetics_text(meta)
    assert "mean-field estimate" not in body
    assert ("chain estimate" if source else "source was not recorded") in body
    assert meta == before


@pytest.mark.parametrize(
    ("source", "label"),
    [
        (None, "source was not recorded; the values are unreliable"),
        ("mf", "mean-field estimate"),
        ("chain", "chain estimate"),
    ],
)
def test_sweep_top_step_labels_its_own_source(source, label):
    optimum: dict[str, Any] = {
        "value": 1.0,
        "T": 300.0,
        "p_total": 1.0,
        "drc_top": "A->B",
    }
    if source is not None:
        optimum["drc_source"] = source
    meta = _meta({"drc_source": "mf"})
    meta["results"]["kinetics_sweep"] = {"points": [{"T": 300.0}], "optimum": optimum}
    before = copy.deepcopy(meta)
    body = kinetics_sweep_text(meta)
    assert "drc_top A->B (sum check not stored)" in body
    assert label in body
    assert meta == before


def test_sweep_zero_rate_control_does_not_name_a_top_step():
    meta = {
        "results": {
            "kinetics_sweep": {
                "points": [{"T": 300.0}],
                "optimum": {"value": 1.0, "drc_top": "A->B", "drc": {"A->B": 0}},
            }
        }
    }
    body = kinetics_sweep_text(meta)
    assert "drc_top A->B" not in body
    assert "No usable rate control" in body


@pytest.mark.parametrize(
    ("record_version", "expected"),
    [
        ("0.50.0", "kinetics engine 0.50.0 (current"),
        (None, "kinetics engine 0.49.0 (stale"),
    ],
)
def test_kinetics_view_staleness_uses_the_solve_stamp(
    monkeypatch, record_version, expected
):
    from precis.quest import compute

    monkeypatch.setattr(compute, "_autocatpath_current_version", lambda: "0.50.0")
    record: dict[str, Any] = {"tof_mf": 1e-3}
    if record_version is not None:
        record["engine_version"] = record_version
    meta = _meta(record)
    meta["autocatpath_version"] = "0.49.0"
    assert expected in kinetics_text(meta)
