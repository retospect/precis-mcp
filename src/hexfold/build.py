"""build(spec) -> Net: Volterra surgery per SPEC section 4.

Every primitive is a decorated lattice patch.  Phase 1 builds sheet, tube,
cone and the C60 table; connects/frags parse but do not build (phase 2
fuses ports and bonds).  Bond order is 1 everywhere in phase 1 — Kekule /
Pauling assignment is deferred.  The constructor asserts V - E + F = chi on
every component (code ``internal.euler``).
"""

from __future__ import annotations

import functools
import itertools
import json
import math
import platform
import re
from dataclasses import dataclass, replace
from typing import Any, cast

import numpy as np

from . import __version__, domains, menus
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
from .extent import snap_extents
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
from .place import place_graph
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

    @property
    def rim_type(self) -> tuple[str, int] | None:
        """SPEC 10 rim type: ``("z", N)`` for a pure zigzag rim, ``("a", N)``
        for a pure armchair rim, ``None`` for anything mixed.

        Read off the rim walk's dangling pattern, not the edge-word: the
        word records turn magnitudes (every lattice turn is 60 degrees, so
        both pure rims are ``z^n``), while the type is the *arrangement* of
        the degree-2 atoms -- alternating with degree-3 atoms (``SD``
        period, dangling bonds at 90 degrees to the rim line) for zigzag,
        in bonded pairs (``SSDD`` period, 60 degrees) for armchair.  N is
        the dangling count; two rims fuse iff N matches, and a pure-z onto
        pure-a fuse of equal N is the 30-degree grain-boundary adapter
        (its seam rings are the 5-7 line, reported by ``seam.rings``).
        Chiral tube ends, cap rims and hole rims are mixed.  Derived, never
        serialised into the authored sections (the content hash is
        unchanged); the se generator mirrors it into its port payload.
        """
        n = len(self.atoms)
        nd = len(self.dangling)
        if n == 0 or nd == 0 or n != 2 * nd:
            return None
        dang = set(self.dangling)
        pat = [a in dang for a in self.atoms]
        for r in range(2):
            if all(pat[i] == ((i + r) % 2 == 1) for i in range(n)):
                return ("z", nd)
        if n % 4 == 0:
            for r in range(4):
                if all(pat[i] == ((i + r) % 4 >= 2) for i in range(n)):
                    return ("a", nd)
        return None


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
                        **({"source": h.source} if h.source is not None else {}),
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
                        "source": c.source,
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


def _hex_flake_sites(lat: Lattice, r: int) -> list[Site]:
    """The ``hex(r)`` flake: the hexagonal cluster of rings within
    ring-adjacency radius ``r`` of the hexagon closest to the origin
    (r=0 is one hexagon, 6(r+1)**2 atoms).

    Ring-adjacency BFS on a large sheet patch -- the same algorithm the
    ``hex(r)`` hole cut uses (``build.kind`` ring_size <= -10) -- so the
    flat-lid patch and a ``- hex(r)@...`` hole are the identical cell
    complex, just centred on the nearest ring instead of an authored site.
    """
    big = Patch(lat, _hexagon_sites(2 * r + 3))
    rings, _rims = big.rings_and_rims()
    hexrings = [rr for rr in rings if len(rr) == 6]
    centre = min(
        hexrings,
        key=lambda rr: (
            round(
                float(np.linalg.norm(np.mean([big.flatpos[v] for v in rr], axis=0))),
                6,
            ),
            _ring_canon(rr),
        ),
    )
    edge_rings: dict[frozenset[Vid], list[int]] = {}
    for ri, rr in enumerate(rings):
        for ei in range(len(rr)):
            edge_rings.setdefault(
                frozenset((rr[ei], rr[(ei + 1) % len(rr)])), []
            ).append(ri)
    centre_i = next(i for i, rr in enumerate(rings) if rr is centre)
    cluster = {centre_i}
    frontier = {centre_i}
    for _ in range(r):
        nxt: set[int] = set()
        for ri in frontier:
            rr = rings[ri]
            for ei in range(len(rr)):
                for adj in edge_rings.get(
                    frozenset((rr[ei], rr[(ei + 1) % len(rr)])), ()
                ):
                    if adj not in cluster:
                        nxt.add(adj)
        cluster |= nxt
        frontier = nxt
    sites = sorted({v for ri in cluster for v in rings[ri]}, key=str)
    return cast(list[Site], sites)


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
    patch.authored_sint += sum(6 - d.ring for d in authored)
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
    lifted = _disclination_seed(patch) if patch.discl else None
    out = {}
    for v, p in patch.flatpos.items():
        if isinstance(v, Site):
            u, w = v.u, v.v
        else:
            t = cast(tuple, v)
            u, w = int(t[2]), int(t[3])
        ripple = np.array([0.0, 0.0, 0.05 * sig * math.sin(u) * math.cos(w)])
        base = lifted[v] if lifted is not None else np.array([p[0], p[1], 0.0])
        out[v] = base + ripple
    return out, "disclination" if lifted is not None else "flat-perturbed"


@functools.lru_cache(maxsize=8)
def _saddle_table(k: int) -> tuple[np.ndarray, np.ndarray, float]:
    """Unit-sphere saddle curve u(t) = (cos t, sin t, b cos 2t)/|.| whose
    length is 2*pi*(6+k)/6, as (t samples, cumulative arc length, b).

    The cone over this curve is isometric to the 360+60k deg sheet around
    an inserted wedge: an atom at intrinsic (r, phi) sits at r*u(t) with t
    the arc-length inverse of phi.
    """
    t = np.linspace(0.0, 2.0 * math.pi, 4097)
    target = 2.0 * math.pi * (6 + k) / 6.0

    def curve(b: float) -> np.ndarray:
        u = np.stack([np.cos(t), np.sin(t), b * np.cos(2.0 * t)], axis=1)
        return u / np.linalg.norm(u, axis=1)[:, None]

    def length(b: float) -> float:
        return float(np.linalg.norm(np.diff(curve(b), axis=0), axis=1).sum())

    lo, hi = 0.0, 1.0
    while length(hi) < target:
        hi *= 2.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if length(mid) < target else (lo, mid)
    b = 0.5 * (lo + hi)
    seg = np.linalg.norm(np.diff(curve(b), axis=0), axis=1)
    return t, np.concatenate([[0.0], np.cumsum(seg)]), b


def _disclination_seed(patch: Patch) -> dict[Vid, np.ndarray]:
    """Sheet seed that unstacks inserted wedges (C3, gr459567).

    Ring defects whose cores lie within 2.3 lattice cells (4 sigma) of each
    other, by single linkage, form one cluster -- a 5-7 glyph or a
    Stone-Wales quad, whose cores sit one cell apart; a lone defect is its
    own.  Two authored heptagons within that radius therefore merge into
    one K = 2 cluster with a single saddle of order 2 about their centre.
    Each cluster gets one intrinsic angle about its centre: the flat angle
    from a reference direction clear of every member's cut, plus each
    member's (intrinsic - flat) offset from its own chart -- +60 past a
    heptagon's copies, -60 past a pentagon's gap.  The cluster's net charge
    K maps that 360+60K deg onto 360: by the saddle curve's arc length
    (K > 0), uniformly (K < 0), or not at all (K = 0, a dislocation: the
    offsets alone close the gap and unstack the copies).  The turns are
    summed over clusters as displacements, so the result does not depend
    on surgery order.  On top, a charged cluster's lift -- the isometric
    saddle (K > 0) or cone (K < 0) minus the flat point -- is blended out
    between 1/4 and 1/2 of the distance to the nearest other cluster; a
    lone cluster keeps it to the sheet edge.  A K = 0 cluster is an edge
    dislocation: its turns leave the Burgers vector open on one row of
    bonds, which :func:`_volterra` closes.
    """
    recs = patch.discl
    clusters: list[list[int]] = []
    link = 4.0 * patch.lat.sigma_A
    for i, r in enumerate(recs):
        hit = [
            c
            for c in clusters
            if any(float(np.linalg.norm(r.core - recs[j].core)) <= link for j in c)
        ]
        merged = [i] + [j for c in hit for j in c]
        clusters = [c for c in clusters if c not in hit] + [sorted(merged)]
    centres = [np.mean([recs[j].core for j in c], axis=0) for c in clusters]
    reach: list[float | None] = []
    for ci, c0 in enumerate(centres):
        ds = [
            float(np.linalg.norm(c0 - c1)) for cj, c1 in enumerate(centres) if cj != ci
        ]
        reach.append(0.5 * min(ds) if ds else None)
    refs = [_clear_direction([recs[j] for j in c]) for c in clusters]
    # net-zero clusters: the turn each vertex got, to label the cut after
    turns: dict[int, dict[Vid, float]] = {
        ci: {} for ci, c in enumerate(clusters) if sum(recs[j].k for j in c) == 0
    }

    def flat_deg(v: np.ndarray, o: np.ndarray) -> float:
        return math.degrees(math.atan2(float(v[1] - o[1]), float(v[0] - o[0])))

    out: dict[Vid, np.ndarray] = {}
    for v, p in patch.flatpos.items():
        xy = np.asarray(p, dtype=float)[:2]
        disp = np.zeros(2)
        lift = np.zeros(3)
        for ci, (c, centre, ref, h) in enumerate(
            zip(clusters, centres, refs, reach, strict=True)
        ):
            rel = xy - centre
            s = float(np.linalg.norm(rel))
            if s < 1e-9:
                continue
            off = (flat_deg(xy, centre) - ref) % 360.0
            big_k = 0
            phi_tot = off
            for j in c:
                r = recs[j]
                big_k += r.k
                w = 60.0 * abs(r.k)
                total = 360.0 + 60.0 * r.k
                x = (flat_deg(xy, r.core) - r.ray) % 360.0
                x_ref = (ref - r.ray) % 360.0
                phi = r.phi.get(v)
                if phi is None:
                    phi = x if r.k > 0 else (x - w) % 360.0
                phi_ref = x_ref if r.k > 0 else (x_ref - w) % 360.0
                d_flat = (x - x_ref) % 360.0
                d_int = (phi - phi_ref) % total
                # keep this core's branch on the centre's side of the ref ray
                if d_flat - off > 180.0:
                    d_flat -= 360.0
                    d_int -= total
                elif off - d_flat > 180.0:
                    d_flat += 360.0
                    d_int += total
                phi_tot += d_int - d_flat
            total_c = 360.0 + 60.0 * big_k
            if big_k > 0:
                tt, cum, b = _saddle_table(big_k)
                e = float(np.interp(math.radians(phi_tot), cum, tt))
                u = np.array([math.cos(e), math.sin(e), b * math.cos(2.0 * e)])
                loc = s * (
                    u / np.linalg.norm(u) - np.array([math.cos(e), math.sin(e), 0.0])
                )
            elif big_k < 0:
                e = math.radians(phi_tot * 360.0 / total_c)
                sin_psi = total_c / 360.0
                cos_psi = math.sqrt(max(0.0, 1.0 - sin_psi * sin_psi))
                loc = s * np.array(
                    [
                        (sin_psi - 1.0) * math.cos(e),
                        (sin_psi - 1.0) * math.sin(e),
                        -cos_psi,
                    ]
                )
            else:
                e = math.radians(phi_tot)
                loc = np.zeros(3)
                turns[ci][v] = phi_tot - off
            dt = e - math.radians(off)
            cs, sn = math.cos(dt), math.sin(dt)
            disp += (
                np.array([cs * rel[0] - sn * rel[1], sn * rel[0] + cs * rel[1]]) - rel
            )
            fade = 1.0
            if h is not None:
                q = min(1.0, max(0.0, (s - 0.5 * h) / (0.5 * h)))
                fade = 1.0 - q * q * (3.0 - 2.0 * q)
            if big_k != 0 and fade > 0.0:
                cr, sr = math.cos(math.radians(ref)), math.sin(math.radians(ref))
                lift += fade * np.array(
                    [cr * loc[0] - sr * loc[1], sr * loc[0] + cr * loc[1], loc[2]]
                )
        out[v] = np.array([xy[0] + disp[0], xy[1] + disp[1], 0.0]) + lift
    if turns:
        cores = [
            v
            for ci in turns
            for v, p in patch.flatpos.items()
            if float(np.linalg.norm(np.asarray(p)[:2] - centres[ci][:2])) <= link
        ]
        _volterra(patch, out, list(turns.values()), cores)
    return out


