"""build(spec) -> Net: Volterra surgery per SPEC section 4.

Every primitive is a decorated lattice patch.  Phase 1 builds sheet, tube,
cone and the C60 table; connects/frags parse but do not build (phase 2
fuses ports and bonds).  Bond order is 1 everywhere in phase 1 — Kekule /
Pauling assignment is deferred.  The constructor asserts V - E + F = chi on
every component (code ``internal.euler``).
"""

from __future__ import annotations

import itertools
import json
import math
import platform
import re
from dataclasses import dataclass, replace
from typing import Any, cast

import numpy as np

from . import __version__, menus
from .defects import (
    Defect,
    Patch,
    Vid,
    _RawDefect,
    cut_disk,
    defect_apex,
    defect_ray_angle,
    glyph_footprint,
    run_length,
)
from .fullerene import C60Data
from .ids import AtomPath
from .lattice import (
    Lattice,
    Site,
    cell_index,
    circumferential,
    neighbors,
    translation_vector,
    tube_sites,
    wrap_tube,
)
from .report import (
    BuildError,
    Finding,
    Profile,
    Report,
    Severity,
)
from .text import Instance, SiteDefect, Spec, parse


@dataclass(frozen=True)
class Atom:
    path: AtomPath
    ord: int
    element: str
    hyb: str
    instance: str


@dataclass(frozen=True)
class Port:
    name: str
    atoms: tuple[int, ...]  # ordinals, cyclic order (full rim walk)
    dangling: tuple[int, ...]  # deg-2 rim atoms in cyclic order (bondable)
    word: str
    normal: str  # "axis" for tube ends, integer dir otherwise
    b: int
    # boundary term the rim's primitive expects it to bound: sheet outer
    # +6, cone base 6-P, tube/cap end 0; hole rims carry the removed cell
    # complex's term.  (b stays the combinatorial consistency value.)
    b_expected: int = 0

    @property
    def size(self) -> int:
        return len(self.dangling)


@dataclass(frozen=True)
class SeamRecord:
    """A realised ``seam`` (SPEC 11.3): ``atoms`` are the ordinals of the
    seam-atom chain, one per rim period, in period order."""

    name: str
    rims: tuple[str, ...]
    k: int
    atoms: tuple[int, ...]


@dataclass(frozen=True)
class Net:
    atoms: tuple[Atom, ...]
    bonds: tuple[tuple[int, int, int], ...]
    rings: tuple[tuple[int, ...], ...]
    ports: tuple[tuple[str, Port], ...]
    regions: tuple[tuple[str, tuple[int, ...]], ...]
    report: Report
    spec: Spec
    lattice: Lattice
    seed3: tuple[tuple[float, float, float], ...] | None = None
    # closed-form | cylinder | cone | flat-perturbed | mixed | spectral
    seed_kind: str = "spectral"
    # authored attachment bonds (bond connects): excluded from the
    # per-component Euler bookkeeping
    attach: tuple[tuple[int, int], ...] = ()
    # rim walks consumed by fuses/bond attachments (atoms sets) — those
    # components are no longer standalone surfaces
    consumed_rims: tuple[tuple[int, ...], ...] = ()
    # rims passivated by `terminate` (atoms, B, B_expected): still surface
    # boundaries for valence and the counting law, though no longer ports
    term_rims: tuple[tuple[tuple[int, ...], int, int], ...] = ()
    # part-graph edges for registry closure (SPEC 12.2): (u_inst, v_inst,
    # k, N) — one entry per fuse (k steps of rim symmetry N) or bond link
    # (0 steps, N=1), in the order the connects were applied.  u -> v is
    # the connect's src -> dst direction.
    registry_edges: tuple[tuple[str, str, int, int], ...] = ()
    # realised k>=3 seams (SPEC 11.3): the authored statement lives in
    # spec.seams; this carries the realised seam-atom ordinals
    seams: tuple[SeamRecord, ...] = ()
    # rim walks consumed by a seam (atoms, B, B_expected): unlike a fuse,
    # a seam does not merge its rims' sheets, so each rim's own boundary
    # term must survive its port's deletion for the per-sheet counting
    # law (SPEC 6.3) -- these are never in consumed_rims.
    seam_rims: tuple[tuple[tuple[int, ...], int, int], ...] = ()
    # sheet partition (SPEC 6.3): connected components of the surface
    # graph -- atoms joined by fuse, excluding bond-verb attachments and
    # seam atoms/bonds -- each (name, atom ordinals).  Named by its sole
    # instance, or "sheet<i>" when it spans more than one.
    sheet_atoms: tuple[tuple[str, tuple[int, ...]], ...] = ()
    # the same partition as face indices into `rings` (SPEC 18 "sheets")
    sheets: tuple[tuple[str, tuple[int, ...]], ...] = ()

    def authored_dict(self) -> dict[str, Any]:
        """The authored-only sections (SPEC 4/14/18): everything a ``.hx``
        text file carries, expanded and sorted -- never atoms, bonds,
        rings, the report, the content hash, or the generated cache.
        Canonical form (:func:`hexfold.canon.canonical_json`) and the
        content hash cover exactly this dict.

        ``smooth`` is a 0.2 section with no parser/builder support yet and
        is omitted rather than always emitted empty; a later slice adds
        it. ``seams`` carries the authored statement (name, rims, k,
        atoms="sp2") -- never the realised seam-atom ordinals, which live
        under ``generated.atoms`` like every other atom (SPEC 18 lists
        ``"atoms":[ord...]`` on the seam entry itself; this build keeps
        the authored/generated split load-bearing everywhere else, so an
        authored-only section carrying live ordinals would be the odd
        one out -- flagged as a judgment call). ``sheets`` is derived
        (the fuse/seam partition of ``generated.rings``, SPEC 6.3/18) and
        lives on ``Net.to_dict()`` instead, next to ``generated``, not
        here. ``bond`` connects stay inside ``connects`` (unchanged from
        0.1); ``ops`` is
        the ordered list of the *other* atom-addressed authored
        statements replayed after generation (SPEC 23.2) -- today that is
        only ``terminate``, so ``ops`` mirrors ``terminate`` one-for-one
        under a ``{"op": ..., ...}`` envelope that leaves room for a
        future atom-level ``bond``/``port`` statement without a shape
        change.
        """
        lat = self.lattice
        insts: dict[str, Any] = {}
        for inst in self.spec.instances:
            idata: dict[str, Any] = {
                "kind": inst.kind,
                "params": {k: v for k, v in inst.params},
            }
            if inst.holes:
                idata["holes"] = [
                    {
                        "dir": h.dir,
                        "ring": "notch" if h.ring == -1 else h.ring,
                        "site": str(h.site),
                    }
                    for h in inst.holes
                ]
            if inst.defects:
                idata["defects"] = [
                    {"kind": d.kind, "site": str(d.site), "dir": d.dir}
                    for d in inst.defects
                ]
            if inst.repeat != 1:
                idata["repeat"] = inst.repeat
            if inst.source is not None:
                idata["source"] = inst.source
            insts[inst.name] = idata
        out: dict[str, Any] = {
            "hexfold": self.spec.version,
            "instances": insts,
            "lattice": {
                "element": [lat.elements[0], lat.elements[1]],
                "sigma_A": repr(lat.sigma_A),
                "sigma_CH_A": repr(lat.sigma_CH_A),
            },
            "ports": {
                name: {
                    "B": p.b,
                    "B_expected": p.b_expected,
                    "atoms": list(p.atoms),
                    "dangling": list(p.dangling),
                    "normal": p.normal,
                    "size": p.size,
                    "word": p.word,
                }
                for name, p in self.ports
            },
            "regions": {k: list(v) for k, v in self.regions},
        }
        if self.spec.origin:
            out["origin"] = self.spec.origin
        if self.spec.prov:
            out["prov"] = {k: v for k, v in self.spec.prov if k != "_"}
        if self.spec.registry:
            out["registry"] = [[a, b] for a, b, _ in self.spec.registry]
        if self.spec.terminate:
            out["terminate"] = [[g, x] for g, x, _ in self.spec.terminate]
            out["ops"] = [
                {"op": "terminate", "glob": g, "element": x}
                for g, x, _ in self.spec.terminate
            ]
        if self.spec.connects:
            out["connects"] = [
                {
                    k: v
                    for k, v in {
                        "dst": c.dst,
                        "expanded": c.expanded,
                        "k": c.k,
                        "menu": c.menu,
                        "order": c.order,
                        "src": c.src,
                        "verb": c.verb,
                    }.items()
                    if v is not None
                }
                for c in self.spec.connects
            ]
        if self.spec.frags:
            used = {
                f.name: sum(
                    1
                    for c in self.spec.connects
                    for ref in (c.src, c.dst)
                    if ref.startswith(f"{f.name}.")
                )
                for f in self.spec.frags
            }
            out["frags"] = {
                f.name: {"attachments": used[f.name], "smiles": f.body}
                for f in self.spec.frags
            }
        if self.spec.seams:
            out["seams"] = [
                {"name": s.name, "rims": list(s.rims), "k": s.k, "atoms": s.atoms}
                for s in sorted(self.spec.seams, key=lambda s: s.name)
            ]
        return out

    def _generated_dict(self, of_hash: str, fidelity: str) -> dict[str, Any]:
        xyz: np.ndarray | None = None
        if fidelity == "stick":
            from .stick import stick  # deferred: stick.py imports this module

            xyz = stick(self)
        atoms: list[dict[str, Any]] = []
        for a in self.atoms:
            adata: dict[str, Any] = {
                "element": a.element,
                "hyb": a.hyb,
                "instance": a.instance,
                "ord": a.ord,
                "path": str(a.path),
            }
            if xyz is not None:
                adata["xyz_A"] = [f"{v:.3f}" for v in xyz[a.ord]]
            atoms.append(adata)
        return {
            "atoms": atoms,
            "bonds": [list(b) for b in self.bonds],
            "fidelity": fidelity,
            "generator": f"hexfold@{__version__}",
            "of": of_hash,
            "platform": platform.platform(),
            "relaxer": None,
            "rings": [list(r) for r in self.rings],
        }

    def to_dict(self, *, fidelity: str = "check") -> dict[str, Any]:
        """The full sectioned JSON (SPEC 18): the authored sections, the
        content hash over them (SPEC 14.6), the report, and the
        generated cache. ``fidelity="check"`` (the default) omits
        coordinates, matching what a dry-run check produces;
        ``fidelity="stick"`` runs the stick-model relaxer and adds
        ``xyz_A`` to every generated atom.
        """
        if fidelity not in ("check", "stick"):
            raise ValueError(f"fidelity must be 'check' or 'stick', got {fidelity!r}")
        from .canon import content_hash  # deferred: canon.py imports this module

        out = self.authored_dict()
        content = content_hash(self)
        out["hash"] = content
        out["report"] = self.report.to_dict()
        out["generated"] = self._generated_dict(content, fidelity)
        if self.sheets:
            out["sheets"] = {name: list(faces) for name, faces in self.sheets}
        return out

    def to_json(self, *, fidelity: str = "check") -> str:
        return (
            json.dumps(self.to_dict(fidelity=fidelity), sort_keys=True, indent=2) + "\n"
        )


# --- per-primitive patch construction --------------------------------------


def _sheet_sites(w: int, h: int) -> list[Site]:
    return [Site(u, v, s) for u in range(w) for v in range(h) for s in (0, 1)]


def _hexagon_sites(radius: int) -> list[Site]:
    out = []
    for u in range(-radius, radius + 1):
        for v in range(-radius, radius + 1):
            if max(abs(u), abs(v), abs(u + v)) <= radius:
                out.append(Site(u, v, 0))
                out.append(Site(u, v, 1))
    return out


def _tube_patch(lat: Lattice, n: int, m: int, length: int, seam: float = 0.0) -> Patch:
    """Patch on the rolled strip: identification along C_h is exact.

    ``seam`` chooses where the periodic boundary falls in circumferential
    coordinate [0,1) — shifted so collar surgery regions stay interior.
    """
    p = Patch(lat, [])
    sites = [wrap_tube(s, n, m, seam) for s in tube_sites(n, m, length)]
    canon = set(sites)
    for s in sites:
        p.flatpos[s] = lat.cart(s)
    p.tube_nm = (n, m)
    p.seam = seam
    for a in sites:
        for nb in neighbors(a):
            cell = cell_index(nb, n, m)
            if not (0 <= cell < length):
                continue
            w = wrap_tube(nb, n, m, seam)
            if w not in canon:
                continue
            e = frozenset((a, w))
            if e in p.edges:
                continue
            p.edges.add(e)
            dv = lat.cart(nb) - lat.cart(a)
            p.dirs[(a, w)] = dv
            p.dirs[(w, a)] = -dv
    return p


def _defect_of(sd: object) -> Defect | None:
    if not isinstance(sd, SiteDefect):
        return None
    ring = {"pentagon": 5, "hexagon": 6, "heptagon": 7, "square": 4, "octagon": 8}.get(
        sd.kind
    )
    if ring is None:
        try:
            ring = int(sd.kind)
        except ValueError:
            return None
    return Defect(ring, sd.site, sd.dir)


def _apply_defects(
    patch: Patch, inst: Instance, lat: Lattice, findings: list[Finding]
) -> None:
    authored: list[Defect] = []
    glyphs: list[tuple[str, Site, int]] = []
    for sd in inst.defects:
        if sd.kind in ("sw", "57"):
            glyphs.append((sd.kind, sd.site, sd.dir))
        else:
            d = _defect_of(sd)
            if d is not None:
                authored.append(d)
    # disjointness among authored defects
    for i in range(len(authored)):
        for j in range(i + 1, len(authored)):
            if cut_disk(authored[i], lat) & cut_disk(authored[j], lat):
                findings.append(
                    Finding(
                        "cut.overlap",
                        Severity.ERROR,
                        f"cut disks of {authored[i].ring}@"
                        f"{authored[i].site} and {authored[j].ring}@"
                        f"{authored[j].site} intersect",
                        where=inst.name,
                        span=inst.span,
                    )
                )
    idx = 0
    for d in authored:
        if d.ring < 6:
            patch.excise(d)
        elif d.ring > 6:
            patch.insert(d, idx)
        idx += 1
    for name, site, gdir in glyphs:
        fp = glyph_footprint(name, site, gdir, lat)
        if fp is None:
            findings.append(
                Finding(
                    "cut.overlap",
                    Severity.ERROR,
                    f"glyph {name} has no valid footprint here",
                    where=inst.name,
                    span=inst.span,
                )
            )
            continue
        for op, gd in fp:
            if op == "x":
                patch.excise(gd)
            else:
                patch.insert(gd, idx)
                idx += 1


