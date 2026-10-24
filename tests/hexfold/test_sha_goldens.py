"""SHA-256 goldens for every tube builder and SE generator block.

Slice 0 of docs/backlog/hexfold-multicell-prism.md: before the builders
are re-expressed as layouts over ``hexfold.sheetnet``, lock today's
outputs.  Per builder (``prism_tube`` +/- outlines with and without
pores, ``rail_tube``, ``channel_junction`` paired and unpaired,
``fin120.seam_tube``, ``y_junction.straight_y``,
``sp3_seam.straight_k4``/``straight_k3z``/``straight_k3a``/
``compose_sp3``, ``fin.fin_tube``) and per SE generator module's emitted
block, at two sizes each, the goldens record named digests of

- ``seed3``: the seed coordinates canonicalised to a 1e-6 Å grid,
  packed float64 (``block``: stored coords after termination and
  canonical framing),
- ``bonds`` / ``rings``: sorted,
- ``paths``: ``AtomPath`` name -> member ordinals,
- ``regions`` in declared order,
- ``pinned``: the composite's pinned registration zone,
- ``sites`` / ``junctions`` / ``seam_atoms`` / ``terminated``: the block
  topology records, canonical JSON with floats on a 1e-6 grid.

A mismatch names the entry and the field that moved.  When a change is
intended (never inside slice 1, which is byte-identical by contract)
regenerate with ``PRECIS_UPDATE_HEXFOLD_GOLDENS=1`` set in the test
process -- the env var is in ``scripts/test``'s forwarded list -- and
list the change in the commit body.

One platform note on the digests: ``seed3`` / ``coords`` / floats
inside the topology records sit on a 1e-6 grid — libm
``sin``/``cos`` differ by 1-2 ulp between the aarch64 dev container
and x86 CI, and raw float64 bytes red the gate on noise. The
termination decisions behind them are margined at
``hexfold_spec._CAP_EPS_A`` (gr512667), so a borderline distance
counts as under the bar on every platform and no ``<field>.alt``
digests are needed.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from hexfold.build import Net
from hexfold.channel import channel_junction
from hexfold.fin import fin_tube
from hexfold.fin120 import seam_tube
from hexfold.prism import prism_tube
from hexfold.railtube import rail_tube
from hexfold.sp3_seam import (
    _seam_net,
    compose_sp3,
    straight_k3a,
    straight_k3z,
    straight_k4,
)
from hexfold.y_junction import _sheet, straight_y
from precis_se.atomic.generators._types import GeneratedBlock
from precis_se.atomic.generators.channel import build_channel
from precis_se.atomic.generators.fin import build_fin
from precis_se.atomic.generators.fin120 import build_fin120
from precis_se.atomic.generators.prism120 import build_prism120
from precis_se.atomic.generators.rail120 import build_rail120
from precis_se.atomic.generators.sp3_seam import build_sp3_seam
from precis_se.atomic.generators.y_junction import build_y_junction

_GOLDEN = Path(__file__).parent / "data" / "goldens" / "sha256.json"
_UPDATE = bool(os.environ.get("PRECIS_UPDATE_HEXFOLD_GOLDENS"))
_FORMAT = "hexfold-sha256-goldens/1"
_REGENERATE = (
    "PRECIS_UPDATE_HEXFOLD_GOLDENS=1 scripts/test tests/hexfold/test_sha_goldens.py"
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_coords(arr: Any) -> str:
    """Digest of coordinates, canonicalised to a 1e-6 Å grid first.

    Transcendentals (``sin``/``cos``) differ by 1-2 ulp between the
    aarch64 dev container and x86 CI libm, so raw float64 bytes redden
    the gate on platform noise alone.  A 1e-6 Å fixed point absorbs
    that while still catching any real drift.  ``+ 0.0`` normalises
    ``-0.0``.
    """
    return _sha((np.round(np.asarray(arr, dtype="<f8"), 6) + 0.0).tobytes())


def _sha_lines(lines: Any) -> str:
    return _sha("\n".join(str(line) for line in lines).encode())


def _canon_floats(value: Any) -> Any:
    """Round every float in a topology record to a 1e-6 grid.

    ``sites``/``junctions`` records carry positions and angles whose last
    bits are libm noise across arches — same fix as ``_sha_coords``.
    ``+ 0.0`` normalises ``-0.0``.
    """
    if isinstance(value, float):
        return float(round(value, 6)) + 0.0
    if isinstance(value, dict):
        return {k: _canon_floats(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canon_floats(v) for v in value]
    if isinstance(value, np.integer):
        return int(value)
    return value


def _sha_json(value: Any) -> str:
    return _sha(
        json.dumps(
            _canon_floats(value), sort_keys=True, default=str, separators=(",", ":")
        ).encode()
    )


def _net_shas(net: Net, composite: object) -> dict[str, str]:
    """The named digests of a builder ``(net, composite)`` pair."""
    assert net.seed3 is not None
    paths: dict[str, list[int]] = {}
    for atom in net.atoms:
        paths.setdefault(str(atom.path), []).append(atom.ord)
    return {
        "seed3": _sha_coords(net.seed3),
        "bonds": _sha_lines(
            f"{i},{j},{o}"
            for i, j, o in sorted((min(i, j), max(i, j), o) for i, j, o in net.bonds)
        ),
        "rings": _sha_lines(
            ",".join(map(str, ring))
            for ring in sorted(tuple(sorted(r)) for r in net.rings)
        ),
        "paths": _sha_lines(
            f"{p}={','.join(map(str, sorted(ords)))}"
            for p, ords in sorted(paths.items())
        ),
        "regions": _sha_lines(
            f"{name}={','.join(map(str, ords))}" for name, ords in net.regions
        ),
        "pinned": _sha_lines(sorted(getattr(composite, "pinned", ()) or ())),
    }


def _block_shas(block: GeneratedBlock) -> dict[str, str]:
    """The named digests of an SE generator's emitted block."""
    shas = {
        "elements": _sha_lines(block.elements),
        "coords": _sha_coords(block.coords),
        "bonds": _sha_lines(f"{i},{j},{o},{k}" for i, j, o, k in sorted(block.bonds)),
    }
    for key in ("sites", "junctions", "seam_atoms", "terminated"):
        if key in block.topology:
            shas[key] = _sha_json(block.topology[key])
    return shas


