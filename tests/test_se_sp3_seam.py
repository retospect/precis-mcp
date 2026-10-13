"""sp3 seam scene rows: admission, analytic graph and real test-DB tethered mint."""

from __future__ import annotations

import copy
from typing import Any

import numpy as np
import pytest

from hexfold.sp3_seam import straight_k3z, straight_k4
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_se import persist
from precis_se.atomic.generators import GeneratorError
from precis_se.atomic.generators.hexfold_scene import build_hexfold_scene
from precis_se.handler import SeHandler

ROWS = {
    "k4-sp3-90-a": ([4, 4], [90, 90, 90, 90], straight_k4),
    "k3-sp3-z": ([4, 4], [120, 120, 120], straight_k3z),
}


def params(kind: str = "k4-sp3-90-a", sheet: list[int] | None = None) -> dict[str, Any]:
    sh, dh, _build = ROWS[kind]
    return {
        "sheet": list(sheet or sh),
        "features": [{"name": "s", "type": kind, "dihedrals_deg": list(dh)}],
        "k_tether": 1.0,
    }


def _pinned_set(net, seam_atoms) -> set[int]:
    seam_set = set(seam_atoms)
    pinned = set(seam_set)
    for i, j, _ in net.bonds:
        if i in seam_set:
            pinned.add(j)
        if j in seam_set:
            pinned.add(i)
    return pinned


def _open_rim_slots(elements, bonds) -> int:
    """Expected H caps: every carbon's missing valence (3 for sp2; the
    seam atoms are degree four by construction, so nothing is short of
    its sp3 valence)."""
    deg = np.bincount(
        [v for i, j, *_ in bonds for v in (i, j)], minlength=len(elements)
    )
    return int(sum(max(0, 3 - deg[i]) for i, e in enumerate(elements) if e == "C"))


@pytest.mark.parametrize("kind", list(ROWS))
def test_public_sp3_seam_route_tethered_test_db_roundtrip(
    store: Store, hub_no_embedder: Hub, kind: str
) -> None:
    sheet, dh, build = ROWS[kind]
    p = params(kind)
    slug = f"hexfold-sp3-seam-test-{kind}"
    op = {
        "op": "generate",
        "generator": "hexfold_scene",
        "name": "s",
        "params": p,
    }
    handler = SeHandler(hub=hub_no_embedder)
    before = copy.deepcopy(op)
    put = handler.put(id=slug, args={"ops": [op]}).body
    assert op == before
    se_ref = store.get_ref(kind="se", id=slug)
    assert se_ref is not None
    tree = persist.load_tree(store, se_ref.id)
    node = tree.blocks["s"]
    assert node.bound_kind == "structure" and node.bound is not None
    ref = store.get_ref(kind="structure", id=node.bound)
    assert ref is not None
    record = (ref.meta or {})["generated"]
    assert record["scene"] == p
    assert record["plan"]["passes"] == 1 and record["plan"]["relax"] == "tethered"
    assert "spec" not in record and "surface_target" not in record
    findings = record["report"]["findings"]
    assert not [f for f in findings if f["code"] in ("geom.clash", "geom.seed_overlap")]
    seam = next(f["data"] for f in findings if f["code"] == "seam.geometry")
    assert seam["degree"] == 4 and seam["hybridisation"] == "sp3"
    assert len(seam["angles_deg"]) == sheet[0]
    for table in seam["angles_deg"].values():
        assert len(table) == 6
    # the bonded rim atoms' angles are reported too: for k3-z they carry
    # the row's tilt residual, pinned at the authored seed registration
    assert len(seam["rim_angles_deg"]) == sheet[0] * (4 if kind == "k4-sp3-90-a" else 3)
    residuals = {
        dict(f["data"])["sheet"]: dict(f["data"])["residual"]
        for f in findings
        if f["code"] == "euler.residual"
    }
    assert len(residuals) == (4 if kind == "k4-sp3-90-a" else 3)
    assert all(v == 0 for v in residuals.values())

    # pinned seam atoms + neighbours keep the analytic registration
    seed_net, seed = build(*sheet)
    pinned = _pinned_set(seed_net, seed.seam_atoms)
    snap = store.structure_positions_snapshot(ref.id)
    assert snap is not None
    relaxed = np.asarray(snap["fractional"]) @ np.asarray(snap["lattice"])
    keep = sorted(pinned)

    def pair_distances(xyz: np.ndarray) -> np.ndarray:
        return np.linalg.norm(xyz[:, None] - xyz[None], axis=-1)

    assert pair_distances(relaxed[keep]) == pytest.approx(
        pair_distances(seed.coords[keep]), abs=1e-6
    )

    # every carbon short of valence got one cap per missing bond, minus
    # the caps dropped for landing inside another atom's 1.0 A bar on the
    # converging seam rims: a bare edge is honest, an overlapping H is not
    direct = build_hexfold_scene(p)
    n_c = sum(1 for e in direct.elements if e == "C")
    n_h = sum(1 for e in direct.elements if e == "H")
    carbon_bonds = [(i, j) for i, j, *_ in direct.bonds if i < n_c and j < n_c]
    assert direct.bonds and len(direct.bonds[0]) >= 2
    dropped = record["terminated"]["dropped"]
    assert dropped > 0
    assert n_h == _open_rim_slots(direct.elements[:n_c], carbon_bonds) - dropped
    assert record["terminated"]["count"] == n_h
    assert any(f["code"] == "terminate.dropped" for f in findings)
    # no minted non-bonded pair involving H sits under 1.0 A
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
    block = handler.get(id=slug, view="block", args={"name": "s"}).body
    assert "seam.rings" in block and "geom.summary" in block and "tethered" in block
    unknown = handler.get(id=slug, view="surface_deviation", args={"name": "s"}).body
    assert "unknown: no stored or spec-derivable authored target" in unknown