def _hexagon_centers(lat: Lattice, site: Site) -> list[np.ndarray]:
    ang = (30.0, 150.0, 270.0) if site.s == 1 else (90.0, 210.0, 330.0)
    p = lat.cart(site)
    return [
        p
        + lat.sigma_A * np.array([math.cos(math.radians(a)), math.sin(math.radians(a))])
        for a in ang
    ]


def _cone_defects(p: int, lat: Lattice) -> list[Defect]:
    """P pentagon wedge excisions clustered around the cone apex.

    Wedges point outward along evenly spaced lattice vertex-rays; the
    cluster radius grows until the cut disks are disjoint.
    """
    for rho_cells in (6, 8, 10, 12):
        rho = rho_cells * lat.a
        defs: list[Defect] = []
        ok = True
        for k in range(p):
            phi = 360.0 * k / p + 90.0
            target = rho * np.array(
                [math.cos(math.radians(phi)), math.sin(math.radians(phi))]
            )
            # nearest hexagon centre to target
            best_c = None
            bd = 1e18
            for u in range(-rho_cells - 2, rho_cells + 3):
                for v in range(-rho_cells - 2, rho_cells + 3):
                    for s in (0, 1):
                        for c in _hexagon_centers(lat, Site(u, v, s)):
                            d2 = float(np.linalg.norm(c - target))
                            if d2 < bd:
                                bd, best_c = d2, c
            assert best_c is not None
            # ray direction: vertex ray (30+60j) closest to phi
            cand = [(30.0 + 60.0 * j) % 360.0 for j in range(6)]
            ray = min(cand, key=lambda t: abs((t - phi + 180.0) % 360.0 - 180.0))
            # first site on the ray
            pp = best_c + lat.sigma_A * np.array(
                [math.cos(math.radians(ray)), math.sin(math.radians(ray))]
            )
            best_s = None
            bd = 1e18
            for u in range(-rho_cells - 2, rho_cells + 3):
                for v in range(-rho_cells - 2, rho_cells + 3):
                    for s in (0, 1):
                        st = Site(u, v, s)
                        d2 = float(np.linalg.norm(lat.cart(st) - pp))
                        if d2 < bd:
                            bd, best_s = d2, st
            assert best_s is not None
            defs.append(_RawDefect(5, best_s, 0, best_c, ray))
        # check pairwise disjointness
        for i in range(len(defs)):
            for j in range(i + 1, len(defs)):
                if cut_disk(defs[i], lat) & cut_disk(defs[j], lat):
                    ok = False
        if ok:
            return defs
    return defs  # last resort; caller reports overlap


def _vid_to_path(inst: str, v: Vid) -> AtomPath:
    if isinstance(v, Site):
        return AtomPath(inst, v)
    assert isinstance(v, tuple)
    tag = v[0]
    if tag == "d":
        _, i, u, vv, s = v
        return AtomPath(inst, Site(u, vv, s), defect=i)
    _, i, u, vv, s = v  # "w" wedge-interior sites share the defect frame
    return AtomPath(inst, Site(u, vv, s), defect=i)


def _canon_ring(ring: list[int]) -> tuple[int, ...]:
    """Rotate a ring to start at its minimum ord, lex-min direction."""
    n = len(ring)
    best = None
    for seq in (ring, ring[::-1]):
        for i in range(n):
            cand = tuple(seq[i:] + seq[:i])
            if best is None or cand < best:
                best = cand
    return best if best is not None else tuple(ring)


def _patch_seed3(patch: Patch, lat: Lattice) -> tuple[dict[Vid, np.ndarray], str]:
    """Deterministic relax seed for a patch (SPEC section 11).

    closed-form for fullerene tables; cylinder roll-up for tubes; cone
    wrap for cones; flat lattice positions with a small fixed out-of-plane
    perturbation for sheets so defects can buckle during relaxation.
    """
    if patch.pos3:
        return dict(patch.pos3), "closed-form"
    sig = lat.sigma_A
    if patch.tube_nm is not None:
        n, m = patch.tube_nm
        ch = n * lat.a1 + m * lat.a2
        t1, t2 = translation_vector(n, m)
        tv = t1 * lat.a1 + t2 * lat.a2
        r = float(np.linalg.norm(ch)) / (2.0 * math.pi)
        ch_hat = ch / np.linalg.norm(ch)
        t_hat = tv / np.linalg.norm(tv)
        out = {}
        for v, p in patch.flatpos.items():
            phi = 2.0 * math.pi * float(p @ ch_hat) / float(np.linalg.norm(ch))
            out[v] = np.array([r * math.cos(phi), r * math.sin(phi), float(p @ t_hat)])
        return out, "cylinder"
    if patch.cone_p is not None:
        return _cone_seed(patch, lat), "cone"
    out = {}
    for v, p in patch.flatpos.items():
        if isinstance(v, Site):
            u, w = v.u, v.v
        else:
            t = cast(tuple, v)
            u, w = int(t[2]), int(t[3])
        out[v] = np.array([p[0], p[1], 0.05 * sig * math.sin(u) * math.cos(w)])
    return out, "flat-perturbed"


def _cone_seed(patch: Patch, lat: Lattice) -> dict[Vid, np.ndarray]:
    """Flat disc positions lifted to a cone-height bump.

    The wedge-excised patch is already a flat isometric embedding of the
    cone metric (all bonds exact, angles ideal); only the seam edges span
    the removed wedges.  A deterministic bump z = R*cos(psi)*(1-rho/R),
    with psi the cone half-angle (sin psi = 1 - P/6), breaks the planarity
    so the spring relax zips the seams and folds the net onto the cone.
    """
    k = 1.0 - (patch.cone_p or 0) / 6.0
    psi = math.asin(max(0.0, k))
    r_max = max(float(np.linalg.norm(p)) for p in patch.flatpos.values())
    h = r_max * math.cos(psi)
    out = {}
    for v, p in patch.flatpos.items():
        rho = float(np.linalg.norm(p))
        out[v] = np.array([p[0], p[1], h * (1.0 - rho / r_max)])
    return out


def _assemble(
    spec: Spec,
    lat: Lattice,
    patches: dict[str, Patch],
    hole_requests: dict[str, list[tuple[int, Site, int | None]]],
    instance_of: dict[str, str],  # patch key -> instance name
    findings: list[Finding],
    hole_index: dict[tuple[str, frozenset[Vid]], int] | None = None,
) -> tuple[Net, dict[str, Any]]:
    atoms: list[Atom] = []
    bonds: list[tuple[int, int, int]] = []
    rings: list[tuple[int, ...]] = []
    ports: dict[str, Port] = {}
    regions: dict[str, list[int]] = {}
    ords: dict[tuple[str, Vid], int] = {}
    path_to_ord: dict[AtomPath, int] = {}
    qual = len(patches) > 1  # qualify port names for multi-instance specs

    # assign ordinals globally: sort by (instance, path-string)
    all_paths: list[tuple[str, Vid, AtomPath]] = []
    for key, patch in patches.items():
        inst = instance_of[key]
        for v in patch.flatpos:
            all_paths.append((key, v, _vid_to_path(inst, v)))
    all_paths.sort(key=lambda t: (t[2].instance, str(t[2])))
    for key, v, path in all_paths:
        ords[(key, v)] = len(ords)
        path_to_ord[path] = ords[(key, v)]

    for key, patch in patches.items():
        inst = instance_of[key]
        region = []
        for v in patch.flatpos:
            path = _vid_to_path(inst, v)
            o = ords[(key, v)]
            region.append(o)
            atoms.append(
                Atom(
                    path=path,
                    ord=o,
                    element=lat.element(path.site)
                    if isinstance(v, Site)
                    else lat.elements[0],
                    hyb="sp2",
                    instance=inst,
                )
            )
        regions[inst] = sorted(region)
        deg = patch.degrees()
        for e in sorted(patch.edges, key=lambda e: sorted(map(str, e))):
            a, b = tuple(e)
            oi, oj = ords[(key, a)], ords[(key, b)]
            bonds.append((min(oi, oj), max(oi, oj), 1))
        ring_list, rims = patch.rings_and_rims()
        for ring in ring_list:
            rings.append(_canon_ring([ords[(key, v)] for v in ring]))
        # interior ring adjacent to each rim edge, for <r> symbols
        ring_of: dict[frozenset[Vid], int] = {}
        for ring in ring_list:
            rr = len(ring)
            for i in range(rr):
                ring_of[frozenset((ring[i], ring[(i + 1) % rr]))] = rr
        inst_obj = spec.instance(inst)
        kind = inst_obj.kind if inst_obj else ""
        rims_sorted = _sort_rims(kind, rims, patch)
        # combinatorial boundary term: B_total = 6*chi_open - sum_int(6-n);
        # hole rims take -(removed ring size), outer rims share the rest.
        chi_p = len(patch.flatpos) - len(patch.edges) + len(ring_list)
        b_total = 6 * chi_p - sum(6 - len(r) for r in ring_list)
        rim_b: list[int] = []
        hole_sum = 0
        outer_ri: list[int] = []
        for rim, _t in rims_sorted:
            hb = patch.hole_b.get(frozenset(rim))
            rim_b.append(hb if hb is not None else 0)
            if hb is None:
                outer_ri.append(len(rim_b) - 1)
            else:
                hole_sum += hb
        if outer_ri:
            rim_b[outer_ri[0]] = b_total - hole_sum
        for ri, (rim, turns) in enumerate(rims_sorted):
            word = patch.rim_word(rim, turns, ring_of)
            b = rim_b[ri]
            hbe = patch.hole_bexp.get(frozenset(rim))
            if hbe is not None:
                b_exp = hbe
            elif kind == "sheet":
                b_exp = 6
            elif kind == "cone":
                b_exp = 6 - (patch.cone_p or 0)
            else:
                b_exp = 0
            ords_list = tuple(ords[(key, v)] for v in rim)
            dangling = tuple(ords[(key, v)] for v in rim if deg.get(v, 0) == 2)
            hi = (hole_index or {}).get((key, frozenset(rim)))
            if hi is not None:
                pname = "hole" if hi == 0 else f"hole{hi}"
                normal = "hole"
            else:
                pname, normal = _port_name_normal(
                    key, kind, ri, len(rims_sorted), rim, patch
                )
            qname = f"{inst}.{pname}" if qual else pname
            ports[qname] = Port(
                name=qname,
                atoms=ords_list,
                dangling=dangling,
                word=run_length(word),
                normal=normal,
                b=b,
                b_expected=b_exp,
            )
    bonds.sort()
    atoms.sort(key=lambda a: a.ord)
    rings_t = tuple(sorted(set(rings)))
    seed3: list[tuple[float, float, float] | None] = [None] * len(atoms)
    have3 = False
    kinds: set[str] = set()
    for key, patch in patches.items():
        src, kind = _patch_seed3(patch, lat)
        kinds.add(kind)
        for v, p3 in src.items():
            if (key, v) in ords:
                seed3[ords[(key, v)]] = (float(p3[0]), float(p3[1]), float(p3[2]))
                have3 = True
    return Net(
        atoms=tuple(atoms),
        bonds=tuple(bonds),
        rings=rings_t,
        ports=tuple(sorted(ports.items())),
        regions=tuple((k, tuple(v)) for k, v in sorted(regions.items())),
        report=Report(()),
        spec=spec,
        lattice=lat,
        seed3=(
            tuple(x if x is not None else (0.0, 0.0, 0.0) for x in seed3)
            if have3
            else None
        ),
        seed_kind=next(iter(kinds)) if len(kinds) == 1 else "mixed",
    ), {"ords": ords, "path_to_ord": path_to_ord}


def _rim_cell(patch: Patch, rim: list[Vid]) -> float:
    if patch.tube_nm is None:
        return 0.0
    n, m = patch.tube_nm
    return float(
        sum(cell_index(v, n, m) for v in rim if isinstance(v, Site)) / len(rim)
    )


def _sort_rims(
    kind: str,
    rims: list[tuple[list[Vid], list[float]]],
    patch: Patch,
) -> list[tuple[list[Vid], list[float]]]:
    """Deterministic rim order: tube ends by axial cell, otherwise the
    largest rim (outer boundary) first, then holes by min vertex id."""
    if kind == "tube":
        return sorted(rims, key=lambda rt: _rim_cell(patch, rt[0]))
    return sorted(rims, key=lambda rt: (-len(rt[0]), sorted(map(str, rt[0]))[0]))


def _port_name_normal(
    key: str,
    kind: str,
    ri: int,
    n_rims: int,
    rim: list[Vid],
    patch: Patch,
) -> tuple[str, str]:
    if kind == "tube":
        return ("in", "axis") if ri == 0 else ("out", "axis")
    if kind == "cap":
        return "in", "axis"
    name = "base" if kind == "cone" else "rim"
    # normal: outward mean direction snapped to a lattice dir
    pts = [patch.flatpos[v] for v in rim]
    cen = np.mean(list(patch.flatpos.values()), axis=0)
    d = np.mean(pts, axis=0) - cen
    ang = math.degrees(math.atan2(d[1], d[0])) % 360.0
    return name, str(round(ang / 60.0) % 6)


def _lattice_from_spec(spec: Spec) -> Lattice:
    kv = dict(spec.lattice)
    sigma = float(kv.get("sigma", "1.42"))
    if "element" in kv:
        el = (kv["element"], kv["element"])
    elif "elements" in kv:
        t = kv["elements"].strip().strip("()")
        a, b = (x.strip() for x in t.split(","))
        el = (a, b)
    else:
        el = ("C", "C")
    sigma_ch = float(kv.get("sigma_CH", "1.09"))
    return Lattice(elements=el, sigma_A=sigma, sigma_CH_A=sigma_ch)


def _param(inst: Instance, name: str, default: str) -> str:
    d = dict(inst.params)
    return d.get(name, default)


_FIT_CAP = 64  # SPEC 12.1: every fit site enumerates its family up to this


def _set_fit_len(spec: Spec, length: int) -> Spec:
    """Return spec with every tube len=fit param replaced by ``length``."""
    out_insts = []
    for inst in spec.instances:
        params = tuple((k, str(length) if v == "fit" else v) for k, v in inst.params)
        out_insts.append(replace(inst, params=params))
    return replace(spec, instances=tuple(out_insts))


def _rank_fit(
    candidates: list[tuple[Any, float, float, Any]],
) -> list[tuple[Any, float, float, Any]]:
    """Rank a fit family by the SPEC 12.1 cost tuple and return it sorted
    best-first: ``(value, seam_cost, residual_cost, lattice_index)``.

    Cost (1), distance to the smooth target, has no ``smooth:`` section to
    measure against yet (SPEC 12.1 note: "Hand-authored files without a
    smooth section therefore keep their 0.1 results") — it is a hook,
    always 0, until that section exists. Ties surviving all four terms
    would mean two distinct candidates are indistinguishable by every
    ranking criterion the spec defines; each site's lattice-index term is
    unique by construction, so that can't happen.
    """
    smooth_cost = 0.0  # hook: SPEC 12.1 cost (1), needs `smooth:` (0.2 later)
    ranked = sorted(candidates, key=lambda t: (smooth_cost, t[1], t[2], t[3]))
    keys = [(smooth_cost, t[1], t[2], t[3]) for t in ranked]
    assert len(set(keys)) == len(keys), "fit family: tie past lattice index"
    return ranked


