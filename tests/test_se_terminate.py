"""Default H-termination of open edges, the hexfold family's last build step
(Reto, 2026-10-07): every carbon short of its valence gets an H after the
relax and the judgement; recorded on the block, tagged on the structure,
opt-out per op."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.store import Store
from precis_se import persist
from precis_se.atomic.generators import GeneratorError
from precis_se.atomic.generators.hexfold_scene import build_hexfold_scene
from precis_se.atomic.generators.hexfold_spec import (
    TERMINATED_TAG,
    build_hexfold,
    terminate_mode,
)
from precis_se.handler import SeHandler

TUBE = "hexfold 0.2\nt: tube(8,8, len=4)\n"


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


def test_converging_edges_report_terminate_clash_and_keep_the_caps() -> None:
    # the Y's guard columns leave a degree-one carbon at each sheet end:
    # two H each, three sheets converging -- no room, reported not dropped
    block = build_hexfold_scene(
        {
            "sheet": [4, 3],
            "features": [
                {"name": "y", "type": "k3-sp2-120-z", "dihedrals_deg": [120, 120, 120]}
            ],
        }
    )
    term = block.topology["terminated"]
    assert term["count"] == 42 and len(term["hosts"]) == 36 and term["clashes"] > 0
    clash = [
        f
        for f in block.topology["report"]["findings"]
        if f["code"] == "terminate.clash"
    ]
    assert len(clash) == 1 and clash[0]["severity"] == "WARN"
    data = (
        dict(clash[0]["data"])
        if isinstance(clash[0]["data"], list)
        else clash[0]["data"]
    )
    assert data["count"] == term["clashes"] and data["min_A"] < 1.0
    assert block.elements.count("H") == 42


def test_fin_scene_is_capped_by_default() -> None:
    params = {
        "tube": [6, 4],
        "features": [{"name": "f", "type": "fin-sp3-z", "side": "out", "rows": 3}],
    }
    block = build_hexfold_scene(params)
    bare = build_hexfold_scene({**params, "terminate": "none"})
    assert block.elements.count("H") == 34 and bare.elements.count("H") == 0
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
