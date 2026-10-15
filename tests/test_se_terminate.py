"""Default H-termination of open edges, the hexfold family's last build step
(Reto, 2026-10-07): every carbon short of its valence gets an H after the
relax and the judgement; recorded on the block, tagged on the structure,
opt-out per op."""

from __future__ import annotations

import itertools
import math
from typing import Any

import numpy as np
import pytest

from hexfold.build import Atom, Net
from hexfold.ids import AtomPath
from hexfold.lattice import Lattice, Site
from hexfold.report import Report
from hexfold.text import Spec
from precis.dispatch import Hub
from precis.store import Store
from precis_se import persist
from precis_se.atomic.generators import GeneratorError
from precis_se.atomic.generators.hexfold_scene import build_hexfold_scene
from precis_se.atomic.generators.hexfold_spec import (
    TERMINATED_TAG,
    _block_from_net,
    build_hexfold,
    terminate_mode,
)
from precis_se.handler import SeHandler

TUBE = "hexfold 0.2\nt: tube(8,8, len=4)\n"


def _toy_net(
    xyz: list[tuple[float, float, float]], bonds: list[tuple[int, int]]
) -> Net:
    atoms = tuple(
        Atom(
            path=AtomPath(instance="t", site=Site(i, 0, 0)),
            ord=i,
            element="C",
            hyb="sp2",
            instance="t",
        )
        for i in range(len(xyz))
    )
    return Net(
        atoms=atoms,
        bonds=tuple(sorted((min(i, j), max(i, j), 1) for i, j in bonds)),
        rings=(),
        ports=(),
        regions=(("t", tuple(range(len(xyz)))),),
        report=Report(()),
        spec=Spec(),
        lattice=Lattice(),
        seed3=None,
        seed_kind="flat-perturbed",
    )


def _min_hh(block: Any) -> float:
    pos = np.asarray(block.coords)
    h = [i for i, e in enumerate(block.elements) if e == "H"]
    return min(
        (
            float(np.linalg.norm(pos[i] - pos[j]))
            for i, j in itertools.combinations(h, 2)
        ),
        default=math.inf,
    )


def _h_bonds(block: Any) -> list[tuple[int, int]]:
    el = block.elements
    return [(i, j) for i, j, _o, _k in block.bonds if el[i] == "H" or el[j] == "H"]


def test_terminate_mode_defaults_to_h_and_refuses_other_values() -> None:
    assert terminate_mode({}) == "H"
    assert terminate_mode({"terminate": "ports-open"}) == "ports-open"
    assert terminate_mode({"terminate": "none"}) == "none"
    with pytest.raises(GeneratorError, match="terminate must be one of"):
        terminate_mode({"terminate": "D"})
    with pytest.raises(GeneratorError):
        build_hexfold({"spec": TUBE, "terminate": True})


def test_open_tube_rims_get_one_h_per_dangling_atom_at_sigma_ch() -> None:
    block = build_hexfold({"spec": TUBE})
    bare = build_hexfold({"spec": TUBE, "terminate": "none"})
    n_c = len(bare.elements)
    assert bare.elements.count("H") == 0 and bare.tags == []
    assert bare.topology["terminated"] == {
        **bare.topology["terminated"],
        "count": 0,
        "mode": "none",
    }
    # 16 dangling atoms per rim, two rims; H appended after the carbons
    assert block.elements[:n_c] == bare.elements
    assert block.elements.count("H") == 32
    assert block.hybridizations is not None
    assert block.hybridizations[n_c:] == ["s"] * 32
    hb = _h_bonds(block)
    assert len(hb) == 32
    for i, j in hb:
        assert np.linalg.norm(block.coords[i] - block.coords[j]) == pytest.approx(
            1.09, abs=1e-6
        )
    assert {(o, k) for i, j, o, k in block.bonds if block.elements[j] == "H"} == {
        (1.0, "pairwise")
    }
    term = block.topology["terminated"]
    assert term["element"] == "H" and term["mode"] == "H" and term["count"] == 32
    assert len(term["hosts"]) == 32 and term["clashes"] == 0
    assert block.tags == [TERMINATED_TAG]
    assert "32 open edge(s) H-terminated" in block.provenance
    # the carbon-side record is untouched: ports, regions, findings
    assert [p.name for p in block.ports] == [p.name for p in bare.ports]
    assert block.topology["report"] == bare.topology["report"]
    assert block.topology["n_atoms"] == bare.topology["n_atoms"]
    # the relaxed carbon coordinates are the same build
    shift = block.coords[:n_c] - bare.coords
    assert shift[:, :2] == pytest.approx(0.0, abs=1e-12)
    assert np.ptp(shift[:, 2]) == pytest.approx(0.0, abs=1e-12)  # one rigid z lift
    assert block.coords[:, 2].min() == pytest.approx(1.7, abs=1e-9)
    assert block.envelope != bare.envelope


