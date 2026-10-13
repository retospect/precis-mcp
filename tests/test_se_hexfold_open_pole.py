"""The open-pole sphere top (Reto 2026-10-13): ``open_pole`` on a
``top: "sphere"`` feature builds the washer and bulge but no ``cap(6k,0)``
lid, so the bulge's ``out`` rim stays open and the default H-termination
rings the apex with ``6k`` hydrogens — the instrumentable H ring.  The
block's ``topology["sites"]["ring"]`` lists each site (host carbon, H
ordinal, z/theta about the feature axis, the C→H normal) in the scene
frame, ``scene.top.open`` reports the rim, and the plan row is the closed
sibling's (nothing is re-planned; pole-only numbers read n/a, never zero).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from hexfold.check import check
from precis_se.atomic.generators import GENERATORS, GeneratorError
from precis_se.atomic.generators import authored_foot as af

_FEATURE: dict[str, Any] = {
    "name": "q",
    "at": [6, 6],
    "n": 12,
    "radius": 5.0,
    "tube_len": 3,
    "top": "sphere",
}

#: The closed sibling's spec, byte-for-byte — the pre-existing emitted
#: text ``open_pole`` must not move (table row k=4, L=3 on n=12).
_CLOSED_SPEC = """\
hexfold 0.2
origin s
s: sheet(12,12) - hex(2)@(6,6,A):0
qf: cap(36,0) + 3@(-1,0,A):2 - hex(3)@(-1,0,A):0
q: tube(12,0, len=3)
qw: cap(24,0) - hex(1)@(0,0,A):0
qb: tube(24,0, len=3)
qc: cap(24,0)
s.hole --fuse k=0--> qf.in
qf.hole --fuse k=0--> q.in
q.out --fuse k=0--> qw.hole
qw.in --fuse k=0--> qb.in
qb.out --fuse k=0--> qc.in
"""

#: The open variant: the same, minus the lid and its fuse.
_OPEN_SPEC = """\
hexfold 0.2
origin s
s: sheet(12,12) - hex(2)@(6,6,A):0
qf: cap(36,0) + 3@(-1,0,A):2 - hex(3)@(-1,0,A):0
q: tube(12,0, len=3)
qw: cap(24,0) - hex(1)@(0,0,A):0
qb: tube(24,0, len=3)
s.hole --fuse k=0--> qf.in
qf.hole --fuse k=0--> q.in
q.out --fuse k=0--> qw.hole
qw.in --fuse k=0--> qb.in
"""


def _scene(**feature: Any) -> Any:
    return GENERATORS["hexfold_scene"](
        {"sheet": [12, 12], "features": [{**_FEATURE, **feature}]}
    )


# ── refusals and the emitted spec (fast) ───────────────────────────────


@pytest.mark.parametrize(
    ("top", "feature"),
    [("lid", {}), ("open", {}), ("ball", {"n": 6})],
    ids=["lid", "open", "ball"],
)
def test_open_pole_on_a_non_sphere_top_is_refused(
    top: str, feature: dict[str, Any]
) -> None:
    with pytest.raises(
        GeneratorError, match=r"open_pole belongs to top 'sphere'.*" + top
    ):
        _scene(top=top, open_pole=True, **feature)


def test_open_pole_must_be_a_boolean() -> None:
    with pytest.raises(GeneratorError, match="open_pole must be a boolean"):
        _scene(open_pole="yes")


def test_open_pole_needs_its_sphere_n_rules_too() -> None:
    with pytest.raises(GeneratorError, match=r"sphere top needs n a multiple of 6"):
        _scene(n=9, open_pole=True)


def test_the_open_spec_drops_the_lid_and_its_fuse() -> None:
    tp = af.plan_top(12, "sphere")  # the table row: k=4, L=3 — no build
    assert tp.source == "table"
    closed = af.SceneFeature(
        name="q", at=(6, 6), n=12, radius=5.0, tube_len=3, top="sphere"
    )
    opened = af.SceneFeature(
        name="q",
        at=(6, 6),
        n=12,
        radius=5.0,
        tube_len=3,
        top="sphere",
        open_pole=True,
    )
    # the same (n, fillet) plan row drives both — nothing is re-planned
    closed_text = af.scene_text((12, 12), (closed,), {"q": 6}, top_plans={"q": tp})
    open_text = af.scene_text((12, 12), (opened,), {"q": 6}, top_plans={"q": tp})
    assert closed_text == _CLOSED_SPEC  # the closed golden, byte-for-byte
    assert open_text == _OPEN_SPEC
    assert af._top_names("q", "sphere", True) == ("qw", "qb")
    assert af._top_names("q", "sphere") == ("qw", "qb", "qc")


def test_the_stored_top_record_carries_no_open_pole_key_by_default() -> None:
    from precis_se.atomic.generators.hexfold_scene import _top_record

    tp = af.plan_top(12, "sphere")
    assert "open_pole" not in _top_record(tp, tp.chosen)  # unchanged record


# ── the mint (slow: one tethered scene relax, smallest table n) ────────


@pytest.fixture(scope="module")
def open_block() -> Any:
    return GENERATORS["hexfold_scene"](
        {
            "sheet": [12, 12],
            "features": [{**_FEATURE, "open_pole": True}],
            "terminate": "H",
        }
    )


def _scene_coords(block: Any) -> np.ndarray:
    """Stored atoms back in the scene frame (sheet at z=0, +z up the
    feature axis) via the ``surface_target`` receipt's map."""
    mapping = block.topology["surface_target"]["map"]
    return np.asarray(block.coords) @ np.asarray(mapping["Q"]) + np.asarray(
        mapping["b_A"]
    )


