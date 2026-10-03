"""``geom.clash`` (gr459567): non-bonded atoms closer than ``Profile.clash_A``
in the stick geometry.  Before it, ``geom.*`` covered bonded terms only, so
a fullerene sunk into its host and a crumpled lid both checked clean."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from hexfold.build import build
from hexfold.check import _clash_pairs, check
from hexfold.report import Profile, Severity
from hexfold.stick import stick

_EX = Path(__file__).resolve().parents[2] / "hexfold" / "examples"

#: gr459595's repro: an unholed cap(36,0) lid once crumpled to 0.63 A
#: self-clashes in stick(); flat (24.7 x 27 x 0.5 A) and clash-free since the
#: winding-signed flat-rim seed normal.
LARGE_LID = """hexfold 0.2
g: tube(36,0, len=7)
l: cap(36,0)
g.out --fuse k=0--> l.in
"""


def _clash(spec: str) -> tuple[list[dict], dict]:
    r = check(spec, geometry=True)
    clashes = [dict(f.data) for f in r.findings if f.code == "geom.clash"]
    summary = next(dict(f.data) for f in r.findings if f.code == "geom.summary")
    return clashes, summary


def test_squeezed_bud_neck_reports_a_warn_clash_naming_both_instances() -> None:
    # a [9-6] neck squeezes to ~1.5 A under stick: a clash, not an overlap
    r = check((_EX / "nanobud_96.hx").read_text(encoding="utf-8"), geometry=True)
    found = [f for f in r.findings if f.code == "geom.clash"]
    clashes = [dict(f.data) for f in found]
    summary = next(dict(f.data) for f in r.findings if f.code == "geom.summary")
    assert clashes
    assert {f.severity for f in found} == {Severity.WARN}
    assert summary["clash_count"] >= len(clashes)
    assert len(clashes) <= 10  # capped like the other geom.* codes
    assert {"b", "h"} in [set(c["instances"]) for c in clashes]
    worst = min(c["distance"] for c in clashes)
    assert worst == summary["clash_min"]


def test_clash_under_the_overlap_bar_is_an_error() -> None:
    spec = (_EX / "nanobud_96.hx").read_text(encoding="utf-8")
    r = check(spec, geometry=True, profile=Profile(clash_error_A=1.6))
    found = [f for f in r.findings if f.code == "geom.clash"]
    by_sev = {
        sev: [dict(f.data)["distance"] for f in found if f.severity == sev]
        for sev in (Severity.ERROR, Severity.WARN)
    }
    assert by_sev[Severity.ERROR] and all(d < 1.6 for d in by_sev[Severity.ERROR])
    assert all(d >= 1.6 for d in by_sev[Severity.WARN])


# ---------- nanobud placement (gr459567 gap 3) ----------

#: se design nanobud-review-figs' five blocks, verbatim (prod, 2026-10-02):
#: before the placement fix they measured 0.62-0.85 A ([2+2] ball lying on
#: the wall, [9-6]/[8-7] ball inside its tube or under its sheet)
REVIEW_FIGS = {
    "bud22": ("tube(8,8, len=12)", "h", "(6,0,A):0 [2+2]"),
    "bud87": ("tube(8,8, len=12)", "h", "(6,0,A):0 [8-7]"),
    "bud96": ("tube(8,8, len=12)", "h", "(6,0,A):0 [9-6]"),
    "gsheet22": ("sheet(25A, 12)", "s", "(5,5,A):0 [2+2]"),
    "gsheet96": ("sheet(25A, 12)", "s", "(5,5,A):0 [9-6]"),
}


def _fig(host: str, h: str, site: str, origin: str | None = None) -> str:
    return (
        f"hexfold 0.2\norigin {origin or h}\n{h}: {host}\n"
        f"b: fullerene(C60)\nb @ {h}/{site}\n"
    )


@pytest.mark.parametrize("name", sorted(REVIEW_FIGS))
def test_review_figure_buds_do_not_overlap_their_host(name: str) -> None:
    host, h, site = REVIEW_FIGS[name]
    r = check(_fig(host, h, site), geometry=True)
    errors = [
        f for f in r.findings if f.code == "geom.clash" and f.severity == Severity.ERROR
    ]
    assert errors == []
    summary = next(dict(f.data) for f in r.findings if f.code == "geom.summary")
    if "[2+2]" in site:
        # the cycloadduct leaves no squeezed neck
        assert summary["clash_count"] == 0, summary["clash_min"]


def _summary_and_place(spec: str) -> tuple[dict, set[str]]:
    r = check(spec, geometry=True)
    summary = next(dict(f.data) for f in r.findings if f.code == "geom.summary")
    return summary, {f.code for f in r.findings if f.code.startswith("place.")}


@pytest.mark.parametrize(
    ("host", "h", "site"),
    [
        REVIEW_FIGS["bud22"],  # bond path, tube
        REVIEW_FIGS["gsheet22"],  # bond path, sheet: seated edge-on if wrong
        ("tube(8,8, len=12)", "h", "(1,0,A):0 [2+2]"),  # 12.9 of 14.1 A to the end
        REVIEW_FIGS["bud96"],  # menu path
    ],
    ids=["bud22", "gsheet22", "tube_end_22", "bud96"],
)
def test_bud_placement_does_not_depend_on_the_bfs_origin(
    host: str, h: str, site: str
) -> None:
    # with the bud as origin the host is the moved side: the bond path
    # orients it by its own surface normal and the menu path reflects the
    # bud's seed up front, so both roots give the same geometry
    by_root = {o: _summary_and_place(_fig(host, h, site, o)) for o in (h, "b")}
    (s_h, p_h), (s_b, p_b) = by_root[h], by_root["b"]
    assert s_h["clash_count"] == s_b["clash_count"], by_root
    assert p_h == p_b, by_root
    assert s_b["clash_min"] is None or s_b["clash_min"] > Profile.DEFAULT.clash_error_A


@pytest.mark.parametrize(
    "second",
    ["b/(1,0,B) --bond--> h/(6,0,B)", "h/(6,0,B) --bond--> b/(1,0,B)"],
    ids=["same_order", "opposite_order"],
)
def test_bond_pair_seats_both_bonds_whatever_order_they_are_written(
    second: str,
) -> None:
    # the [2+2] pair, hand-written: a link written host-to-bud must still
    # find its bud-to-host partner, or the second bond seeds at 2.04 A
    spec = (
        "hexfold 0.2\norigin h\nh: tube(8,8, len=12)\nb: fullerene(C60)\n"
        f"b/(0,0,A) --bond--> h/(6,0,A)\n{second}\n"
    )
    net = build(spec, strict=False)
    assert net.seed3 is not None
    pos = np.array(net.seed3)
    inst = [a.instance for a in net.atoms]
    lengths = [
        float(np.linalg.norm(pos[i] - pos[j]))
        for i, j, _ in net.bonds
        if inst[i] != inst[j]
    ]
    assert len(lengths) == 2
    assert max(lengths) < 1.5, lengths


#: sheet + pillar (hole -> tube -> flat cap(12,0) lid -> C60 [9-6]) + a
#: C60 [2+2] on the sheet: the nanobuds hero figure's core (2026-10-02)
_PILLAR_AND_SHEET_BUD = """hexfold 0.2
origin s
s: sheet(30,24) - hex(1)@(10,12,A):0
t: tube(12,0, len=4)
c: cap(12,0)
b: fullerene(C60)
d: fullerene(C60)
s.hole --fuse k=0--> t.in
t.out --fuse k=0--> c.in
b @ c/(0,0,A):0 [9-6]
d @ s/(22,16,A):0 [2+2]
"""


def test_buds_on_flat_hosts_sit_on_the_open_face() -> None:
    # A flat lid's or sheet's centroid lies in its own plane, so it cannot
    # say which face is outside: the lid's C60 seeded inside the tube under
    # it (an endohedral peapod no clash check sees) and the sheet's C60
    # landed under the sheet while the pillar rose above it.
    net = build(_PILLAR_AND_SHEET_BUD, strict=False)
    pos = stick(net)
    inst = np.array([a.instance for a in net.atoms])
    sheet = pos[inst == "s"]
    c = sheet.mean(axis=0)
    n = np.linalg.svd(sheet - c)[2][2]
    if (pos[inst == "t"].mean(axis=0) - c) @ n < 0:
        n = -n  # +z is the face the pillar rises from
    z = {name: (pos[inst == name] - c) @ n for name in ("t", "c", "b", "d")}
    # the lid's ball sits above the lid, not inside the tube below it
    assert z["b"].min() > z["c"].max() - 0.5, (z["b"].min(), z["c"].max())
    # the sheet's ball is on the pillar's face
    assert z["d"].min() > 0.5, z["d"].min()


def test_mirrored_bud_is_reported() -> None:
    _s, menu = _summary_and_place(_fig(*REVIEW_FIGS["bud96"]))
    assert menu == {"place.mirrored"}
    _s, bond = _summary_and_place(_fig(*REVIEW_FIGS["bud22"]))
    assert bond == set()


@pytest.mark.parametrize(
    "spec",
    [
        (_EX / "pillar.hx").read_text(encoding="utf-8"),
        (_EX / "valve_shell.hx").read_text(encoding="utf-8"),
        LARGE_LID,
    ],
    ids=["pillar", "valve_shell", "large_lid_gr459595"],
)
def test_clean_builds_report_no_clash(spec: str) -> None:
    clashes, summary = _clash(spec)
    assert clashes == []
    assert summary["clash_count"] == 0
    assert summary["clash_min"] is None


def test_clash_pairs_skip_bonded_and_shared_neighbour_pairs() -> None:
    # a 4-chain 0-1-2-3 folded so every pair sits 1.0 A apart, plus a
    # free atom 4 near atom 0: only the 1-4 pair (0,3) and the unbonded
    # (0,4) count; 1-2 and 1-3 pairs are left to bond/angle checks
    coords = np.array(
        [[0, 0, 0], [1, 0, 0], [0.5, 0.8, 0], [0, 0.9, 0.3], [0, 0, 1.2]],
        dtype=np.float64,
    )
    bonds = [(0, 1, 1), (1, 2, 1), (2, 3, 1)]
    pairs = _clash_pairs(coords, bonds, 1.8)
    found = {(i, j) for _d, i, j in pairs}
    assert (0, 1) not in found and (0, 2) not in found and (1, 3) not in found
    assert (0, 3) in found and (0, 4) in found
    assert [d for d, *_ in pairs] == sorted(d for d, *_ in pairs)


def test_clash_bar_comes_from_the_profile() -> None:
    # nanobud_96 clashes at the default bar (its [9-6] neck), so the
    # loosened profile is what clears it, not a clean build
    spec = (_EX / "nanobud_96.hx").read_text(encoding="utf-8")
    assert [f for f in check(spec, geometry=True).findings if f.code == "geom.clash"]
    loose = check(spec, geometry=True, profile=Profile(clash_A=0.1))
    assert not [f for f in loose.findings if f.code == "geom.clash"]


# ---------- the seed tier ----------


def test_stacked_seed_is_an_error_even_when_stick_untangles_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a 57 seeded flat (the pre-C3 seed, kept here as the fixture) stacks
    # atoms 0.007 A apart; stick happens to relax it to 1.8 A, so geom.clash
    # alone would call it clean.  (The fixture was sheet_sw until sw became
    # a bond rotation, which stacks nothing.)
    import hexfold.build as hb

    monkeypatch.setattr(
        hb,
        "_disclination_seed",
        lambda patch: {
            v: np.array([p[0], p[1], 0.0]) for v, p in patch.flatpos.items()
        },
    )
    r = check("hexfold 0.2\norigin s\ns: sheet(12, 12) + 57@(4,4,A):1\n", geometry=True)
    summary = next(dict(f.data) for f in r.findings if f.code == "geom.summary")
    seed = [f for f in r.findings if f.code == "geom.seed_overlap"]
    assert seed and all(f.severity == Severity.ERROR for f in seed)
    assert all(dict(f.data)["distance"] < Profile.DEFAULT.seed_overlap_A for f in seed)
    assert summary["seed_clash_min"] < Profile.DEFAULT.seed_overlap_A
    assert summary["clash_min"] > Profile.DEFAULT.clash_error_A


@pytest.mark.parametrize("name", ["pillar", "nanobud_96", "valve_shell"])
def test_sound_seeds_raise_no_seed_overlap(name: str) -> None:
    # a bud neck seeds near 1.6 A: inside clash_A, far above an overlap
    r = check((_EX / f"{name}.hx").read_text(encoding="utf-8"), geometry=True)
    assert not [f for f in r.findings if f.code == "geom.seed_overlap"]
    summary = next(dict(f.data) for f in r.findings if f.code == "geom.summary")
    assert summary["seed_clash_min"] is None or summary["seed_clash_min"] > 1.0


# ---------- the disclination seed (C3) ----------


@pytest.mark.parametrize(
    "defects",
    [
        "+ heptagon@(15,15,A):1",
        "+ heptagon@(20,20,A):1 + heptagon@(8,8,A):4",
        "+ pentagon@(15,15,A):0",
        "+ sw@(15,15,A):0",
        "+ 57@(15,15,A):1",
    ],
)
def test_defected_sheets_seed_and_relax_without_overlap(defects: str) -> None:
    # a heptagon's wedge copies used to sit on their originals (0.00 A) and
    # stick left 0.5 A pairs; the seed now turns each cluster by its
    # intrinsic angle and lifts it onto a saddle or cone
    r = check(f"hexfold 0.2\norigin s\ns: sheet(30,30) {defects}\n", geometry=True)
    bad = [
        f
        for f in r.findings
        if f.code == "geom.seed_overlap"
        or (f.code == "geom.clash" and f.severity == Severity.ERROR)
    ]
    assert not bad


def test_single_sheet_pentagon_zips() -> None:
    # the flat seed left the pentagon's seam open at 2.06 A; the cone seed
    # is isometric, so stick closes it to within a few mA
    r = check(
        "hexfold 0.2\norigin s\ns: sheet(30,30) + pentagon@(15,15,A):0\n", geometry=True
    )
    summary = next(dict(f.data) for f in r.findings if f.code == "geom.summary")
    assert summary["bond_max"] < 0.05


def test_heptagon_seed_does_not_depend_on_authoring_order() -> None:
    def seed(defects: str) -> np.ndarray:
        net = build(f"hexfold 0.2\norigin s\ns: sheet(30,30) {defects}\n", strict=False)
        return np.array(sorted(map(tuple, np.round(np.asarray(net.seed3), 6))))

    a = seed("+ heptagon@(20,20,A):1 + heptagon@(8,8,A):4")
    b = seed("+ heptagon@(8,8,A):4 + heptagon@(20,20,A):1")
    assert a.shape == b.shape and np.allclose(a, b, atol=1e-5)


# ---------- the dislocation seed (K0) ----------


def _ideal_steps() -> np.ndarray:
    from hexfold.lattice import Lattice, Site, neighbors

    lat = Lattice()
    s0 = Site(0, 0, 0)
    nn = [lat.cart(b)[:2] - lat.cart(s0)[:2] for b in neighbors(s0)]
    return np.array(nn + [-d for d in nn])


def _ring_loop(net: Any, pos: np.ndarray, centre: np.ndarray) -> list:
    """A closed bond loop round ``centre``: the boundary of the rings whose
    centroid lies within the smallest radius from 8 A that gives one simple
    cycle."""
    for radius in np.arange(8.0, 14.0, 0.5):
        count: dict = {}
        for r in net.rings:
            if np.linalg.norm(pos[list(r), :2].mean(0) - centre) > radius:
                continue
            for a, b in zip(r, r[1:] + r[:1], strict=True):
                e = (min(a, b), max(a, b))
                count[e] = count.get(e, 0) + 1
        adj: dict = {}
        for (a, b), c in count.items():
            if c == 1:
                adj.setdefault(a, []).append(b)
                adj.setdefault(b, []).append(a)
        if any(len(n) != 2 for n in adj.values()):
            continue
        loop = [next(iter(adj))]
        prev = None
        while True:
            nxt = next(n for n in adj[loop[-1]] if n != prev)
            if nxt == loop[0]:
                break
            prev = loop[-1]
            loop.append(nxt)
        if len(loop) == len(adj):
            return loop
    raise AssertionError("no simple ring boundary round the core")


@pytest.mark.parametrize("d", [0, 1])
def test_dislocation_seed_closes_its_cut(d: int) -> None:
    # a 5-7 pair is an edge dislocation; the cluster seed left its Burgers
    # vector on one row of 2.56 / 1.88 A bonds from the core to the edge.
    # The Volterra pass closes that row and spreads the jump about the core
    net = build(
        f"hexfold 0.2\norigin s\ns: sheet(30,30) + 57@(15,15,A):{d}\n", strict=False
    )
    pos = np.asarray(net.seed3, dtype=float)
    bonds = np.array([(i, j) for i, j, _ in net.bonds])
    core = sorted({o for r in net.rings if len(r) != 6 for o in r})
    bl = np.linalg.norm(pos[bonds[:, 0]] - pos[bonds[:, 1]], axis=1)
    mid = 0.5 * (pos[bonds[:, 0]] + pos[bonds[:, 1]])
    dcore = np.min(
        np.linalg.norm(mid[:, None, :] - pos[core][None, :, :], axis=2), axis=1
    )
    far = bl[dcore >= 10.0]
    assert len(far) > 1000
    assert np.all(np.abs(far - 1.42) <= 0.05 * 1.42), (far.min(), far.max())
    assert bl.max() < 1.9  # was 2.56 along the whole row
    # the snapped Burgers circuit reads one lattice vector a (2.46 A)
    steps = _ideal_steps()
    loop = _ring_loop(net, pos, pos[core, :2].mean(0))
    b = np.zeros(2)
    for i, j in zip(loop, loop[1:] + loop[:1], strict=True):
        v = pos[j, :2] - pos[i, :2]
        b += steps[np.argmin(np.linalg.norm(steps - v, axis=1))]
    assert abs(np.linalg.norm(b) - 2.46) < 0.05, b


def test_dislocation_cut_is_the_set_the_bond_deviation_finds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the cut is read off the seed's own turn boundary; independently, the
    # bonds the cluster seed leaves > 0.4 A from every ideal step are the
    # same set (16 row bonds plus the compressed bond the 5 and 7 share)
    import hexfold.build as hb

    seen: list = []
    orig = hb._volterra

    def spy(patch: Any, out: dict, turns: list, cores: list) -> None:
        seen.append((patch, {v: p.copy() for v, p in out.items()}, turns))
        orig(patch, out, turns, cores)

    monkeypatch.setattr(hb, "_volterra", spy)
    build("hexfold 0.2\norigin s\ns: sheet(30,30) + 57@(15,15,A):0\n", strict=False)
    [(patch, out, turns)] = seen
    steps = _ideal_steps()
    labelled, suspect = set(), set()
    for e in patch.edges:
        a, b = tuple(e)
        if any(abs((t[a] - t[b] + 180.0) % 360.0 - 180.0) > 1e-3 for t in turns):
            labelled.add(e)
        v = out[b][:2] - out[a][:2]
        if np.min(np.linalg.norm(steps - v, axis=1)) > 0.4:
            suspect.add(e)
    assert len(labelled) == 17
    assert labelled == suspect


# ---------- the Stone-Wales bond rotation (K0 SW) ----------


def _sw(defects: str = "+ sw@(15,15,A):0") -> Any:
    return build(f"hexfold 0.2\norigin s\ns: sheet(30,30) {defects}\n", strict=False)


def _atom_at(net: Any, site: Any) -> int:
    return next(
        i
        for i, a in enumerate(net.atoms)
        if a.path.site == site and a.path.defect is None
    )


def _turned(net: Any, d: int = 0) -> tuple[int, int]:
    from hexfold.defects import glyph_bond
    from hexfold.lattice import Lattice, Site

    a, b, _ = glyph_bond(Site(15, 15, 0), d, Lattice())
    return _atom_at(net, a), _atom_at(net, b)


def _ring_pairs(net: Any) -> dict[tuple[int, int], int]:
    """Bonds shared by two rings, by ring sizes, leaving out 6|6."""
    by_bond: dict = {}
    for r in net.rings:
        for a, b in zip(r, r[1:] + r[:1], strict=True):
            by_bond.setdefault(frozenset((a, b)), []).append(len(r))
    out: dict[tuple[int, int], int] = {}
    for sizes in by_bond.values():
        if len(sizes) == 2 and sizes != [6, 6]:
            k = (min(sizes), max(sizes))
            out[k] = out.get(k, 0) + 1
    return out


def test_sw_is_a_bond_rotation_not_a_dipole() -> None:
    # the old wedge footprint was a 5577 dislocation dipole: its pentagons
    # shared an edge and its Burgers vector was one lattice vector
    net, pristine = _sw(), _sw("")
    assert not net.report.errors()
    assert sorted(len(r) for r in net.rings if len(r) != 6) == [5, 5, 7, 7]
    pairs = _ring_pairs(net)
    assert pairs.get((7, 7)) == 1 and pairs.get((5, 7)) == 4
    assert (5, 5) not in pairs
    assert len(net.atoms) == len(pristine.atoms)
    assert len(net.bonds) == len(pristine.bonds)


def test_sw_seed_turns_the_bond_about_its_centre() -> None:
    from hexfold.check import geometry_findings

    net, pristine = _sw(), _sw("")
    pos = np.asarray(net.seed3, dtype=float)
    ia, ib = _turned(net)
    old = {frozenset((i, j)) for i, j, _ in pristine.bonds}
    new = {frozenset((i, j)) for i, j, _ in net.bonds}
    assert frozenset((ia, ib)) in new
    assert abs(np.linalg.norm(pos[ia] - pos[ib]) - 1.42) < 0.01
    # each end keeps one neighbour and takes one from the far end, both
    # near 1.51 A; the neighbour it gave up sits 2.40 A away
    for v in (ia, ib):
        nbrs = [e for e in new if v in e and e != frozenset((ia, ib))]
        assert len(nbrs) == 2 and len([e for e in nbrs if e not in old]) == 1
        for e in nbrs:
            assert abs(np.linalg.norm(np.subtract(*pos[list(e)])) - 1.51) < 0.01
        [gone] = [e for e in old - new if v in e]
        assert abs(np.linalg.norm(np.subtract(*pos[list(gone)])) - 2.40) < 0.01
    codes = {f.code for f in geometry_findings(net)}
    assert not codes & {"geom.seed_short_bond", "geom.seed_overlap"}


def test_sw_burgers_circuit_closes() -> None:
    net = _sw()
    pos = np.asarray(stick(net), dtype=float)
    core = sorted({o for r in net.rings if len(r) != 6 for o in r})
    steps = _ideal_steps()
    loop = _ring_loop(net, pos, pos[core, :2].mean(0))
    b = np.zeros(2)
    for i, j in zip(loop, loop[1:] + loop[:1], strict=True):
        v = pos[j, :2] - pos[i, :2]
        b += steps[np.argmin(np.linalg.norm(steps - v, axis=1))]
    assert np.linalg.norm(b) < 1e-9, b


def _local_shape(net: Any, ia: int, ib: int, radius: int = 8) -> list[int]:
    """Weisfeiler-Lehman labels of the radius-``radius`` bond neighbourhood
    of a-b, sorted: equal for isomorphic neighbourhoods."""
    adj: dict[int, list[int]] = {}
    for i, j, _ in net.bonds:
        adj.setdefault(i, []).append(j)
        adj.setdefault(j, []).append(i)
    dist = {ia: 0, ib: 0}
    front = [ia, ib]
    for k in range(1, radius + 1):
        front = [w for v in front for w in adj[v] if w not in dist]
        dist.update((w, k) for w in front)
    label = {v: hash((dist[v], len(adj[v]))) for v in dist}
    for _ in range(radius):
        label = {
            v: hash((label[v], tuple(sorted(label[w] for w in adj[v] if w in dist))))
            for v in dist
        }
    return sorted(label.values())


@pytest.mark.parametrize("d", [1, 2, 3, 4, 5])
def test_sw_every_dir_builds_the_same_local_graph(d: int) -> None:
    # the whole sheets differ (the outline is not sixfold); the neighbourhood
    # of the turned bond is isomorphic for all six dirs (networkx-checked
    # once at radius 8), and dir d and d+3 are mirror images
    ref = _sw()
    net = _sw(f"+ sw@(15,15,A):{d}")
    assert not net.report.errors()
    assert _local_shape(net, *_turned(net, d)) == _local_shape(ref, *_turned(ref))


def test_a_rotation_of_a_bond_an_earlier_glyph_dropped_names_both() -> None:
    # sw:0 gives up (15,15,A)-(14,15,B), the bond sw:1 would turn
    net = _sw("+ sw@(15,15,A):0 + sw@(15,15,A):1")
    [err] = [f for f in net.report.errors() if f.code == "cut.overlap"]
    assert "sw@(15,15,A):1" in err.message
    assert "glyph sw@(15,15,A):0 already consumed" in err.message


def test_cut_jump_invents_no_jump_on_a_cut_free_sheet() -> None:
    # the per-bond sign is a free choice; on bonds that are all lattice
    # steps, no J != 0 closes all three directions, so the fit must say 0
    import hexfold.build as hb

    net = build("hexfold 0.2\norigin s\ns: sheet(16,16)\n", strict=False)
    pos = np.asarray(net.seed3, dtype=float)
    b = np.array([(i, j) for i, j, _ in net.bonds])
    jump, sign = hb._cut_jump(pos[b[:, 1], :2] - pos[b[:, 0], :2], _ideal_steps())
    assert np.linalg.norm(jump) < 1e-9
    assert np.all(sign != 0)


@pytest.mark.parametrize("defect", ["", "+ heptagon@(8,8,A):1", "+ pentagon@(8,8,A):0"])
def test_charged_and_pristine_sheets_never_reach_the_cut_fit(
    defect: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # fed every bond of a saddle or cone seed, _cut_jump finds |J| ~ 0.3 A
    # (projected curved bonds are not lattice steps); only K = 0 clusters
    # may call it
    import hexfold.build as hb

    calls: list = []
    monkeypatch.setattr(hb, "_volterra", lambda *a: calls.append(a))
    build(f"hexfold 0.2\norigin s\ns: sheet(16,16) {defect}\n", strict=False)
    assert not calls


def test_laplace_solver_cg_agrees_with_dense(monkeypatch: pytest.MonkeyPatch) -> None:
    import hexfold.build as hb

    net = build("hexfold 0.2\norigin s\ns: sheet(16,16)\n", strict=False)
    n = len(net.atoms)
    assert n >= 500
    edges = np.array([(i, j) for i, j, _ in net.bonds])
    t = np.random.default_rng(7).normal(size=(len(edges), 2))
    rhs = np.zeros((n, 2))
    np.add.at(rhs, edges[:, 1], t)
    np.add.at(rhs, edges[:, 0], -t)
    gauge = [0, 1, 2]
    dense = hb._laplace_solve(n, edges, rhs, gauge)
    monkeypatch.setattr(hb, "_DENSE_MAX", 0)
    cg = hb._laplace_solve(n, edges, rhs, gauge)
    assert np.allclose(dense, cg, atol=1e-8)
    assert np.allclose(dense[gauge].mean(axis=0), 0.0, atol=1e-10)


@pytest.mark.parametrize("defect", ["+ heptagon@(12,12,A):1", "+ 57@(12,12,A):0"])
def test_bud_on_a_defected_sheet_sits_off_it(defect: str) -> None:
    # a heptagon sheet is no longer flat to _flat_normals (its seed is a
    # saddle); the bud must still land on one face, clear of the sheet
    spec = f"hexfold 0.2\norigin s\ns: sheet(30,30) {defect}\nd: fullerene(C60)\nd @ s/(22,20,A):0 [2+2]\n"
    r = check(spec, geometry=True)
    assert not [f for f in r.findings if f.severity == Severity.ERROR]
    net = build(spec, strict=False)
    pos = stick(net)
    inst = np.array([a.instance for a in net.atoms])
    ball = pos[inst == "d"]
    sheet = pos[inst == "s"]
    near = sheet[np.linalg.norm(sheet - ball.mean(0), axis=1) < 10.0]
    c = near.mean(0)
    nrm = np.linalg.svd(near - c)[2][2]
    side = (ball - c) @ nrm
    if side.mean() < 0:
        side = -side
    assert side.min() > 0.5, side.min()


def test_a_nearly_stacked_bond_in_the_seed_is_a_warn() -> None:
    # the overlap test skips bonded pairs, so a bond seeded at 0.36 A (the
    # old wedge sw seed's, gr462144) went unreported
    import dataclasses

    from hexfold.check import geometry_findings

    net = build("hexfold 0.2\norigin s\ns: sheet(8,8)\n", strict=False)
    assert not [f for f in geometry_findings(net) if f.code == "geom.seed_short_bond"]
    i, j, _o = net.bonds[0]
    seed = np.asarray(net.seed3, dtype=float).copy()
    seed[j] = seed[i] + 0.36 * (seed[j] - seed[i]) / np.linalg.norm(seed[j] - seed[i])
    found = geometry_findings(dataclasses.replace(net, seed3=tuple(map(tuple, seed))))
    short = [f for f in found if f.code == "geom.seed_short_bond"]
    assert len(short) == 1 and short[0].severity == Severity.WARN
    assert sorted(dict(short[0].data)["atoms"]) == sorted([i, j])
    summary = next(dict(f.data) for f in found if f.code == "geom.summary")
    assert summary["seed_short_bond_count"] == 1


def test_relaxed_coordinates_are_judged_as_given_not_re_relaxed() -> None:
    # S4: a tethered relax is judged on its own coordinates; an untethered
    # re-relax inside geometry_findings would judge a different geometry
    from hexfold.check import Relaxed, geometry_findings
    from hexfold.stick import stick_info

    net = build("hexfold 0.2\norigin s\ns: sheet(8,8)\n", strict=False)
    pos, force = stick_info(net)

    def summary(found: list[Any]) -> dict[str, Any]:
        return next(dict(f.data) for f in found if f.code == "geom.summary")

    plain = geometry_findings(net)
    same = geometry_findings(net, relaxed=Relaxed(pos, force, "untethered"))
    # the same coordinates judge the same; only the label is added
    assert [f for f in same if f.code != "geom.summary"] == [
        f for f in plain if f.code != "geom.summary"
    ]
    assert "relax" not in summary(plain)
    assert summary(same) == {**summary(plain), "relax": "untethered"}
    squeezed = pos.copy()
    i, j, _o = net.bonds[0]
    far = max(range(len(pos)), key=lambda a: float(np.linalg.norm(pos[a] - pos[i])))
    squeezed[far] = pos[i] + np.array([0.0, 0.0, 0.5])  # stacked on atom i
    found = geometry_findings(net, relaxed=Relaxed(squeezed, force, "tethered"))
    clash = [f for f in found if f.code == "geom.clash"]
    assert clash and clash[0].severity == Severity.ERROR
    assert summary(found)["relax"] == "tethered"
    assert not [f for f in plain if f.code == "geom.clash"]
