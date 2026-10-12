"""Private helper regressions on complete graphs; no native science jobs."""

from __future__ import annotations

import copy
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest

from hexfold.build import Atom, Net, Port, build
from hexfold.ids import AtomPath
from hexfold.lattice import Lattice, Site
from hexfold.report import Profile, Report
from hexfold.text import Spec
from precis.structure.completion_codec import dumps_wire, loads_wire
from precis.structure.completion_identity import geometry_snapshot, stored_graph_token
from precis.structure.completion_types import GeometrySnapshot
from precis_se.atomic.generate import generated_record
from precis_se.atomic.generators.hexfold_spec import build_hexfold
from precis_se.atomic.termination import (
    _encode_preparation,
    _Exposure,
    _finalize_termination,
    _Parent,
    _ParentMap,
    _replay_preparation,
    _Request,
    _resolve_termination_scope,
    _source_token,
    _TerminationError,
    _topology_token,
    _verify_generated_caps,
)


@pytest.fixture
def graph() -> tuple[Net, np.ndarray]:
    # Complete two-carbon graph: two deficits at EACH end, not a fragment
    # standing in for a larger unknown graph. No claimed electronic result.
    atoms = tuple(
        Atom(AtomPath("x", Site(i, 0, 0)), i, "C", "sp2", "x") for i in range(2)
    )
    net = Net(atoms, ((0, 1, 1),), (), (), (), Report(), Spec(), Lattice())
    return net, np.array([[0.0, 0.0, 0.0], [1.42, 0.0, 0.0]])


def exposure(net: Net, reserved: frozenset[int] = frozenset()) -> _Exposure:
    degree = [0] * len(net.atoms)
    for i, j, _ in net.bonds:
        degree[i] += 1
        degree[j] += 1
    return _Exposure(
        _topology_token(net),
        frozenset(i for i, d in enumerate(degree) if d < 3) - reserved,
        reserved,
        tuple(name for name, _ in net.ports),
        net.consumed_rims,
    )


@pytest.mark.parametrize("parent", ["H", "ports-open", "none"])
@pytest.mark.parametrize("requested_mode", [None, "H", "ports-open", "none"])
def test_complete_mode_table(
    graph: tuple[Net, np.ndarray], parent: Any, requested_mode: Any
) -> None:
    net, _ = graph
    req = _Request(requested_mode is not None, requested_mode)
    rejects = (parent == "none" and requested_mode in ("H", "ports-open")) or (
        parent == "ports-open" and requested_mode == "H"
    )
    if rejects:
        with pytest.raises(_TerminationError, match="overrides"):
            _resolve_termination_scope(
                net,
                requested=req,
                parent_modes=(parent, parent),
                exposure=exposure(net),
            )
    else:
        scope = _resolve_termination_scope(
            net, requested=req, parent_modes=(parent, parent), exposure=exposure(net)
        )
        assert scope.effective == (requested_mode or parent)
        assert scope.requested == req


@pytest.mark.parametrize(
    "parents",
    [
        ("H", "none"),
        ("H", "ports-open"),
        ("none", "ports-open"),
        (None, "H"),
        (None, None),
    ],
)
@pytest.mark.parametrize("requested_mode", [None, "H", "ports-open", "none"])
def test_conflicts_unknown_cannot_be_overridden(
    graph: tuple[Net, np.ndarray], parents: Any, requested_mode: Any
) -> None:
    net, _ = graph
    with pytest.raises(_TerminationError):
        _resolve_termination_scope(
            net,
            requested=_Request(requested_mode is not None, requested_mode),
            parent_modes=parents,
            exposure=exposure(net),
        )


@pytest.mark.parametrize(
    "requested_mode",
    [
        _Request(None, None),
        _Request(False, "H"),
        _Request(True, None),
        _Request(cast(Any, 1), "H"),
    ],
)
def test_request_shape_is_not_inferred(
    graph: tuple[Net, np.ndarray], requested_mode: _Request
) -> None:
    net, _ = graph
    with pytest.raises(_TerminationError):
        _resolve_termination_scope(
            net,
            requested=requested_mode,
            parent_modes=("H", "H"),
            exposure=exposure(net),
        )


