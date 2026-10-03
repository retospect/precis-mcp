"""The ``hexfold_scene`` generator —
:mod:`precis_se.atomic.generators.hexfold_scene`: params validation, the
planner's refusals as :class:`GeneratorError`, and one real tethered mint.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis_se.atomic.generate import generated_record
from precis_se.atomic.generators import GENERATORS, GeneratorError

_T: dict[str, Any] = {
    "name": "t",
    "at": [13, 15],
    "n": 6,
    "radius": 3.0,
    "tube_len": 5,
    "top": "ball",
}
_Q: dict[str, Any] = {
    "name": "q",
    "at": [20, 6],
    "n": 12,
    "radius": 5.0,
    "tube_len": 1,
    "top": "lid",
}


def _scene(**params: Any) -> Any:
    return GENERATORS["hexfold_scene"]({"sheet": [30, 24], "features": [_T], **params})


def test_the_generator_is_registered() -> None:
    assert callable(GENERATORS["hexfold_scene"])


def test_the_stored_record_keeps_scene_and_plan_and_drops_the_rest() -> None:
    """``finish_generate`` persists ``generated_record(...)`` on the
    structure ref: the regeneration input (``scene``) and the planner
    columns (``plan``) must survive the trim, per-atom/bulk keys must not."""
    topology = {
        "spec": "hexfold 0.2\n",
        "scene": {"sheet": [30, 24], "features": []},
        "plan": {"ks": {"t": 4}, "passes": 2},
        "report": {"findings": []},
        "canonical_json": "{}",
    }
    rec = generated_record("hexfold_scene", topology)
    assert rec["generator"] == "hexfold_scene"
    assert rec["scene"] == topology["scene"]
    assert rec["plan"] == topology["plan"]
    assert rec["spec"] == topology["spec"] and rec["report"] == topology["report"]
    assert "canonical_json" not in rec


def test_a_planner_crash_is_named_as_a_hexfold_internal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A KeyError out of the compiler/planner is a bug, not a spec error;
    it must not reach the dispatcher as ``internal error (see server log)``."""
    from precis_se.atomic.generators import hexfold_scene as mod

    def boom(*_a: object, **_k: object) -> object:
        raise KeyError("x")

    monkeypatch.setattr(mod, "plan_scene", boom)
    with pytest.raises(GeneratorError, match="hexfold internal error.*KeyError"):
        _scene()


def test_an_unknown_top_level_key_is_refused_by_name() -> None:
    with pytest.raises(GeneratorError, match="unknown param.*'fidelity'"):
        _scene(fidelity="stick")


def test_an_unknown_feature_key_is_refused_by_name() -> None:
    with pytest.raises(GeneratorError, match=r"features\[0\].*unknown.*'tube_length'"):
        _scene(features=[{**_T, "tube_length": 4}])


@pytest.mark.parametrize(
    ("params", "match"),
    [
        ({"sheet": [30]}, "sheet must be a pair"),
        ({"sheet": [30.5, 24]}, "sheet must be a pair"),
        ({"features": []}, "non-empty list"),
        ({"features": "t"}, "non-empty list"),
        ({"features": [3]}, r"features\[0\] must be an object"),
        ({"features": [{**_T, "name": ""}]}, "name must be a non-empty string"),
        ({"features": [{**_T, "at": [1]}]}, r"at must be a pair"),
        ({"features": [{**_T, "n": 6.0}]}, r"n must be an integer"),
        ({"features": [{**_T, "n": True}]}, r"n must be an integer"),
        ({"features": [{**_T, "radius": "3"}]}, r"radius must be a number"),
        ({"features": [{**_T, "top": 3}]}, r"top must be a string"),
        ({"extra": 3}, "extra must be a string"),
        ({"k_tether": "1"}, "k_tether must be a number"),
    ],
)
def test_bad_types_are_refused(params: dict[str, object], match: str) -> None:
    with pytest.raises(GeneratorError, match=match):
        _scene(**params)


def test_a_missing_key_is_named() -> None:
    gone = {k: v for k, v in _T.items() if k != "radius"}
    with pytest.raises(GeneratorError, match=r"missing key.*'radius'"):
        _scene(features=[gone])
    with pytest.raises(GeneratorError, match="needs 'sheet'"):
        GENERATORS["hexfold_scene"]({"features": [_T]})


def test_a_seam_phase_cell_is_a_generator_error() -> None:
    bad = {"name": "t", "at": [9, 12], "n": 6, "radius": 3.0, "tube_len": 3}
    with pytest.raises(GeneratorError, match=r"seam at cell \(9, 12\)"):
        _scene(features=[{**bad, "top": "open"}])


def test_repeated_feature_names_are_a_generator_error() -> None:
    with pytest.raises(GeneratorError, match="distinct"):
        _scene(features=[_T, _T])


@pytest.mark.slow
def test_a_pillar_and_a_bump_mint_tethered_with_their_caveats() -> None:
    block = GENERATORS["hexfold_scene"]({"sheet": [30, 24], "features": [_T, _Q]})
    topo = block.topology
    findings = topo["report"]["findings"]
    assert not [f for f in findings if f["severity"] == "ERROR"], findings
    joints = [f for f in findings if f["code"] == "scene.top.joint"]
    assert len(joints) == 1 and joints[0]["message"].startswith("t:")
    assert all(f["severity"] == "WARN" for f in joints)
    assert "relax=tethered" in block.provenance
    assert joints[0]["message"] in block.provenance
    assert "scene" in topo and "plan" in topo
    assert topo["plan"]["ks"] == {"t": 4, "q": 6}
    assert topo["scene"]["sheet"] == [30, 24]
    assert [f["name"] for f in topo["scene"]["features"]] == ["t", "q"]
    assert set(topo["plan"]["tops"]) == {"t", "q"}
    summary = next(f for f in findings if f["code"] == "geom.summary")
    assert summary["data"]["relax"] == "tethered"
    assert len(block.elements) == topo["n_atoms"]
