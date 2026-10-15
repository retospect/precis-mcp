"""Channel-junction scene: wall arcs on paired k3-sp3-z corner seams with
per-rail strips in or out — admission, tethered build, real test-DB mint."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from hexfold.channel import channel_junction
from precis.dispatch import Hub
from precis.store import Store
from precis_se import persist
from precis_se.atomic.generators import GeneratorError
from precis_se.atomic.generators.hexfold_scene import build_hexfold_scene
from precis_se.handler import SeHandler


def params(
    rows: int = 4,
    periods: int = 6,
    sides: list[str] | None = None,
    strip_rows: int = 3,
) -> dict[str, Any]:
    feature: dict[str, Any] = {
        "name": "s",
        "type": "chan-k3-sp3-z",
        "rows": strip_rows,
    }
    if sides is not None:
        feature["sides"] = sides
    return {
        "tube": [rows, periods],
        "features": [feature],
        "k_tether": 1.0,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"tube": [3, 6]},
        {"tube": [5, 6]},
        {"tube": [4, 5]},
        {"tube": [4, 61]},
        {"tube": [4.0, 6]},
        {"sheet": [6, 4]},
        {"k_tether": 0},
        {"features": [{"name": "s", "type": "k3-sp3-z"}]},
        {"features": [{"name": "s", "type": "chan-k3-sp3-z", "rows": 1}]},
        {"features": [{"name": "s", "type": "chan-k3-sp3-z", "sides": ["up", "out"]}]},
        {"features": [{"name": "s", "type": "chan-k3-sp3-z", "sides": []}]},
        {"features": [{"name": "s", "type": "chan-k3-sp3-z", "sides": ["out"] * 7}]},
        {"features": [{"name": "s", "type": "chan-k3-sp3-z", "pores": []}]},
        {"features": [{"name": "s", "type": "chan-k3-sp3-z"}, {"name": "x"}]},
    ],
)
def test_channel_invalid_input_refuses_before_build(
    change: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("invalid channel input crossed into channel_junction")

    monkeypatch.setattr(
        "precis_se.atomic.generators.channel.channel_junction", forbidden
    )
    with pytest.raises(GeneratorError):
        build_hexfold_scene({**params(), **change})


def test_channel_inward_misfit_refuses() -> None:
    with pytest.raises(GeneratorError, match="does not fit"):
        build_hexfold_scene(params(4, 6, sides=["in", "in"]))


def test_public_channel_tethered_test_db_roundtrip(
    store: Store, hub_no_embedder: Hub
) -> None:
    slug = "hexfold-channel-test"
    p = params(sides=["out", "out", "out"])
    op = {
        "op": "generate",
        "generator": "hexfold_scene",
        "name": "s",
        "params": p,
    }
    handler = SeHandler(hub=hub_no_embedder)
    handler.put(id=slug, args={"ops": [op]})
    se_ref = store.get_ref(kind="se", id=slug)
    assert se_ref is not None
    node = persist.load_tree(store, se_ref.id).blocks["s"]
    assert node.bound is not None
    ref = store.get_ref(kind="structure", id=node.bound)
    assert ref is not None
    record = (ref.meta or {})["generated"]
    assert record["scene"] == p
    assert record["plan"]["relax"] == "tethered"
    findings = record["report"]["findings"]
    assert not [f for f in findings if f["code"] in ("geom.clash", "geom.seed_overlap")]
    seam = next(f["data"] for f in findings if f["code"] == "seam.geometry")
    assert seam["degree"] == 4
    assert seam["rails"] == 3
    assert seam["angle_deviation_max_deg"] < 10.0
    seam_rings = next(f["data"] for f in findings if f["code"] == "seam.rings")
    assert len(seam_rings["rings"]) == 45
    cross = next(f["data"] for f in findings if f["code"] == "channel.cross_section")
    assert len(cross["arcs"]) == 3 and cross["arc_tether"] == "none"
    assert cross["sides"] == ["out", "out", "out"]
    for arc in cross["arcs"]:
        assert arc["rho_min_A"] <= arc["rho_mean_A"] <= arc["rho_max_A"]
        assert arc["seam_rho_A"] > arc["rho_mean_A"]  # outward seams
    # no minted non-bonded pair involving H sits under the drop bar
    direct = build_hexfold_scene(p)
    pos = np.asarray(direct.coords)
    bonded = {frozenset((i, j)) for i, j, *_ in direct.bonds}
    assert not [
        (i, j)
        for i in range(len(direct.elements))
        for j in range(i + 1, len(direct.elements))
        if "H" in (direct.elements[i], direct.elements[j])
        and frozenset((i, j)) not in bonded
        and np.linalg.norm(pos[i] - pos[j]) < 1.0
    ]
    # the carbon net is the authored one; the sp3 seam atoms took no H
    net, channel = channel_junction(6, 4, 3, ("out", "out", "out"))
    n_c = sum(1 for e in direct.elements if e == "C")
    n_h = sum(1 for e in direct.elements if e == "H")
    assert n_c == len(net.atoms)
    assert record["terminated"]["count"] == n_h
    block = handler.get(id=slug, view="block", args={"name": "s"}).body
    assert "channel.cross_section" in block and "tethered" in block
    unknown = handler.get(id=slug, view="surface_deviation", args={"name": "s"}).body
    assert "unknown: no stored or spec-derivable authored target" in unknown