def test_missing_incomplete_and_stale_exposure(graph: tuple[Net, np.ndarray]) -> None:
    net, _ = graph
    valid = exposure(net)
    for intent in (
        None,
        replace(valid, topology_token="old"),
        replace(valid, eligible_hosts=frozenset()),
        replace(valid, reserved_hosts=frozenset({0})),
        replace(valid, eligible_hosts=frozenset({False, 1})),
    ):
        with pytest.raises(_TerminationError):
            _resolve_termination_scope(
                net,
                requested=_Request(True, "none"),
                parent_modes=("none", "none"),
                exposure=intent,
            )


def test_reserved_ports_and_consumed_identity(graph: tuple[Net, np.ndarray]) -> None:
    net, xyz = graph
    net = replace(net, ports=(("rim", Port("rim", (0,), (0,), "z", "axis", 0)),))
    with pytest.raises(_TerminationError, match="retained port"):
        _resolve_termination_scope(
            net,
            requested=_Request(False, None),
            parent_modes=("H", "H"),
            exposure=exposure(net),
        )
    intent = exposure(net, frozenset({0}))
    for broken in (
        replace(intent, reserved_ports=()),
        replace(intent, reserved_ports=("rim", "rim")),
        replace(intent, consumed_rims=((0,),)),
    ):
        with pytest.raises(_TerminationError):
            _resolve_termination_scope(
                net,
                requested=_Request(False, None),
                parent_modes=("H", "H"),
                exposure=broken,
            )
    scope = _resolve_termination_scope(
        net, requested=_Request(False, None), parent_modes=("H", "H"), exposure=intent
    )
    result = _finalize_termination(net, xyz, scope=scope)
    assert [cap.host for cap in result.caps] == [1, 1]
    assert len(result.elements) == 4  # reserved carbon deficits deliberately open


def test_complete_valence_and_identity_without_phantom_rows(
    graph: tuple[Net, np.ndarray],
) -> None:
    net, xyz = graph
    original = xyz.copy()
    scope = _resolve_termination_scope(
        net,
        requested=_Request(False, None),
        parent_modes=("H", "H"),
        exposure=exposure(net),
    )
    first = _finalize_termination(net, xyz, scope=scope)
    assert np.array_equal(xyz, original)
    assert first.coordinates[:2] == tuple(map(tuple, xyz))
    assert first.elements == ("C", "C", "H", "H", "H", "H")
    assert first.hybridizations == ("sp2", "sp2", "s", "s", "s", "s")
    assert [cap.host for cap in first.caps] == [0, 0, 1, 1]
    assert [cap.bond for cap in first.caps] == [1, 2, 3, 4]
    assert all(
        first.bonds[cap.bond] == (cap.host, cap.h, 1.0, "pairwise")
        for cap in first.caps
    )
    assert first == _finalize_termination(net, xyz, scope=scope)  # no duplicate caps
    assert first.precap["natoms"] == 2 and first.final["natoms"] == 6
    assert first.precap["snapshot_digest"] != first.final["snapshot_digest"]
    assert not {"ref_id", "version", "geometry"} & first.precap.keys()
    assert "hexfold.termination.artifact" in first.precap["snapshot_digest"]
    shifted = _finalize_termination(net, xyz + [0, 0, 0.1], scope=scope)
    assert shifted.precap != first.precap
    assert shifted.final != first.final


def test_none_preserves_exact_atoms_and_does_not_cap(
    graph: tuple[Net, np.ndarray],
) -> None:
    net, xyz = graph
    scope = _resolve_termination_scope(
        net,
        requested=_Request(True, "none"),
        parent_modes=("H", "H"),
        exposure=exposure(net),
    )
    result = _finalize_termination(net, xyz, scope=scope)
    assert not result.caps and result.precap == result.final


