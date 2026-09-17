"""Canonical form (SPEC section 10) and the canonical JSON (section 14).

Two files describe the same structure iff their canonical JSON is
byte-equal.  The frame search enumerates (defect site -> origin, rotation r)
candidates inside the origin instance: C6 rotations for flat patches,
circumferential + axial translations only on a tube.  Mirrors are excluded.
Edge-words are stored as their lexicographically minimal rotation (Booth).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace

import numpy as np

from .build import Net, build
from .defects import expand_word, run_length
from .lattice import Lattice, Site, rotate_axial
from .text import Instance, Spec, spec_from_dict, to_text


def booth(seq: list[str]) -> int:
    """Index of the lexicographically minimal rotation of ``seq``."""
    n = len(seq)
    if n == 0:
        return 0
    s = seq + seq
    i, j, k = 0, 1, 0
    while i < n and j < n and k < n:
        a, b = s[i + k], s[j + k]
        if a == b:
            k += 1
            continue
        if a > b:
            i = i + k + 1
        else:
            j = j + k + 1
        if i == j:
            i += 1
        k = 0
    return min(i, j)


def lexmin_word(word: str) -> str:
    """Lex-min rotation of a run-length edge-word, re-run-lengthed."""
    syms = list(expand_word(word))
    if not syms:
        return word
    k = booth(syms)
    rot = syms[k:] + syms[:k]
    return run_length("".join(rot))


def _transform_site(s: Site, t: tuple[int, int], r: int) -> Site:
    u, v = s.u - t[0], s.v - t[1]
    u, v = rotate_axial(u, v, r)
    return Site(u, v, s.s)


def _frame_candidates(spec: Spec, inst: Instance) -> list[tuple[Site, int]]:
    """(anchor site, rotation) frames; tube: translations only (C_gcd and
    axial quotient is realised by anchoring each defect at the origin)."""
    anchors = [d.site for d in inst.defects] + [h.site for h in inst.holes]
    if not anchors:
        return []
    anchors = sorted(set(anchors), key=lambda s: (s.u, s.v, s.s))
    if inst.kind == "tube":
        return [(a, 0) for a in anchors]
    return [(a, r) for a in anchors for r in range(6)]


def _apply_frame(inst: Instance, anchor: Site, rot: int) -> Instance:
    t = (anchor.u, anchor.v)
    inst = replace(
        inst,
        defects=tuple(
            replace(
                d,
                site=_transform_site(d.site, t, rot),
                dir=(d.dir + rot) % 6,
            )
            for d in inst.defects
        ),
        holes=tuple(
            replace(h, site=_transform_site(h.site, t, rot)) for h in inst.holes
        ),
    )
    # canonicalise order of defects and holes
    inst = replace(
        inst,
        defects=tuple(sorted(inst.defects, key=lambda d: (str(d.site), d.kind, d.dir))),
        holes=tuple(sorted(inst.holes, key=lambda h: (str(h.site), h.ring))),
    )
    return inst


def _frame_serial(inst: Instance) -> str:
    payload = [(d.kind, str(d.site), d.dir) for d in inst.defects] + [
        ("hole", h.ring, str(h.site)) for h in inst.holes
    ]
    return json.dumps(sorted(payload))


def _sheet_cell_ring(cu: int, cv: int) -> set[Site]:
    """Vertices of the hexagon at cell (cu, cv) on a bounded sheet."""
    return {
        Site(cu, cv, 1),
        Site(cu, cv + 1, 0),
        Site(cu, cv + 1, 1),
        Site(cu + 1, cv, 0),
        Site(cu + 1, cv, 1),
        Site(cu + 1, cv + 1, 0),
    }


def _incident_cells(s: Site) -> list[tuple[int, int]]:
    """Cells of the (up to) three hexagons incident on a lattice site."""
    if s.s == 0:
        return [(s.u - 1, s.v), (s.u, s.v - 1), (s.u - 1, s.v - 1)]
    return [(s.u, s.v), (s.u, s.v - 1), (s.u - 1, s.v)]


def _holes_fit_sheet(inst: Instance) -> bool:
    """All authored hole sites are buildable inside a sheet(w, h).

    A translation/rotation frame that pushes a hole to the boundary is not
    a symmetry of a bounded sheet: the cluster's rings must all exist.
    """
    if inst.kind != "sheet":
        return True
    kv = dict(inst.params)
    w = int(kv.get("0", kv.get("w", "20")))
    h = int(kv.get("1", kv.get("h", "20")))

    def inb(s: Site) -> bool:
        return 0 <= s.u < w and 0 <= s.v < h

    lat = Lattice()
    for hole in inst.holes:
        cells = _incident_cells(hole.site)
        good = [
            (cu, cv)
            for cu, cv in cells
            if all(inb(v) for v in _sheet_cell_ring(cu, cv))
        ]
        if hole.dir is not None:
            p0 = lat.cart(hole.site)

            def cell_dir(cell: tuple[int, int], p0: np.ndarray = p0) -> int:
                cen = np.mean([lat.cart(v) for v in _sheet_cell_ring(*cell)], axis=0)
                dv = cen - p0
                return round(math.degrees(math.atan2(dv[1], dv[0])) / 60.0) % 6

            good = [c for c in good if cell_dir(c) == hole.dir % 6]
        if not good:
            return False
        centre = min(good)  # lex-min cell as build does
        if hole.ring <= -10:
            # hex(r): the r-radius cell cluster must fit entirely
            r = -10 - hole.ring
            for du in range(-r, r + 1):
                for dv in range(-r, r + 1):
                    if (abs(du) + abs(dv) + abs(du + dv)) // 2 > r:
                        continue
                    ring = _sheet_cell_ring(centre[0] + du, centre[1] + dv)
                    if not all(inb(v) for v in ring):
                        return False
    return True


def canonicalise(spec: Spec) -> Spec:
    """Return the spec with the origin instance in its canonical frame."""
    if spec.origin is None:
        return spec
    inst = spec.instance(spec.origin)
    if inst is None:
        return spec
    best = None
    best_inst = inst
    for anchor, rot in _frame_candidates(spec, inst):
        cand = _apply_frame(inst, anchor, rot)
        if not _holes_fit_sheet(cand):
            continue
        s = _frame_serial(cand)
        if best is None or s < best:
            best, best_inst = s, cand
    instances = tuple(best_inst if i.name == inst.name else i for i in spec.instances)
    return replace(spec, instances=instances)


def canonical_json(net_or_spec: Net | Spec | str) -> str:
    """The canonical JSON string: sorted keys, no floats, lex-min words,
    **authored sections only** (SPEC 14/18) -- never atoms, bonds, rings,
    the report, the content hash, or the generated cache. Two files
    describe the same structure iff this string is byte-equal."""
    if isinstance(net_or_spec, str):
        if net_or_spec.lstrip().startswith("{"):
            net_or_spec = spec_from_dict(json.loads(net_or_spec))
        else:
            from .text import parse

            net_or_spec = parse(net_or_spec)
    spec = net_or_spec.spec if isinstance(net_or_spec, Net) else net_or_spec
    canon_spec = canonicalise(spec)
    net = build(canon_spec, strict=False)
    d = net.authored_dict()
    # canonicalise edge-words
    for pdata in d["ports"].values():
        pdata["word"] = lexmin_word(pdata["word"])
    return json.dumps(d, sort_keys=True, separators=(",", ":")) + "\n"


def content_hash(net_or_spec: Net | Spec | str) -> str:
    """sha256 hex of :func:`canonical_json` -- the content hash that keys
    the ``generated`` section (SPEC 14.6) and the catalogue (SPEC 26)."""
    return hashlib.sha256(canonical_json(net_or_spec).encode("utf-8")).hexdigest()


def text_of(canonical_json_str: str) -> str:
    """Emit the text form of a canonical JSON document (round-trip leg)."""
    d = json.loads(canonical_json_str)
    spec = spec_from_dict(d)
    return to_text(spec)