def test_ports_open_leaves_join_port_rims_bare() -> None:
    block = build_hexfold({"spec": TUBE, "terminate": "ports-open"})
    assert block.elements.count("H") == 0 and block.tags == []
    assert block.topology["terminated"]["mode"] == "ports-open"
    # a sheet with a bud: the sheet rim is a port, the bud's sp3 atoms are
    # complete, only corner stubs remain capped
    spec = (
        "hexfold 0.2\norigin h\nh: sheet(8,8)\nb: fullerene(C60)\n"
        "b @ h/(4,4,A):0 [2+2]\n"
    )
    capped = build_hexfold({"spec": spec})
    open_ports = build_hexfold({"spec": spec, "terminate": "ports-open"})
    assert capped.elements.count("H") > open_ports.elements.count("H")
    assert set(open_ports.topology["terminated"]["hosts"]).isdisjoint(
        set(capped.topology["ports"]["h_rim"]["atoms"])
    )


def test_a_spec_terminate_line_is_not_doubled_and_still_tags() -> None:
    block = build_hexfold({"spec": TUBE + "terminate: t.* = H\n"})
    assert block.elements.count("H") == 32
    assert block.topology["terminated"]["count"] == 0
    assert block.tags == [TERMINATED_TAG]


def test_converging_edges_ring_close_the_clashing_cap_hosts() -> None:
    # the Y's seam end converges three sheet rims: the colliding caps'
    # hosts are within bonding reach, so the resolver drops the caps and
    # knits the rims shut -- no colliding H is minted (gr486406)
    block = build_hexfold_scene(
        {
            "sheet": [4, 3],
            "features": [
                {"name": "y", "type": "k3-sp2-120-z", "dihedrals_deg": [120, 120, 120]}
            ],
        }
    )
    term = block.topology["terminated"]
    assert term["clashes"] == 0 and term["trimmed"] == []
    assert term["count"] == 35 and block.elements.count("H") == 35
    findings = block.topology["report"]["findings"]
    assert not [f for f in findings if f["code"] == "terminate.clash"]
    closed = [f for f in findings if f["code"] == "terminate.closed"]
    assert len(closed) == 1 and closed[0]["severity"] == "INFO"
    data = (
        dict(closed[0]["data"])
        if isinstance(closed[0]["data"], list)
        else closed[0]["data"]
    )
    assert data["count"] == 5
    assert len(term["closed"]) == 5
    for a, b in term["closed"]:
        assert block.elements[a] == "C" and block.elements[b] == "C"
        assert np.linalg.norm(block.coords[a] - block.coords[b]) < 1.8
    assert _min_hh(block) >= 1.5


# the gripe's design (hexfold-relax-seam-out) and its siblings: the rims
# at each seam end knit shut and no H-H pair survives under the clash bar
@pytest.mark.parametrize(
    "params",
    [
        {
            "tube": [8, 8],
            "features": [
                {"name": "y", "type": "fin-k3-120-z", "side": "out", "rows": 4}
            ],
        },
        {
            "sheet": [8, 8],
            "features": [
                {
                    "name": "y",
                    "type": "k3-sp2-120-z",
                    "dihedrals_deg": [120, 120, 120],
                }
            ],
        },
        {
            "tube": [18, 8],
            "features": [
                {"name": "y", "type": "fin-k3-120-z", "side": "in", "rows": 4}
            ],
        },
    ],
    ids=["seam-out", "y-a", "seam-in"],
)
def test_seam_convergence_resolves_to_a_clean_block(params: dict[str, Any]) -> None:
    block = build_hexfold_scene(params)
    term = block.topology["terminated"]
    assert term["clashes"] == 0
    assert term["closed"] and term["trimmed"] == []
    findings = block.topology["report"]["findings"]
    assert not [f for f in findings if f["severity"] == "ERROR"]
    assert not [f for f in findings if f["code"] == "terminate.clash"]
    assert _min_hh(block) >= 1.5


def test_seam_out_validate_reports_zero_errors(
    store: Store, hub_no_embedder: Hub
) -> None:
    slug = "hexfold-relax-seam-out"
    handler = SeHandler(hub=hub_no_embedder)
    handler.put(
        id=slug,
        args={
            "ops": [
                {
                    "op": "generate",
                    "generator": "hexfold_scene",
                    "name": "y",
                    "params": {
                        "tube": [8, 8],
                        "features": [
                            {
                                "name": "y",
                                "type": "fin-k3-120-z",
                                "side": "out",
                                "rows": 4,
                            }
                        ],
                    },
                }
            ]
        },
    )
    se_ref = store.get_ref(kind="se", id=slug)
    assert se_ref is not None
    node = persist.load_tree(store, se_ref.id).blocks["y"]
    assert node.bound is not None
    ref = store.get_ref(kind="structure", id=node.bound)
    assert ref is not None
    record = (ref.meta or {})["generated"]
    findings = record["report"]["findings"]
    assert not [f for f in findings if f["severity"] == "ERROR"]
    assert [f for f in findings if f["code"] == "terminate.closed"]
    assert not [f for f in findings if f["code"] == "terminate.clash"]
    assert record["terminated"]["closed"] and record["terminated"]["clashes"] == 0
    validation = handler.get(id=slug, view="validate").body
    assert "generated_report_error" not in validation
    assert "terminate.clash" not in validation
    snap = store.structure_positions_snapshot(ref.id)
    assert snap is not None
    pos = np.asarray(snap["fractional"]) @ np.asarray(snap["lattice"])
    # the caps are appended after the carbons, in mint order
    h_pos = pos[-record["terminated"]["count"] :]
    assert (
        min(np.linalg.norm(p - q) for p, q in itertools.combinations(h_pos, 2)) >= 1.5
    )