def test_contact_species_and_two_hop_visibility(graph: tuple[Net, np.ndarray]) -> None:
    net, xyz = graph
    scope = _resolve_termination_scope(
        net,
        requested=_Request(True, "H"),
        parent_modes=("H", "H"),
        exposure=exposure(net),
    )
    base = _finalize_termination(net, xyz, scope=scope)
    # Two H on the same host are a two-hop pair, not hidden by ordinary clash exclusions.
    broad_h = replace(
        Profile.DEFAULT,
        clash_A=0.01,
        clash_pair_A=(("H", "C", 100.0), ("H", "H", 100.0)),
    )
    result = _finalize_termination(net, xyz, scope=scope, profile=broad_h)
    assert {(i, j) for i, j, _ in result.contacts} >= {(3, 2), (5, 4), (2, 1)}
    assert len(result.contacts) > len(base.contacts)
    assert all(j != result.caps[i - 2].host for i, j, _ in result.contacts)
    assert result.final == base.final  # diagnostics do not mutate geometry


@pytest.mark.parametrize(
    "fault",
    [
        "zero",
        "nan",
        "shape",
        "duplicate",
        "self",
        "dangling",
        "overvalent",
        "isolated",
        "authored-H",
        "sp3",
        "ring",
    ],
)
def test_complete_graph_refusals(graph: tuple[Net, np.ndarray], fault: str) -> None:
    net, xyz = graph
    if fault == "zero":
        xyz[1] = xyz[0]
    elif fault == "nan":
        xyz[0, 0] = float("nan")
    elif fault == "shape":
        xyz = xyz[:1]
    elif fault == "duplicate":
        net = replace(net, bonds=net.bonds * 2)
    elif fault == "self":
        net = replace(net, bonds=((0, 0, 1),))
    elif fault == "dangling":
        net = replace(net, bonds=((0, 2, 1),))
    elif fault == "overvalent":
        atoms = tuple(
            replace(net.atoms[0], ord=i, path=AtomPath("x", Site(i, 0, 0)))
            for i in range(5)
        )
        net = replace(net, atoms=atoms, bonds=tuple((0, i, 1) for i in range(1, 5)))
    elif fault == "isolated":
        net = replace(net, bonds=())
    elif fault == "authored-H":
        net = replace(net, atoms=(replace(net.atoms[0], element="H"), net.atoms[1]))
    elif fault == "sp3":
        net = replace(net, atoms=(replace(net.atoms[0], hyb="sp3"), net.atoms[1]))
    else:
        net = replace(net, rings=((0, 1, 2),))
    with pytest.raises(_TerminationError):
        scope = _resolve_termination_scope(
            net,
            requested=_Request(False, None),
            parent_modes=("H", "H"),
            exposure=exposure(net) if fault != "dangling" else None,
        )
        _finalize_termination(net, xyz, scope=scope)


def snapshot_from_block(block: Any) -> GeometrySnapshot:
    raw = {
        "ref_id": 7,
        "version": 1,
        "lattice": np.eye(3).tolist(),
        "pbc": [False] * 3,
        "atoms": [
            {
                "id": i + 10,
                "ref_id": 7,
                "label": f"a{i}",
                "element": el,
                "fa": float(block.coords[i, 0]),
                "fb": float(block.coords[i, 1]),
                "fc": float(block.coords[i, 2]),
                "fixed": 0,
                "magmom": None,
                "oxidation": None,
                "charge": None,
                "hybridization": block.hybridizations[i],
            }
            for i, el in enumerate(block.elements)
        ],
        "bonds": [
            {
                "id": k + 1000,
                "ref_id": 7,
                "i": i + 10,
                "j": j + 10,
                "image": [0, 0, 0],
                "bond_order": order,
                "kind": kind,
                "provenance": "fixture",
            }
            for k, (i, j, order, kind) in enumerate(block.bonds)
        ],
    }
    return geometry_snapshot(raw)


@pytest.fixture(scope="module")
def stored() -> tuple[Net, GeometrySnapshot, dict[str, Any], dict[str, Any]]:
    spec = "hexfold 0.2\nt: tube(8,8, len=4)\n"
    bare = build(spec)
    block = build_hexfold({"spec": spec})
    snapshot = snapshot_from_block(block)
    expected = {
        "kind": "stored",
        "geometry": copy.deepcopy(snapshot["identity"]),
        "graph_token": stored_graph_token(snapshot),
    }
    return bare, snapshot, generated_record("hexfold", block.topology), expected