@pytest.mark.parametrize(
    "kind, change",
    [
        ("k4-sp3-90-a", {"sheet": [True, 4]}),
        ("k4-sp3-90-a", {"sheet": [31, 30]}),
        ("k4-sp3-90-a", {"sheet": [4.0, 4]}),
        # k4's armchair sheet needs >= 3 atom levels to carry a hexagon
        ("k4-sp3-90-a", {"sheet": [4, 2]}),
        ("k4-sp3-90-a", {"extra": "do not mix"}),
        ("k4-sp3-90-a", {"k_tether": 0}),
        ("k4-sp3-90-a", {"k_tether": float("nan")}),
        (
            "k4-sp3-90-a",
            {
                "features": [
                    {"name": "s", "type": "k4-sp3-90-a", "dihedrals_deg": [90, 90, 90]}
                ]
            },
        ),
        (
            "k4-sp3-90-a",
            {
                "features": [
                    {
                        "name": "s",
                        "type": "k4-sp3-90-a",
                        "dihedrals_deg": [90, 90, 90, 91],
                    }
                ]
            },
        ),
        ("k4-sp3-90-a", {"features": [{"name": "s", "type": "k4-sp3-90-a"}]}),
        ("k4-sp3-90-a", {"features": [{"name": "s", "type": "k5-sp3"}]}),
        # k3-z with unequal dihedrals refuses before any build
        (
            "k3-sp3-z",
            {
                "features": [
                    {
                        "name": "s",
                        "type": "k3-sp3-z",
                        "dihedrals_deg": [120, 120, 121],
                    }
                ]
            },
        ),
        (
            "k4-sp3-90-a",
            {
                "features": [
                    {"name": "s", "type": "k4-sp3-90-a", "dihedrals_deg": [90] * 4},
                    {"name": "foot"},
                ]
            },
        ),
    ],
)
def test_sp3_seam_invalid_input_refuses_before_build_or_relax(
    monkeypatch: pytest.MonkeyPatch, kind: str, change: dict[str, Any]
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("invalid sp3 seam reached build/relax")

    monkeypatch.setattr("precis_se.atomic.generators.sp3_seam.straight_k4", forbidden)
    monkeypatch.setattr("precis_se.atomic.generators.sp3_seam.straight_k3z", forbidden)
    monkeypatch.setattr(
        "precis_se.atomic.generators.sp3_seam.stick_relax_pinned", forbidden
    )
    with pytest.raises(GeneratorError):
        build_hexfold_scene({**params(kind), **change})


def test_k3z_odd_periods_refuse_without_persistence(
    store: Store, hub_no_embedder: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("odd-period k3-sp3-z crossed build/persistence boundary")

    monkeypatch.setattr("precis_se.atomic.generators.sp3_seam.straight_k3z", forbidden)
    monkeypatch.setattr(type(store), "insert_ref", forbidden)
    bad = params("k3-sp3-z")
    bad["sheet"] = [5, 4]
    slug = "hexfold-sp3-seam-odd-refusal"
    assert store.get_ref(kind="se", id=slug) is None
    with pytest.raises(BadInput, match="fit.unsolvable"):
        SeHandler(hub=hub_no_embedder).put(
            id=slug,
            args={
                "ops": [
                    {
                        "op": "generate",
                        "generator": "hexfold_scene",
                        "name": "s",
                        "params": bad,
                    }
                ]
            },
        )
    assert store.get_ref(kind="se", id=slug) is None
    assert store.get_ref(kind="structure", id=f"{slug}-s") is None