def test_cap_clash_within_bonding_reach_ring_closes() -> None:
    # two bent rim ends facing across a 2.5 A gap: each tip's cap aims
    # into the gap and they collide; the hosts are within reach, so the
    # resolver drops both caps and adds the C-C bond
    xyz = [
        (0.0, 0.0, 0.0),
        (-1.35, 1.35, 0.0),
        (-1.35, -1.35, 0.0),
        (2.5, 0.0, 0.0),
        (3.85, 1.35, 0.0),
        (3.85, -1.35, 0.0),
    ]
    net = _toy_net(xyz, [(0, 1), (0, 2), (3, 4), (3, 5)])
    block = _block_from_net(
        net, np.asarray(xyz, dtype=float), spec="", report=Report(()), fidelity="stick"
    )
    term = block.topology["terminated"]
    assert term["closed"] == [[0, 3]] and term["trimmed"] == []
    assert term["clashes"] == 0 and block.elements.count("H") == 8
    assert np.linalg.norm(block.coords[0] - block.coords[3]) < 1.8


def _trim_fixture() -> tuple[Net, np.ndarray]:
    # a deg-1 stub whose cap lands 1.77 A from a full-valence carbon while
    # the hosts sit 2.86 A apart -- out of ring-close reach
    xyz = [
        (0.0, 0.0, 0.0),  # 0: the dangling rim atom (cap aims at +x)
        (-0.71, 1.23, 0.0),  # 1
        (0.0, 2.46, 0.0),  # 2
        (2.86, 0.0, 0.0),  # 3: deg-3 target atom
        (3.5, 1.2, 0.0),
        (3.5, -1.2, 0.0),
        (4.4, 0.0, 0.0),
    ]
    net = _toy_net(xyz, [(0, 1), (1, 2), (3, 4), (3, 5), (3, 6)])
    return net, np.asarray(xyz, dtype=float)


def test_cap_clash_beyond_reach_trims_the_dangling_rim_atom() -> None:
    net, xyz = _trim_fixture()
    block = _block_from_net(net, xyz, spec="", report=Report(()), fidelity="stick")
    term = block.topology["terminated"]
    assert term["trimmed"] == ["t/(0,0,A)"]
    assert term["clashes"] == 0 and block.elements.count("C") == 6
    findings = block.topology["report"]["findings"]
    trimmed = [f for f in findings if f["code"] == "terminate.trimmed"]
    assert len(trimmed) == 1 and trimmed[0]["severity"] == "INFO"


def test_cap_clash_on_a_pinned_atom_refuses_the_build() -> None:
    net, xyz = _trim_fixture()
    with pytest.raises(GeneratorError, match=r"terminate\.clash.*t/\(0,0,A\)"):
        _block_from_net(
            net,
            xyz,
            spec="",
            report=Report(()),
            fidelity="stick",
            pinned=frozenset({0}),
        )


def test_fin_scene_is_capped_by_default() -> None:
    params = {
        "tube": [6, 4],
        "features": [{"name": "f", "type": "fin-sp3-z", "side": "out", "rows": 3}],
    }
    block = build_hexfold_scene(params)
    bare = build_hexfold_scene({**params, "terminate": "none"})
    # two cap pairs collide at the graft corners; their hosts are within
    # bonding reach and ring-close (gr486406), so 4 caps fewer mint
    assert block.elements.count("H") == 30 and bare.elements.count("H") == 0
    assert len(block.topology["terminated"]["closed"]) == 2
    assert "terminate" not in block.topology["scene"]
    assert bare.topology["scene"] == {**block.topology["scene"], "terminate": "none"}
    assert block.tags == [TERMINATED_TAG]


def test_generate_tags_the_structure_terminated_h(
    store: Store, hub_no_embedder: Hub
) -> None:
    slug = "hexfold-terminate-test-tube"
    handler = SeHandler(hub=hub_no_embedder)
    handler.put(
        id=slug,
        args={
            "ops": [
                {
                    "op": "generate",
                    "generator": "hexfold",
                    "name": "t",
                    "params": {"spec": TUBE},
                }
            ]
        },
    )
    se_ref = store.get_ref(kind="se", id=slug)
    assert se_ref is not None
    node = persist.load_tree(store, se_ref.id).blocks["t"]
    assert node.bound is not None
    ref = store.get_ref(kind="structure", id=node.bound)
    assert ref is not None
    tags = {str(t) for t in store.tags_for(ref.id)}
    assert TERMINATED_TAG in tags
    record = (ref.meta or {})["generated"]
    assert record["terminated"]["count"] == 32
    snap = store.structure_positions_snapshot(ref.id)
    assert snap is not None and len(snap["fractional"]) == 128 + 32
    block = handler.get(id=slug, view="block", args={"name": "t"}).body
    assert "H-terminated" in block
