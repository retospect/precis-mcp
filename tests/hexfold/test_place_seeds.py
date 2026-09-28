"""``_place_seeds`` (build.py): the rigid placement of per-instance seeds
along the connect graph puts every fused rim face to face.

Regression for the keying bug found 2026-09-27: the per-instance
transform table was filed under the *destination* instance and read back
as the source's, so each neighbour received the transform computed for the
other side and landed mirrored behind the far rim -- 8-78 A crossing bonds
in every multi-instance example, which ``stick``'s spring stage then
"repaired" by telescoping the halves into each other.  The rim-frame
normal was also signed against the whole-net centroid instead of the
owning instance's (a coin toss for a hole rim near a sheet's centre).

Root-caused 2026-09-28 (docs/backlog/hexfold-integration.md "Root-caused
2026-09-28"): three further placement gaps, each masked by ``stick()``
until a pre-relax ``net.seed3`` assertion is added here.  Item 1 (nanobud
``@`` menus): the menu verb never fed ``fuse_frames``, so ``_place_seeds``
skipped the whole spec.  Item 2 (k>=3 seams): the seam pass minted seam
atoms/bonds but fed no placement edge, so an origin that only touches a
seam left the whole spec unplaced.  Item 3 (a flat instance with two
fused rims): ``_frame``'s sign rule is z-noise for both rims of a
zero-thickness ``cap(6k,0)`` patch, so a hole rim and the outer rim ended
up signed the same way instead of oppositely.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from hexfold.build import Net, _flat_normals, build
from hexfold.stick import stick

_EXAMPLES = Path(__file__).resolve().parents[2] / "hexfold" / "examples"


def _crossing_lengths(net: Net) -> np.ndarray:
    assert net.seed3 is not None
    pos = np.array(net.seed3, dtype=np.float64)
    inst = [a.instance for a in net.atoms]
    cross = [(i, j) for i, j, _ in net.bonds if inst[i] != inst[j]]
    assert cross, "no inter-instance bonds -- not a fused net"
    return np.array([np.linalg.norm(pos[i] - pos[j]) for i, j in cross])


def _bond_lengths(net: Net, pos: np.ndarray) -> np.ndarray:
    return np.array([np.linalg.norm(pos[i] - pos[j]) for i, j, _ in net.bonds])


@pytest.mark.parametrize(
    "body, skew_deg",
    [
        # tip-to-tip zigzag tubes (hexfold/examples/tube_fuse.hx)
        ("a: tube(5,0, len=3)\nb: tube(5,0, len=3)\na.out --fuse k=0--> b.in", 0.0),
        # a three-instance chain: exercises the transform composition
        (
            "a: tube(12,0, len=3)\nb: tube(12,0, len=3)\nc: tube(12,0, len=3)\n"
            "a.out --fuse k=0--> b.in\nb.out --fuse k=0--> c.in",
            0.0,
        ),
        # phase k != 0
        ("a: tube(6,0, len=3)\nb: tube(6,0, len=3)\na.out --fuse k=2--> b.in", 0.0),
        # armchair: the dangling bonds sit 30 degrees off the axis (SPEC 10,
        # alpha = 60 degrees to the rim line), and _fuse_transform spaces the
        # rim centroids one sigma apart along the normal regardless of rim
        # type, so the seed's crossing bonds are sigma / cos 30 -- a 15%
        # stretch stick's spring stage closes, not a mis-placement.
        ("a: tube(5,5, len=3)\nb: tube(5,5, len=3)\na.out --fuse k=0--> b.in", 30.0),
    ],
)
def test_fused_tube_seeds_meet_at_one_bond_length(body: str, skew_deg: float) -> None:
    net = build("hexfold 0.2\n" + body + "\n")
    expected = net.lattice.sigma_A / np.cos(np.radians(skew_deg))
    lengths = _crossing_lengths(net)
    assert np.all(np.abs(lengths - expected) < 0.06), (expected, lengths)


@pytest.mark.parametrize(
    "name, bound_A",
    [
        # sheet with a tube budded into a hex hole -- the hole-rim normal
        # sign case (whole-net centroid vs instance centroid)
        ("sheet_bud_22.hx", 1.6),
        # tube + lid + tube: three instances, two fuses
        ("pillar.hx", 1.6),
        # flat lid on a tube (dome-seeded cap onto a cylinder rim)
        ("lid_pillbox.hx", 2.0),
        # item 1, nanobud `@` menu attach: measured pre-fix max 8.57 A (no
        # fuse_frames entry at all); routed through _fuse_transform_kabsch
        # (a menu registration's six host atoms mix a hexagon's ring
        # vertices with its second-neighbour shell, so no single rim
        # normal + twist angle fits them) measured max 3.52 A, bound with
        # ~15% headroom
        ("nanobud_87.hx", 4.0),
        # same menu mechanism, the 9-6 junction: measured pre-fix max
        # 15.21 A, post-fix max 3.27 A
        ("nanobud_96.hx", 3.6),
        # item 2, k>=3 seam ("foot: s.hole == up.in == down.in"): the
        # origin ('s') only touches the seam, so the whole spec was
        # unplaced pre-fix (measured max 26.48 A); the seam's own
        # consecutive-rim correspondence fed to _place_seeds as a
        # placement-only entry measured max 3.74 A
        ("sheet_pill_bump.hx", 4.0),
        # item 2, a 3-rim seam where two of the three rims (top, bottom)
        # are already real-fuse-connected through a third instance
        # (wall): measured pre-fix max 11.43 A ('flange' unplaced); after
        # the fix, top/wall/bottom placement is exact (1.44-1.53 A, the
        # two-phase BFS below), but 'flange' -- reachable only through
        # the seam -- inherits a residual rotational mismatch between
        # top's and bottom's independently-chained orientations that no
        # per-edge rigid placement resolves (see check_registry: the
        # part-graph cycle top-wall-bottom-flange-top closes with residual
        # 0 discretely, but that is a k=0-everywhere triviality, not a
        # continuous-angle guarantee) -- measured max 10.91 A, essentially
        # unchanged; bound with a small margin over the measurement, not
        # an assertion that this residual is fixed
        ("flanged_doughnut.hx", 11.3),
        # item 3, a flat washer (cap(24,0) - hex(1) hole) fused on both
        # its hole rim (to a neck tube) and its outer rim (to a bulge):
        # measured pre-fix max 17.86 A (both rims signed the same way);
        # forcing the outer rim to the hole rim's exact negation (the
        # hole rim's own sign is reliable on its own, see _flat_sign)
        # measured max 2.12 A, matching the backlog dossier's own "drops
        # to 2.1 A" empirical note
        ("valve_shell.hx", 2.3),
    ],
)
def test_example_seeds_have_no_long_crossing_bonds(name: str, bound_A: float) -> None:
    text = (_EXAMPLES / name).read_text(encoding="utf-8")
    net = build(text, strict=False)
    lengths = _crossing_lengths(net)
    assert lengths.max() < bound_A, (name, lengths.min(), lengths.max())


def test_nanobud_menu_seed_has_no_stick_clash() -> None:
    """Item 1's six-point Kabsch fit places the bud's six attach atoms
    close but the rest of the C60 ball still needs clearance from the
    host tube's convex surface -- verified via ``stick()``: at zero
    outward offset the closest non-bonded bud/host pair relaxed to 0.69 A
    (a real clash); one sigma of clearance in the seed (see
    :func:`hexfold.build._fuse_transform_kabsch`) relaxes to 1.22 A.
    """
    text = (_EXAMPLES / "nanobud_87.hx").read_text(encoding="utf-8")
    net = build(text, strict=False)
    pos = stick(net)
    inst = [a.instance for a in net.atoms]
    bonded = {(i, j) for i, j, _ in net.bonds} | {(j, i) for i, j, _ in net.bonds}
    b_idx = [i for i, x in enumerate(inst) if x == "b"]
    h_idx = [i for i, x in enumerate(inst) if x == "h"]
    min_d = min(
        float(np.linalg.norm(pos[i] - pos[j]))
        for i in b_idx
        for j in h_idx
        if (i, j) not in bonded
    )
    assert min_d > 1.0, min_d


def test_flat_normals_threshold_is_absolute_not_relative() -> None:
    """``_flat_normals``' criterion is ``extent < 0.5 * sigma`` on the
    smallest-variance axis alone -- not a fraction of the largest axis:
    a ``len=1`` tube's axial extent is a fixed ``2 * sigma`` regardless
    of circumference, but its *relative* smallest/largest ratio shrinks
    with diameter (measured 0.091 on tube(40,0,len=1), 0.060 on
    tube(60,0,len=1)) and a 10%-of-largest threshold misclassified both
    as flat.  ``cap(12,0)``'s flat-perturbed extent (bounded by
    ``_patch_seed3``'s ``0.05 * sigma`` amplitude at ``~0.1 * sigma``,
    regardless of instance size) sits 5x under the 0.5 sigma line; a
    len=1 tube's 2 sigma sits 4x over it.
    """
    for body, want_flat in (
        ("a: tube(60,0,len=1)\n", False),
        ("a: cap(12,0)\n", True),
    ):
        net = build("hexfold 0.2\n" + body)
        assert net.seed3 is not None
        pos = np.array(net.seed3, dtype=np.float64)
        inst_ords: dict[str, list[int]] = {}
        for a in net.atoms:
            inst_ords.setdefault(a.instance, []).append(a.ord)
        flat = _flat_normals(pos, inst_ords, net.lattice.sigma_A)
        assert ("a" in flat) == want_flat, (body, flat)


def test_flanged_doughnut_top_wall_bottom_placement_is_exact() -> None:
    """Item 2's two-phase BFS (real edges before a seam's placement-only
    ones) keeps the real fuse chain top<->wall<->bottom exact even though
    a seam edge (root-caused 2026-09-28 item 2) also names top<->bottom
    directly: a single-pass BFS could pick whichever edge sorts first at
    the same depth, and briefly did during development (10.47-10.63 A on
    bottom<->wall, a real regression caught by this assertion).  A tight
    bound here is a regression guard specifically for the real-edge
    priority, independent of the file's own residual (the seam's third
    rim, 'flange', is reachable only through the seam and inherits a
    genuine rotational mismatch between top's and bottom's independently
    -chained orientations -- check_registry's cycle closes with residual
    0, but that is a k=0-everywhere triviality, not a continuous-angle
    guarantee -- which is why the file-level bound above stays at 11.3 A
    rather than tightening with this one).
    """
    text = (_EXAMPLES / "flanged_doughnut.hx").read_text(encoding="utf-8")
    net = build(text, strict=False)
    assert net.seed3 is not None
    pos = np.array(net.seed3, dtype=np.float64)
    inst = [a.instance for a in net.atoms]
    keep = {"top", "wall", "bottom"}
    lengths = np.array(
        [
            np.linalg.norm(pos[i] - pos[j])
            for i, j, _ in net.bonds
            if inst[i] != inst[j] and inst[i] in keep and inst[j] in keep
        ]
    )
    assert lengths.size, "no top/wall/bottom crossing bonds found"
    assert lengths.max() < 1.7, (lengths.min(), lengths.max())


def test_stick_no_longer_telescopes_the_fused_tube() -> None:
    """With the seed straight, stick's spring stage has nothing to fold:
    every bond stays near sigma and the composite spans two tube lengths
    along its axis (the telescoped seed came out half as long)."""
    body = "a: tube(12,0, len=4)\nb: tube(12,0, len=4)\na.out --fuse k=0--> b.in"
    net = build("hexfold 0.2\n" + body + "\n")
    free = build("hexfold 0.2\na: tube(12,0, len=4)\n")
    pos = stick(net)
    bl = _bond_lengths(net, pos)
    assert bl.min() > 1.35 and bl.max() < 1.50, (bl.min(), bl.max())

    def _axial_extent(p: np.ndarray) -> float:
        c = p - p.mean(axis=0)
        axis = np.linalg.svd(c, full_matrices=False)[2][0]
        proj = c @ axis
        return float(proj.max() - proj.min())

    assert _axial_extent(pos) > 1.9 * _axial_extent(stick(free))