def test_real_generated_caps_verified_not_tag_trusted(stored: Any) -> None:
    bare, snapshot, record, expected = stored
    before = copy.deepcopy(snapshot)
    parent = _verify_generated_caps(
        snapshot, expected=expected, bare=bare, generated=record
    )
    assert parent.requested == _Request(None, None)
    assert parent.effective == "H" and len(parent.caps) == 32
    assert snapshot == before
    record = {**record, "trusted": True, "producer": "magic"}
    record["terminated"] = {**record["terminated"], "count": 31}
    with pytest.raises(_TerminationError):
        _verify_generated_caps(snapshot, expected=expected, bare=bare, generated=record)


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "count",
        "negative",
        "bool",
        "host",
        "mode",
        "producer",
        "spec",
        "bond-length",
    ],
)
def test_metadata_refusals(stored: Any, fault: str) -> None:
    bare, snapshot, record, expected = stored
    record = copy.deepcopy(record)
    if fault == "missing":
        del record["terminated"]
    elif fault == "producer":
        record["generator"] = "unknown"
    elif fault == "spec":
        record["spec"] = "hexfold 0.2\nt: tube(9,9,len=4)\n"
    else:
        key, val = {
            "count": ("count", 31),
            "negative": ("count", -1),
            "bool": ("count", True),
            "host": ("hosts", []),
            "mode": ("mode", "bad"),
            "bond-length": ("bond_A", 2.0),
        }[fault]
        record["terminated"][key] = val
    with pytest.raises(_TerminationError):
        _verify_generated_caps(snapshot, expected=expected, bare=bare, generated=record)


def test_same_version_content_edit_and_forged_token_rejected(stored: Any) -> None:
    bare, snapshot, record, expected = stored
    edit = copy.deepcopy(snapshot)
    edit["bonds"][-1]["order"] = 2
    assert edit["identity"]["version"] == snapshot["identity"]["version"]
    with pytest.raises(_TerminationError, match="token"):
        _verify_generated_caps(edit, expected=expected, bare=bare, generated=record)
    with pytest.raises(_TerminationError, match="identity"):
        _verify_generated_caps(
            snapshot,
            expected={**expected, "graph_token": "old"},
            bare=bare,
            generated=record,
        )


def parents_from(stored: Any) -> tuple[_Parent, _Parent]:
    bare, snapshot, record, expected = stored
    p = _verify_generated_caps(snapshot, expected=expected, bare=bare, generated=record)
    row_to_ord = {a["row_id"]: i for i, a in enumerate(snapshot["atoms"])}
    second = snapshot_from_block(
        SimpleNamespace(
            coords=np.array([a["fractional"] for a in snapshot["atoms"]]) + [0.5, 0, 0],
            elements=[a["element"] for a in snapshot["atoms"]],
            hybridizations=[a["hybridization"] for a in snapshot["atoms"]],
            bonds=[
                (row_to_ord[b["i"]], row_to_ord[b["j"]], b["order"], b["kind"])
                for b in snapshot["bonds"]
            ],
        )
    )
    second_expected = {
        "kind": "stored",
        "geometry": second["identity"],
        "graph_token": stored_graph_token(second),
    }
    q = _verify_generated_caps(
        second, expected=second_expected, bare=bare, generated=record
    )
    return p, q


def mapped_union(
    parents: tuple[_Parent, _Parent],
) -> tuple[Net, np.ndarray, tuple[_ParentMap, _ParentMap]]:
    """Fixture-only complete disjoint union, derived from verified parent rows."""
    coords: list[tuple[float, ...]] = []
    bonds: list[tuple[int, int, int]] = []
    mappings = []
    for side, parent in enumerate(parents):
        offset = len(coords)
        translation = (0.0, 0.0, 40.0 * side)
        coords.extend(
            tuple(float(a + b) for a, b in zip(x, translation, strict=True))
            for x in parent.carbon.coordinates
        )
        bonds.extend((i + offset, j + offset, 1) for i, j, _, _ in parent.carbon.bonds)
        mappings.append(
            _ParentMap(
                _source_token(parent), tuple(range(offset, len(coords))), translation
            )
        )
    atoms = tuple(
        Atom(AtomPath("union", Site(i, 0, 0)), i, "C", "sp2", "union")
        for i in range(len(coords))
    )
    net = Net(atoms, tuple(bonds), (), (), (), Report(), Spec(), Lattice())
    return net, np.array(coords), (mappings[0], mappings[1])