def _fit_alternatives_finding(
    param: str,
    where: str,
    span: tuple[int, int] | None,
    applied: Any,
    rest: list[tuple[Any, float, float, Any]],
) -> Finding:
    """The ``fit.alternatives`` INFO finding (SPEC 12.1/13): the ranked
    remainder of a solved fit family, best first, excluding the winner."""
    alts = [{"value": v, "cost": [c2, c3, c4]} for v, c2, c3, c4 in rest]
    return Finding(
        "fit.alternatives",
        Severity.INFO,
        f"{param}=fit family: applied {applied!r}, {len(alts)} alternative(s)",
        where=where,
        span=span,
        data=(
            ("alternatives", alts),
            ("applied", applied),
            ("param", param),
        ),
    )


def _registry_redundant_findings(net: Net) -> list[Finding]:
    return [
        Finding(
            "registry.redundant",
            Severity.INFO,
            f"registry: {a} == {b} in register by construction: "
            "len is in whole periods",
            span=rsp,
        )
        for a, b, rsp in net.spec.registry
    ]


def check_registry(net: Net) -> list[Finding]:
    """``registry.closure`` / ``registry.redundant`` (SPEC 12.2, §25.3
    ``check_registry``): the part graph -- instances as nodes, one edge
    per ``fuse`` connect or ``bond`` link (``net.registry_edges``) -- is
    either a tree, in which case every ``registry:`` line is
    ``registry.redundant`` (in register by construction), or it has a
    cycle, in which case each fundamental cycle of a cycle basis composes
    its edges' frame maps and reports the residual as ``registry.closure``
    (INFO at residual 0, WARN otherwise; STRICT promotes to ERROR).

    A third edge source (0.2): a ``seam`` of k rims contributes one edge
    per *consecutive* pair of rims in authored order -- k-1 edges, not
    the k a full cyclic closure would give.  Judgment call: SPEC 11.3
    calls a seam a curve identifying k rims, not a cyclic adjacency
    among their instances, and the acceptance example (a sheet's hole
    seamed to two open tube ends) is explicitly a path in the part graph,
    not a triangle -- ``registry.closure`` must stay absent for it. A
    wrap-around edge would instead close every k>=3 seam into a cycle by
    construction, which is a structural artifact of the seam, not a
    registration claim the author made.
    """
    edges = net.registry_edges
    if not edges:
        return _registry_redundant_findings(net)

    # BFS spanning forest over the part graph; parent[node] = (parent,
    # edge index, forward) where forward says the edge's recorded
    # direction (src -> dst) is parent -> node (True) or node -> parent
    # (False).  Self-loops (u == v, e.g. a tube fused to itself) can
    # never be tree edges and are collected straight into `extra`.
    adj: dict[str, list[tuple[str, int, bool]]] = {}
    extra: list[int] = []
    seen_extra: set[int] = set()
    for ei, (u, v, _k, _en) in enumerate(edges):
        if u == v:
            extra.append(ei)
            seen_extra.add(ei)
            continue
        adj.setdefault(u, []).append((v, ei, True))
        adj.setdefault(v, []).append((u, ei, False))

    nodes = sorted({u for u, v, _, _ in edges} | {v for u, v, _, _ in edges})
    visited: set[str] = set()
    parent: dict[str, tuple[str, int, bool] | None] = {}
    tree_edges: set[int] = set()
    for start in nodes:
        if start in visited:
            continue
        visited.add(start)
        parent[start] = None
        queue = [start]
        while queue:
            cur = queue.pop(0)
            for nbr, ei, fwd in sorted(adj.get(cur, ()), key=lambda t: t[1]):
                if ei in tree_edges:
                    continue
                if nbr not in visited:
                    visited.add(nbr)
                    parent[nbr] = (cur, ei, fwd)
                    tree_edges.add(ei)
                    queue.append(nbr)
                elif ei not in seen_extra:
                    seen_extra.add(ei)
                    extra.append(ei)

    if not extra:
        return _registry_redundant_findings(net)

    def root_path(x: str) -> list[tuple[str, int | None, bool | None]]:
        """[(root, None, None), ..., (x, edge_to_parent, forward)]."""
        chain: list[tuple[str, int | None, bool | None]] = []
        cur: str | None = x
        while cur is not None:
            p = parent[cur]
            chain.append((cur, None, None) if p is None else (cur, p[1], p[2]))
            cur = p[0] if p is not None else None
        chain.reverse()
        return chain

    out: list[Finding] = []
    extra.sort()
    for ei in extra:
        u, v, k_edge, n_edge = edges[ei]
        steps: list[tuple[int, int, int]] = []
        if u == v:
            cycle_nodes: tuple[str, ...] = (u,)
            steps.append((k_edge, n_edge, 1))
        else:
            pu, pv = root_path(u), root_path(v)
            common = 0
            while (
                common < len(pu) and common < len(pv) and pu[common][0] == pv[common][0]
            ):
                common += 1
            lca = common - 1
            up = pu[lca + 1 :]  # root..u order, excluding the LCA
            down = pv[lca + 1 :]  # root..v order, excluding the LCA
            cycle_nodes = (
                tuple(n for n, _, _ in reversed(up))
                + (pu[lca][0],)
                + tuple(n for n, _, _ in down)
            )
            for _node, eidx, is_fwd in reversed(up):
                assert eidx is not None and is_fwd is not None
                _eu, _ev, ek, en = edges[eidx]
                steps.append((ek, en, -1 if is_fwd else 1))
            for _node, eidx, is_fwd in down:
                assert eidx is not None and is_fwd is not None
                _eu, _ev, ek, en = edges[eidx]
                steps.append((ek, en, 1 if is_fwd else -1))
            # close the cycle: traverse the extra edge v -> u, the
            # reverse of its recorded u -> v direction
            steps.append((k_edge, n_edge, -1))
        period = math.lcm(*(n for _k, n, _s in steps))
        residual = sum(k * s * (period // n) for k, n, s in steps) % period
        closing = "→".join((*cycle_nodes, cycle_nodes[0]))
        if residual == 0:
            out.append(
                Finding(
                    "registry.closure",
                    Severity.INFO,
                    f"registry: cycle {closing} closes (residual 0)",
                    data=(
                        ("cycle", cycle_nodes),
                        ("residual", residual),
                        ("period", period),
                    ),
                )
            )
        else:
            out.append(
                Finding(
                    "registry.closure",
                    Severity.WARN,
                    f"registry: cycle {closing} closes with residual "
                    f"{residual} of {period} symmetry steps",
                    data=(
                        ("cycle", cycle_nodes),
                        ("residual", residual),
                        ("period", period),
                    ),
                )
            )
    return out


def build(
    spec_text_or_ast: str | Spec,
    *,
    profile: Profile = Profile.DEFAULT,
    strict: bool = True,
) -> Net:
    spec = (
        parse(spec_text_or_ast)
        if isinstance(spec_text_or_ast, str)
        else spec_text_or_ast
    )
    spec = menus.expand(spec)

    # len=fit (SPEC section 8): the smallest integer len for which the
    # whole spec builds with no ERROR findings.  len is in whole
    # translation periods, so the lattice is in register by construction;
    # fit only has to clear surgery footprints (cut.overlap, rim clips).
    fit_insts = [
        i for i in spec.instances if _param(i, "len", _param(i, "2", "")) == "fit"
    ]
    if fit_insts:
        tried: list[int] = []
        clean: list[int] = []
        winner_net: Net | None = None
        for length in range(1, _FIT_CAP + 1):
            tried.append(length)
            spec2 = _set_fit_len(spec, length)
            net = build(spec2, profile=profile, strict=False)
            if net.report.ok:
                clean.append(length)
                if winner_net is None:
                    winner_net = net
                # SPEC 12.1: family capped at the winner plus <=7 further
                # members for len (a full 64-deep scan is unbounded work
                # for a report nobody reads that far into).
                if len(clean) >= 8:
                    break
        if winner_net is None:
            report = profile.apply_all(
                [
                    Finding(
                        "fit.unsolvable",
                        Severity.ERROR,
                        f"no len <= {_FIT_CAP} builds cleanly "
                        f"(tried {tried[0]}..{tried[-1]})",
                        data=(("tried", tried),),
                    )
                ]
            )
            raise BuildError(report)
        # 0.1 kept the first (smallest) clean len; (2) max seam ring and
        # (3) |euler.residual| are constant across clean lens absent a
        # `smooth:` section, so ranking collapses to (4) lowest len — the
        # 0.1 winner, unchanged.
        candidates = [(v, 0.0, 0.0, v) for v in clean]
        ranked = _rank_fit(candidates)
        winner = ranked[0][0]
        finding = _fit_alternatives_finding(
            "len",
            ",".join(sorted(i.name for i in fit_insts)),
            fit_insts[0].span,
            winner,
            ranked[1:],
        )
        report = profile.apply_all(list(winner_net.report.findings) + [finding])
        return replace(winner_net, report=report)

    lat = _lattice_from_spec(spec)
    findings: list[Finding] = []
    # registry.redundant / registry.closure (SPEC 12.2) are decided from
    # the part graph, only known after connects are applied below; see
    # the check_registry(net) call near the end of this function.
    patches: dict[str, Patch] = {}
    instance_of: dict[str, str] = {}
    hole_requests: dict[str, list[tuple[int, Site, int | None]]] = {}
    # fuse dst 'inst @ inst/site[:d]' mints a hole on that instance
    conn_hole: dict[int, tuple[str, Site, int | None]] = {}
    for ci, c in enumerate(spec.connects):
        if c.verb != "fuse":
            continue
        dst_inst, dst_site, dst_dir = _fuse_dst(c.dst)
        if dst_site is not None:
            hole_requests.setdefault(dst_inst, []).append((6, dst_site, dst_dir))
            conn_hole[ci] = (dst_inst, dst_site, dst_dir)

    # shift a collar-target tube's seam to sit opposite the hole site
    seam_of: dict[str, float] = {}
    for ci, (dinst, dsite, _dd) in conn_hole.items():
        c = spec.connects[ci]
        if not (c.menu and "@fit" in c.menu):
            continue
        src_inst = spec.instance(dinst)
        if src_inst is not None and src_inst.kind == "tube":
            n0 = int(_param(src_inst, "0", _param(src_inst, "n", "5")))
            m0 = int(_param(src_inst, "1", _param(src_inst, "m", "5")))
            al = circumferential(dsite, n0, m0)
            seam_of[dinst] = (al + 0.5) % 1.0

    for inst in spec.instances:
        copies = [inst.name] + [f"{inst.name}_{i}" for i in range(1, inst.repeat)]
        for cname in copies:
            key = cname
            instance_of[key] = cname
            if inst.kind == "sheet":
                w = int(_param(inst, "0", _param(inst, "W", "10")))
                h = int(_param(inst, "1", _param(inst, "H", "10")))
                patch = Patch(lat, _sheet_sites(w, h))
            elif inst.kind == "tube":
                n = int(_param(inst, "0", _param(inst, "n", "5")))
                m = int(_param(inst, "1", _param(inst, "m", "5")))
                lp = _param(inst, "len", _param(inst, "2", "1"))
                length = 1 if lp == "fit" else int(lp)
                patch = _tube_patch(lat, n, m, length, seam_of.get(cname, 0.0))
            elif inst.kind == "cone":
                pnum = int(_param(inst, "0", _param(inst, "P", "1")))
                rad = int(_param(inst, "rad", _param(inst, "1", "12")))
                defs = _cone_defects(pnum, lat)
                patch = Patch(lat, _hexagon_sites(rad))
                patch.cone_p = pnum
                inside = set(patch.flatpos)
                disks = [cut_disk(d, lat) for d in defs]
                bad = any(not dk <= inside for dk in disks)
                bad = bad or any(
                    disks[i] & disks[j]
                    for i in range(len(disks))
                    for j in range(i + 1, len(disks))
                )
                if bad:
                    findings.append(
                        Finding(
                            "cut.overlap",
                            Severity.ERROR,
                            f"cone rad={rad} too small: wedge cut disks "
                            "overlap or leave the patch",
                            where=inst.name,
                            span=inst.span,
                        )
                    )
                else:
                    for d in defs:
                        patch.excise(d)
            elif inst.kind == "fullerene":
                n_at = _param(inst, "0", "C60")
                if n_at not in ("C60", "60"):
                    findings.append(
                        Finding(
                            "build.fullerene",
                            Severity.ERROR,
                            f"only C60 supported in v1, got {n_at}",
                            where=inst.name,
                            span=inst.span,
                        )
                    )
                    continue
                patch = _c60_patch(lat)
            elif inst.kind == "cap":
                cn = int(_param(inst, "0", _param(inst, "n", "5")))
                cm = int(_param(inst, "1", _param(inst, "m", "5")))
                cpatch = _cap_patch(lat, cn, cm)
                if cpatch is None:
                    findings.append(
                        Finding(
                            "build.kind",
                            Severity.ERROR,
                            f"cap({cn},{cm}) v0.1: only (5,5)",
                            where=inst.name,
                            span=inst.span,
                        )
                    )
                    continue
                patch = cpatch
            else:
                findings.append(
                    Finding(
                        "build.kind",
                        Severity.ERROR,
                        f"primitive {inst.kind!r} not supported in phase 1",
                        where=inst.name,
                        span=inst.span,
                    )
                )
                continue
            if inst.kind not in ("fullerene", "cap"):
                _apply_defects(patch, inst, lat, findings)
            hole_requests.setdefault(key, []).extend(
                (h.ring, h.site, h.dir) for h in inst.holes
            )
            patches[key] = patch

    # holes: remove ring atoms after faces known
    conn_rims: dict[int, tuple[str, frozenset[Vid]]] = {}
    conn_disks: dict[int, frozenset[Site]] = {}
    hole_index: dict[tuple[str, frozenset[Vid]], int] = {}
    rim_vids: dict[tuple[str, str], frozenset[Vid]] = {}
    for key, reqs in hole_requests.items():
        patch = patches[key]
        for hreq_i, (ring_size, site, hdir) in enumerate(reqs):
            req_site = site
            if patch.tube_nm is not None:
                site = wrap_tube(site, *patch.tube_nm, getattr(patch, "seam", 0.0))
            got = _apply_hole(patch, ring_size, site, hdir, key, findings)
            if got is None:
                continue
            rim_fs, disk = got
            hole_index.setdefault((key, rim_fs), hreq_i)
            rim_vids[(key, f"hole{hreq_i}" if hreq_i else "hole")] = rim_fs
            for ci, (dinst, dsite, _dd) in conn_hole.items():
                if dinst == key and dsite == req_site and ci not in conn_rims:
                    conn_rims[ci] = (key, rim_fs)
                    conn_disks[ci] = disk

    # collars: fuse{Rxk @fit} places k defects around the dst hole
    for ci, c in enumerate(spec.connects):
        if c.verb == "fuse" and c.menu and "@fit" in c.menu:
            if ci not in conn_rims:
                # named-port endpoint: the collar decorates whichever side
                # carries the hole rim (dst normally; src when the fuse is
                # written hole-first, e.g. substrate.hole --> post.in)
                rim_got = None
                key = c.dst.rsplit(".", 1)[0]
                for ref in (c.dst, c.src):
                    key = ref.rsplit(".", 1)[0]
                    rim_got = rim_vids.get((key, ref.rsplit(".", 1)[1]))
                    if rim_got is not None:
                        break
                if rim_got is None:
                    findings.append(
                        Finding(
                            "port.unknown",
                            Severity.ERROR,
                            f"collar endpoints {c.src} -> {c.dst}: "
                            "neither is a hole port",
                            span=c.span,
                        )
                    )
                    continue
                # hole disk = rim sites plus their one-corona neighbours
                corona = {
                    w
                    for e in patches[key].edges
                    if e & set(rim_got)
                    for w in e
                    if isinstance(w, Site)
                }
                conn_rims[ci] = (key, rim_got)
                conn_disks[ci] = frozenset(
                    v for v in set(rim_got) | corona if isinstance(v, Site)
                )
            key, rim_fs = conn_rims[ci]
            mm = _COLLAR_RE.match(c.menu)
            if mm:
                ring_n, kk = int(mm["ring"]), int(mm["k"])
                solved = _solve_collar(
                    patches[key],
                    rim_fs,
                    conn_disks[ci],
                    ring_n,
                    kk,
                    key,
                    findings,
                    c.span,
                )
                for di, d in enumerate(solved):
                    patches[key].insert(d, 1000 + 10 * ci + di)
                spec = _record_collar(spec, ci, solved)
                # hole rim may have changed under the collar
                new_rim = _hole_rim_after(patches[key], rim_fs)
                conn_rims[ci] = (key, new_rim)
                hi = hole_index.pop((key, rim_fs), None)
                if hi is not None:
                    hole_index[(key, new_rim)] = hi
                hb = patches[key].hole_b.pop(rim_fs, None)
                if hb is not None:
                    patches[key].hole_b[new_rim] = hb
                hbe = patches[key].hole_bexp.pop(rim_fs, None)
                if hbe is not None:
                    patches[key].hole_bexp[new_rim] = hbe

    net, ctx = _assemble(
        spec, lat, patches, hole_requests, instance_of, findings, hole_index
    )

    net, spec = _apply_connects(spec, net, ctx, patches, conn_rims, findings)
    net = _apply_terminate(spec, net, findings)

    # internal euler assertion per component
    chi, comp = net_euler(patches)
    if chi != 2 * comp:
        findings.append(
            Finding(
                "internal.euler",
                Severity.ERROR,
                f"V-E+F={chi} != 2*{comp}",
            )
        )
    findings.extend(check_registry(net))
    report = profile.apply_all(findings)
    net = Net(
        atoms=net.atoms,
        bonds=net.bonds,
        rings=net.rings,
        ports=net.ports,
        regions=net.regions,
        report=report,
        spec=spec,
        lattice=lat,
        seed3=net.seed3,
        seed_kind=net.seed_kind,
        attach=net.attach,
        consumed_rims=net.consumed_rims,
        term_rims=net.term_rims,
        registry_edges=net.registry_edges,
        seams=net.seams,
        seam_rims=net.seam_rims,
        sheet_atoms=net.sheet_atoms,
        sheets=net.sheets,
    )
    if strict and not report.ok:
        raise BuildError(report)
    return net


def net_euler(patches: dict[str, Patch]) -> tuple[int, int]:
    total = 0
    comps = 0
    for p in patches.values():
        e, c = p.euler_components()
        total += e
        comps += c
    return total, comps


def _ring_canon(ring: list[Vid]) -> tuple[str, ...]:
    ss = [str(v) for v in ring]
    return min(tuple(ss[i:] + ss[:i]) for i in range(len(ss)))


def _apply_hole(
    patch: Patch,
    ring_size: int,
    site: Site,
    hdir: int | None,
    key: str,
    findings: list[Finding],
) -> tuple[frozenset[Vid], frozenset[Site]] | None:
    """Remove the ring of ``ring_size`` incident on ``site``.

    With ``hdir`` (dir from the site toward the ring centre) the matching
    ring is selected; otherwise the lex-min canonical vertex tuple wins
    (SPEC section 7).  Returns (hole rim, hole disk = ring + one corona).
    """
    rings, pre_rims = patch.rings_and_rims()
    pre_rim_atoms = {v for r, _t in pre_rims for v in r}
    dead: set[Vid] | None = None
    f_rem = 0
    sint_rem = 0
    if ring_size == -2:
        # path3: site, its neighbour along ``hdir``, then the zigzag-
        # straight continuation (the neighbour at dir d +/- 2 from the
        # second atom, preferring d + 2; a corner bend never results).
        d0 = (hdir or 0) % 6

        def dir_of(a: Site, b: Site) -> int:
            dv = patch.flatpos[b] - patch.flatpos[a]
            return round(math.degrees(math.atan2(dv[1], dv[0])) / 60.0) % 6

        nbrs = [n for n in neighbors(site) if n in patch.flatpos]
        n1 = min(
            (n for n in nbrs if dir_of(site, n) == d0),
            key=str,
            default=None,
        )
        if n1 is None:
            n1 = min(
                nbrs,
                key=lambda n: (
                    min((dir_of(site, n) - d0) % 6, (d0 - dir_of(site, n)) % 6),
                    str(n),
                ),
                default=None,
            )
        n2 = None
        if n1 is not None:
            conts = [n for n in neighbors(n1) if n != site and n in patch.flatpos]
            for want in ((d0 + 2) % 6, (d0 - 2) % 6):
                got = [n for n in conts if dir_of(n1, n) == want]
                if got:
                    n2 = min(got, key=str)
                    break
            if n2 is None and conts:
                n2 = min(conts, key=str)
        if n1 is not None and n2 is not None:
            dead = {site, n1, n2}
        if dead is None:
            findings.append(
                Finding(
                    "hole.missing",
                    Severity.ERROR,
                    f"path3 at {site} needs three lattice atoms",
                    where=key,
                )
            )
            return None
        cands = []
        target = None
    elif ring_size <= -10:
        # hex(r): the hexagonal cluster of rings within ring-adjacency
        # radius r of the ring containing ``site`` (r=0 is one hexagon).
        # Dangling count follows 3|S| - 2e_S for the removed vertex set S.
        rr = -10 - ring_size
        cands = [r6 for r6 in rings if len(r6) == 6 and site in r6]
        if hdir is not None and len(cands) > 1:
            p0c = patch.flatpos[site]

            def ring_dir_c(r: list[Vid]) -> int:
                cen = np.mean([patch.flatpos[v] for v in r], axis=0)
                dd = cen - p0c
                return round(math.degrees(math.atan2(dd[1], dd[0])) / 60.0) % 6

            by_dir = [r6 for r6 in cands if ring_dir_c(r6) == hdir % 6]
            if by_dir:
                cands = by_dir
        cands.sort(key=_ring_canon)
        target = cands[0] if cands else None
        if target is not None:
            edge_rings: dict[frozenset[Vid], list[int]] = {}
            for ri6, r6 in enumerate(rings):
                for ei in range(len(r6)):
                    edge_rings.setdefault(
                        frozenset((r6[ei], r6[(ei + 1) % len(r6)])), []
                    ).append(ri6)
            centre_ring = next(i for i, r6 in enumerate(rings) if r6 is target)
            cluster = {centre_ring}
            frontier = {centre_ring}
            for _ in range(rr):
                nxt: set[int] = set()
                for ri6 in frontier:
                    r6 = rings[ri6]
                    for ei in range(len(r6)):
                        for adj in edge_rings.get(
                            frozenset((r6[ei], r6[(ei + 1) % len(r6)])), ()
                        ):
                            if adj not in cluster:
                                nxt.add(adj)
                cluster |= nxt
                frontier = nxt
            dead = {v for ri6 in cluster for v in rings[ri6]}
            f_rem = len(cluster)
            sint_rem = sum(6 - len(rings[i]) for i in cluster)
    else:
        if ring_size == -1:
            # notch: hexagon minus one vertex (5 dangling rim atoms)
            cands = [r for r in rings if len(r) == 6 and site in r]
        else:
            cands = [r for r in rings if len(r) == ring_size and site in r]
        if hdir is not None and len(cands) > 1:
            p0 = patch.flatpos[site]

            def ring_dir(r: list[Vid]) -> int:
                cen = np.mean([patch.flatpos[v] for v in r], axis=0)
                d = cen - p0
                return round(math.degrees(math.atan2(d[1], d[0])) / 60.0) % 6

            by_dir = [r for r in cands if ring_dir(r) == hdir % 6]
            if by_dir:
                cands = by_dir
        cands.sort(key=_ring_canon)
        target = cands[0] if cands else None
    if target is None and site not in patch.flatpos and patch.hole_b:
        # site already removed by an overlapping hole: return the rim that
        # now contains its former position (merged-hole semantics)
        _, rims_now = patch.rings_and_rims()
        near = min(
            (r for r, _t in rims_now),
            key=lambda r: min(
                float(np.linalg.norm(patch.flatpos[v] - np.zeros(2))) for v in r
            ),
            default=None,
        )
        if near is not None:
            prev = patch.hole_b.get(frozenset(near))
            patch.hole_b[frozenset(near)] = (prev or 0) - abs(ring_size)
            pexp = patch.hole_bexp.get(frozenset(near))
            own = -ring_size if ring_size > 0 else -6
            patch.hole_bexp[frozenset(near)] = (pexp or 0) + own + 6
            return frozenset(near), frozenset(v for v in near if isinstance(v, Site))
        return None
    if dead is None:
        if target is None:
            findings.append(
                Finding(
                    "hole.missing",
                    Severity.ERROR,
                    f"no {ring_size}-ring contains {site}",
                    where=key,
                )
            )
            return None
        dead = set(target)
    if ring_size > 0:
        f_rem, sint_rem = 1, 6 - ring_size
    if ring_size == -1:
        assert target is not None
        cen = np.mean([patch.flatpos[v] for v in target], axis=0)

        def vdir(v: Vid) -> int:
            d = patch.flatpos[v] - cen
            return round(math.degrees(math.atan2(d[1], d[0])) / 60.0) % 6

        if hdir is not None:
            keep = min(
                target,
                key=lambda v: (min((vdir(v) - hdir) % 6, (hdir - vdir(v)) % 6), str(v)),
            )
        else:
            keep = min(target, key=str)
        dead.discard(keep)
    adjacent = {w for e in patch.edges if e & dead for w in e if w not in dead}
    disk = frozenset(v for v in dead | adjacent if isinstance(v, Site))
    e_s = sum(1 for e in patch.edges if len(e & dead) == 2)
    patch.edges = {e for e in patch.edges if not (e & dead)}
    patch.dirs = {
        k: v for k, v in patch.dirs.items() if k[0] not in dead and k[1] not in dead
    }
    for v in dead:
        patch.flatpos.pop(v, None)
    _, new_rims = patch.rings_and_rims()
    for rim, _turns in new_rims:
        if set(rim) & adjacent:
            fs = frozenset(rim)
            # thin-host guard: a rim walk that revisits a vertex means the
            # opening spans the tube circumference; merging into a rim that
            # pre-existed means the disk clipped a tube end
            if len(set(rim)) != len(rim):
                findings.append(
                    Finding(
                        "cut.overlap",
                        Severity.ERROR,
                        "opening spans the circumference; use a wider tube",
                        where=key,
                    )
                )
            elif fs & pre_rim_atoms:
                findings.append(
                    Finding(
                        "cut.overlap",
                        Severity.ERROR,
                        "opening clipped by an existing rim; use a longer "
                        "or wider host",
                        where=key,
                    )
                )
            # carry B through rim merges: the new rim inherits the boundary
            # terms of every earlier hole rim it absorbed
            b_new = -(3 * len(dead) - 2 * e_s)
            for old_fs, old_b in list(patch.hole_b.items()):
                if old_fs & fs:
                    b_new += old_b
                    del patch.hole_b[old_fs]
            patch.hole_b[fs] = b_new
            own_bexp = -(6 * (len(dead) - e_s + f_rem) - sint_rem)
            n_abs = 0
            be_new = own_bexp
            for old_fs, old_be in list(patch.hole_bexp.items()):
                if old_fs & fs:
                    be_new += old_be
                    n_abs += 1
                    del patch.hole_bexp[old_fs]
            patch.hole_bexp[fs] = be_new + 6 * n_abs
            return fs, disk
    return None


_COLLAR_RE = re.compile(r"^(?P<ring>\d+)\s*[x\u00d7]\s*(?P<k>\d+)\s*@fit$")


def _fuse_dst(dst: str) -> tuple[str, Site | None, int | None]:
    """'inst @ inst/site[:d]' -> hole on inst; 'inst.port' -> (inst, None)."""
    if "@" in dst:
        lhs, rhs = dst.split("@", 1)
        inst = lhs.strip()
        _, ref = rhs.strip().split("/", 1)
        site_s, _, d = ref.partition(":")
        return inst, Site.parse(site_s), int(d) if d else None
    return dst.rsplit(".", 1)[0], None, None


def _collar_disk(d: Defect, lat: Lattice) -> frozenset[Site]:
    """Collar disjointness disk: the defect's wedge sector only.

    The full excision ``cut_disk`` (wedge + hexagon corona) is too wide for
    collar orbits — members sit ~1 circumferential step apart — so collar
    disjointness is judged on the wedge sector alone.
    """
    c = defect_apex(d, lat)
    t1 = defect_ray_angle(d, lat)
    w = 60.0 * d.wedges
    out: set[Site] = set()
    for u in range(d.site.u - 5, d.site.u + 6):
        for v in range(d.site.v - 5, d.site.v + 6):
            for ss in (0, 1):
                st = Site(u, v, ss)
                p = lat.cart(st) - c
                r = float(np.linalg.norm(p))
                if r > 3.6 * lat.sigma_A or r < 1e-9:
                    continue
                ang = math.degrees(math.atan2(p[1], p[0])) % 360.0
                if (ang - t1) % 360.0 <= w + 1e-6:
                    out.add(st)
    return frozenset(out)


def _dir_toward(patch: Patch, site: Site, centre: np.ndarray) -> int:
    d = centre - patch.flatpos[site]
    return round(math.degrees(math.atan2(d[1], d[0])) / 60.0) % 6


def _solve_collar(
    patch: Patch,
    rim: frozenset[Vid],
    disk: frozenset[Site],
    ring_n: int,
    k: int,
    key: str,
    findings: list[Finding],
    span: tuple[int, int] | None = None,
) -> list[Defect]:
    """Solve a ``{Rxk @fit}`` collar: k ring-n defects in a C_k orbit at the
    smallest radius whose collar disks are pairwise disjoint and disjoint
    from the hole disk.  Tie-break lowest (u,v,s,dir); each member's dir
    prefers the direction toward the hole centre.  Candidate placements are
    validated by running the insertions on a patch copy."""
    import copy

    centre = np.mean([patch.flatpos[v] for v in rim], axis=0)
    sites = sorted(
        (s for s in patch.flatpos if isinstance(s, Site)),
        key=lambda s: (
            float(np.linalg.norm(patch.flatpos[s] - centre)),
            s.u,
            s.v,
            s.s,
        ),
    )
    if patch.tube_nm is not None:
        n, m = patch.tube_nm
        if math.gcd(n, m) % k != 0:
            findings.append(
                Finding(
                    "port.symmetry",
                    Severity.ERROR,
                    f"collar k={k} needs k | gcd({n},{m})",
                    where=key,
                )
            )
            return []
        step = (n // k, m // k)

        def orbit(base: Site) -> list[Site]:
            return [
                wrap_tube(
                    Site(base.u + j * step[0], base.v + j * step[1], base.s),
                    n,
                    m,
                    getattr(patch, "seam", 0.0),
                )
                for j in range(k)
            ]

    else:
        if k not in (1, 2, 3, 6):
            findings.append(
                Finding(
                    "port.symmetry",
                    Severity.ERROR,
                    f"sheet collar needs k in {{1,2,3,6}}, got {k}",
                    where=key,
                )
            )
            return []

        def orbit(base: Site) -> list[Site]:
            out = []
            p = patch.flatpos[base]
            for j in range(k):
                th = 2.0 * math.pi * j / k
                rm = np.array(
                    [[math.cos(th), -math.sin(th)], [math.sin(th), math.cos(th)]]
                )
                q = rm @ (p - centre) + centre
                out.append(
                    min(
                        (s for s in patch.flatpos if isinstance(s, Site)),
                        key=lambda s: (
                            float(np.linalg.norm(patch.flatpos[s] - q)),
                            s.u,
                            s.v,
                            s.s,
                        ),
                    )
                )
            return out

    flat = set(patch.flatpos)
    n_comp0 = patch.euler_components()
    # pre-existing low-degree atoms (sheet corners are deg-1) are not the
    # collar's fault: only *new* deg<2 atoms invalidate an insertion
    low0 = {v for v, d in patch.degrees().items() if d < 2}

    def dir_order(site: Site) -> list[int]:
        # wedge sector away from the hole centre first: a ray opening into
        # the hole void produces dangling wedge copies; outward sectors
        # land in bulk material and merge the collar into the surface
        toward = _dir_toward(patch, site, centre)

        def score(d: int) -> tuple[float, int]:
            delta = (d - toward) % 6
            delta = min(delta, 6 - delta)
            return (float(delta), d)

        return sorted(range(6), key=score, reverse=True)

    def try_orbit(orb: list[Site]) -> list[Defect] | None:
        work = copy.deepcopy(patch)
        placed: list[Defect] = []
        used: set[Site] = set()
        for di, o in enumerate(orb):
            for d in dir_order(o):
                cand = Defect(ring_n, o, d)
                dk = set(_collar_disk(cand, work.lat))
                if patch.tube_nm is not None:
                    n2, m2 = patch.tube_nm
                    sh = getattr(patch, "seam", 0.0)
                    dk = {wrap_tube(x, n2, m2, sh) for x in dk}
                    if len(dk) != len(_collar_disk(cand, work.lat)):
                        findings.append(
                            Finding(
                                "cut.overlap",
                                Severity.ERROR,
                                "collar disk spans the circumference; use a wider tube",
                                where=key,
                            )
                        )
                        return None
                if dk & disk or dk & used:
                    continue
                snapshot = copy.deepcopy(work)
                try:
                    work.insert(cand, 5000 + di)
                except Exception:
                    work = snapshot
                    continue
                if work.euler_components() != n_comp0:
                    work = snapshot
                    continue
                # a clean insertion: no deg<2 atoms anywhere, and new
                # atoms are trivalent unless they landed on a rim
                wdeg = work.degrees()
                if any(d < 2 for v, d in wdeg.items() if v not in low0):
                    work = snapshot
                    continue
                _wr, wrims = work.rings_and_rims()
                rimset = {v for rim, _t in wrims for v in rim}
                if any(wdeg[v] < 3 and v not in rimset for v in work.flatpos):
                    work = snapshot
                    continue
                placed.append(cand)
                used |= dk
                break
            else:
                return None
        return placed

    seen: set[tuple[Site, ...]] = set()
    # SPEC 12.1 family: every admissible orbit up to the cap, enumerated in
    # the 0.1 order (smallest radius, tie-break lowest lattice index — the
    # `sites` sort key above).  A collar has no seam ring (2) and every
    # placement inserts the same (ring_n, k), so its own contribution to
    # the counting law is identical across candidates (3); neither term
    # discriminates, so ranking collapses to (4), which for a collar site
    # *is* that 0.1 enumeration order — kept verbatim so the winner is
    # unchanged.
    family: list[tuple[list[Defect], list[Site]]] = []
    for base in sites:
        if base in disk:
            continue
        orb = orbit(base)
        if any(o not in flat or o in disk or not isinstance(o, Site) for o in orb):
            continue
        key_orb = tuple(sorted(orb, key=lambda s: (s.u, s.v, s.s)))
        if key_orb in seen or len(set(orb)) != k:
            continue
        seen.add(key_orb)
        got = try_orbit(orb)
        if got is not None:
            family.append((got, orb))
            if len(family) >= _FIT_CAP:
                break
    if family:
        winner_defs, winner_orb = family[0]
        sig = patch.lat.sigma_A
        rim_r = (
            float(np.mean([np.linalg.norm(patch.flatpos[v] - centre) for v in rim]))
            / sig
        )
        dist = (
            float(
                np.mean(
                    [
                        np.linalg.norm(patch.flatpos[d.site] - centre)
                        for d in winner_defs
                    ]
                )
            )
            / sig
        )
        if dist > 2.0 * rim_r + 3.0:
            findings.append(
                Finding(
                    "collar.far",
                    Severity.WARN,
                    f"collar solved {dist:.1f} cells from the hole "
                    f"centre (rim radius {rim_r:.1f}); it does not "
                    "seat the opening",
                    where=key,
                    data=(("distance", round(dist, 2)),),
                )
            )
        candidates = [
            (sorted(str(s) for s in orb), 0.0, 0.0, idx)
            for idx, (_defs, orb) in enumerate(family)
        ]
        ranked = _rank_fit(candidates)
        findings.append(
            _fit_alternatives_finding(
                "collar", key, span, sorted(str(s) for s in winner_orb), ranked[1:]
            )
        )
        return winner_defs
    findings.append(
        Finding(
            "fit.unsolvable",
            Severity.ERROR,
            f"no radius places {k}x{ring_n}-ring collar disjoint from hole",
            where=key,
        )
    )
    return []


def _hole_rim_after(patch: Patch, old_rim: frozenset[Vid]) -> frozenset[Vid]:
    """Re-locate the hole rim after collar insertion (nearest centroid)."""
    cen = np.mean([patch.flatpos[v] for v in old_rim if v in patch.flatpos], axis=0)
    _, rims = patch.rings_and_rims()
    scored = []
    for rim, _t in rims:
        rc = np.mean([patch.flatpos[v] for v in rim], axis=0)
        scored.append((float(np.linalg.norm(rc - cen)), frozenset(rim)))
    return min(scored, key=lambda t: (t[0], sorted(map(str, t[1]))))[1]


def _record_collar(spec: Spec, ci: int, defs: list[Defect]) -> Spec:
    """Attach the solved collar defects to the fuse connect's expanded."""
    cs = list(spec.connects)
    c = cs[ci]
    exp = dict(c.expanded or {})
    exp["defects"] = [{"dir": d.dir, "ring": d.ring, "site": str(d.site)} for d in defs]

    cs[ci] = replace(c, expanded=exp)
    return replace(spec, connects=tuple(cs))


def _c60_patch(lat: Lattice) -> Patch:
    """Closed truncated-icosahedron net as a Patch (3D embedding).

    The dart directions come from the 3D embedding: each vertex's incident
    edges are projected onto its local tangent frame so the rotation system
    yields the 32 faces.
    """
    data = C60Data()
    p = Patch(lat, [])
    # map atom key -> Site label by 2-colouring
    order = data.order
    color: dict[tuple[int, int], int] = {order[0]: 0}
    stack = [order[0]]
    adjacency: dict[tuple[int, int], list[tuple[int, int]]] = {a: [] for a in order}
    for e in data.bonds:
        a, b = tuple(e)
        adjacency[a].append(b)
        adjacency[b].append(a)
    while stack:
        a = stack.pop()
        for b in adjacency[a]:
            if b not in color:
                color[b] = 1 - color[a]
                stack.append(b)
    site_of = {a: Site(i, 0, color[a]) for i, a in enumerate(order)}
    pos3 = {a: data.atom_pos[a] for a in order}
    _b0 = data.bonds[0]
    _scale = lat.sigma_A / np.linalg.norm(pos3[_b0[0]] - pos3[_b0[1]])
    p.pos3 = {site_of[a]: pos3[a] * _scale for a in order}
    for a in order:
        p.flatpos[site_of[a]] = np.zeros(2)
    # tangent frame per vertex: normal = position direction
    for e in data.bonds:
        a, b = tuple(e)
        sa, sb = site_of[a], site_of[b]
        p.edges.add(frozenset((sa, sb)))
        for x, y in ((a, b), (b, a)):
            sx = site_of[x]
            n = pos3[x] / np.linalg.norm(pos3[x])
            d = pos3[y] - pos3[x]
            d = d - (d @ n) * n
            ref = np.array([1.0, 0.0, 0.0])
            if abs(n @ ref) > 0.9:
                ref = np.array([0.0, 1.0, 0.0])
            e1 = ref - (ref @ n) * n
            e1 /= np.linalg.norm(e1)
            e2 = np.cross(n, e1)
            p.dirs[(sx, site_of[y])] = np.array([d @ e1, d @ e2])
    return p


def _cap_patch(lat: Lattice, n: int, m: int) -> Patch | None:
    """Fullerene cap: C60 cut along a rim perpendicular to a face axis.

    ``cap(5,5)`` cuts perpendicular to a C5 axis (30 atoms, 10 dangling,
    6 pentagons).  The kept hemisphere is the one containing the lex-min
    atom.  Returns None for other (n,m).
    """
    if (n, m) != (5, 5):
        return None
    full = _c60_patch(lat)
    pos3 = full.pos3
    assert pos3 is not None
    # face centres: pentagon axis for (5,5), hexagon axis for (9,0)
    rings, _rims = full.rings_and_rims()
    want = 5
    axes = []
    for r in rings:
        if len(r) == want:
            c = np.mean([pos3[v] for v in r], axis=0)
            axes.append(c / np.linalg.norm(c))
    n_dangling = 10
    lexmin = min(pos3, key=str)
    best: frozenset[Vid] | None = None
    for axis in axes:
        dots = {v: float(pos3[v] @ axis) for v in pos3}
        # candidate cuts: midpoints between distinct dot layers
        vals = sorted(set(round(d, 6) for d in dots.values()))
        for lo, hi in itertools.pairwise(vals):
            t = (lo + hi) / 2.0
            for keep_lo in (True, False):
                cand = {v for v in pos3 if (dots[v] < t) == keep_lo}
                if lexmin not in cand:
                    continue
                bnd = [
                    v
                    for v in cand
                    if any(
                        w not in cand for e in full.edges if v in e for w in e if w != v
                    )
                ]
                # clean cut: every boundary vertex loses exactly one bond
                if not all(
                    sum(
                        1
                        for e in full.edges
                        if v in e
                        for w in e
                        if w != v and w not in cand
                    )
                    == 1
                    for v in bnd
                ):
                    continue
                if len(bnd) != n_dangling:
                    continue
                fs = frozenset(cand)
                if best is None or sorted(map(str, fs)) < sorted(map(str, best)):
                    best = fs
    if best is None:
        return None
    p = Patch(lat, [])
    p.pos3 = {v: pos3[v] for v in best}
    for v in best:
        p.flatpos[v] = np.zeros(2)
    p.edges = {e for e in full.edges if e <= best}
    p.dirs = {k: v for k, v in full.dirs.items() if k[0] in best and k[1] in best}
    return p


def _bisect_gap(lst: list[tuple[float, int]]) -> float:
    """Mid-angle of the largest cyclic gap in a sorted dart list."""
    ang = sorted(t[0] for t in lst)
    n = len(ang)
    if n == 0:
        return 0.0
    best_i, best_w = 0, -1.0
    for i in range(n):
        w = (ang[(i + 1) % n] - ang[i]) % (2.0 * math.pi)
        if w > best_w:
            best_w, best_i = w, i
    return (ang[best_i] + best_w / 2.0) % (2.0 * math.pi)


def _rim_arc(
    pos: dict[int, int],
    atoms: tuple[int, ...],
    u: int,
    v: int,
    avoid: set[int],
) -> list[int]:
    """The rim-walk arc from ``u`` to ``v`` (inclusive) that passes no
    other consumed rim endpoint -- the short way round unless that way
    clips another fused/seamed pair, in which case the long way."""
    L = len(atoms)
    i, j = pos[u], pos[v]
    fwd = [atoms[(i + t) % L] for t in range((j - i) % L + 1)]
    if not any(x in avoid for x in fwd[1:-1]):
        return fwd
    return [atoms[(i - t) % L] for t in range((i - j) % L + 1)]


def _seam_faces(
    p_atoms: tuple[int, ...],
    q_atoms: tuple[int, ...],
    fused: list[tuple[int, int]],
) -> list[tuple[int, ...]]:
    """Seam faces between consecutive fused bonds, from the rim walks.

    Face ``i`` closes fused bonds ``i`` and ``i+1`` with the rim arc on
    each port that contains no other fused endpoint.  ``fused`` lists the
    pairs in ``p``-dangling order; consecutive ``i`` are adjacent on both
    rims (the ``q`` side in reversed order).
    """
    pos_p = {v: i for i, v in enumerate(p_atoms)}
    pos_q = {v: i for i, v in enumerate(q_atoms)}
    ends_p = {a for a, _ in fused}
    ends_q = {b for _, b in fused}

    n = len(fused)
    faces = []
    for i in range(n):
        a1, b1 = fused[i]
        a2, b2 = fused[(i + 1) % n]
        pa = _rim_arc(pos_p, p_atoms, a1, a2, ends_p)
        qa = _rim_arc(pos_q, q_atoms, b2, b1, ends_q)
        faces.append(_canon_ring(pa + qa))
    return faces


def _seam_faces_k(
    rim_atoms: list[tuple[int, ...]],
    rim_dangling: list[tuple[int, ...]],
    seam_ords: list[int],
    kphase: int,
) -> list[tuple[int, ...]]:
    """Seam faces for a k>=3 seam (SPEC 11.3/6.2), generalising
    ``_seam_faces`` from one direct rim-to-rim bond to a chain of seam
    atoms each bonded to one dangling atom of every rim.

    The rims are taken in a cyclic order (the order they were authored);
    faces come in ``k`` cyclic-consecutive families -- for k=3 that is
    exactly the "AB, BC, CA" triple from SPEC 6.2.  Family ``(j, j+1)``'s
    face at seam period ``i`` borders seam atom ``i``, the rim-``j`` arc
    from its period-``i`` to period-``(i+1)`` dangling atom, seam atom
    ``i+1``, and the rim-``(j+1)`` arc back from period-``(i+1)`` to
    period-``i``.  The phase index is applied exactly as ``fuse`` applies
    it to its destination rim -- ``rim0`` at index ``i``, every rim after
    it at index ``(kphase - i) % n`` -- so a (disallowed) k=2 seam would
    degenerate to the fuse convention.
    """
    n = len(seam_ords)
    krim = len(rim_atoms)

    def idx(j: int, i: int) -> int:
        return i if j == 0 else (kphase - i) % n

    positions = [{v: p for p, v in enumerate(atoms)} for atoms in rim_atoms]
    avoids = [set(d) for d in rim_dangling]
    faces = []
    for j in range(krim):
        j2 = (j + 1) % krim
        for i in range(n):
            i2 = (i + 1) % n
            a1 = rim_dangling[j][idx(j, i)]
            a2 = rim_dangling[j][idx(j, i2)]
            b1 = rim_dangling[j2][idx(j2, i)]
            b2 = rim_dangling[j2][idx(j2, i2)]
            arc_a = _rim_arc(positions[j], rim_atoms[j], a1, a2, avoids[j])
            arc_b = _rim_arc(positions[j2], rim_atoms[j2], b2, b1, avoids[j2])
            face = [seam_ords[i], *arc_a, seam_ords[i2], *arc_b]
            faces.append(_canon_ring(face))
    return faces


def _compute_sheets(
    atoms: list[Atom],
    bonds: list[tuple[int, int, int]],
    attach: set[tuple[int, int]],
    seam_atom_ords: set[int],
) -> tuple[tuple[str, tuple[int, ...]], ...]:
    """The sheet partition (SPEC 6.3): connected components of atoms
    joined by fuse, excluding bond-verb attachments (a generic covalent
    join, not a manifold gluing -- SPEC 6.4/11.3) and seam atoms/bonds (a
    k>=3 seam is explicitly not a fuse -- it does not merge sheets).

    Judgment call: SPEC phrases the partition as a union-find over faces
    sharing an edge.  A fuse never adds atoms, and every seam face always
    includes a seam atom (excluded from every sheet by construction), so
    the face-adjacency and atom-adjacency constructions land on the same
    components here; this build does the equivalent, simpler atom-graph
    version, which also directly gives each sheet's vertex/edge set for
    the per-sheet Euler bookkeeping in check.py.
    """
    parent: dict[int, int] = {
        a.ord: a.ord for a in atoms if a.ord not in seam_atom_ords
    }

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i, j, _ in bonds:
        if i in seam_atom_ords or j in seam_atom_ords:
            continue
        if (min(i, j), max(i, j)) in attach:
            continue
        union(i, j)

    groups: dict[int, list[int]] = {}
    for o in parent:
        groups.setdefault(find(o), []).append(o)

    inst_of = {a.ord: a.instance for a in atoms}
    ordered = sorted(groups.values(), key=min)
    out = []
    for i, ords in enumerate(ordered):
        insts = sorted({inst_of[o] for o in ords})
        name = insts[0] if len(insts) == 1 else f"sheet{i}"
        out.append((name, tuple(sorted(ords))))
    return tuple(out)


def _atom_ref_ord(
    ref: str,
    path_to_ord: dict[AtomPath, int],
    frag_names: set[str],
) -> tuple[int | None, str | None]:
    """'inst/(u,v,s)' -> ord; 'frag.N' -> (None, fragname)."""
    head = ref.split(".", 1)[0].split("/", 1)[0]
    if head in frag_names and "/" not in ref:
        return None, head
    try:
        return path_to_ord.get(AtomPath.parse(ref)), None
    except ValueError:
        return None, None


def _frame(pos: np.ndarray, dang: tuple[int, ...]) -> tuple[np.ndarray, np.ndarray]:
    """Rim frame from dangling-atom seed positions: centroid + normal.

    The normal is the least-variance axis of the dangling ring, signed to
    point away from the instance centroid (outward through the opening).
    """
    pts = pos[list(dang)]
    c = pts.mean(axis=0)
    cov = (pts - c).T @ (pts - c)
    n = np.linalg.eigh(cov)[1][:, 0]
    inst_c = pos.mean(axis=0)
    if float(n @ (c - inst_c)) < 0:
        n = -n
    return c, n


def _rot_min(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Minimal rotation taking unit vector a to unit vector b."""
    v = np.cross(a, b)
    c = float(a @ b)
    if c < -1.0 + 1e-9:  # antiparallel: rotate pi about any perpendicular
        ax = np.cross(a, np.array([1.0, 0.0, 0.0]))
        if np.linalg.norm(ax) < 1e-9:
            ax = np.cross(a, np.array([0.0, 1.0, 0.0]))
        ax = ax / np.linalg.norm(ax)
        return _rot_axis(ax, math.pi)
    kk = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    r: np.ndarray = np.eye(3) + kk + kk @ kk / (1.0 + c)
    return r


def _rot_axis(ax: np.ndarray, th: float) -> np.ndarray:
    ax = ax / np.linalg.norm(ax)
    x, y, z = ax
    c, s = math.cos(th), math.sin(th)
    m: np.ndarray = np.array(
        [
            [
                c + x * x * (1 - c),
                x * y * (1 - c) - z * s,
                x * z * (1 - c) + y * s,
            ],
            [
                y * x * (1 - c) + z * s,
                c + y * y * (1 - c),
                y * z * (1 - c) - x * s,
            ],
            [
                z * x * (1 - c) - y * s,
                z * y * (1 - c) + x * s,
                c + z * z * (1 - c),
            ],
        ]
    )
    return m


def _fuse_transform(
    pos: np.ndarray,
    p_dang: tuple[int, ...],
    q_dang: tuple[int, ...],
    k: int,
    sigma: float,
) -> tuple[np.ndarray, np.ndarray]:
    """(R, t) mapping instance Q's seed so its port faces P's fused port.

    Centroids coincide offset by sigma along P's rim normal; normals are
    antiparallel; the residual twist about the normal pairs
    ``P.dangling[i]`` with ``Q.dangling[(k-i) mod N]``.
    """
    c_p, n_p = _frame(pos, p_dang)
    c_q, n_q = _frame(pos, q_dang)
    r0 = _rot_min(n_q, -n_p)
    n = len(p_dang)
    # best twist about n_p: circular mean of the angular offsets
    basis_u = np.cross(n_p, np.array([1.0, 0.0, 0.0]))
    if np.linalg.norm(basis_u) < 1e-9:
        basis_u = np.cross(n_p, np.array([0.0, 1.0, 0.0]))
    basis_u /= np.linalg.norm(basis_u)
    basis_v = np.cross(n_p, basis_u)

    def ang(x: np.ndarray, c: np.ndarray) -> float:
        d = x - c
        return math.atan2(float(d @ basis_v), float(d @ basis_u))

    c_qr = r0 @ c_q
    offs = []
    for i in range(n):
        a = ang(pos[p_dang[i]], c_p)
        b = ang(r0 @ pos[q_dang[(k - i) % n]], c_qr)
        offs.append(a - b)
    th = math.atan2(sum(math.sin(o) for o in offs), sum(math.cos(o) for o in offs))
    r = _rot_axis(n_p, th) @ r0
    t = c_p + sigma * n_p - r @ c_q
    return r, t


def _bond_transform(
    pos: np.ndarray,
    a_ord: int,
    b_ord: int,
    sigma: float,
) -> tuple[np.ndarray, np.ndarray]:
    """(R, t) placing instance B so atom b sits sigma outside atom a.

    B's bulk lies on the far side: the b->B-centroid direction maps onto
    the outward direction u = a - centroid(A)."""
    c_a = pos.mean(axis=0)
    u = pos[a_ord] - c_a
    u = u / (np.linalg.norm(u) or 1.0)
    b_centroid_off = pos.mean(axis=0) - pos[b_ord]
    # rotate (b -> centroid B) onto +u so B's body sits beyond the bond
    r = _rot_min(b_centroid_off / (np.linalg.norm(b_centroid_off) or 1.0), u)
    t = pos[a_ord] + sigma * u - r @ pos[b_ord]
    return r, t


def _place_seeds(
    spec: Spec,
    net: Net,
    fuse_frames: list[tuple[str, tuple[int, ...], str, tuple[int, ...], int]],
    bond_links: list[tuple[int, int]],
) -> tuple[tuple[tuple[float, float, float], ...] | None, str]:
    """Rigidly place per-instance seeds along the connect graph.

    BFS from the origin instance: each fused port aligns its neighbour by
    the fused-port frame (centroids sigma apart along the normal, normals
    antiparallel, k-phase twist); each bond link places the dst instance
    sigma outside the src atom.  Unconnected instances keep their local
    seed.  Returns (seed3, seed_kind); seed_kind is "mixed" when any
    transform was applied.
    """
    if net.seed3 is None:
        return net.seed3, net.seed_kind
    pos = np.array(net.seed3, dtype=np.float64)
    inst_of = {a.ord: a.instance for a in net.atoms}
    sigma = net.lattice.sigma_A

    # per-instance local coords and ord lists
    inst_ords: dict[str, list[int]] = {}
    for a in net.atoms:
        inst_ords.setdefault(a.instance, []).append(a.ord)

    edges: dict[str, list[tuple[str, np.ndarray, np.ndarray]]] = {}

    def add(inst_a: str, inst_b: str, r: np.ndarray, t: np.ndarray) -> None:
        edges.setdefault(inst_b, []).append((inst_a, r, t))

    for _pn, p_dang, _qn, q_dang, k in fuse_frames:
        ia, ib = inst_of[p_dang[0]], inst_of[q_dang[0]]
        if ia == ib:
            continue
        r, t = _fuse_transform(pos, p_dang, q_dang, k, sigma)
        add(ia, ib, r, t)
        r2, t2 = _fuse_transform(pos, q_dang, p_dang, k, sigma)
        add(ib, ia, r2, t2)
    for ai, bi in bond_links:
        ia, ib = inst_of[ai], inst_of[bi]
        if ia == ib:
            continue
        r, t = _bond_transform(pos, ai, bi, sigma)
        add(ia, ib, r, t)
        r2, t2 = _bond_transform(pos, bi, ai, sigma)
        add(ib, ia, r2, t2)

    origin = spec.origin or net.atoms[0].instance
    placed: dict[str, tuple[np.ndarray, np.ndarray]] = {
        origin: (np.eye(3), np.zeros(3))
    }
    queue = [origin]
    while queue:
        cur = queue.pop(0)
        r_c, t_c = placed[cur]
        for nxt, r_e, t_e in sorted(edges.get(cur, []), key=lambda x: x[0]):
            if nxt in placed:
                continue
            placed[nxt] = (r_e @ r_c, r_e @ t_c + t_e)
            queue.append(nxt)
    if len(placed) == len(inst_ords):
        pass
    if len(placed) <= 1:
        return net.seed3, net.seed_kind
    for inst, (r, t) in placed.items():
        idx = inst_ords[inst]
        pos[idx] = pos[idx] @ r.T + t
    seed: tuple[tuple[float, float, float], ...] = tuple(
        (float(r0), float(r1), float(r2)) for r0, r1, r2 in pos
    )
    return seed, "mixed"


def _select_ring(
    patch: Patch, ring_size: int, site: Site, hdir: int | None
) -> list[Vid] | None:
    """The ring of ``ring_size`` incident on ``site`` (``:d`` disambiguates,
    else lex-min canonical tuple) — same rule as ``_apply_hole``."""
    rings, _ = patch.rings_and_rims()
    cands = [r for r in rings if len(r) == ring_size and site in r]
    if hdir is not None and len(cands) > 1:
        p0 = patch.flatpos[site]

        def ring_dir(r: list[Vid]) -> int:
            cen = np.mean([patch.flatpos[v] for v in r], axis=0)
            d = cen - p0
            return round(math.degrees(math.atan2(d[1], d[0])) / 60.0) % 6

        by_dir = [r for r in cands if ring_dir(r) == hdir % 6]
        if by_dir:
            cands = by_dir
    cands.sort(key=_ring_canon)
    return cands[0] if cands else None


def _apply_terminate(
    spec: Spec,
    net: Net,
    findings: list[Finding],
) -> Net:
    """``terminate: inst.port = X`` — cap dangling rim atoms with element X.

    One termination atom per dangling atom (bond order 1); its atom path
    is ``inst/H/<dangling atom path>`` with ``hyb="s"``.  Terminated ports
    are consumed.  H seeds at sigma_CH along the outward in-plane
    bisector of the carbon's two rim bonds.
    """
    if not spec.terminate:
        return net
    atoms = list(net.atoms)
    bonds = list(net.bonds)
    ports = dict(net.ports)
    seed: list[tuple[float, float, float]] | None = (
        list(net.seed3) if net.seed3 is not None else None
    )
    pos = np.array(net.seed3, dtype=np.float64) if net.seed3 is not None else None
    nbr: dict[int, list[int]] = {a.ord: [] for a in atoms}
    for i, j, _o in net.bonds:
        nbr[i].append(j)
        nbr[j].append(i)
    new_ord = len(atoms)
    term_rims: list[tuple[tuple[int, ...], int, int]] = list(net.term_rims)
    for glob, elem, gspan in spec.terminate:
        ginst, _, gport = glob.partition(".")
        matched = False
        for name, port in list(ports.items()):
            if not port.dangling:
                continue
            inst = atoms[port.dangling[0]].instance
            pname = name[len(inst) + 1 :] if name.startswith(inst + ".") else name
            if ginst != inst or gport not in ("*", pname):
                continue
            matched = True
            for d_ord in port.dangling:
                cpath = atoms[d_ord].path
                hpath = AtomPath(cpath.instance, cpath.site, cpath.defect, h=True)
                atoms.append(
                    Atom(
                        path=hpath,
                        ord=new_ord,
                        element=elem,
                        hyb="s",
                        instance=inst,
                    )
                )
                bonds.append((min(d_ord, new_ord), max(d_ord, new_ord), 1))
                if pos is not None:
                    u = np.zeros(3)
                    for nb in nbr[d_ord]:
                        dv = pos[nb] - pos[d_ord]
                        ln = np.linalg.norm(dv)
                        if ln > 1e-9:
                            u += dv / ln
                    out = (
                        -u / np.linalg.norm(u)
                        if np.linalg.norm(u)
                        else np.array([0.0, 0.0, 1.0])
                    )
                    assert seed is not None
                    seed.append(tuple(pos[d_ord] + out * net.lattice.sigma_CH_A))
                new_ord += 1
            findings.append(
                Finding(
                    "terminate.done",
                    Severity.INFO,
                    f"{inst} terminated {name} with {len(port.dangling)} {elem}",
                )
            )
            term_rims.append((tuple(port.atoms), port.b, port.b_expected))
            del ports[name]
        if not matched:
            findings.append(
                Finding(
                    "op.dangling",
                    Severity.ERROR,
                    f"terminate op matches no port: {glob!r}",
                    where=glob,
                    span=gspan,
                    data=(("op", "terminate"),),
                )
            )
    return Net(
        atoms=tuple(atoms),
        bonds=tuple(sorted(bonds)),
        rings=net.rings,
        ports=tuple(sorted(ports.items())),
        regions=net.regions,
        report=net.report,
        spec=spec,
        lattice=net.lattice,
        seed3=tuple(seed) if seed is not None else net.seed3,
        seed_kind=net.seed_kind,
        attach=net.attach,
        consumed_rims=net.consumed_rims,
        term_rims=tuple(term_rims),
        # pre-existing gap fixed in passing: this constructor dropped
        # registry_edges (masked because check_registry's empty-edges
        # fallback also emits registry.redundant, so a tree-shaped part
        # graph looked the same either way; a genuine cycle plus a
        # `terminate` would have silently lost its registry.closure).
        registry_edges=net.registry_edges,
        seams=net.seams,
        seam_rims=net.seam_rims,
        sheet_atoms=net.sheet_atoms,
        sheets=net.sheets,
    )


def _c3_orbits(
    pos: dict[Vid, np.ndarray],
    centre: np.ndarray,
    axis: np.ndarray,
    members: set[Vid],
) -> list[list[Vid]]:
    """C3 orbits of ``members`` about (centre, axis) in 2D/3D positions."""
    two_d = next(iter(pos.values())).shape[0] == 2
    rot = np.array(
        [
            [math.cos(2 * math.pi / 3), -math.sin(2 * math.pi / 3)],
            [math.sin(2 * math.pi / 3), math.cos(2 * math.pi / 3)],
        ]
    )
    seen: set[frozenset[Vid]] = set()
    orbits: list[list[Vid]] = []
    for v in sorted(members, key=str):
        p = pos[v] - centre
        chain = [v]
        ok = True
        q = p
        for _ in range(2):
            if two_d:
                q = rot @ q
                target = centre + q
            else:
                t = 2 * math.pi / 3
                q = (
                    math.cos(t) * q
                    + math.sin(t) * np.cross(axis, q)
                    + (1 - math.cos(t)) * axis * (axis @ q)
                )
                target = centre + q
            nv = min(pos, key=lambda w: float(np.linalg.norm(pos[w] - target)))
            if float(np.linalg.norm(pos[nv] - target)) > 0.1 or nv not in members:
                ok = False
                break
            chain.append(nv)
        fs = frozenset(chain)
        if ok and len(chain) == 3 and fs not in seen:
            seen.add(fs)
            orbits.append(sorted(chain, key=str))
    return sorted(orbits, key=lambda o: [str(x) for x in o])


def _shortest_path(adj: dict[Vid, set[Vid]], a: Vid, b: Vid) -> list[Vid] | None:
    """Lex-min shortest path a..b (BFS with sorted expansion)."""
    from collections import deque

    prev: dict[Vid, Vid | None] = {a: None}
    dq = deque([a])
    while dq:
        cur = dq.popleft()
        if cur == b:
            break
        for y in sorted(adj[cur], key=str):
            if y not in prev:
                prev[y] = cur
                dq.append(y)
    if b not in prev:
        return None
    path = []
    x: Vid | None = b
    while x is not None:
        path.append(x)
        x = prev[x]
    return path[::-1]


def _solve_bud_attach(
    menu: str,
    bud_key: str,
    bud_port: Port,
    host_key: str,
    hsite: Site,
    hdir: int | None,
    patches: dict[str, Patch],
    ords: dict[tuple[str, Vid], int],
    findings: list[Finding],
    span: tuple[int, int] | None,
) -> tuple[list[tuple[Vid, Vid]], list[tuple[int, ...]], dict[str, Any]] | None:
    """Solve the C3-symmetric six-bond attachment of a C54 hole rim onto the
    atoms around one intact host hexagon (Wang & Li 2009 9-6 / 8-7).

    Enumerates the two C3 orbits of the bud's six dangling atoms against
    the C3 orbits of the host hexagon's vertex+first-neighbour shell;
    keeps the lex-min registration producing the named seam multiset.
    """
    target = {6: 3, 9: 3} if menu == "9-6" else {7: 3, 8: 3}
    bud = patches[bud_key]
    host = patches[host_key]
    ring = _select_ring(host, 6, hsite, hdir)
    if ring is None:
        findings.append(
            Finding(
                "hole.missing",
                Severity.ERROR,
                f"no hexagon contains {hsite} on {host_key}",
                span=span,
            )
        )
        return None
    hcen = np.mean([host.flatpos[v] for v in ring], axis=0)
    shell = set(ring)
    for v in ring:
        shell |= {n for n in neighbors(cast(Site, v)) if n in host.flatpos}
    horbits = _c3_orbits(host.flatpos, hcen, np.array([0.0, 0.0]), shell)

    # bud side: rim atoms in walk order; orbits in the 3D embedding
    vid_of_ord = {o: v for (k, v), o in ords.items() if k == bud_key}
    rim_walk = [vid_of_ord[o] for o in bud_port.atoms]
    dang = [vid_of_ord[o] for o in bud_port.dangling]
    pos3 = bud.pos3 if bud.pos3 is not None else bud.flatpos
    rim_pos = [pos3[v] for v in rim_walk]
    bcen = np.mean(rim_pos, axis=0)
    if rim_pos[0].shape[0] == 3:
        P = np.array(rim_pos) - bcen
        baxis = np.linalg.eigh(P.T @ P)[1][:, 0]
    else:
        baxis = np.zeros(2)
    borbits = _c3_orbits(pos3, bcen, baxis, set(dang))
    if len(horbits) < 2 or len(borbits) != 2:
        findings.append(
            Finding(
                "fit.unsolvable",
                Severity.ERROR,
                f"{menu}: no two C3 orbits on host shell / bud rim",
                span=span,
            )
        )
        return None

    # cyclic order inside an orbit: angular order about its centre
    def cyc(orbit: list[Vid], pos: dict[Vid, np.ndarray], cen: np.ndarray) -> list[Vid]:
        return sorted(
            orbit,
            key=lambda v: math.atan2(
                float((pos[v] - cen)[1]), float((pos[v] - cen)[0])
            ),
        )

    bcyc = [cyc(o, pos3, bcen) for o in borbits]
    hcyc = [cyc(o, host.flatpos, hcen) for o in horbits]
    h_adj: dict[Vid, set[Vid]] = {v: set() for v in host.flatpos}
    for e in host.edges:
        a, b = tuple(e)
        h_adj[a].add(b)
        h_adj[b].add(a)
    rim_pos_of = {v: i for i, v in enumerate(rim_walk)}
    rl = len(rim_walk)

    def rim_arc(a: Vid, b: Vid) -> list[Vid]:
        i, j = rim_pos_of[a], rim_pos_of[b]
        return [rim_walk[(i + t) % rl] for t in range((j - i) % rl + 1)]

    best: (
        tuple[
            tuple[int, int, int, int, int, int],
            list[tuple[Vid, Vid]],
            list[tuple[int, ...]],
        ]
        | None
    ) = None
    for oi in range(len(hcyc)):
        for oj in range(len(hcyc)):
            if oi == oj:
                continue
            for r1 in (0, 1):
                for r2 in (0, 1):
                    for s1 in range(3):
                        for s2 in range(3):
                            o1 = hcyc[oi][:: -1 if r1 else 1]
                            o2 = hcyc[oj][:: -1 if r2 else 1]
                            mp: dict[Vid, Vid] = {}
                            for i in range(3):
                                mp[bcyc[0][i]] = o1[(i + s1) % 3]
                                mp[bcyc[1][i]] = o2[(i + s2) % 3]
                            # seam faces: consecutive dangling atoms on the
                            # rim, each closing a face with the host path
                            # between their attachment atoms
                            order = sorted(dang, key=lambda v: rim_pos_of[v])
                            faces: list[tuple[int, ...]] = []
                            bad = False
                            for i in range(len(order)):
                                bi = order[i]
                                bj = order[(i + 1) % len(order)]
                                arc = rim_arc(bi, bj)
                                hp = _shortest_path(h_adj, mp[bj], mp[bi])
                                if hp is None:
                                    bad = True
                                    break
                                faces.append(
                                    _canon_ring(
                                        [ords[(bud_key, v)] for v in arc]
                                        + [ords[(host_key, v)] for v in hp]
                                    )
                                )
                            if bad:
                                continue
                            sizes: dict[int, int] = {}
                            for f in faces:
                                sizes[len(f)] = sizes.get(len(f), 0) + 1
                            if sizes != target:
                                continue
                            key = (oi, oj, r1, r2, s1, s2)
                            bonds = [(bv, mp[bv]) for bv in order]
                            if best is None or key < best[0]:
                                best = (key, bonds, faces)
    if best is None:
        findings.append(
            Finding(
                "fit.unsolvable",
                Severity.ERROR,
                f"{menu}: no C3 registration gives the named seam",
                span=span,
            )
        )
        return None
    return best[1], best[2], {"registration": list(best[0])}


def _record_k(spec: Spec, ci: int, k: int) -> Spec:
    """Record the fitted fuse phase on the connect's expanded."""
    conn = spec.connects[ci]
    exp = dict(conn.expanded or {})
    exp["k"] = k
    return replace(
        spec,
        connects=tuple(
            replace(c, expanded=exp) if i == ci else c
            for i, c in enumerate(spec.connects)
        ),
    )


def _record_menu(
    spec: Spec,
    ci: int,
    vbonds: list[tuple[Vid, Vid]],
    bud_key: str,
    host_key: str,
    ords: dict[tuple[str, Vid], int],
) -> Spec:
    """Record the solved six-bond registration on the menu connect."""
    conn = spec.connects[ci]
    exp = dict(conn.expanded or {})
    lines = [f"{bud_key}/{bv} --bond--> {host_key}/{hv}" for bv, hv in vbonds]
    # idempotent: a rebuild of an already-solved spec (canonical_json on a
    # Net, the sectioned-JSON hash) re-solves the same registration and
    # must not append the six lines a second time
    prior = list(exp.get("connects", []))
    exp["connects"] = prior + sorted(ln for ln in set(lines) if ln not in prior)
    return replace(
        spec,
        connects=tuple(
            replace(c, expanded=exp) if i == ci else c
            for i, c in enumerate(spec.connects)
        ),
    )


def _apply_connects(
    spec: Spec,
    net: Net,
    ctx: dict[str, Any],
    patches: dict[str, Patch],
    conn_rims: dict[int, tuple[str, frozenset[Vid]]],
    findings: list[Finding],
) -> tuple[Net, Spec]:
    """Execute fuse/bond connects on the assembled net."""
    ords = ctx["ords"]
    path_to_ord: dict[AtomPath, int] = ctx["path_to_ord"]
    frag_names = {f.name for f in spec.frags}
    ports = dict(net.ports)
    bonds = list(net.bonds)
    consumed_rims: list[tuple[int, ...]] = []
    rings = list(net.rings)
    fused_edges: list[tuple[int, int]] = []
    fuse_frames: list[tuple[str, tuple[int, ...], str, tuple[int, ...], int]] = []
    bond_links: list[tuple[int, int]] = []
    attach_bonds: list[tuple[int, int]] = []
    atoms = list(net.atoms)
    # fullerene atom vids are Sites too, but the C60 2-colouring is not a
    # physical sublattice — only hex-lattice instances (sheet/tube/cone)
    # count as host sites for annot.sublattice
    key_kind = {}
    for at in atoms:
        ii = spec.instance(at.instance)
        key_kind[at.instance] = ii.kind if ii is not None else ""
    lat_keys = {k for k, kd in key_kind.items() if kd != "fullerene"}
    site_of_ord = {
        o: v for (k, v), o in ords.items() if isinstance(v, Site) and k in lat_keys
    }

    def annot(i: int, j: int, where: str) -> str | None:
        """Sublattice annotation: parity when both endpoints are lattice
        sites; the host-side sublattice when exactly one is (a fullerene
        atom is not a bipartite site)."""
        sa, sb = site_of_ord.get(i), site_of_ord.get(j)
        if sa is not None and sb is not None:
            findings.append(
                Finding(
                    "annot.sublattice",
                    Severity.INFO,
                    f"bond {i}-{j} parity={'same' if sa.s == sb.s else 'cross'}",
                    where=where,
                    data=(("parity", "same" if sa.s == sb.s else "cross"),),
                )
            )
            return None
        host_site = sa if sa is not None else sb
        if host_site is None:
            return None
        sub = "A" if host_site.s == 0 else "B"
        findings.append(
            Finding(
                "annot.sublattice",
                Severity.INFO,
                f"bond {i}-{j} host sublattice={sub}",
                where=where,
                data=(("host_sublattice", sub),),
            )
        )
        return sub

    for ci, c in enumerate(spec.connects):
        if c.verb == "menu":
            name = (c.menu or "").split("(", 1)[0]
            if name not in ("9-6", "8-7"):
                continue
            bud_port = ports.get(f"{c.src}.hole")
            _i2, hsite, hdir = _fuse_dst("x@" + c.dst)
            host_key = c.dst.split("/", 1)[0]
            if bud_port is None or hsite is None:
                findings.append(
                    Finding(
                        "port.unknown",
                        Severity.ERROR,
                        f"{name} needs a bud hole port and host site",
                        span=c.span,
                    )
                )
                continue
            got = _solve_bud_attach(
                name,
                c.src,
                bud_port,
                host_key,
                hsite,
                hdir,
                patches,
                ords,
                findings,
                c.span,
            )
            if got is None:
                continue
            vbonds, sfaces, _reg = got
            host_subs: dict[str, int] = {"A": 0, "B": 0}
            for bv, hv in vbonds:
                a, b = ords[(c.src, bv)], ords[(host_key, hv)]
                bonds.append((min(a, b), max(a, b), 1))
                sub = annot(a, b, c.src)
                if sub is not None:
                    host_subs[sub] += 1
                attach_bonds.append((min(a, b), max(a, b)))
            findings.append(
                Finding(
                    "annot.host_sublattices",
                    Severity.INFO,
                    f"{name} host atoms A={host_subs['A']} B={host_subs['B']}",
                    span=c.span,
                    data=(("A", host_subs["A"]), ("B", host_subs["B"])),
                )
            )
            rings.extend(sfaces)
            scensus: dict[int, int] = {}
            for f in sfaces:
                scensus[len(f)] = scensus.get(len(f), 0) + 1
            findings.append(
                Finding(
                    "seam.rings",
                    Severity.INFO,
                    f"{name} seam rings {dict(sorted(scensus.items()))}",
                    span=c.span,
                    data=(("k", 2), ("rings", dict(sorted(scensus.items())))),
                )
            )
            # the bud rim is fully bonded: the hole port is consumed
            consumed_rims.append(tuple(bud_port.atoms))
            del ports[bud_port.name]
            spec = _record_menu(spec, ci, vbonds, c.src, host_key, ords)
            continue
        if c.verb == "fuse":
            p = ports.get(c.src)
            q: Port | None = ports.get(c.dst)
            if q is None or q is p:
                q = None
                if ci in conn_rims:
                    key, rim_fs = conn_rims[ci]
                    want = {ords[(key, v)] for v in rim_fs}
                    for port in ports.values():
                        if port is not p and set(port.atoms) == want:
                            q = port
                            break
            if p is None or q is None:
                findings.append(
                    Finding(
                        "port.unknown",
                        Severity.ERROR,
                        f"fuse endpoints unresolved: {c.src} -> {c.dst}",
                        span=c.span,
                    )
                )
                continue
            if len(p.dangling) != len(q.dangling):
                findings.append(
                    Finding(
                        "port.mismatch",
                        Severity.ERROR,
                        f"port sizes {len(p.dangling)} != {len(q.dangling)}",
                        span=c.span,
                    )
                )
                continue
            n = len(p.dangling)
            if c.k == -1:
                # k=fit: enumerate every registration (SPEC 12.1 family),
                # rank by (2) smallest max seam-ring size, (3) |residual|,
                # (4) lowest k — DA-neck host seat.  V, bond count and the
                # total seam face count are the same for every kc (only
                # which atoms pair up changes, not how many faces or bonds
                # form), so the seam's own contribution to the counting
                # law, sum(6-n) over its faces, differs from the true
                # global euler.residual by a constant that is the same
                # for every kc here — ranking on the local term ranks the
                # family identically to ranking on the global one.
                candidates: list[tuple[int, float, float, int]] = []
                for kc in range(min(n, _FIT_CAP)):
                    trial = [
                        (p.dangling[i], q.dangling[(kc - i) % n]) for i in range(n)
                    ]
                    fs = _seam_faces(p.atoms, q.atoms, trial)
                    mx = max((len(f) for f in fs), default=0)
                    local_resid = abs(sum(6 - len(f) for f in fs))
                    candidates.append((kc, float(mx), float(local_resid), kc))
                ranked = _rank_fit(candidates)
                kk = ranked[0][0]
                spec = _record_k(spec, ci, kk)
                findings.append(
                    _fit_alternatives_finding("k", c.src, c.span, kk, ranked[1:])
                )
            else:
                kk = c.k or 0
            fuse_frames.append((p.name, p.dangling, q.name, q.dangling, kk))
            new = [(p.dangling[i], q.dangling[(kk - i) % n]) for i in range(n)]
            for a, b in new:
                bonds.append((min(a, b), max(a, b), 1))
                annot(a, b, c.src)
            fused_edges.extend(new)
            faces = _seam_faces(p.atoms, q.atoms, new)
            fcensus: dict[int, int] = {}
            for face in faces:
                rings.append(face)
                fcensus[len(face)] = fcensus.get(len(face), 0) + 1
            findings.append(
                Finding(
                    "seam.rings",
                    Severity.INFO,
                    f"seam rings {dict(sorted(fcensus.items()))}",
                    span=c.span,
                    data=(("k", 2), ("rings", dict(sorted(fcensus.items())))),
                )
            )
            for sz in fcensus:
                if not 4 <= sz <= 8:
                    findings.append(
                        Finding(
                            "ring.size.unusual",
                            Severity.WARN,
                            f"seam ring of size {sz}",
                            span=c.span,
                        )
                    )
            consumed_rims.append(tuple(p.atoms))
            consumed_rims.append(tuple(q.atoms))
            del ports[p.name]
            del ports[q.name]
        elif c.verb == "bond":
            ai, af = _atom_ref_ord(c.src, path_to_ord, frag_names)
            bi, bf = _atom_ref_ord(c.dst, path_to_ord, frag_names)
            for frag in (af, bf):
                if frag is not None:
                    findings.append(
                        Finding(
                            "frag.unrealized",
                            Severity.INFO,
                            f"fragment {frag} bond recorded, not built",
                            span=c.span,
                        )
                    )
            if ai is not None and bi is not None:
                bonds.append((min(ai, bi), max(ai, bi), c.order or 1))
                annot(ai, bi, c.src.split("/", 1)[0])
                bond_links.append((ai, bi))
                attach_bonds.append((min(ai, bi), max(ai, bi)))
            else:
                # Any endpoint that resolves to neither a real atom nor a
                # fragment names an atom that does not exist (SPEC 23.2:
                # op.dangling) -- flagged per-ref, never silently. This
                # also fixes a latent gap in the old check (which only
                # fired when *both* endpoints failed): a mixed ref pair
                # (one live fragment, one missing atom) used to fall
                # through with no finding at all.
                for ref, ordv, frag in ((c.src, ai, af), (c.dst, bi, bf)):
                    if ordv is None and frag is None:
                        findings.append(
                            Finding(
                                "op.dangling",
                                Severity.ERROR,
                                f"bond op references an atom that does not exist: {ref!r}",
                                where=ref,
                                span=c.span,
                                data=(("op", "bond"),),
                            )
                        )

    # k>=3 seams (SPEC 11.3): resolved after fuse/bond so a seam can name
    # a rim a menu/fuse just minted.  Each rim's own atoms/faces stay
    # with its own sheet (SPEC 6.3) -- a seam never merges components,
    # so it is a parallel pass, not folded into the fuse branch above.
    seam_records: list[SeamRecord] = []
    seam_rims: list[tuple[tuple[int, ...], int, int]] = []
    seam_atom_ords: set[int] = set()
    seam_registry_edges: list[tuple[str, str, int, int]] = []
    for seam in spec.seams:
        missing = [r for r in seam.rims if r not in ports]
        if missing:
            findings.append(
                Finding(
                    "port.unknown",
                    Severity.ERROR,
                    f"seam {seam.name}: rim(s) unresolved: {', '.join(missing)}",
                    span=seam.span,
                )
            )
            continue
        resolved = [ports[r] for r in seam.rims]
        counts = [len(p.dangling) for p in resolved]
        if len(set(counts)) > 1:
            maj = max(set(counts), key=counts.count)
            odd = next(r for r, cnt in zip(seam.rims, counts) if cnt != maj)
            by_rim = dict(zip(seam.rims, counts))
            findings.append(
                Finding(
                    "port.mismatch",
                    Severity.ERROR,
                    f"seam {seam.name}: rim {odd} has {by_rim[odd]} dangling "
                    f"atoms, the other rims have {maj}",
                    span=seam.span,
                    data=(("counts", by_rim),),
                )
            )
            continue

        # "compatible edge-words" (SPEC 11.3): the rims fuse regardless of
        # raw walk length (a sheet's hex(0) hole is an 18-atom rim, a
        # tube(6,0) end is 12, both 6 dangling) -- _seam_faces_k's arcs
        # handle that difference by construction.  What must agree is
        # the *turn-type skeleton*: run_length's digits are how many
        # edges share a turn type, which varies with rim length; the
        # letters/ring-size tokens are the turn *shape*, and those must
        # line up for the seam to be a simple curve rather than routing
        # through a rim with actual corners the others don't have.
        # Judgment call, since SPEC does not define "compatible".
        def _skeleton(word: str) -> str:
            return re.sub(r"\d+", "", word)

        skeletons = [_skeleton(p.word) for p in resolved]
        if len(set(skeletons)) > 1:
            maj_w = max(set(skeletons), key=skeletons.count)
            odd = next(r for r, w in zip(seam.rims, skeletons) if w != maj_w)
            by_rim_w = dict(zip(seam.rims, (p.word for p in resolved)))
            findings.append(
                Finding(
                    "port.mismatch",
                    Severity.ERROR,
                    f"seam {seam.name}: rim {odd} edge-word {by_rim_w[odd]!r} "
                    "is incompatible with the other rims' turn pattern",
                    span=seam.span,
                    data=(("words", by_rim_w),),
                )
            )
            continue
        n = counts[0]
        krim = len(resolved)

        def _idx(j: int, i: int, _n: int = n, _kk: int = seam.k) -> int:
            return i if j == 0 else (_kk - i) % _n

        new_ords: list[int] = []
        for i in range(n):
            o = len(atoms)
            path = AtomPath(seam.name, Site(0, 0, 0), label=f"s{i}")
            atoms.append(
                Atom(
                    path=path,
                    ord=o,
                    element=net.lattice.elements[0],
                    hyb="sp2",
                    instance=seam.name,
                )
            )
            path_to_ord[path] = o
            new_ords.append(o)
        for i in range(n):
            for j, port in enumerate(resolved):
                rim_ord = port.dangling[_idx(j, i)]
                a, b = new_ords[i], rim_ord
                bonds.append((min(a, b), max(a, b), 1))
                annot(min(a, b), max(a, b), seam.name)
        seam_atom_ords.update(new_ords)
        faces = _seam_faces_k(
            [p.atoms for p in resolved],
            [p.dangling for p in resolved],
            new_ords,
            seam.k,
        )
        seam_census: dict[int, int] = {}
        for face in faces:
            rings.append(face)
            seam_census[len(face)] = seam_census.get(len(face), 0) + 1
        findings.append(
            Finding(
                "seam.rings",
                Severity.INFO,
                f"{seam.name} seam rings {dict(sorted(seam_census.items()))}",
                span=seam.span,
                data=(("k", krim), ("rings", dict(sorted(seam_census.items())))),
            )
        )
        # no ring.size.unusual for k>=3 seam faces: they are in no census
        # (SPEC 6.3) -- a triple seam of hexagonal rims is 9-rings by
        # construction, and `seam.rings` above already reports the sizes
        for port in resolved:
            seam_rims.append((port.atoms, port.b, port.b_expected))
            del ports[port.name]
        # part-graph edges for registry closure: the bonding loop above
        # offsets only the primary rim by seam.k (_idx: rim 0 uses i, every
        # other rim uses (k - i) % n), so rim0-rim1 carries phase k and
        # every later consecutive pair is in direct register (phase 0)
        for j in range(krim - 1):
            seam_registry_edges.append(
                (
                    atoms[resolved[j].atoms[0]].instance,
                    atoms[resolved[j + 1].atoms[0]].instance,
                    seam.k if j == 0 else 0,
                    n,
                )
            )
        seam_records.append(SeamRecord(seam.name, seam.rims, seam.k, tuple(new_ords)))

    seed3, seed_kind = _place_seeds(spec, net, fuse_frames, bond_links)
    # part-graph edges for registry closure (SPEC 12.2) -- resolve each
    # fuse/bond endpoint to its owning instance while atoms still carry
    # their pre-hybridisation identity (ord -> instance is unaffected by
    # the sp3 relabelling below).
    inst_of_ord = {a.ord: a.instance for a in atoms}
    registry_edges: list[tuple[str, str, int, int]] = []
    for _pn, p_dang, _qn, q_dang, fk in fuse_frames:
        registry_edges.append(
            (
                inst_of_ord[p_dang[0]],
                inst_of_ord[q_dang[0]],
                fk,
                len(p_dang),
            )
        )
    for ai, bi in bond_links:
        registry_edges.append((inst_of_ord[ai], inst_of_ord[bi], 0, 1))
    registry_edges.extend(seam_registry_edges)
    # derived hybridisation: 4 bonds -> sp3; a seam atom stays sp2
    # regardless of degree (SPEC 11.3: k=3 is the sp2 case, k>3 is
    # over-valent sp2, not a promotion to sp3 -- valence.over is the
    # point, not a hybridisation change that would hide it)
    deg: dict[int, int] = {}
    for i, j, _ in bonds:
        deg[i] = deg.get(i, 0) + 1
        deg[j] = deg.get(j, 0) + 1
    atoms = [
        replace(a, hyb="sp3")
        if deg.get(a.ord, 0) == 4 and a.ord not in seam_atom_ords
        else a
        for a in atoms
    ]
    if fused_edges:
        # consumed rims carried their hole_b away; recompute the merged
        # surface's combinatorial B total and give outer rims the remainder
        sigma_terms = sum(6 - len(r) for r in set(rings))
        chi_n = len(atoms) - len(bonds) + len(set(rings))
        hole_sum = sum(p.b for _, p in ports.items() if p.normal == "hole")
        rem = 6 * chi_n - sigma_terms - hole_sum
        outer = sorted(nm for nm, p in ports.items() if p.normal != "hole")
        for oi, nm in enumerate(outer):
            ports[nm] = replace(ports[nm], b=rem if oi == 0 else 0)
    atoms_t = tuple(sorted(atoms, key=lambda a: a.ord))
    bonds_t = tuple(sorted(bonds))
    rings_t = tuple(sorted(set(rings)))
    attach_t = tuple(sorted(set(attach_bonds)))
    sheet_atoms = _compute_sheets(
        list(atoms_t), list(bonds_t), set(attach_t), seam_atom_ords
    )
    sheets = tuple(
        (name, tuple(i for i, r in enumerate(rings_t) if set(r) <= set(ords)))
        for name, ords in sheet_atoms
    )
    return Net(
        atoms=atoms_t,
        bonds=bonds_t,
        rings=rings_t,
        ports=tuple(sorted(ports.items())),
        regions=net.regions,
        report=net.report,
        spec=spec,
        lattice=net.lattice,
        seed3=seed3,
        seed_kind=seed_kind,
        attach=attach_t,
        consumed_rims=tuple(consumed_rims),
        registry_edges=tuple(registry_edges),
        seams=tuple(seam_records),
        seam_rims=tuple(seam_rims),
        sheet_atoms=sheet_atoms,
        sheets=sheets,
    ), spec