def _compose_k3z() -> tuple[Net, object]:
    """``compose_sp3`` direct: three ``_sheet(6, 5)`` zigzag sheets."""
    block, rim = _sheet(6, 5)
    result = compose_sp3(
        (block, block, block),
        (rim, rim, rim),
        seam_type="k3-sp3-z",
        dihedrals_deg=(120, 120, 120),
    )
    return _seam_net(3, result), result


#: ``id -> (call, thunk)`` for the builder net goldens, two sizes each.
_NET_CASES: dict[str, tuple[str, Callable[[], tuple[Net, object]]]] = {
    "prism/a/plus6": (
        "prism_tube(8, 6, 4, '++++++')",
        lambda: prism_tube(8, 6, 4, "++++++"),
    ),
    "prism/a/mixed12": (
        "prism_tube(8, 6, 4, '+++-+++-+++-')",
        lambda: prism_tube(8, 6, 4, "+++-+++-+++-"),
    ),
    "prism/a/pores": (
        "prism_tube(8, 6, 4, '++++++', pores=[(0,1,2),(3,2,2)])",
        lambda: prism_tube(8, 6, 4, "++++++", pores=[(0, 1, 2), (3, 2, 2)]),
    ),
    "prism/b/plus6": (
        "prism_tube(4, 6, 2, '++++++')",
        lambda: prism_tube(4, 6, 2, "++++++"),
    ),
    "prism/b/mixed12": (
        "prism_tube(4, 6, 2, '+++-+++-+++-')",
        lambda: prism_tube(4, 6, 2, "+++-+++-+++-"),
    ),
    "prism/b/pores": (
        "prism_tube(4, 6, 2, '++++++', pores=[(0,1,2)])",
        lambda: prism_tube(4, 6, 2, "++++++", pores=[(0, 1, 2)]),
    ),
    "rail/a/plain": (
        "rail_tube(6, 10, 3, 3)",
        lambda: rail_tube(6, 10, 3, 3),
    ),
    "rail/a/pores": (
        "rail_tube(6, 10, 3, 3, pores=[(0,2,4),(1,1,5)])",
        lambda: rail_tube(6, 10, 3, 3, pores=[(0, 2, 4), (1, 1, 5)]),
    ),
    "rail/b/plain": (
        "rail_tube(4, 14, 2, 2)",
        lambda: rail_tube(4, 14, 2, 2),
    ),
    "rail/b/pores": (
        "rail_tube(4, 14, 2, 2, pores=[(0,1,2)])",
        lambda: rail_tube(4, 14, 2, 2, pores=[(0, 1, 2)]),
    ),
    "channel/a/paired": (
        "channel_junction(6, 10, 3, ('in','out','out'), paired=True)",
        lambda: channel_junction(6, 10, 3, ("in", "out", "out"), paired=True),
    ),
    "channel/a/unpaired": (
        "channel_junction(6, 10, 3, ('in','out','out'), paired=False)",
        lambda: channel_junction(6, 10, 3, ("in", "out", "out"), paired=False),
    ),
    "channel/b/paired": (
        "channel_junction(4, 8, 2, ('out',)*4, paired=True)",
        lambda: channel_junction(4, 8, 2, ("out", "out", "out", "out"), paired=True),
    ),
    "channel/b/unpaired": (
        "channel_junction(4, 8, 2, ('out',)*4, paired=False)",
        lambda: channel_junction(4, 8, 2, ("out", "out", "out", "out"), paired=False),
    ),
    "fin120/a/out": (
        "seam_tube(6, 10, 3, 'out')",
        lambda: seam_tube(6, 10, 3, "out"),
    ),
    "fin120/b/in": (
        "seam_tube(6, 18, 3, 'in')",
        lambda: seam_tube(6, 18, 3, "in"),
    ),
    "y/a": ("straight_y(6, 4)", lambda: straight_y(6, 4)),
    "y/b": ("straight_y(4, 3)", lambda: straight_y(4, 3)),
    "sp3/k4/a": ("straight_k4(4, 5)", lambda: straight_k4(4, 5)),
    "sp3/k4/b": ("straight_k4(6, 4)", lambda: straight_k4(6, 4)),
    "sp3/k3z/a": ("straight_k3z(4, 4)", lambda: straight_k3z(4, 4)),
    "sp3/k3z/b": ("straight_k3z(6, 3)", lambda: straight_k3z(6, 3)),
    "sp3/k3a/a": ("straight_k3a(5, 4)", lambda: straight_k3a(5, 4)),
    "sp3/k3a/b": ("straight_k3a(4, 3)", lambda: straight_k3a(4, 3)),
    "sp3/compose": (
        "compose_sp3(_sheet(6,5)x3, 'k3-sp3-z') -> _seam_net",
        _compose_k3z,
    ),
    "fin/a/out": ("fin_tube(6, 4, 3, 'out')", lambda: fin_tube(6, 4, 3, "out")),
    "fin/b/in": ("fin_tube(16, 4, 4, 'in')", lambda: fin_tube(16, 4, 4, "in")),
}