@pytest.mark.slow
def test_the_open_pole_mints_with_the_h_ring_and_sites(open_block: Any) -> None:
    topo = open_block.topology
    findings = topo["report"]["findings"]
    assert not [f for f in findings if f["severity"] == "ERROR"], findings
    # the plan row is the closed sibling's, tabled: nothing re-planned
    rec = topo["plan"]["top_plans"]["q"]
    assert rec["planned"] == "table" and rec["kind"] == "sphere"
    assert rec["open_pole"] is True and rec["k"] == 4 and rec["L"] == 3
    assert topo["scene"]["features"][0]["open_pole"] is True
    # scene.top.open reports the rim: 24 carbons, 24 H (6k, k=4)
    opened = [f for f in findings if f["code"] == "scene.top.open"]
    assert len(opened) == 1 and opened[0]["severity"] == "INFO"
    assert "24 rim carbons" in opened[0]["message"]
    assert "24 capped with H" in opened[0]["message"]
    # the pole-drop figure is n/a on an open rim, never a measured zero
    shape = next(f for f in findings if f["code"] == "scene.top.relaxed_shape")
    assert "pole drop is n/a" in shape["message"]
    # the bulge's open rim is H-terminated: 6k = 24 sites on the block,
    # every host a bulge carbon
    sites = topo["sites"]["ring"]
    assert len(sites) == 24 == 6 * rec["k"]
    hosts = {s["host"] for s in sites}
    assert len(hosts) == 24 and hosts <= set(topo["regions"]["qb"])
    # every ring H's z is above every bulge carbon's z (the ring sticks out)
    scene = _scene_coords(open_block)
    bulge = np.asarray(topo["regions"]["qb"])
    ring = np.asarray([s["h"] for s in sites])
    assert scene[ring, 2].min() > scene[bulge, 2].max()
    # each site round-trips (z, theta) to the relaxed H within 0.1 A
    cx, cy = scene[np.asarray(topo["regions"]["q"])][:, :2].mean(axis=0)
    for s in sites:
        p = scene[s["h"]]
        rho = math.hypot(p[0] - cx, p[1] - cy)
        back = np.array(
            [
                cx + rho * math.cos(math.radians(s["theta_deg"])),
                cy + rho * math.sin(math.radians(s["theta_deg"])),
                s["z_A"],
            ]
        )
        assert np.linalg.norm(back - p) <= 0.1, (s, back, p)
        host = scene[s["host"]]
        normal = (p - host) / np.linalg.norm(p - host)
        assert np.allclose(s["normal"], normal, atol=1e-6)
    # the summary line counts them
    assert "24 ring sites" in open_block.provenance


# ── the shipped example ────────────────────────────────────────────────


def test_the_nanobud_open_example_checks_clean_with_the_ring_census() -> None:
    from collections import Counter

    from hexfold.build import build

    text = (
        Path(__file__).resolve().parents[1] / "hexfold" / "examples" / "nanobud_open.hx"
    ).read_text(encoding="utf-8")
    rep = check(text)
    assert rep.ok, rep.render()
    net = build(text, strict=False)
    assert not net.report.errors()
    # six heptagons at the washer seam plus the foot's six, six pentagons at
    # the bulge foot — none at the open top
    assert Counter(len(r) for r in net.rings) == {5: 6, 6: 609, 7: 12}
    # the sheet rim and the bulge's open out rim are H-terminated
    assert sum(1 for a in net.atoms if a.element == "H") == 76 + 24
    done = [f.message for f in rep.findings if f.code == "terminate.done"]
    assert any("b.out" in m and "24 H" in m for m in done), done
    assert any("s.rim" in m for m in done), done
