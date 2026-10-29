"""``sheetnet.decorate`` — interior H on a Net's sites (multicell slice 2,
built under molecular-workspace-code slice 2) and the ``geom.sp3.mismatch``
check that goes with it."""

from __future__ import annotations

from collections import Counter

import numpy as np
import pytest

from hexfold import sheetnet
from hexfold.build import Net, build
from hexfold.check import SP3_CC_A, Relaxed, _findings_from_net, _geometry_findings
from hexfold.report import Profile, Report
from hexfold.stick import stick
from precis_se.atomic.generators.hexfold_spec import (
    HYDROGENATED_TAG,
    TERMINATED_TAG,
    _block_from_net,
)

_SPEC = "hexfold 0.2\nlattice: element=C sigma=1.42\n\norigin s\ns: sheet(10,10)\n"


def _degrees(net: Net) -> Counter[int]:
    deg: Counter[int] = Counter()
    for i, j, _o in net.bonds:
        deg[i] += 1
        deg[j] += 1
    return deg


def _interior_ring(net: Net) -> tuple[int, ...]:
    deg = _degrees(net)
    return next(r for r in net.rings if len(r) == 6 and all(deg[a] == 3 for a in r))


def _euler(net: Net) -> list[tuple[str, str, object]]:
    return [
        (f.code, f.message, f.data)
        for f in _findings_from_net(net)
        if f.code.startswith("euler.")
    ]


@pytest.fixture(scope="module")
def sheet() -> Net:
    return build(_SPEC)


def test_decorated_carbons_have_four_bonds_and_one_h(sheet: Net) -> None:
    sites = list(_interior_ring(sheet)[:4])
    net = sheetnet.decorate(sheet, sites, [1, -1, 1, -1])
    deg = _degrees(net)
    assert len(net.atoms) == len(sheet.atoms) + 4
    for s in sites:
        assert net.atoms[s].hyb == "sp3"
        assert deg[s] == 4
        (h,) = [
            a
            for i, a, _o in ((i, j, o) for i, j, o in net.bonds if i == s)
            if net.atoms[a].element == "H"
        ]
        assert deg[h] == 1
    assert [d.host for d in net.decorations] == sites
    assert [d.face for d in net.decorations] == [1, -1, 1, -1]
    # authored atoms, bonds and rings keep their numbers
    assert net.atoms[: len(sheet.atoms)] != sheet.atoms  # the hosts changed hyb
    assert net.rings == sheet.rings
    assert net.bonds[: len(sheet.bonds)] == sheet.bonds


def test_h_sits_at_sigma_ch_on_the_chosen_side(sheet: Net) -> None:
    sites = list(_interior_ring(sheet)[:2])
    net = sheetnet.decorate(sheet, sites, [1, -1])
    xyz = np.asarray(net.seed3)
    for rec in net.decorations:
        v = xyz[rec.atom] - xyz[rec.host]
        assert abs(float(np.linalg.norm(v)) - net.lattice.sigma_CH_A) < 1e-9
        # the sheet is flat in z for this fixture: +1 is up (the default up=+z)
        assert np.sign(v[2]) == rec.face


def test_counting_residual_is_the_undecorated_nets(sheet: Net) -> None:
    ring = _interior_ring(sheet)
    before = _euler(sheet)
    assert before  # the census exists
    for sites in (ring[:5], ring):  # an open arc and a closed band
        assert _euler(sheetnet.decorate(sheet, sites, 1)) == before


def test_block_records_hydrogenated_beside_terminated(sheet: Net) -> None:
    sites = list(_interior_ring(sheet))
    plain = _block_from_net(
        sheet, stick(sheet), spec=_SPEC, report=Report(), fidelity="stick"
    )
    net = sheetnet.decorate(sheet, sites, [1, -1] * 3)
    block = _block_from_net(
        net, stick(net), spec=_SPEC, report=Report(), fidelity="stick"
    )
    hyd = block.topology["hydrogenated"]
    assert hyd["count"] == len(sites)
    assert hyd["sites"] == sites
    assert hyd["face"] == [1, -1] * 3
    assert "hydrogenated" not in plain.topology
    # the edge caps are the plain block's, not the decoration's
    for key in ("count", "hosts", "closed", "trimmed", "dropped", "bare"):
        assert block.topology["terminated"][key] == plain.topology["terminated"][key]
    assert HYDROGENATED_TAG in block.tags
    assert TERMINATED_TAG in block.tags
    assert HYDROGENATED_TAG not in plain.tags
    # decoration H are bonded C-H pairwise, never the sp2 aromatic order
    kinds = {
        k
        for (i, j, _o, k) in block.bonds
        if block.elements[i] == "H" or block.elements[j] == "H"
    }
    assert kinds == {"pairwise"}


def _sp3_codes(net: Net) -> list[str]:
    xyz = np.asarray(net.seed3, dtype=float)
    found = _geometry_findings(net, Profile.DEFAULT, Relaxed(xyz, 0.0, "seed"))
    return [f.code for f in found if f.code == "geom.sp3.mismatch"]


def test_sp3_mismatch_fires_on_a_closed_band_not_an_open_arc(sheet: Net) -> None:
    ring = _interior_ring(sheet)
    assert _sp3_codes(sheet) == []
    assert _sp3_codes(sheetnet.decorate(sheet, ring[:5], 1)) == []
    closed = sheetnet.decorate(sheet, ring, [1, -1] * 3)
    assert _sp3_codes(closed) == ["geom.sp3.mismatch"]
    (f,) = [
        f
        for f in _geometry_findings(
            closed, Profile.DEFAULT, Relaxed(np.asarray(closed.seed3), 0.0, "seed")
        )
        if f.code == "geom.sp3.mismatch"
    ]
    assert f.severity.name == "WARN"
    data = dict(f.data)
    assert data["sp3_atoms"] == 6 and data["loops"] == 1
    assert data["sp3_cc_A"] == SP3_CC_A
    assert data["mismatch"] == pytest.approx((SP3_CC_A - 1.42) / 1.42, abs=1e-3)


def test_decorated_valence_is_checked_as_four(sheet: Net) -> None:
    net = sheetnet.decorate(sheet, _interior_ring(sheet)[:3], 1)
    under = [f for f in _findings_from_net(net) if "decorated atom" in f.message]
    assert under == []
    # drop one decorated carbon's H bond: valence 3, not 4 -> a finding
    h = net.decorations[0].atom
    bonds = tuple(b for b in net.bonds if h not in b[:2])
    broken = Net(**{**net.__dict__, "bonds": bonds})
    (f,) = [f for f in _findings_from_net(broken) if "decorated atom" in f.message]
    assert f.code == "valence.under"


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"sites": [0, 0]}, "distinct"),
        ({"sites": [10**6]}, "not an atom ordinal"),
        ({"sites": [1], "element": "F"}, "H only"),
        ({"sites": [1], "face": 7}, "must be"),
    ],
)
def test_decorate_refusals(sheet: Net, kwargs: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        sheetnet.decorate(sheet, **kwargs)


def test_decorate_refuses_a_rim_atom_and_a_repeat(sheet: Net) -> None:
    deg = _degrees(sheet)
    rim = next(a.ord for a in sheet.atoms if deg[a.ord] == 2)
    with pytest.raises(ValueError, match="saturated sp2"):
        sheetnet.decorate(sheet, [rim])
    site = _interior_ring(sheet)[0]
    once = sheetnet.decorate(sheet, [site])
    with pytest.raises(ValueError, match="sp2 lattice carbon|already decorated"):
        sheetnet.decorate(once, [site])