def _feature(ftype: str, **kw: Any) -> dict[str, Any]:
    return {"name": "g", "type": ftype, **kw}


#: ``id -> (call, params, build)`` for the SE generator block goldens,
#: two sizes each.
_BLOCK_CASES: dict[str, tuple[str, dict[str, Any], Any]] = {
    "se/prism/a": (
        "build_prism120(prism-k3-120-z rows=6 periods=8 strip_rows=4 '++++++')",
        {
            "features": [
                _feature(
                    "prism-k3-120-z",
                    rows=6,
                    periods=8,
                    strip_rows=4,
                    seams="++++++",
                )
            ]
        },
        build_prism120,
    ),
    "se/prism/b": (
        "build_prism120(prism-k3-120-z rows=6 periods=4 strip_rows=2 '+++-+++-+++-' pores)",
        {
            "features": [
                _feature(
                    "prism-k3-120-z",
                    rows=6,
                    periods=4,
                    strip_rows=2,
                    seams="+++-+++-+++-",
                    pores=[[0, 1, 2]],
                )
            ]
        },
        build_prism120,
    ),
    "se/rail/a": (
        "build_rail120(rail-k3-120-z tube=[10,6] rails=3 rows=3)",
        {
            "tube": [10, 6],
            "features": [_feature("rail-k3-120-z", rails=3, rows=3)],
        },
        build_rail120,
    ),
    "se/rail/b": (
        "build_rail120(rail-k3-120-z tube=[14,4] rails=2 rows=2 pores)",
        {
            "tube": [14, 4],
            "features": [
                _feature(
                    "rail-k3-120-z",
                    rails=2,
                    rows=2,
                    pores=[{"arc": 0, "at": [1, 2]}],
                )
            ],
        },
        build_rail120,
    ),
    "se/channel/a": (
        "build_channel(chan-k3-sp3-z tube=[10,6] sides=[in,out,out] rows=3)",
        {
            "tube": [10, 6],
            "features": [_feature("chan-k3-sp3-z", sides=["in", "out", "out"], rows=3)],
        },
        build_channel,
    ),
    "se/channel/b": (
        "build_channel(chan-k3-sp3-z tube=[8,4] sides=[out,out] rows=2)",
        {
            "tube": [8, 4],
            "features": [_feature("chan-k3-sp3-z", sides=["out", "out"], rows=2)],
        },
        build_channel,
    ),
    "se/fin/a": (
        "build_fin(fin-sp3-z tube=[6,4] side=out rows=3)",
        {
            "tube": [6, 4],
            "features": [_feature("fin-sp3-z", side="out", rows=3)],
        },
        build_fin,
    ),
    "se/fin/b": (
        "build_fin(fin-sp3-z tube=[16,4] side=in rows=4)",
        {
            "tube": [16, 4],
            "features": [_feature("fin-sp3-z", side="in", rows=4)],
        },
        build_fin,
    ),
    "se/fin120/a": (
        "build_fin120(fin-k3-120-z tube=[10,6] side=out rows=3)",
        {
            "tube": [10, 6],
            "features": [_feature("fin-k3-120-z", side="out", rows=3)],
        },
        build_fin120,
    ),
    "se/fin120/b": (
        "build_fin120(fin-k3-120-z tube=[18,6] side=in rows=3)",
        {
            "tube": [18, 6],
            "features": [_feature("fin-k3-120-z", side="in", rows=3)],
        },
        build_fin120,
    ),
    "se/y/a": (
        "build_y_junction(k3-sp2-120-z sheet=[6,4])",
        {
            "sheet": [6, 4],
            "features": [_feature("k3-sp2-120-z", dihedrals_deg=[120, 120, 120])],
        },
        build_y_junction,
    ),
    "se/y/b": (
        "build_y_junction(k3-sp2-120-z sheet=[4,3])",
        {
            "sheet": [4, 3],
            "features": [_feature("k3-sp2-120-z", dihedrals_deg=[120, 120, 120])],
        },
        build_y_junction,
    ),
    "se/sp3/k4/a": (
        "build_sp3_seam(k4-sp3-90-a sheet=[4,5])",
        {
            "sheet": [4, 5],
            "features": [_feature("k4-sp3-90-a", dihedrals_deg=[90, 90, 90, 90])],
        },
        build_sp3_seam,
    ),
    "se/sp3/k4/b": (
        "build_sp3_seam(k4-sp3-90-a sheet=[6,4])",
        {
            "sheet": [6, 4],
            "features": [_feature("k4-sp3-90-a", dihedrals_deg=[90, 90, 90, 90])],
        },
        build_sp3_seam,
    ),
    "se/sp3/k3z/a": (
        "build_sp3_seam(k3-sp3-z sheet=[4,4])",
        {
            "sheet": [4, 4],
            "features": [_feature("k3-sp3-z", dihedrals_deg=[120] * 3)],
        },
        build_sp3_seam,
    ),
    "se/sp3/k3z/b": (
        "build_sp3_seam(k3-sp3-z sheet=[6,3])",
        {
            "sheet": [6, 3],
            "features": [_feature("k3-sp3-z", dihedrals_deg=[120] * 3)],
        },
        build_sp3_seam,
    ),
    "se/sp3/k3a/a": (
        "build_sp3_seam(k3-sp3-a sheet=[5,4])",
        {
            "sheet": [5, 4],
            "features": [_feature("k3-sp3-a", dihedrals_deg=[120] * 3)],
        },
        build_sp3_seam,
    ),
    "se/sp3/k3a/b": (
        "build_sp3_seam(k3-sp3-a sheet=[4,3])",
        {
            "sheet": [4, 3],
            "features": [_feature("k3-sp3-a", dihedrals_deg=[120] * 3)],
        },
        build_sp3_seam,
    ),
}