@pytest.mark.parametrize(
    "fault",
    [
        "wrong-host",
        "swapped-hosts",
        "extra-bond",
        "missing-bond",
        "H-H",
        "C-tail",
        "carbon-order",
        "carbon-missing",
    ],
)
def test_actual_incidence_refusals_even_with_fresh_row_tokens(
    stored: Any, fault: str
) -> None:
    bare, snapshot, record, _ = stored
    row_ord = {a["row_id"]: i for i, a in enumerate(snapshot["atoms"])}
    block = SimpleNamespace(
        coords=np.array([a["fractional"] for a in snapshot["atoms"]], dtype=float),
        elements=[a["element"] for a in snapshot["atoms"]],
        hybridizations=[a["hybridization"] for a in snapshot["atoms"]],
        bonds=[
            (row_ord[b["i"]], row_ord[b["j"]], float(b["order"]), b["kind"])
            for b in snapshot["bonds"]
        ],
    )
    i, j, order, kind = block.bonds[-1]
    if fault == "wrong-host":
        block.bonds[-1] = (0, j, order, kind)
    elif fault == "swapped-hosts":
        oi, oj, oo, ok = block.bonds[-2]
        block.bonds[-2], block.bonds[-1] = (i, oj, oo, ok), (oi, j, order, kind)
    elif fault == "extra-bond":
        block.bonds.append((0, j, 1.0, "pairwise"))
    elif fault == "missing-bond":
        block.bonds.pop()
    elif fault == "H-H":
        block.bonds[-1] = (j - 1, j, 1.0, "pairwise")
    elif fault == "C-tail":
        block.elements[-1] = "C"
    elif fault == "carbon-order":
        ci, cj, _, ck = block.bonds[0]
        block.bonds[0] = (ci, cj, 1.0, ck)
    else:
        block.bonds.pop(0)
    changed = snapshot_from_block(block)
    fresh_identity = {
        "kind": "stored",
        "geometry": changed["identity"],
        "graph_token": stored_graph_token(changed),
    }
    with pytest.raises(_TerminationError):
        _verify_generated_caps(
            changed, expected=fresh_identity, bare=bare, generated=record
        )


@pytest.mark.parametrize("mode", ["none", "ports-open"])
def test_legacy_known_effective_preserves_unknown_request(mode: str) -> None:
    spec = "hexfold 0.2\nt: tube(8,8,len=4)\n"
    bare = build(spec)
    block = build_hexfold({"spec": spec, "terminate": mode})
    snap = snapshot_from_block(block)
    binding = {
        "kind": "stored",
        "geometry": snap["identity"],
        "graph_token": stored_graph_token(snap),
    }
    p = _verify_generated_caps(
        snap,
        expected=binding,
        bare=bare,
        generated=generated_record("hexfold", block.topology),
    )
    assert p.effective == mode and p.requested == _Request(None, None)
    assert not p.caps


def test_authored_H_kept_even_with_generated_zero_count() -> None:
    spec = "hexfold 0.2\nt: tube(8,8,len=4)\nterminate: t.* = H\n"
    bare = build(spec)
    block = build_hexfold({"spec": spec})
    snap = snapshot_from_block(block)
    before = copy.deepcopy(snap)
    record = generated_record("hexfold", block.topology)
    assert record["terminated"]["count"] == 0
    binding = {
        "kind": "stored",
        "geometry": snap["identity"],
        "graph_token": stored_graph_token(snap),
    }
    with pytest.raises(_TerminationError, match="carbon sp2"):
        _verify_generated_caps(snap, expected=binding, bare=bare, generated=record)
    assert snap == before


