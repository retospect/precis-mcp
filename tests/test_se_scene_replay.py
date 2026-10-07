"""Typed scene replay preserves the author's explicit edge termination."""

from __future__ import annotations

import copy
from typing import Any

import numpy as np
import pytest

from precis_se.atomic.generators import GeneratorError
from precis_se.atomic.generators.hexfold_scene import build_hexfold_scene

SCENES = [
    (
        {
            "sheet": [4, 3],
            "features": [
                {"name": "y", "type": "k3-sp2-120-z", "dihedrals_deg": [120] * 3}
            ],
        },
        "y_junction.straight_y",
    ),
    (
        {
            "tube": [6, 4],
            "features": [{"name": "f", "type": "fin-sp3-z", "side": "out", "rows": 3}],
        },
        "fin.fin_tube",
    ),
    (
        {
            "tube": [6, 4],
            "features": [
                {"name": "f", "type": "fin-k3-120-z", "side": "out", "rows": 3}
            ],
        },
        "fin120.seam_tube",
    ),
]


@pytest.mark.parametrize(("scene", "_builder"), SCENES)
@pytest.mark.parametrize("termination", ["none", "ports-open", "H"])
def test_explicit_termination_survives_scene_replay(
    scene: dict[str, Any], _builder: str, termination: str
) -> None:
    params = {**copy.deepcopy(scene), "terminate": termination}
    original = copy.deepcopy(params)
    first = build_hexfold_scene(params)
    replay = build_hexfold_scene(first.topology["scene"])
    assert params == original
    assert first.topology["scene"]["terminate"] == termination
    assert replay.topology["terminated"] == first.topology["terminated"]
    assert replay.topology["terminated"]["mode"] == termination
    assert replay.elements == first.elements
    assert replay.bonds == first.bonds
    np.testing.assert_array_equal(replay.coords, first.coords)
    if termination == "none":
        assert "H" not in replay.elements
    else:
        assert "H" in replay.elements


@pytest.mark.parametrize(("scene", "builder"), SCENES)
def test_invalid_termination_refuses_before_net_build(
    scene: dict[str, Any], builder: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("invalid termination crossed into net construction")

    monkeypatch.setattr(f"precis_se.atomic.generators.{builder}", forbidden)
    with pytest.raises(GeneratorError, match="terminate must be"):
        build_hexfold_scene({**copy.deepcopy(scene), "terminate": "typo"})