def _net_entries() -> dict[str, dict[str, Any]]:
    out = {}
    for key, (call, thunk) in _NET_CASES.items():
        net, composite = thunk()
        out[key] = {
            "call": call,
            "atoms": len(net.atoms),
            "sha256": _net_shas(net, composite),
        }
    return out


def _block_entries() -> dict[str, dict[str, Any]]:
    out = {}
    for key, (call, params, build) in _BLOCK_CASES.items():
        block = build(params)
        out[key] = {
            "call": call,
            "atoms": len(block.elements),
            "sha256": _block_shas(block),
        }
    return out


def _read() -> dict[str, Any]:
    return json.loads(_GOLDEN.read_text(encoding="utf-8"))


def _write(entries: dict[str, dict[str, Any]]) -> None:
    _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    _GOLDEN.write_text(
        json.dumps(
            {
                "format": _FORMAT,
                "regenerate": _REGENERATE,
                "entries": entries,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _check(entries: dict[str, dict[str, Any]]) -> None:
    gold = _read()
    assert gold["format"] == _FORMAT
    missing = set(entries) - set(gold["entries"])
    assert not missing, f"no golden for {sorted(missing)}; {_REGENERATE}"
    bad: list[str] = []
    for key, entry in sorted(entries.items()):
        want = gold["entries"][key]
        if want["atoms"] != entry["atoms"]:
            bad.append(f"{key}: atom count {want['atoms']} -> {entry['atoms']}")
        for field, sha in sorted(entry["sha256"].items()):
            if sha != want["sha256"].get(field):
                bad.append(f"{key}: {field} moved to {sha} ({entry['call']})")
        if set(want["sha256"]) != set(entry["sha256"]):
            bad.append(
                f"{key}: golden fields {sorted(want['sha256'])} vs "
                f"{sorted(entry['sha256'])}"
            )
    assert not bad, "\n".join(bad + ["", f"if intended, regenerate with {_REGENERATE}"])


def test_builder_sha_goldens() -> None:
    """Every builder net is byte-identical to its slice-0 golden."""
    entries = _net_entries()
    if _UPDATE:
        gold = _read() if _GOLDEN.is_file() else {"entries": {}}
        gold["entries"].update(entries)
        _write(gold["entries"])
        pytest.skip("goldens regenerated")
    _check(entries)


def test_se_block_sha_goldens() -> None:
    """Every SE generator's emitted block is byte-identical."""
    entries = _block_entries()
    if _UPDATE:
        gold = _read() if _GOLDEN.is_file() else {"entries": {}}
        gold["entries"].update(entries)
        _write(gold["entries"])
        pytest.skip("goldens regenerated")
    _check(entries)