def test_encode_roundtrip_nested_replay_and_no_qualification(
    stored: Any,
) -> None:
    parents = parents_from(stored)
    net, xyz, mapping = mapped_union(parents)
    intent = exposure(net)
    scope = _resolve_termination_scope(
        net, requested=_Request(False, None), parent_modes=("H", "H"), exposure=intent
    )
    result = _finalize_termination(net, xyz, scope=scope)
    record = _encode_preparation(result, parents=parents, mapping=mapping)
    assert record["parents"][0]["requested"] == {"present": None, "mode": None}
    stored_cap = record["parents"][0]["verified_caps"][0]
    assert {"h_row_id", "host_row_id", "bond_row_id"} <= stored_cap.keys()
    assert "bond" not in stored_cap  # a real row ID is never called an ordinal
    assert set(record["ordered_caps"][0]) == {"h", "host", "bond"}
    assert record["requested"] == {"present": False, "mode": None}
    assert record["qualification"] is None
    assert not {"last_relax", "cache_origin", "run_id", "trusted"} & record.keys()
    encoded = dumps_wire(record)
    replayed, child = _replay_preparation(
        encoded,
        net=net,
        coordinates=xyz,
        parents=parents,
        exposure=intent,
        mapping=mapping,
    )
    assert replayed == result
    assert child.binding["kind"] == "artifact" and "geometry" not in child.binding
    nested_parents = (child, parents[1])
    outer_net, outer_xyz, outer_mapping = mapped_union(nested_parents)
    outer_intent = exposure(outer_net)
    outer_scope = _resolve_termination_scope(
        outer_net,
        requested=_Request(False, None),
        parent_modes=("H", "H"),
        exposure=outer_intent,
    )
    outer_result = _finalize_termination(outer_net, outer_xyz, scope=outer_scope)
    outer = _encode_preparation(
        outer_result, parents=nested_parents, mapping=outer_mapping
    )
    assert len(outer_result.coordinates) > len(result.coordinates)
    assert set(outer["parents"][0]["verified_caps"][0]) == {"h", "host", "bond"}
    assert outer["parents"][0]["history"] == loads_wire(encoded)
    _, grandchild = _replay_preparation(
        dumps_wire(outer),
        net=outer_net,
        coordinates=outer_xyz,
        parents=nested_parents,
        exposure=outer_intent,
        mapping=outer_mapping,
    )
    assert grandchild.history is not None
    assert loads_wire(grandchild.history)["parents"][0]["history"] == loads_wire(
        encoded
    )
    # A validated inner artifact cannot be replaced with another source graph,
    # even while retaining its genuine binding and the old mapping.
    altered = replace(child, carbon=replace(child.carbon, bonds=child.carbon.bonds[1:]))
    with pytest.raises(_TerminationError, match="mapping source"):
        _replay_preparation(
            dumps_wire(outer),
            net=outer_net,
            coordinates=outer_xyz,
            parents=(altered, parents[1]),
            exposure=outer_intent,
            mapping=outer_mapping,
        )


@pytest.mark.parametrize(
    "change",
    [
        "cap-host",
        "count",
        "precap",
        "parent",
        "requested_mode",
        "trusted",
        "force",
        "policy",
        "extra",
        "lineage",
        "diagnostic-threshold",
        "diagnostic-method",
    ],
)
def test_replay_rederives_every_field(stored: Any, change: str) -> None:
    parents = parents_from(stored)
    net, xyz, mapping = mapped_union(parents)
    intent = exposure(net)
    scope = _resolve_termination_scope(
        net, requested=_Request(False, None), parent_modes=("H", "H"), exposure=intent
    )
    record = _encode_preparation(
        _finalize_termination(net, xyz, scope=scope), parents=parents, mapping=mapping
    )
    if change == "cap-host":
        record["ordered_caps"][0]["host"] = 1
    elif change == "count":
        record["count"] = 1
    elif change == "precap":
        record["precap_artifact"] = {
            "format": 1,
            "ref_id": 1,
            "version": 1,
            "token": "phantom",
        }
    elif change == "parent":
        record["parents"][0]["binding"]["geometry"]["version"] = 2
    elif change == "requested_mode":
        record["requested"] = {"present": None, "mode": None}
    elif change == "policy":
        record["policy"]["revision"] = 2
    elif change == "lineage":
        record["lineage"]["parents"][0]["translation_A"] = [1, 0, 0]
    elif change == "diagnostic-threshold":
        record["diagnostics"]["h_h_A"] += 0.01
    elif change == "diagnostic-method":
        record["diagnostics"]["exclusions"] = "two-hop"
    else:
        record[change] = True
    with pytest.raises(_TerminationError):
        _replay_preparation(
            dumps_wire(record),
            net=net,
            coordinates=xyz,
            parents=parents,
            exposure=intent,
            mapping=mapping,
        )


