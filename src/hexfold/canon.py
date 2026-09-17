"""Canonical form (SPEC section 10) and the canonical JSON (section 14).

Two files describe the same structure iff their canonical JSON is
byte-equal.  The frame search enumerates (defect site -> origin, rotation r)
candidates inside the origin instance and keeps a frame only if it builds
the same structure (atoms, bonds, ring census, port sizes): on a bounded
sheet or tube a defect's offset from the rim is structural, and a
fullerene's site labels are not a lattice, so in 0.2 every instance keeps
its authored frame -- true symmetries (patch point groups, a tube's
C_gcd rotation, the cage's icosahedral group) are roadmap (SPEC 14.2
note).  Anchors are *authored* defects and holes only --
menu-generated holes (``Hole.source``) exist on a built net but not on
its unexpanded text, and the two must canonicalise alike.  A frame
re-expresses every site reference to the origin instance: its holes and
defects, and ``<inst>/(u,v,s)`` endpoints in connects and menu expansion
records.  Mirrors are excluded.  Edge-words are stored as their
lexicographically minimal rotation (Booth).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import replace
from typing import Any

import numpy as np

from .build import Net, build
from .defects import expand_word, run_length
from .lattice import Lattice, Site, rotate_axial
from .report import HexfoldError
from .text import Connect, Instance, Spec, spec_from_dict, to_text


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
    """(anchor site, rotation) frames over the *authored* defects and holes.

    Candidates only; :func:`canonicalise` keeps a frame solely when it
    builds the same structure (:func:`_structure_key`).  On a bounded
    instance a defect's offset from the rim is structural -- 0.1 anchored
    every defect at (0,0) regardless and dragged a tube's flank hole onto
    its ``in`` rim (found by the se dogfood) -- and a fullerene's site
    labels are not a lattice, so no candidate survives in 0.2; the search
    stays so a true symmetry (roadmap) slots in as a candidate source.
    """
    anchors = [d.site for d in inst.defects] + [
        h.site for h in inst.holes if h.source is None
    ]
    if not anchors:
        return []
    anchors = sorted(set(anchors), key=lambda s: (s.u, s.v, s.s))
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
    """Serial of the authored defects and holes in a candidate frame --
    generated holes are excluded so a built net and its unexpanded text
    rank the same frames the same way."""
    payload = [(d.kind, str(d.site), d.dir) for d in inst.defects] + [
        ("hole", h.ring, str(h.site)) for h in inst.holes if h.source is None
    ]
    return json.dumps(sorted(payload))


_SITE_REF = r"/\(\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*([ABab01])\s*\)"


def _site_ref_re(inst_name: str) -> re.Pattern[str]:
    """``<inst>/(u,v,s)`` references to lattice sites on ``inst_name`` --
    the endpoint form used by ``bond``, ``@`` and menu lines."""
    return re.compile(rf"(?<![\w.]){re.escape(inst_name)}{_SITE_REF}")


def _reframe_text(
    text: str, pat: re.Pattern[str], inst_name: str, t: tuple[int, int], rot: int
) -> str:
    def sub(m: re.Match[str]) -> str:
        site = _transform_site(Site.parse(f"({m[1]},{m[2]},{m[3]})"), t, rot)
        return f"{inst_name}/{site}"

    return pat.sub(sub, text)


def _reframe_record(
    obj: Any, pat: re.Pattern[str], inst_name: str, t: tuple[int, int], rot: int
) -> Any:
    if isinstance(obj, str):
        return _reframe_text(obj, pat, inst_name, t, rot)
    if isinstance(obj, list):
        return [_reframe_record(x, pat, inst_name, t, rot) for x in obj]
    if isinstance(obj, dict):
        return {k: _reframe_record(v, pat, inst_name, t, rot) for k, v in obj.items()}
    return obj


def _reframe_connect(
    c: Connect, pat: re.Pattern[str], inst_name: str, t: tuple[int, int], rot: int
) -> Connect:
    """``c`` with every site reference to ``inst_name`` re-expressed in the
    frame -- endpoints, and the menu expansion record (its connect lines
    use the same endpoint syntax; its ``holes[inst]`` list carries bare
    site strings next to non-site markers such as ``path3``)."""
    exp = c.expanded
    if exp is not None:
        exp = _reframe_record(exp, pat, inst_name, t, rot)
        holes = exp.get("holes")
        if isinstance(holes, dict) and isinstance(holes.get(inst_name), list):
            moved: list[str] = []
            for s in holes[inst_name]:
                try:
                    moved.append(str(_transform_site(Site.parse(s), t, rot)))
                except (ValueError, AttributeError, TypeError):
                    moved.append(s)
            exp = {**exp, "holes": {**holes, inst_name: moved}}
    return replace(
        c,
        src=_reframe_text(c.src, pat, inst_name, t, rot),
        dst=_reframe_text(c.dst, pat, inst_name, t, rot),
        expanded=exp,
    )


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
    pat = _site_ref_re(inst.name)
    # Menu targets carry a neighbour index (``:d``, mod 3) that is not
    # rotation-equivariant in 0.2, so a spec that addresses the origin
    # instance by site keeps its rotation and canonicalises translation only.
    site_refs = any(pat.search(c.src) or pat.search(c.dst) for c in spec.connects)
    candidates = [
        (a, r) for a, r in _frame_candidates(spec, inst) if not (site_refs and r != 0)
    ]
    if not candidates:
        return spec
    reference = _structure_key(spec)
    best = None
    best_spec: Spec | None = None
    for anchor, rot in candidates:
        cand = _apply_frame(inst, anchor, rot)
        if not _holes_fit_sheet(cand):
            continue
        s = _frame_serial(cand)
        if best is not None and s >= best:
            continue
        t = (anchor.u, anchor.v)
        instances = tuple(cand if i.name == inst.name else i for i in spec.instances)
        connects = tuple(
            _reframe_connect(c, pat, inst.name, t, rot) for c in spec.connects
        )
        cand_spec = replace(spec, instances=instances, connects=connects)
        # A frame is only a symmetry if it builds the same structure: a
        # defect glyph or hole pushed against a bounded sheet's edge clips,
        # and a clipped "canonical" form would key a different structure.
        if _structure_key(cand_spec) != reference:
            continue
        best, best_spec = s, cand_spec
    return best_spec if best_spec is not None else spec


def _structure_key(spec: Spec) -> tuple[Any, ...] | None:
    """Frame-independent fingerprint of what ``spec`` builds: atom and
    bond counts, ring census, and each port's dangling count and lex-min
    edge-word. A proxy, not an isomorphism test -- but a frame that keeps
    every rim's word and the ring census is as close to a symmetry as the
    discrete layer can tell. ``None`` when the spec does not build (a
    frame that breaks the build is no symmetry either)."""
    try:
        net = build(spec, strict=False)
    except HexfoldError:
        return None
    rings: dict[int, int] = {}
    for ring in net.rings:
        rings[len(ring)] = rings.get(len(ring), 0) + 1
    return (
        len(net.atoms),
        len(net.bonds),
        tuple(sorted(rings.items())),
        tuple(
            sorted(
                (name, len(p.dangling), lexmin_word(p.word)) for name, p in net.ports
            )
        ),
    )


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