def _clear_direction(members: list[Any]) -> float:
    """Reference direction (deg) farthest from every member's cut sector
    [ray, ray + 60|k|]; ties go to the smallest angle."""
    best, best_gap = 0.0, -1.0
    for step in range(72):
        a = 5.0 * step
        gap = 360.0
        for r in members:
            x = (a - r.ray) % 360.0
            w = 60.0 * abs(r.k)
            gap = min(gap, 0.0 if x <= w else min(x - w, 360.0 - x))
        if gap > best_gap + 1e-9:
            best, best_gap = a, gap
    return best


def _cut_jump(vs: np.ndarray, steps: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The one vector J that brings the cut bond vectors v to ideal lattice
    steps (v - sign*J ~ s), and each bond's sign.

    Truncated least squares at half a bond: each bond's possible
    J = v - s seeds a candidate; under it every bond takes the sign and
    nearest step that fit best and costs min(residual^2, (b/2)^2), so a
    bond the jump cannot reach -- the compressed bond shared by the core
    rings, labelled because it straddles the turn boundary -- costs a
    constant and does not pull J.  The sign is per bond because the turned
    strip a cluster seed opens has two sides: the row to the edge, and a
    bond at the core that crosses the strip the other way.  The winner is
    refined to the mean of sign*(v - s) over the bonds it closes (residual
    under b/2).  Returns (J, sign), sign 0 for a bond J does not close.
    """
    half = 0.5 * float(np.linalg.norm(steps[0]))
    best_cost, best_size = math.inf, math.inf
    best_j, best_sign = np.zeros(2), np.zeros(len(vs))

    def fit(j0: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        # per bond: sign and nearest step of v - sign*J, and the residual
        d = np.stack(
            [
                np.linalg.norm((vs - sg * j0)[:, None, :] - steps[None, :, :], axis=2)
                for sg in (1.0, -1.0)
            ]
        )
        k = np.argmin(d.transpose(1, 0, 2).reshape(len(vs), -1), axis=1)
        sign = np.where(k < len(steps), 1.0, -1.0)
        pick = steps[k % len(steps)]
        res = np.linalg.norm(vs - sign[:, None] * j0 - pick, axis=1)
        return sign, pick, res

    for j0 in (v - s for v in vs for s in steps):
        for _ in range(2):
            sign, pick, res = fit(j0)
            ok = res < half
            if not ok.any():
                break
            j0 = (sign[ok, None] * (vs[ok] - pick[ok])).mean(axis=0)
        sign, pick, res = fit(j0)
        cost = float(np.minimum(res, half) @ np.minimum(res, half))
        size = float(np.linalg.norm(j0))
        if cost < best_cost - 1e-9 or (
            abs(cost - best_cost) <= 1e-9 and size < best_size
        ):
            best_cost, best_size, best_j = cost, size, j0
            best_sign = np.where(res < half, sign, 0.0)
    return best_j, best_sign


_DENSE_MAX = 3000


def _laplace_solve(
    n: int, edges: np.ndarray, rhs: np.ndarray, gauge: list[int]
) -> np.ndarray:
    """Solve the graph Laplacian system L w = rhs (rhs (n, d)) on a
    connected graph, gauge: mean of w over ``gauge`` is 0.

    Dense ``numpy.linalg.solve`` with the gauge replacing one row up to
    3000 vertices (exact); above that, conjugate gradients on the
    mean-free system -- relative residual 1e-10, at most 10 n iterations --
    then the gauge shift.  rhs must sum to zero per column (it does: it is
    an incidence transpose).
    """
    i, j = edges[:, 0], edges[:, 1]
    if n <= _DENSE_MAX:
        lap = np.zeros((n, n))
        np.add.at(lap, (i, j), -1.0)
        np.add.at(lap, (j, i), -1.0)
        np.add.at(lap, (i, i), 1.0)
        np.add.at(lap, (j, j), 1.0)
        lap[0, :] = 0.0
        lap[0, gauge] = 1.0 / len(gauge)
        b = rhs.copy()
        b[0, :] = 0.0
        return np.asarray(np.linalg.solve(lap, b))
    deg = np.bincount(np.concatenate([i, j]), minlength=n).astype(float)

    def apply(x: np.ndarray) -> np.ndarray:
        y = deg[:, None] * x
        np.add.at(y, i, -x[j])
        np.add.at(y, j, -x[i])
        return y

    b = rhs - rhs.mean(axis=0)
    x = np.zeros_like(b)
    r = b.copy()
    p = r.copy()
    rr = (r * r).sum(axis=0)
    tol = 1e-20 * max(float((b * b).sum()), 1e-300)
    for _ in range(10 * n):
        if float(rr.sum()) <= tol:
            break
        ap = apply(p)
        alpha = rr / np.maximum((p * ap).sum(axis=0), 1e-300)
        x += alpha * p
        r -= alpha * ap
        rr_new = (r * r).sum(axis=0)
        p = r + (rr_new / np.maximum(rr, 1e-300)) * p
        rr = rr_new
    return x - x[gauge].mean(axis=0)


def _volterra(
    patch: Patch,
    out: dict[Vid, np.ndarray],
    turns: list[dict[Vid, float]],
    cores: list[Vid],
) -> None:
    """Close the open cut of each net-zero cluster (an edge dislocation) by
    the discrete Volterra field, in place on ``out``.

    The cluster seed turns each vertex by a piecewise-constant angle; the
    bonds whose two ends got different turns are the cut it opened (one
    group per pair of turns), so the cut set is read off the seed, not
    inferred from bond lengths.  Each group's jump J is the least-squares
    vector that brings its bonds to ideal steps (:func:`_cut_jump`).  The
    correction w minimises sum |w_j - w_i - t_ij|^2 over all bonds, with
    t = -J on a cut bond (oriented from the lower turn to the higher) and 0
    elsewhere, gauged to zero mean over the cluster cores: the jump closes
    on the cut and spreads harmonically about the core, with no centre,
    branch angle or tip to choose.
    """
    verts = list(patch.flatpos)
    index = {v: k for k, v in enumerate(verts)}
    edges = np.array([[index[a] for a in e] for e in patch.edges if len(e) == 2])
    if len(edges) == 0:
        return
    lat = patch.lat
    s0 = Site(0, 0, 0)
    nn = [lat.cart(b) - lat.cart(s0) for b in neighbors(s0)]
    steps = np.array([d[:2] for d in nn] + [-d[:2] for d in nn], dtype=float)
    pos = np.array([out[v][:2] for v in verts], dtype=float)
    target = np.zeros((len(edges), 2))
    for turn in turns:
        groups: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
        for k, (a, b) in enumerate(edges):
            ta, tb = turn.get(verts[a]), turn.get(verts[b])
            if ta is None or tb is None:
                continue
            d = (tb - ta + 180.0) % 360.0 - 180.0
            if abs(d) < 1e-3:
                continue
            lo, hi = (a, b) if d > 0 else (b, a)
            key = (round(turn[verts[lo]]) % 360, round(turn[verts[hi]]) % 360)
            groups.setdefault(key, []).append((k, lo, hi))
        for members in groups.values():
            vs = np.array([pos[hi] - pos[lo] for _k, lo, hi in members])
            jump, sign = _cut_jump(vs, steps)
            for (k, lo, _hi), sg in zip(members, sign, strict=True):
                target[k] -= sg * jump if edges[k, 0] == lo else -sg * jump
    if not target.any():
        return
    rhs = np.zeros((len(verts), 2))
    np.add.at(rhs, edges[:, 1], target)
    np.add.at(rhs, edges[:, 0], -target)
    core_set = {index[v] for v in cores}
    # one solve per connected component (overlapping glyphs can split the
    # patch graph), gauged on its cores, or on its own mean if it has none
    parent = list(range(len(verts)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        parent[find(int(a))] = find(int(b))
    comps: dict[int, list[int]] = {}
    for k in range(len(verts)):
        comps.setdefault(find(k), []).append(k)
    w = np.zeros((len(verts), 2))
    for comp in comps.values():
        if len(comp) < 2 or not rhs[comp].any():
            continue
        local = {g: k for k, g in enumerate(comp)}
        sub = np.array([[local[a], local[b]] for a, b in edges if int(a) in local])
        gauge = [local[g] for g in comp if g in core_set] or list(range(len(comp)))
        w[comp] = _laplace_solve(len(comp), sub, rhs[comp], gauge)
    for v, k in index.items():
        out[v] = out[v] + np.array([w[k, 0], w[k, 1], 0.0])


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
        if (
            patch.tube_nm is not None
            and len(rims) == 2
            and set(rims[0][0]) == set(rims[1][0])
        ):
            # len=1 tube: the generic walk collapses `in`/`out` into one
            # merged rim (see _split_degenerate_tube_rims) -- split it.
            tn, tm = patch.tube_nm
            tlen = (
                max(cell_index(v, tn, tm) for v in patch.flatpos if isinstance(v, Site))
                + 1
            )
            rims = _split_degenerate_tube_rims(patch, tn, tm, tlen)
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
            elif kind == "sheet" or patch.flat_lid:
                b_exp = 6 - patch.authored_sint
            elif kind == "cone":
                b_exp = 6 - (patch.cone_p or 0) - patch.authored_sint
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


def _split_degenerate_tube_rims(
    patch: Patch, n: int, m: int, length: int
) -> list[tuple[list[Vid], list[float]]]:
    """Split a one-period tube's merged boundary walk into its two rims.

    At ``len=1`` every atom of the tube's unit-cell patch sits on the
    surface -- there is no interior ring row left to anchor a face-orbit
    walk on just one end, so :meth:`Patch.rings_and_rims` returns the
    *same* whole-patch cycle walked in each direction and both ``in``/
    ``out`` census end up with every atom (the reported defect).  The two
    physical rims are still well defined geometrically: an atom belongs
    to ``in`` iff its lattice bond toward the previous unit cell (cell
    index < 0) is the one the ``len=1`` cutoff dropped, and to ``out`` iff
    it is the bond toward the next cell (index >= ``length``) that was
    dropped.  Each side is sorted by circumferential position for a
    deterministic, geometrically consistent cyclic order.
    """
    groups: dict[str, list[Site]] = {"lo": [], "hi": []}
    for v in patch.flatpos:
        if not isinstance(v, Site):
            continue
        for nb in neighbors(v):
            c = cell_index(nb, n, m)
            if 0 <= c < length:
                continue
            groups["lo" if c < 0 else "hi"].append(v)
            break
    out: list[tuple[list[Vid], list[float]]] = []
    for key in ("lo", "hi"):
        atoms = sorted(groups[key], key=lambda v: circumferential(v, n, m))
        n_a = len(atoms)
        turns: list[float] = []
        for i in range(n_a):
            prev, cur, nxt = atoms[i - 1], atoms[i], atoms[(i + 1) % n_a]
            d_in = patch.flatpos[cur] - patch.flatpos[prev]
            d_out = patch.flatpos[nxt] - patch.flatpos[cur]
            a_in = math.atan2(d_in[1], d_in[0])
            a_out = math.atan2(d_out[1], d_out[0])
            t = (a_out - a_in) % (2 * math.pi) - math.pi
            turns.append(math.degrees(t))
        out.append((cast(list[Vid], atoms), turns))
    return out


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
    """Return spec with every tube len=fit param replaced by ``length``
    (the ``len``/positional-2 key only: a roll-up domain ``fit`` on key
    ``0`` is :mod:`hexfold.domains`' business)."""
    out_insts = []
    for inst in spec.instances:
        params = tuple(
            (k, str(length) if v == "fit" and k in ("len", "2") else v)
            for k, v in inst.params
        )
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


def _k_cost(faces: list[tuple[int, ...]]) -> tuple[float, float]:
    """SPEC 12.1 cost terms (2) and (3) for one fuse phase ``k``: the
    largest seam ring, then the seam's defect charge ``sum|6 - n|``.

    The signed seam sum ``sum(6 - n)`` is the same for every phase of one
    fuse (V, bond count and seam face count do not depend on which atoms
    pair up), so ``|euler.residual|`` cannot rank a k family; the unsigned
    charge can.  It separates a minimal seam from one carrying extra 5-7
    pairs at the same max ring — on a graded bend (gr459928) a clean
    ``{7:3}`` against a ``{5:3, 7:6}`` that a lowest-k tie-break picks."""
    mx = max((len(f) for f in faces), default=0)
    charge = sum(abs(6 - len(f)) for f in faces)
    return float(mx), float(charge)


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


def _domain_cost(net: Net) -> tuple[float, float]:
    """SPEC 12.1 cost terms (2) and (3) for a built candidate: the largest
    seam ring across every ``seam.rings`` census, and the summed
    ``|euler.residual|`` over its sheets."""
    mx = 0.0
    resid = 0.0
    for f in net.report.findings:
        d = dict(f.data)
        if f.code == "seam.rings":
            rings = d.get("rings") or {}
            if rings:
                mx = max(mx, float(max(int(k) for k in rings)))
        elif f.code == "euler.residual":
            resid += abs(float(d.get("residual", 0)))
    return mx, resid


def _solve_domains(
    spec: Spec, profile: Profile, strict: bool, extra: list[Finding]
) -> Net:
    """Resolve every roll-up domain (SPEC 12.1 0.2, :mod:`hexfold.domains`):
    propagate the rim equalities from the pinned ends, then build each
    surviving combination (Cartesian product in domain order, capped at
    ``_FIT_CAP``), rank the clean ones by the SPEC 12.1 cost tuple and
    apply the first.  Reports ``fit.propagated`` (INFO) per domain
    instance, ``fit.alternatives`` (``param="domain"``) for the ranked
    remainder, and ``fit.unsolvable`` (ERROR) when propagation empties a
    domain or no combination builds cleanly."""
    prop = domains.propagate(
        spec, probe=lambda s: build(s, profile=profile, strict=False)
    )
    names = sorted(prop.before)
    where = ",".join(names)
    first = spec.instance(names[0])
    span = first.span if first is not None else None

    def propagated() -> list[Finding]:
        out = []
        for name in names:
            inst = spec.instance(name)
            out.append(
                Finding(
                    "fit.propagated",
                    Severity.INFO,
                    f"{name}: domain {len(prop.before[name])} -> "
                    f"{len(prop.after[name])} value(s) after propagation"
                    + (
                        f" (pruned by {'; '.join(prop.pruned_by[name])})"
                        if name in prop.pruned_by
                        else ""
                    ),
                    where=name,
                    span=inst.span if inst is not None else None,
                    data=(
                        ("after", [list(v) for v in prop.after[name]]),
                        ("before", [list(v) for v in prop.before[name]]),
                        ("pruned_by", list(prop.pruned_by.get(name, []))),
                        ("unpinned", list(prop.unpinned)),
                    ),
                )
            )
        return out

    if prop.conflict is not None:
        c = prop.conflict
        report = profile.apply_all(
            extra
            + propagated()
            + [
                Finding(
                    "fit.unsolvable",
                    Severity.ERROR,
                    f"{c.instance}: no domain value fits {c.constraint} "
                    f"(needs N in {list(c.needs)}, domain offers "
                    f"{list(c.offers)})",
                    where=c.instance,
                    span=c.span,
                    data=(
                        ("constraint", c.constraint),
                        ("instance", c.instance),
                        ("needs", list(c.needs)),
                        ("offers", list(c.offers)),
                    ),
                )
            ]
        )
        raise BuildError(report)

    combos = list(itertools.product(*(prop.after[n] for n in names)))
    truncated = len(combos) > _FIT_CAP
    combos = combos[:_FIT_CAP]
    candidates: list[tuple[Any, float, float, Any]] = []
    nets: dict[int, Net] = {}
    rejected: list[dict[str, Any]] = []
    for idx, combo in enumerate(combos):
        spec2 = spec
        for name, value in zip(names, combo):
            spec2 = domains.set_value(spec2, name, value)
        try:
            net = build(spec2, profile=profile, strict=False)
        except BuildError as exc:
            codes = sorted({f.code for f in exc.report.errors()})
            rejected.append({"value": [list(v) for v in combo], "errors": codes})
            continue
        if not net.report.ok:
            codes = sorted({f.code for f in net.report.errors()})
            rejected.append({"value": [list(v) for v in combo], "errors": codes})
            continue
        mx, resid = _domain_cost(net)
        combo_value = {n: list(v) for n, v in zip(names, combo)}
        candidates.append((combo_value, mx, resid, idx))
        nets[idx] = net
    if not candidates:
        report = profile.apply_all(
            extra
            + propagated()
            + [
                Finding(
                    "fit.unsolvable",
                    Severity.ERROR,
                    f"{where}: none of {len(combos)} domain combination(s) "
                    f"builds cleanly (errors seen: "
                    f"{', '.join(sorted({c for r in rejected for c in r['errors']}))})"
                    + (
                        f"; {len(prop.unpinned)} equality(ies) could not be "
                        "pinned by the probe build"
                        if prop.unpinned
                        else ""
                    ),
                    where=where,
                    span=span,
                    data=(
                        ("rejected", rejected),
                        ("truncated", truncated),
                        ("unpinned", list(prop.unpinned)),
                    ),
                )
            ]
        )
        raise BuildError(report)
    ranked = _rank_fit(candidates)
    winner = ranked[0]
    winner_net = nets[winner[3]]
    applied = winner[0]
    alts = _fit_alternatives_finding("domain", where, span, applied, ranked[1:])
    findings = list(winner_net.report.findings) + [alts] + propagated() + list(extra)
    report = profile.apply_all(findings)
    if strict and not report.ok:
        raise BuildError(report)
    return replace(winner_net, report=report)


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
    # Angstrom sheet extents snap to whole cells first (SPEC 7,
    # hexfold.extent): the snap findings ride on whichever net this call
    # ends up returning.  The recursive builds below see cells only.
    spec, snap_findings = snap_extents(spec, _lattice_from_spec(spec))

    # Domain fits (SPEC 12.1 0.2 / 22.3, hexfold.domains) resolve before
    # menus and before len=fit: each candidate combination is a full
    # spec that goes through this same function.
    try:
        has_domains = domains.has_domains(spec)
    except domains.DomainError as exc:
        raise BuildError(
            profile.apply_all(
                snap_findings + [Finding("fit.unsolvable", Severity.ERROR, str(exc))]
            )
        ) from exc
    if has_domains:
        return _solve_domains(spec, profile, strict, snap_findings)

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
                snap_findings
                + [
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
        report = profile.apply_all(
            list(winner_net.report.findings) + [finding] + snap_findings
        )
        return replace(winner_net, report=report)

    lat = _lattice_from_spec(spec)
    findings: list[Finding] = list(snap_findings)
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
                            f"cap({cn},{cm}): supported are (5,5) (C60 "
                            "hemisphere) and (6k,0) flat lids, k >= 1",
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
            # a flat lid is a flat disc, so authored surgery applies to it
            # as to a sheet (a centred wedge cut makes it a cone frustum)
            if inst.kind not in ("fullerene", "cap") or patch.flat_lid:
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
        # Centred on a disclination core (an authored |6-r|-wedge cut at
        # this site's apex), the cluster grows from the core so the hole
        # is symmetric about the cone tip; a 4-wedge core collapses to a
        # digon, leaving the site on only its two flanking hexagons.
        # (the digon's darts overlap, so its second flank walks as a rim)
        at_site = [f for f, _t in patch.faces() if site in f and len(f) <= 6]
        core = [r6 for r6 in at_site if len(r6) < 6]
        seeds: list[list[Vid]] = []
        collapsed_sint = 0
        if core:
            seeds = core[:1]
        elif len(at_site) == 2 and rr >= 1:
            seeds = at_site
            rr -= 1
            collapsed_sint = 4  # the digon core is not a ring of its own
        for f in seeds:
            if not any(r6 is f or r6 == f for r6 in rings):
                rings.append(f)
        seeds = [next(r6 for r6 in rings if r6 == f) for f in seeds]
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
        target = seeds[0] if seeds else (cands[0] if cands else None)
        if target is not None:
            edge_rings: dict[frozenset[Vid], list[int]] = {}
            for ri6, r6 in enumerate(rings):
                for ei in range(len(r6)):
                    edge_rings.setdefault(
                        frozenset((r6[ei], r6[(ei + 1) % len(r6)])), []
                    ).append(ri6)
            cluster = {
                i
                for i, r6 in enumerate(rings)
                if any(r6 is t for t in (seeds or [target]))
            }
            frontier = set(cluster)
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
            sint_rem = sum(6 - len(rings[i]) for i in cluster) + collapsed_sint
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
    """``cap(n,m)``: the C60 hemisphere ``(5,5)``, or a ``(6k,0)`` flat lid.

    ``cap(5,5)`` cuts C60 perpendicular to a C5 axis (30 atoms, 10 dangling,
    6 pentagons).  The kept hemisphere is the one containing the lex-min
    atom.

    ``cap(6k,0)``, k >= 1, is the flat lid: the ``hex(k-1)`` flake (§28.3),
    the same cell complex a ``- hex(k-1)@...`` hole removes from a sheet.
    Its rim is all-zigzag with 6k dangling atoms, so it seats on a
    ``(6k,0)`` tube end at any registry; the lid itself carries no
    pentagons -- fusing it to a tube produces six pentagons as seam rings
    at the flake's six corners (SPEC 6.1, 28.3).  Punching a further
    ``- hex(r)@...`` hole in that same lid (a plain ``_apply_hole`` call
    downstream, no change needed here) turns it into a washer: the
    radius-changing shell steps a narrower neck through the ``hole`` port
    while a wider bulge seats on the unchanged ``in`` rim, minting six
    heptagons and six pentagons respectively as seam rings (SPEC 7, 28.3).
    Returns None for every other ``(n,m)``.
    """
    if (n, m) != (5, 5):
        if m == 0 and n > 0 and n % 6 == 0:
            flake_r = n // 6 - 1
            p = Patch(lat, _hex_flake_sites(lat, flake_r))
            p.flat_lid = True
            return p
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


def _frame(
    pos: np.ndarray,
    dang: tuple[int, ...],
    inst_c: np.ndarray | None = None,
    forced_normal: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Rim frame from dangling-atom seed positions: centroid + normal.

    The normal is the least-variance axis of the dangling ring, signed to
    point away from the instance centroid ``inst_c`` (outward through the
    opening).  ``inst_c`` must be the owning *instance's* centroid: the
    whole-net mean is a different point once several instances share the
    array, and for a hole rim near a sheet's centre it made the sign a
    coin toss.

    ``forced_normal`` bypasses the covariance/centroid computation
    entirely (already signed by the caller): a flat, zero-thickness
    instance (SPEC 28.3 ``cap(6k,0)``) has *two* boundaries -- a hole
    rim and the outer rim -- whose covariance-derived normal is z-noise
    either way, so the sign has to come from one shared plane normal per
    instance instead (see :func:`_flat_normals`), not from each rim's own
    ambiguous covariance.
    """
    pts = pos[list(dang)]
    c = pts.mean(axis=0)
    if forced_normal is not None:
        return c, forced_normal
    cov = (pts - c).T @ (pts - c)
    n = np.linalg.eigh(cov)[1][:, 0]
    if inst_c is None:
        inst_c = pos.mean(axis=0)
    if float(n @ (c - inst_c)) < 0:
        n = -n
    return c, n


def _winding_normal(pos: np.ndarray, dang: tuple[int, ...]) -> np.ndarray:
    """A rim's least-variance axis, signed so the dangling list winds
    negatively about it -- the sense every non-flat rim's list has about
    its outward normal (see ``_place_seeds``' ``_flat_sign``)."""
    pts = pos[list(dang)]
    c = pts.mean(axis=0)
    d = pts - c
    n = np.linalg.eigh(d.T @ d)[1][:, 0]
    area = np.cross(d, np.roll(d, -1, axis=0)).sum(axis=0)
    return n if float(area @ n) < 0 else -n


def _flat_normals(
    pos: np.ndarray, inst_ords: dict[str, list[int]], sigma: float
) -> set[str]:
    """Names of the *flat* instances (root-caused 2026-09-28 item 3): an
    instance is flat when its own point cloud's smallest-variance extent
    is under half a lattice spacing (``0.5 * sigma``) -- a cap/sheet patch
    (SPEC 28.3's zero-thickness ``cap(6k,0)`` lid, built with only a
    small deterministic out-of-plane perturbation, ``_patch_seed3``
    "flat-perturbed", whose ``sin(u)*cos(w)`` amplitude bounds the extent
    at ``0.1 * sigma`` regardless of instance size -- measured 0.07-0.10
    sigma on cap(12,0)/cap(24,0)) rather than a tube/cone/fullerene with
    genuine 3D curvature (a ``len=1`` tube's axial extent is a fixed
    ``2 * sigma`` no matter its circumference -- measured on
    tube(40,0,len=1)/tube(60,0,len=1), whose *relative* smallest/largest
    extent ratio drops to 0.09/0.06 at those diameters and would
    misclassify them as flat under a relative threshold; the absolute
    ``sigma``-scaled one does not, by a 4x margin either side).

    Detected by extent, not by ``kind`` name, so it also covers a flat
    ``sheet`` instance -- geometry, not vocabulary, is what makes
    ``_frame``'s per-rim covariance normal a coin toss for a flat
    instance's hole rim vs its outer rim (see ``_place_seeds``'
    ``_flat_sign``, which signs each flat rim by its own dangling-list
    winding, :func:`_winding_normal`).
    """
    out: set[str] = set()
    for inst, idx in inst_ords.items():
        pts = pos[idx]
        if len(pts) < 3:
            continue
        c = pts.mean(axis=0)
        cov = (pts - c).T @ (pts - c)
        _w, v = np.linalg.eigh(cov)  # ascending eigenvalues
        proj = (pts - c) @ v
        extent0 = float(proj[:, 0].max() - proj[:, 0].min())
        if extent0 < 0.5 * sigma:
            out.add(inst)
    return out


def _flat_bud_sides(
    pos: np.ndarray,
    flat: set[str],
    inst_ords: dict[str, list[int]],
    inst_of: dict[int, str],
    fuse_frames: list[
        tuple[str, tuple[int, ...], str, tuple[int, ...], int, bool, bool]
    ],
    ports: tuple[tuple[str, Port], ...],
    findings: list[Finding],
) -> dict[str, np.ndarray]:
    """For each flat instance fused through a rim, the face an attached
    bud belongs on (a unit normal in the instance's local seed frame).

    A flat instance's centroid lies in its own plane, so "away from the
    centroid" signs nothing: a [9-6] C60 on a ``cap(12,0)`` lid seeded
    inside the tube under it, and a [2+2] C60 on a sheet landed under the
    sheet while its pillar rose above (nanobuds-paper's hero figure,
    2026-10-02).  The fuse already decides the faces:
    :func:`_fuse_transform` puts a flat rim's partner along that rim's
    :func:`_winding_normal`, and a flat instance's hole rims and outer
    rim wind oppositely.  So a part fused through a *hole* rises from the
    bud's face (a sheet's pillar), and a part fused through the *outer*
    rim hangs from the other face (the tube under a lid).  The outer rim
    is the instance's rim of largest mean radius, so a hole near the edge
    cannot pass for it.  A washer fused through both must get the same
    face from each; if not, ``place.face_conflict`` (ERROR) names them.
    """
    rims_of: dict[str, list[tuple[str, Port]]] = {}
    for name, port in ports:
        if port.atoms:
            rims_of.setdefault(inst_of[port.atoms[0]], []).append((name, port))
    faces: dict[str, list[tuple[str, np.ndarray]]] = {}
    for _pn, p_dang, _qn, q_dang, _k, _real, kabsch in fuse_frames:
        if kabsch:
            continue
        for dang in (p_dang, q_dang):
            inst = inst_of[dang[0]]
            if inst not in flat:
                continue
            idx = inst_ords[inst]
            c = pos[idx].mean(axis=0)
            rims = rims_of.get(inst, [])

            def radius(port: Port, c: np.ndarray = c) -> float:
                return float(np.linalg.norm(pos[list(port.atoms)] - c, axis=1).mean())

            outer = max(rims, key=lambda np_: radius(np_[1]))[1] if rims else None
            name = next((n for n, pt in rims if set(pt.dangling) == set(dang)), "?")
            is_outer = outer is not None and set(outer.dangling) == set(dang)
            n = _winding_normal(pos, dang)
            faces.setdefault(inst, []).append((name, -n if is_outer else n))
    out: dict[str, np.ndarray] = {}
    for inst, got in faces.items():
        first_name, first = got[0]
        for name, face in got[1:]:
            if float(face @ first) < 0.0:
                findings.append(
                    Finding(
                        "place.face_conflict",
                        Severity.ERROR,
                        f"{inst}: its fused rims {first_name} and {name} put "
                        "an attached bud on opposite faces",
                        where=inst,
                    )
                )
                break
        out[inst] = first
    return out


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
    inst_c_p: np.ndarray | None = None,
    inst_c_q: np.ndarray | None = None,
    n_p_forced: np.ndarray | None = None,
    n_q_forced: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """(R, t) mapping instance Q's seed so its port faces P's fused port.

    Centroids coincide offset by sigma along P's rim normal; normals are
    antiparallel; the residual twist about the normal pairs
    ``P.dangling[i]`` with ``Q.dangling[(k-i) mod N]``.  ``inst_c_p`` /
    ``inst_c_q`` are the two instances' own seed centroids (rim-normal
    sign, see :func:`_frame`); ``n_p_forced`` / ``n_q_forced`` bypass that
    entirely for a flat instance's rim (see :func:`_flat_normals`).
    """
    c_p, n_p = _frame(pos, p_dang, inst_c_p, n_p_forced)
    c_q, n_q = _frame(pos, q_dang, inst_c_q, n_q_forced)
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


def _fuse_transform_kabsch(
    pos: np.ndarray,
    p_dang: tuple[int, ...],
    q_dang: tuple[int, ...],
    k: int,
    sigma: float,
    inst_c_p: np.ndarray | None = None,
    n_host: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """(R, t) via a full six-point rigid best fit (Kabsch/SVD), minimising
    sum-of-squares over the n matched pairs ``P.dangling[i]`` <->
    ``Q.dangling[(k-i) mod N]`` directly, with no reflection (``det(R) ==
    1``).  Used only for the nanobud menu attach (root-caused 2026-09-28
    item 1), not the ordinary rim-to-rim :func:`_fuse_transform`: a menu
    registration's six host atoms are a mix of a hexagon's own ring
    vertices and its second-neighbour shell (Wang & Li 2009's 9-6/8-7
    junction), at genuinely different distances from any single point, so
    no shared rim normal + one twist angle describes them.  Verified
    against nanobud_87.hx: the twist-only fit left three of the six seed
    bonds 3.5-5.4 A short, which ``stick()`` could not fully absorb (a
    0.69 A bud/host clash survived 3000 relax iterations); this fit caps
    the same six at <=3.1 A -- Kabsch is the global optimum rotation for
    a *given* correspondence, so that residual is irreducible for this
    menu's own solved six-bond registration (the host side genuinely is
    not a small planar rim).

    The target is the host positions offset by one ``sigma`` along the
    host rim's own outward normal (:func:`_frame`, signed against
    ``inst_c_p``), not the bare host positions: a *uniform* shift of the
    target only moves Kabsch's translation term (the found rotation and
    the six residual point-to-point distances are unchanged), but it
    rigidly carries the *whole* bud instance -- not just its six bonded
    atoms -- one sigma further from the host surface.  The offset size
    does not decide the stick geometry: a 2 sigma offset measured within
    0.1 A of 1 sigma after ``stick()`` on every bud example (gr459567).
    Which side of the host the body lands on does: see
    :func:`_kabsch_lands_inward`.  ``n_host`` forces the host's outward
    normal (a flat host's, from :func:`_flat_bud_sides`); otherwise it is
    the attach ring's covariance normal signed away from ``inst_c_p``.
    """
    n = len(p_dang)
    _c_p, n_p = _frame(pos, p_dang, inst_c_p, n_host)
    target = pos[list(p_dang)] + sigma * n_p
    src = pos[[q_dang[(k - i) % n] for i in range(n)]]
    src_c = src.mean(axis=0)
    tgt_c = target.mean(axis=0)
    h = (src - src_c).T @ (target - tgt_c)
    u, _s, vt = np.linalg.svd(h)
    d = float(np.sign(np.linalg.det(vt.T @ u.T))) or 1.0
    r = vt.T @ np.diag([1.0, 1.0, d]) @ u.T
    t = tgt_c - r @ src_c
    return r, t


def _kabsch_lands_inward(
    pos: np.ndarray,
    p_dang: tuple[int, ...],
    q_dang: tuple[int, ...],
    k: int,
    sigma: float,
    inst_c_p: np.ndarray,
    q_ords: list[int],
    n_host: np.ndarray | None = None,
) -> bool:
    """Whether :func:`_fuse_transform_kabsch` seats instance Q's body on
    the *inward* side of host P's attach site (behind P's rim normal).

    A proper rotation fitted to a menu correspondence does that whenever
    the two paired lists wind oppositely about the host normal: the only
    det +1 fit is then the reflected placement (gr459812's mirror, on the
    menu path).  Measured on nanobud_96.hx: the C60 centre seeded 2.5 A
    from the axis of a 6.8 A-radius tube, so every [9-6]/[8-7] build sat
    its ball inside its host (bud/host clash 0.62 A on a sheet).  The
    caller reflects the fullerene's local seed in x about its centroid
    when this holds: an achiral cage's mirror image is the same molecule
    (same bonds, same lengths), and the reflected seed winds the other way.  It is decided once per
    fullerene, before any edge transform is computed, so the result does
    not depend on which side the BFS reaches first.
    """
    r, t = _fuse_transform_kabsch(pos, p_dang, q_dang, k, sigma, inst_c_p, n_host)
    _c_p, n_p = _frame(pos, p_dang, inst_c_p, n_host)
    host_c = pos[list(p_dang)].mean(axis=0)
    qc = pos[q_ords].mean(axis=0)
    return float((r @ qc + t - host_c) @ n_p) < 0.0


def _surface_normal(
    pos: np.ndarray,
    a_ord: int,
    a_nbrs: list[int],
    c_a: np.ndarray,
    sigma: float,
    side: np.ndarray | None = None,
) -> np.ndarray:
    """Unit normal to the surface at atom a: the least-variance axis of a
    and its in-instance neighbours, signed away from the instance centroid
    ``c_a``.  On a flat instance that sign is seed noise, so ``side`` (the
    flat instance's bud side, :func:`_flat_bud_sides`) signs it when
    given; failing both, largest-component-positive (deterministic)."""
    pts = pos[[a_ord, *a_nbrs]]
    c = pts.mean(axis=0)
    n = np.linalg.eigh((pts - c).T @ (pts - c))[1][:, 0]
    if side is not None:
        return n if float(n @ side) >= 0.0 else -n
    s = float((pos[a_ord] - c_a) @ n)
    if abs(s) < 0.1 * sigma:
        s = float(n[int(np.argmax(np.abs(n)))])
    return n if s > 0.0 else -n


def _bond_transform(
    pos: np.ndarray,
    a_ord: int,
    b_ord: int,
    sigma: float,
    c_a: np.ndarray,
    c_b: np.ndarray,
    a_nbrs: list[int],
    b_nbrs: list[int],
    partner: tuple[int, int] | None = None,
    side_a: np.ndarray | None = None,
    side_b: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """(R, t) placing instance B so atom b sits sigma outside atom a.

    ``side_a``/``side_b`` sign a *flat* instance's surface normal (see
    :func:`_flat_bud_sides`); a flat sheet's own centroid cannot.

    u is A's surface normal at a (:func:`_surface_normal`); B's own
    outward surface normal at b maps onto -u, so the two surfaces face
    each other across the bond.  That is exact for a ball, a tube and a
    sheet alike, whichever of the two is the moved side; the b ->
    centroid(B) direction is not (from a C60 it is radial, from a tube's
    attach atom away from mid-length it is mostly axial, and from a sheet
    it lies in the plane).  With a
    ``partner`` link (a2, b2) between the same two instances (a [2+2]
    cycloaddition), B is then twisted about u so b -> b2 runs along
    a -> a2, seating the second bond too.

    ``c_a``/``c_b`` are the *instances'* centroids.  This used the
    whole-net mean for both (the trap :func:`_frame`'s docstring names),
    which bears no relation to either instance once several share the
    seed array: every [2+2] bud seeded part-sunk into its host (tube(10,10)
    + C60: ball centre 8.6 A from the axis where 11.7 A clears it; 89
    bud/host pairs under 1.8 A after stick, min 0.85 A, gr459567).
    """
    u = _surface_normal(pos, a_ord, a_nbrs, c_a, sigma, side_a)
    r = _rot_min(_surface_normal(pos, b_ord, b_nbrs, c_b, sigma, side_b), -u)
    if partner is not None:
        a2, b2 = partner
        want = pos[a2] - pos[a_ord]
        have = r @ (pos[b2] - pos[b_ord])
        want = want - (want @ u) * u
        have = have - (have @ u) * u
        ang = math.atan2(float(np.cross(have, want) @ u), float(have @ want))
        ux = np.array([[0.0, -u[2], u[1]], [u[2], 0.0, -u[0]], [-u[1], u[0], 0.0]])
        twist = np.eye(3) + math.sin(ang) * ux + (1.0 - math.cos(ang)) * (ux @ ux)
        r = twist @ r
        # seat the bond pair's midpoints sigma apart, so the cage curvature
        # splits between both bonds instead of stretching the second
        mid_a = (pos[a_ord] + pos[a2]) / 2.0
        mid_b = (pos[b_ord] + pos[b2]) / 2.0
        return r, mid_a + sigma * u - r @ mid_b
    t = pos[a_ord] + sigma * u - r @ pos[b_ord]
    return r, t


def _place_seeds(
    spec: Spec,
    net: Net,
    fuse_frames: list[
        tuple[str, tuple[int, ...], str, tuple[int, ...], int, bool, bool]
    ],
    bond_links: list[tuple[int, int]],
    findings: list[Finding],
) -> tuple[tuple[tuple[float, float, float], ...] | None, str]:
    """Rigidly place per-instance seeds along the connect graph.

    BFS from the origin instance: each fused port aligns its neighbour by
    the fused-port frame (centroids sigma apart along the normal, normals
    antiparallel, k-phase twist -- or, when the entry says so, the full
    Kabsch best fit); each bond link places the dst instance sigma
    outside the src atom.  Unconnected instances keep their local seed.
    Returns (seed3, seed_kind); seed_kind is "mixed" when any transform
    was applied.

    Each ``fuse_frames`` entry has two trailing ``bool``s.  The first is
    ``True`` for a real fuse/menu registration and ``False`` for a
    *placement-only* entry (a k>=3 seam's consecutive-rim correspondence,
    SPEC 11.3): both feed this BFS identically, only registry-closure
    bookkeeping downstream tells them apart (a seam already contributes
    its own registry edges).  The second selects the transform:
    :func:`_fuse_transform_kabsch` instead of :func:`_fuse_transform` for
    a nanobud menu attach (root-caused 2026-09-28 item 1), whose six
    bonds have no shared rim normal to twist-fit about.

    Single-rooted at ``spec.origin``: a fused component the BFS can't
    reach from there is left at its own local seed (docs/backlog/
    hexfold-integration.md "Root-caused 2026-09-28" item 2 notes a
    multi-root BFS as a possible follow-up -- not needed by any current
    example, since a seam's own placement-only edges are what span every
    seam-linked instance into the one tree rooted at origin).

    Third pass (slice 3, 2026-09-28): the two-phase BFS above assigns
    each non-root instance exactly one transform, off the first fuse/seam
    edge that reaches it -- any *other* edge onto an already-placed
    instance (a part-graph cycle, SPEC 12.2) is silently dropped, which
    is exact for a tree part graph and a measured ~10.9 A crossing-bond
    residual for a cycle (``flanged_doughnut.hx``: its real
    ``top<->wall<->bottom`` fuse chain and its ``top<->bottom`` seam edge
    close at registry residual 0 -- the discrete symmetry indices agree
    -- but that is not a continuous-rotation guarantee).  When the
    fuse/seam graph (``fuse_frames``, restricted to distinct-instance
    edges) has a cycle, :func:`hexfold.place.place_graph` re-solves every
    instance in the origin's fuse-connected component jointly
    (alternating Kabsch, Gauss-Seidel) and its transforms replace the
    BFS ones for that component; ``seam.cycle`` reports the per-edge RMS
    residual and sweep count.  Deliberately scoped to ``fuse_frames``
    only, excluding ``bond_links``: a bond link's single atom pair
    carries no rim-normal registration to jointly reconcile, and the only
    two examples where a *bond* link is the sole source of a
    ``registry.closure`` cycle (``sheet_bud_22.hx`` / ``nanobud_22.hx``'s
    ``[2+2]`` cycloaddition, two authored bonds onto the same lattice
    site) must stay on the untouched two-phase BFS result (regression
    guard: ``tests/hexfold/test_place_seeds.py`` tree-graph bit-identity).
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

    inst_cent = {k_: pos[v].mean(axis=0) for k_, v in inst_ords.items()}
    flat = _flat_normals(pos, inst_ords, sigma)
    # the face a bud belongs on, for each flat instance fused through a rim
    bud_side = _flat_bud_sides(
        pos, flat, inst_ords, inst_of, fuse_frames, net.ports, findings
    )

    # A menu attach whose fit would seat the bud inside its host reflects
    # the bud's local seed first (_kabsch_lands_inward), in x about its
    # centroid.  Only C60 is reflected: it is achiral, so its mirror image
    # is the same molecule.  A host tube's mirror is the other hand, and a
    # cage not known to be achiral is refused with a finding rather than
    # silently swapped for its enantiomer.  The decision is made once per
    # cage, off its first menu frame; every frame is then re-checked, so
    # a cage bridging two hosts that still lands inward on one is reported.
    spec_inst = {inst.name: inst for inst in spec.instances}
    menu_frames: list[tuple[str, str, tuple[int, ...], tuple[int, ...], int]] = []
    for _pn, p_dang, _qn, q_dang, k, _real, kabsch in fuse_frames:
        if not kabsch:
            continue
        ia, ib = inst_of[p_dang[0]], inst_of[q_dang[0]]
        for host, bud, h_dang, b_dang in (
            (ia, ib, p_dang, q_dang),
            (ib, ia, q_dang, p_dang),
        ):
            b_inst, h_inst = spec_inst.get(bud), spec_inst.get(host)
            if b_inst and h_inst and b_inst.kind == "fullerene" != h_inst.kind:
                menu_frames.append((host, bud, h_dang, b_dang, k))
                break

    def _inward(frame: tuple[str, str, tuple[int, ...], tuple[int, ...], int]) -> bool:
        host, bud, h_dang, b_dang, kk = frame
        return _kabsch_lands_inward(
            pos,
            h_dang,
            b_dang,
            kk,
            sigma,
            inst_cent[host],
            inst_ords[bud],
            bud_side.get(host),
        )

    decided: set[str] = set()
    for frame in menu_frames:
        host, bud = frame[0], frame[1]
        if bud in decided:
            continue
        decided.add(bud)
        if not _inward(frame):
            continue
        b_inst = spec_inst[bud]
        if dict(b_inst.params).get("0") != "C60":
            findings.append(
                Finding(
                    "place.mirror_refused",
                    Severity.WARN,
                    f"{bud} seeds inside {host}; only an achiral C60 is "
                    "reflected into place, so it is left inward",
                    where=bud,
                    span=b_inst.span,
                )
            )
            continue
        qc = inst_cent[bud]
        pos[inst_ords[bud], 0] = 2.0 * qc[0] - pos[inst_ords[bud], 0]
        findings.append(
            Finding(
                "place.mirrored",
                Severity.INFO,
                f"{bud}'s seed reflected so it sits outside {host} "
                "(the menu pairing winds against the host normal)",
                where=bud,
                span=b_inst.span,
            )
        )
    for frame in menu_frames:
        host, bud = frame[0], frame[1]
        if _inward(frame):
            findings.append(
                Finding(
                    "place.inward",
                    Severity.WARN,
                    f"{bud} still seeds inside {host} after placement",
                    where=bud,
                    span=spec_inst[bud].span,
                )
            )

    edges: dict[str, list[tuple[str, np.ndarray, np.ndarray, bool]]] = {}

    def add(inst_a: str, inst_b: str, r: np.ndarray, t: np.ndarray, real: bool) -> None:
        # (r, t) maps inst_b's local seed so its port faces inst_a's port:
        # file it under inst_a, the instance whose placement it hangs off.
        # (Keying by inst_b handed every neighbour the transform computed
        # for the *other* side, mirroring it behind the far rim -- every
        # fused example seeded with 8-78 A crossing bonds and stick's
        # spring stage then telescoped the halves into each other.)
        edges.setdefault(inst_a, []).append((inst_b, r, t, real))

    # A flat instance's rim normal is z-noise either way (``_frame``'s
    # centroid sign reads the ±0.1 sigma seed perturbation), so it is taken
    # from the rim's own winding instead: ``_fuse_transform``'s pairing
    # ``P[i] <-> Q[(k-i) mod n]`` is only rigid-reachable when both
    # dangling lists wind the same way about their normals, and every
    # non-flat rim's list winds negatively about its outward normal
    # (measured over every hexfold/examples fuse, 2026-10-01). Signing the
    # flat rim to wind negatively too puts its partner on the side the
    # topology demands; the hole and outer rims of one washer then come
    # out opposite on their own (the annulus traverses them in opposite
    # senses). The earlier "hole rim's centroid sign is reliable" rule
    # held for a hex(1) hole only -- a hex(2)/hex(3) washer read the wrong
    # sign and seeded its seams mirrored, 14-28 A bonds.
    def _flat_sign(inst: str, dang: tuple[int, ...]) -> np.ndarray | None:
        if inst not in flat:
            return None
        return _winding_normal(pos, dang)

    for pn, p_dang, qn, q_dang, k, real, kabsch in fuse_frames:
        ia, ib = inst_of[p_dang[0]], inst_of[q_dang[0]]
        if ia == ib:
            continue
        c_a, c_b = inst_cent[ia], inst_cent[ib]
        if kabsch:
            r, t = _fuse_transform_kabsch(
                pos, p_dang, q_dang, k, sigma, c_a, bud_side.get(ia)
            )
            add(ia, ib, r, t, real)
            r2, t2 = _fuse_transform_kabsch(
                pos, q_dang, p_dang, k, sigma, c_b, bud_side.get(ib)
            )
            add(ib, ia, r2, t2, real)
            continue
        n_p, n_q = _flat_sign(ia, p_dang), _flat_sign(ib, q_dang)
        r, t = _fuse_transform(pos, p_dang, q_dang, k, sigma, c_a, c_b, n_p, n_q)
        add(ia, ib, r, t, real)
        r2, t2 = _fuse_transform(pos, q_dang, p_dang, k, sigma, c_b, c_a, n_q, n_p)
        add(ib, ia, r2, t2, real)
    same_nbrs: dict[int, list[int]] = {}
    for i, j, _ in net.bonds:
        if inst_of.get(i) is not None and inst_of.get(i) == inst_of.get(j):
            same_nbrs.setdefault(i, []).append(j)
            same_nbrs.setdefault(j, []).append(i)
    # each link filed under both orientations, (this-side atom, far-side
    # atom), so two links written in opposite orders still find each other
    links_between: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for ai, bi in bond_links:
        links_between.setdefault((inst_of[ai], inst_of[bi]), []).append((ai, bi))
        links_between.setdefault((inst_of[bi], inst_of[ai]), []).append((bi, ai))
    for ai, bi in bond_links:
        ia, ib = inst_of[ai], inst_of[bi]
        if ia == ib:
            continue
        other = next(
            (lk for lk in links_between[(ia, ib)] if lk[0] != ai and lk[1] != bi), None
        )
        c_a, c_b = inst_cent[ia], inst_cent[ib]
        nb_a, nb_b = same_nbrs.get(ai, []), same_nbrs.get(bi, [])
        if other is not None:
            # a bond pair: fit each surface over both attach atoms' patches,
            # so the normal sits square to the pair rather than tilted to a
            nb_a = sorted(({other[0], *nb_a, *same_nbrs.get(other[0], [])}) - {ai})
            nb_b = sorted(({other[1], *nb_b, *same_nbrs.get(other[1], [])}) - {bi})
        s_a, s_b = bud_side.get(ia), bud_side.get(ib)
        r, t = _bond_transform(
            pos, ai, bi, sigma, c_a, c_b, nb_a, nb_b, other, s_a, s_b
        )
        add(ia, ib, r, t, True)
        r2, t2 = _bond_transform(
            pos,
            bi,
            ai,
            sigma,
            c_b,
            c_a,
            nb_b,
            nb_a,
            (other[1], other[0]) if other else None,
            s_b,
            s_a,
        )
        add(ib, ia, r2, t2, True)

    origin = spec.origin or net.atoms[0].instance
    placed: dict[str, tuple[np.ndarray, np.ndarray]] = {
        origin: (np.eye(3), np.zeros(3))
    }

    def _bfs(allow_placeholder: bool) -> None:
        queue = list(placed)
        while queue:
            cur = queue.pop(0)
            r_c, t_c = placed[cur]
            for nxt, r_e, t_e, real_e in sorted(edges.get(cur, []), key=lambda x: x[0]):
                if nxt in placed or (not real_e and not allow_placeholder):
                    continue
                # global(nxt) = place(cur) o edge: the edge transform is in
                # the pre-placement local frames of both instances.
                placed[nxt] = (r_c @ r_e, r_c @ t_e + t_c)
                queue.append(nxt)

    # two passes: real fuse/bond edges first, so a k>=3 seam's
    # placement-only edges (root-caused 2026-09-28 item 2) only bridge a
    # component the real graph can't otherwise reach -- a placement-only
    # edge competing with a real one at the same BFS depth (e.g. a seam
    # naming two rims also joined by a real chain through a third
    # instance) must never win, or it overrides the real chain's accurate
    # placement with the seam's cruder mean-frame approximation.
    _bfs(allow_placeholder=False)
    _bfs(allow_placeholder=True)
    if len(placed) <= 1:
        return net.seed3, net.seed_kind

    # third pass: joint placement across a part-graph cycle (see the
    # docstring above).  `cycle_pairs` restates `fuse_frames` as bare
    # (instance, instance) edges -- one entry per fuse/seam edge between
    # two distinct instances -- so a redundant edge (two entries for the
    # same unordered pair, or a longer cycle through several instances)
    # can be told apart from a tree with plain edge/vertex counting.
    local_of = {
        inst: {o: i for i, o in enumerate(idx)} for inst, idx in inst_ords.items()
    }
    cycle_pairs: list[tuple[str, str, bool]] = []
    graph_edges: list[
        tuple[str, str, list[tuple[int, int]], tuple[np.ndarray, np.ndarray]]
    ] = []
    for pn, p_dang, qn, q_dang, k, real, _kabsch in fuse_frames:
        ia, ib = inst_of[p_dang[0]], inst_of[q_dang[0]]
        if ia == ib:
            continue
        cycle_pairs.append((ia, ib, real))
        n = len(p_dang)
        pairs = [(p_dang[i], q_dang[(k - i) % n]) for i in range(n)]
        _c_p, n_p = _frame(pos, p_dang, inst_cent[ia], _flat_sign(ia, p_dang))
        _c_q, n_q = _frame(pos, q_dang, inst_cent[ib], _flat_sign(ib, q_dang))
        graph_edges.append(
            (
                ia,
                ib,
                [(local_of[ia][a], local_of[ib][b]) for a, b in pairs],
                (n_p, n_q),
            )
        )

    if cycle_pairs and origin in {x for pair in cycle_pairs for x in pair[:2]}:
        # component reachable from `origin` over every fuse/seam edge: a
        # tree spanning it has exactly (size - 1) edges, so `>=` size
        # means at least one redundant edge -- a cycle in that component
        # specifically (not merely somewhere in the net).
        comp_adj: dict[str, set[str]] = {}
        for pu, pv, _real in cycle_pairs:
            comp_adj.setdefault(pu, set()).add(pv)
            comp_adj.setdefault(pv, set()).add(pu)
        comp = {origin}
        queue = [origin]
        while queue:
            cur = queue.pop(0)
            for nbr in sorted(comp_adj.get(cur, ())):
                if nbr not in comp:
                    comp.add(nbr)
                    queue.append(nbr)
        comp_edges = [(pu, pv) for pu, pv, _real in cycle_pairs if pu in comp]
        if len(comp_edges) >= len(comp):
            # scope place_graph's authority to instances that are NOT
            # already uniquely pinned by real fuse/bond edges alone: a
            # placeholder (seam) edge is "the seam's cruder mean-frame
            # approximation" (see the two-phase BFS's own priority
            # comment above) even after a full joint Kabsch fit, so it
            # must never move an instance the REAL sub-graph alone
            # already determines without ambiguity (flanged_doughnut's
            # exact top<->wall<->bottom chain, SPEC 11.3's k=3 seam
            # naming that same top<->bottom pair a second time notwith
            # -standing) -- verified via the SAME edge/vertex count, on
            # the real-only sub-graph: tube_ring_closure.hx has TWO real
            # edges between the same pair (a genuine redundancy even
            # among reals, so that pair stays open to reconciliation),
            # flanged_doughnut's real sub-graph is a plain tree (no
            # redundancy at all -- the seam is the ONLY source of the
            # cycle), so nothing in it is up for grabs.
            real_adj: dict[str, set[str]] = {}
            for pu, pv, real in cycle_pairs:
                if real:
                    real_adj.setdefault(pu, set()).add(pv)
                    real_adj.setdefault(pv, set()).add(pu)
            real_comp = {origin}
            queue = [origin]
            while queue:
                cur = queue.pop(0)
                for nbr in sorted(real_adj.get(cur, ())):
                    if nbr not in real_comp:
                        real_comp.add(nbr)
                        queue.append(nbr)
            real_edges_in_comp = [
                (pu, pv) for pu, pv, real in cycle_pairs if real and pu in real_comp
            ]
            frozen = (
                real_comp if len(real_edges_in_comp) == len(real_comp) - 1 else set()
            )
            free = comp - frozen

            rims = {inst: pos[idx].copy() for inst, idx in inst_ords.items()}
            result = place_graph(rims, graph_edges, origin, sigma=sigma)
            placed = {
                **placed,
                **{k: v for k, v in result.transforms.items() if k in free},
            }
            free_edges = [
                (eu, ev, rms)
                for (eu, ev, prs, _n), rms in zip(graph_edges, result.residuals)
                if prs and eu in comp and (eu in free or ev in free)
            ]
            edge_data = [{"u": eu, "v": ev, "rms": rms} for eu, ev, rms in free_edges]
            worst = max((rms for _eu, _ev, rms in free_edges), default=0.0)
            sev = Severity.INFO if worst < 0.3 else Severity.WARN
            findings.append(
                Finding(
                    "seam.cycle",
                    sev,
                    f"joint placement over {len(edge_data)} cycle edge(s), "
                    f"{len(free)} free instance(s) of {len(comp)}: max rms "
                    f"{worst:.3f} A over {result.sweeps} sweep(s)",
                    data=(("edges", edge_data), ("sweeps", result.sweeps)),
                )
            )

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
    # trailing bools: (real, kabsch) -- real is True for a real fuse/menu
    # registration, False for a k>=3 seam's placement-only entry; kabsch
    # selects _fuse_transform_kabsch over _fuse_transform (see
    # _place_seeds)
    fuse_frames: list[
        tuple[str, tuple[int, ...], str, tuple[int, ...], int, bool, bool]
    ] = []
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
            # _place_seeds placement (gr454650-adjacent root cause,
            # docs/backlog/hexfold-integration.md "Root-caused 2026-09-28"
            # item 1): the menu verb never fed `bond_links`/`fuse_frames`,
            # so _place_seeds' `len(placed) <= 1` early return skipped the
            # whole spec.  Route the solved six-bond registration through
            # a six-point placement transform (not _bond_transform: a
            # single staple has no twist fit, and the BFS keeps only the
            # first edge per neighbour so five of the six pairs would be
            # dropped) -- host atoms as p_dang, bud atoms as q_dang, k=0
            # -- k=0's index convention pairs p_dang[i] with
            # q_dang[(-i) % n], so q_dang is built in the matching
            # reversed order (q_dang[j] = the bud atom actually bonded to
            # p_dang[(-j) % n]) rather than vbonds' own order, or either
            # transform would average six mismatched pairs instead of the
            # six real bonds.  Flagged for _fuse_transform_kabsch, not
            # the ordinary rim-normal _fuse_transform: the six host atoms
            # mix a hexagon's ring vertices with its second-neighbour
            # shell (Wang & Li 2009 9-6/8-7), with no single shared rim
            # normal, and the twist-only fit left a bud/host clash after
            # stick() (see _fuse_transform_kabsch's docstring).
            n_vb = len(vbonds)
            fuse_frames.append(
                (
                    c.dst,
                    tuple(ords[(host_key, hv)] for _bv, hv in vbonds),
                    bud_port.name,
                    tuple(ords[(c.src, vbonds[(-j) % n_vb][0])] for j in range(n_vb)),
                    0,
                    True,
                    True,
                )
            )
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
                # rank by (2) smallest max seam-ring size, (3) smallest
                # seam defect charge, (4) lowest k — DA-neck host seat,
                # graded-bend seams (_k_cost says why not |residual|).
                candidates: list[tuple[int, float, float, int]] = []
                for kc in range(min(n, _FIT_CAP)):
                    trial = [
                        (p.dangling[i], q.dangling[(kc - i) % n]) for i in range(n)
                    ]
                    mx, charge = _k_cost(_seam_faces(p.atoms, q.atoms, trial))
                    candidates.append((kc, mx, charge, kc))
                ranked = _rank_fit(candidates)
                kk = ranked[0][0]
                spec = _record_k(spec, ci, kk)
                findings.append(
                    _fit_alternatives_finding("k", c.src, c.span, kk, ranked[1:])
                )
            else:
                kk = c.k or 0
            fuse_frames.append(
                (p.name, p.dangling, q.name, q.dangling, kk, True, False)
            )
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

    # k>=3 seams (SPEC 11.3): resolved after menu/fuse but before bond
    # connects, so a `--bond-->` can name a seam atom `<seam>/s<i>` that
    # this pass mints (SPEC 9, [spec 0.2]) -- and so a seam can also
    # name a rim a menu/fuse just minted.  Each rim's own atoms/faces
    # stay with its own sheet (SPEC 6.3) -- a seam never merges
    # components, so it is a parallel pass, not folded into the fuse
    # branch above.
    seam_records: list[SeamRecord] = []
    seam_rims: list[tuple[tuple[int, ...], int, int]] = []
    seam_atom_ords: set[int] = set()
    seam_registry_edges: list[tuple[str, str, int, int]] = []
    # ord -> rim ords it bonds to, in minted order -- used below to seed
    # each seam atom's 3D position from its rim neighbours once
    # _place_seeds has placed those (gr347187: seam atoms have no seed
    # of their own, since they don't exist in the pre-seam net).
    seam_nbrs: dict[int, list[int]] = {}
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
            seam_nbrs[o] = []
        for i in range(n):
            for j, port in enumerate(resolved):
                rim_ord = port.dangling[_idx(j, i)]
                a, b = new_ords[i], rim_ord
                bonds.append((min(a, b), max(a, b), 1))
                annot(min(a, b), max(a, b), seam.name)
                seam_nbrs[new_ords[i]].append(rim_ord)
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
        # every later consecutive pair is in direct register (phase 0).
        # The same consecutive-pair correspondence, fed to `fuse_frames`
        # as placement-only entries (the trailing False -- no bonds/atoms
        # minted, no registry edge of their own: seam_registry_edges above
        # already covers that), is what lets _place_seeds' BFS span every
        # seam-linked instance (root-caused 2026-09-28 item 2: with no
        # fuse_frames edge at all, an origin that only touches a seam left
        # the whole spec unplaced).
        for j in range(krim - 1):
            phase = seam.k if j == 0 else 0
            seam_registry_edges.append(
                (
                    atoms[resolved[j].atoms[0]].instance,
                    atoms[resolved[j + 1].atoms[0]].instance,
                    phase,
                    n,
                )
            )
            fuse_frames.append(
                (
                    resolved[j].name,
                    resolved[j].dangling,
                    resolved[j + 1].name,
                    resolved[j + 1].dangling,
                    phase,
                    False,
                    False,
                )
            )
        seam_records.append(SeamRecord(seam.name, seam.rims, seam.k, tuple(new_ords)))

    # bond connects (SPEC 23.2): resolved last so a `--bond-->` endpoint
    # can name a seam atom `<seam>/s<i>` the k>=3 seam pass above just
    # minted, in addition to any menu/fuse atom.
    for c in spec.connects:
        if c.verb != "bond":
            continue
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

    # _place_seeds rigidly transforms whole *instances* along fuse/bond
    # frames using net.atoms (the pre-seam net); a seam atom belongs to
    # no single instance's local frame (SPEC 11.3 mints it fresh) and is
    # seeded separately below (gr347187), so a bond onto one is excluded
    # here rather than crashing on an ord _place_seeds has never seen.
    inst_bond_links = [
        (ai, bi)
        for ai, bi in bond_links
        if ai not in seam_atom_ords and bi not in seam_atom_ords
    ]
    seed3, seed_kind = _place_seeds(spec, net, fuse_frames, inst_bond_links, findings)
    if seed3 is not None and seam_nbrs:
        # gr347187: _place_seeds works from the pre-seam net, so seed3
        # has no rows for the seam atoms minted above -- append one row
        # per seam atom, in ordinal order (they're contiguous after the
        # pre-seam atoms), placed at the mean of its rim neighbours'
        # already-placed seed positions.
        pos = np.array(seed3, dtype=np.float64)
        extra = [tuple(pos[seam_nbrs[o]].mean(axis=0)) for o in sorted(seam_nbrs)]
        seed3 = tuple(seed3) + tuple(
            (float(r0), float(r1), float(r2)) for r0, r1, r2 in extra
        )
        seed_kind = "mixed"
    if seed3 is not None:
        assert len(seed3) == len(atoms), (
            f"seed3 has {len(seed3)} rows for {len(atoms)} atoms"
        )
    # part-graph edges for registry closure (SPEC 12.2) -- resolve each
    # fuse/bond endpoint to its owning instance while atoms still carry
    # their pre-hybridisation identity (ord -> instance is unaffected by
    # the sp3 relabelling below).
    inst_of_ord = {a.ord: a.instance for a in atoms}
    registry_edges: list[tuple[str, str, int, int]] = []
    for _pn, p_dang, _qn, q_dang, fk, real, _kabsch in fuse_frames:
        if not real:
            # k>=3 seam placement-only entry: seam_registry_edges below
            # already carries this pair (minted alongside the seam atoms
            # themselves), so counting it again here would double an
            # edge that is a tree edge or a genuine cycle either way.
            continue
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
    # derived hybridisation: 4 bonds -> sp3, exactly as for a lattice
    # atom -- whether the 4th bond is a `--bond-->` onto a k=3 seam
    # atom's own 3 ring bonds (SPEC 9, [spec 0.2]) or onto a lattice
    # atom's 3 ring bonds.  A seam atom's *native* ring-bond count
    # (len(seam_nbrs), == its seam's rim count) is its own SPEC 11.3
    # floor: k=3 is the sp2 case, promoting on a 4th bond the same as
    # any lattice atom; k>3 is already over-valent sp2 by construction
    # (no --bond--> needed to reach 4) and must stay sp2 regardless of
    # degree so valence.over fires rather than being hidden by a
    # hybridisation change.
    deg: dict[int, int] = {}
    for i, j, _ in bonds:
        deg[i] = deg.get(i, 0) + 1
        deg[j] = deg.get(j, 0) + 1
    atoms = [
        replace(a, hyb="sp3")
        if deg.get(a.ord, 0) == 4 and len(seam_nbrs.get(a.ord, ())) <= 3
        else a
        for a in atoms
    ]
    # sheet partition (SPEC 6.3): needed both to scope the fused-rim B
    # remainder below (per sheet, not per net -- a k>=3 seam can leave
    # several disjoint sheets in one spec) and for the final Net.sheet_atoms.
    sheet_atoms = _compute_sheets(atoms, bonds, set(attach_bonds), seam_atom_ords)
    if fused_edges:
        # consumed rims carried their hole_b away; recompute the
        # combinatorial B total of each *affected* sheet (one that
        # actually absorbed a consumed rim) and give its outer rims the
        # remainder.  A fuse elsewhere in the net -- or a sheet with no
        # fuse at all, e.g. one only touched by a k>=3 seam -- must not
        # have its ports' B overwritten.
        rings_set = set(rings)
        for _name, comp_ords in sheet_atoms:
            comp = set(comp_ords)
            if not any(set(rim) <= comp for rim in consumed_rims):
                continue
            comp_rings = [r for r in rings_set if set(r) <= comp]
            comp_bonds = sum(1 for i, j, _ in bonds if i in comp and j in comp)
            sigma_terms = sum(6 - len(r) for r in comp_rings)
            chi_n = len(comp) - comp_bonds + len(comp_rings)
            hole_sum = sum(
                p.b
                for nm, p in ports.items()
                if p.normal == "hole" and set(p.atoms) <= comp
            )
            rem = 6 * chi_n - sigma_terms - hole_sum
            outer = sorted(
                nm
                for nm, p in ports.items()
                if p.normal != "hole" and set(p.atoms) <= comp
            )
            for oi, nm in enumerate(outer):
                ports[nm] = replace(ports[nm], b=rem if oi == 0 else 0)
    atoms_t = tuple(sorted(atoms, key=lambda a: a.ord))
    bonds_t = tuple(sorted(bonds))
    rings_t = tuple(sorted(set(rings)))
    attach_t = tuple(sorted(set(attach_bonds)))
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