def test_replay_input_coordinate_or_exposure_change(stored: Any) -> None:
    parents = parents_from(stored)
    net, xyz, mapping = mapped_union(parents)
    intent = exposure(net)
    scope = _resolve_termination_scope(
        net, requested=_Request(False, None), parent_modes=("H", "H"), exposure=intent
    )
    encoded = dumps_wire(
        _encode_preparation(
            _finalize_termination(net, xyz, scope=scope),
            parents=parents,
            mapping=mapping,
        )
    )
    for coords, exp in ((xyz + [0, 0, 1], intent), (xyz, None)):
        with pytest.raises(_TerminationError):
            _replay_preparation(
                encoded,
                net=net,
                coordinates=coords,
                parents=parents,
                exposure=exp,
                mapping=mapping,
            )


def test_unrelated_unmapped_swapped_and_changed_composition_refused(
    graph: Any, stored: Any
) -> None:
    parents = parents_from(stored)
    net, xyz, mapping = mapped_union(parents)
    small_net, small_xyz = graph

    def prepare(n: Net, x: np.ndarray) -> Any:
        scope = _resolve_termination_scope(
            n,
            requested=_Request(False, None),
            parent_modes=("H", "H"),
            exposure=exposure(n),
        )
        return _finalize_termination(n, x, scope=scope)

    valid = prepare(net, xyz)
    with pytest.raises(_TerminationError, match="source mapping"):
        _encode_preparation(valid, parents=parents)
    with pytest.raises(_TerminationError):
        _encode_preparation(
            prepare(small_net, small_xyz), parents=parents, mapping=mapping
        )
    with pytest.raises(_TerminationError, match="mapping source"):
        _encode_preparation(valid, parents=(parents[1], parents[0]), mapping=mapping)
    with pytest.raises(_TerminationError, match="mapping source"):
        _encode_preparation(valid, parents=parents, mapping=(mapping[1], mapping[0]))
    # A carbon bond edit is not a supported operation in this revision.
    changed = replace(net, bonds=net.bonds[1:])
    with pytest.raises(_TerminationError, match="topology change"):
        _encode_preparation(prepare(changed, xyz), parents=parents, mapping=mapping)
    for invalid in [
        replace(mapping[0], atoms=(0,) * len(mapping[0].atoms)),
        replace(mapping[0], translation_A=(float("nan"), 0, 0)),
    ]:
        with pytest.raises(_TerminationError):
            _encode_preparation(valid, parents=parents, mapping=(invalid, mapping[1]))


def test_diagnostic_custom_policy_roundtrip_and_independent_binding(
    stored: Any,
) -> None:
    parents = parents_from(stored)
    net, xyz, mapping = mapped_union(parents)
    intent = exposure(net)
    scope = _resolve_termination_scope(
        net, requested=_Request(False, None), parent_modes=("H", "H"), exposure=intent
    )
    broad = replace(Profile.DEFAULT, clash_pair_A=(("H", "C", 2.5), ("H", "H", 2.5)))
    result = _finalize_termination(net, xyz, scope=scope, profile=broad)
    record = _encode_preparation(result, parents=parents, mapping=mapping)
    assert record["diagnostics"]["h_c_A"] == 2.5
    replayed, _ = _replay_preparation(
        dumps_wire(record),
        net=net,
        coordinates=xyz,
        parents=parents,
        exposure=intent,
        mapping=mapping,
        profile=broad,
    )
    assert replayed == result and result.contacts
    with pytest.raises(_TerminationError, match="replay evidence"):
        _replay_preparation(
            dumps_wire(record),
            net=net,
            coordinates=xyz,
            parents=parents,
            exposure=intent,
            mapping=mapping,
        )
    for threshold in [float("nan"), float("inf"), -1.0, 0.0]:
        with pytest.raises(_TerminationError, match="diagnostic thresholds"):
            _finalize_termination(
                net,
                xyz,
                scope=scope,
                profile=replace(Profile.DEFAULT, clash_pair_A=(("H", "C", threshold),)),
            )
