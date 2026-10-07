"""check(spec) -> Report (SPEC section 9).

Never raises for structural problems.  Phase-1 codes:
euler.chi, euler.residual, euler.closed_unreachable, internal.euler,
valence.over, valence.under, cut.overlap, ring.size.unusual,
port.symmetry, rim.nonstandard (INFO, SPEC 10 rim standard), and the
geometry tier (geom.*) when ``geometry=True``.
Phase-2 codes: seam.rings, annot.sublattice, fit.unsolvable, fit.alternatives,
fit.propagated (roll-up domains, hexfold.domains), extent.snap (Angstrom
sheet extents, hexfold.extent),
hole.missing, port.unknown, frag.unrealized, annot.host_sublattices;
an sp3 atom is allowed 4 bonds before valence.over fires.
0.2: ``spec`` may be a ``.hx.json`` sectioned-JSON string; when it carries a
``generated`` block, its cached hash is recomputed against a fresh
rebuild of the authored sections and a mismatch reports ``gen.stale``
(SPEC 4/13/19g) -- the stale block is never reused for the check itself.
"""

from __future__ import annotations

import json
import math
import re
from typing import NamedTuple

import numpy as np

from .build import Net, build
from .canon import content_hash
from .defects import counting_residual
from .lattice import ideal_angle_deg
from .report import (
    BuildError,
    Finding,
    Profile,
    Report,
    Severity,
)
from .text import Spec, parse, spec_from_dict


def _findings_from_net(net: Net) -> list[Finding]:
    out: list[Finding] = list(net.report.findings)
    deg = {a.ord: 0 for a in net.atoms}
    for i, j, _ in net.bonds:
        deg[i] += 1
        deg[j] += 1
    rim_atoms = (
        {o for _, p in net.ports for o in p.atoms}
        | {o for rim, _b, _be in net.term_rims for o in rim}
        | {o for rim, _b, _be in net.seam_rims for o in rim}
    )
    for a in net.atoms:
        if a.element not in net.lattice.elements:
            continue  # termination atoms (e.g. H) carry no lattice valence
        d = deg[a.ord]
        # sp3 attachment atoms legitimately reach 4 bonds; 5+ is an error
        limit = 4 if a.hyb == "sp3" else 3
        if d > limit:
            out.append(
                Finding(
                    "valence.over",
                    Severity.ERROR,
                    f"atom {a.path} has {d} bonds",
                    where=a.instance,
                )
            )
        elif d < 3 and a.ord not in rim_atoms:
            out.append(
                Finding(
                    "valence.under",
                    Severity.WARN,
                    f"atom {a.path} has {d} bonds, not on a rim",
                    where=a.instance,
                )
            )
    # rim standard (SPEC 10): INFO, never WARN -- chiral ends, cap rims
    # and hole rims are legal bespoke interfaces; this only says which
    # ports are outside the two-type, multiples-of-6 library series.
    for name, p in net.ports:
        rt = p.rim_type
        if rt is None:
            reason = "mixed rim (neither pure zigzag nor pure armchair)"
        elif rt[0] == "z" and rt[1] % 6 != 0:
            reason = f"zigzag N={rt[1]} is not a multiple of 6"
        else:
            continue
        out.append(
            Finding(
                "rim.nonstandard",
                Severity.INFO,
                f"port {name}: {reason}",
                where=name,
                data=(
                    ("type", None if rt is None else f"{rt[0]}{rt[1]}"),
                    ("N", p.size),
                ),
            )
        )
    # physical connectivity.  `net.sheet_atoms` below is NOT this number:
    # a sheet deliberately breaks at every bond-verb attachment, so a
    # [2+2] bud is two sheets in one object and `len(sheets)` can never
    # answer "is this net one piece?".  `net.bonds` does carry the attach
    # edges (build.py appends them to both `bonds` and `attach_bonds`),
    # so union-find over it is the physical question.  Reported because a
    # spec that simply never joins its primitives used to pass clean --
    # every per-component finding was emitted separately and nothing said
    # the net was in pieces.
    if net.atoms:
        # net.atoms is a tuple indexed by ordinal, and bond endpoints are
        # those same ordinals.
        parent: dict[int, int] = {o: o for o in range(len(net.atoms))}

        def _root(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for bi, bj, _o in net.bonds:
            ri, rj = _root(bi), _root(bj)
            if ri != rj:
                parent[ri] = rj
        comp_sizes: dict[int, int] = {}
        for atom_ord in range(len(net.atoms)):
            root_ord = _root(atom_ord)
            comp_sizes[root_ord] = comp_sizes.get(root_ord, 0) + 1
        n_comp = len(comp_sizes)
        sizes_desc = sorted(comp_sizes.values(), reverse=True)
        out.append(
            Finding(
                "net.components",
                Severity.INFO if n_comp == 1 else Severity.WARN,
                "net is one connected piece"
                if n_comp == 1
                else (
                    f"net is {n_comp} disconnected pieces "
                    f"({', '.join(str(s) for s in sizes_desc)} atoms) -- "
                    "nothing joins them, so this is a set of parts rather "
                    "than an assembly; add the fuse/bond/seam that was "
                    "meant to connect them"
                ),
                data=(
                    ("n", n_comp),
                    ("sizes", ",".join(str(s) for s in sizes_desc)),
                ),
            )
        )
    # ring census + counting law
    pn: dict[int, int] = {}
    # k>=3 seam faces belong to no sheet and to no census (SPEC 6.3); their
    # sizes are reported once, in the seam's own `seam.rings` finding.
    seam_ords = {o for s in net.seams for o in s.atoms}
    for r in net.rings:
        if seam_ords and any(o in seam_ords for o in r):
            continue
        pn[len(r)] = pn.get(len(r), 0) + 1
        if not 4 <= len(r) <= 8:
            out.append(
                Finding(
                    "ring.size.unusual",
                    Severity.WARN,
                    f"ring of size {len(r)}",
                )
            )
    # Euler bookkeeping, per sheet (SPEC 6.3, 0.2): a sheet is a connected
    # component of the surface graph -- atoms joined by fuse, excluding
    # bond-verb attachments and seam atoms/bonds (net.sheet_atoms, built
    # in build.py's _apply_connects).  A fuse-only net is one sheet; the
    # single-sheet case below reproduces 0.1's whole-net formulas byte
    # for byte (same message text, no `where`) -- only `data.sheet` is
    # new.  A bond-attach net's sheets are exactly 0.1's per-component
    # split (each attach edge stays a sheet boundary, same as a seam);
    # a k>=3 seam is the new way to reach more than one sheet without any
    # `bond` connect at all.
    sheets = net.sheet_atoms
    single = len(sheets) == 1
    for name, comp_ords in sheets:
        comp = set(comp_ords)
        cpn: dict[int, int] = {}
        for r in net.rings:
            if set(r) <= comp:
                cpn[len(r)] = cpn.get(len(r), 0) + 1
        cb_exp = (
            sum(p.b_expected for _, p in net.ports if set(p.atoms) <= comp)
            + sum(be for rim, _b, be in net.term_rims if set(rim) <= comp)
            + sum(be for rim, _b, be in net.seam_rims if set(rim) <= comp)
        )
        cn_rims = (
            sum(1 for _, p in net.ports if set(p.atoms) <= comp)
            + sum(1 for rim, _b, _be in net.term_rims if set(rim) <= comp)
            + sum(1 for rim, _b, _be in net.seam_rims if set(rim) <= comp)
        )
        ce = sum(1 for i, j, _ in net.bonds if i in comp and j in comp)
        cchi = len(comp) - ce + sum(cpn.values())
        where = "" if single else name
        prefix = "" if single else "component "
        out.append(
            Finding(
                "euler.chi",
                Severity.INFO,
                f"{prefix}chi={cchi} rims={cn_rims} rings={cpn}",
                where=where,
                data=(("chi", cchi), ("sheet", name)),
            )
        )
        # a component whose rims were consumed by a fuse/bond attach into
        # a larger structure is no longer a standalone surface -- SPEC
        # 0.1 skips residual/internal.euler for it rather than report on
        # incomplete B data; a seam never does this (its rims keep their
        # own B_expected in net.seam_rims, so this never applies there).
        consumed = not single and any(set(rim) <= comp for rim in net.consumed_rims)
        # "closed" stays a whole-net (single-sheet) concept, reproducing
        # 0.1 exactly: a multi-sheet net (bond-attach or seam) always
        # reports euler.residual for a fully-bonded, 0-rim sheet (e.g. a
        # [2+2]-bonded C60 bud) rather than routing it through the
        # declared-closed check -- residual is 0 for a valid closed cage
        # either way, so this is a reporting simplification, not a gap.
        insts_here = sorted({net.atoms[o].instance for o in comp})
        sheet_closed = (
            single
            and cn_rims == 0
            and all(
                (ii := net.spec.instance(iname)) is not None
                and ii.kind == "fullerene"
                and not ii.holes
                for iname in insts_here
            )
        )
        cres = counting_residual(cpn, cb_exp, cchi)
        if sheet_closed:
            if sum((6 - n) * k for n, k in cpn.items()) != 6 * cchi or cchi < 2:
                out.append(
                    Finding(
                        "euler.closed_unreachable",
                        Severity.ERROR,
                        "declared closed net cannot reach 6chi",
                        where=where,
                        data=(("residual", cres or 0), ("sheet", name)),
                    )
                )
        elif not consumed:
            out.append(
                Finding(
                    "euler.residual",
                    Severity.WARN if cres else Severity.INFO,
                    f"{prefix}open net residual {cres}",
                    where=where,
                    data=(("residual", cres), ("sheet", name)),
                )
            )
        if consumed:
            continue
        # combinatorial-B consistency: the declared rim terms must
        # satisfy sum(6-n) + sum(B) = 6 chi on every sheet (internal.euler)
        cb_comb = (
            sum(p.b for _, p in net.ports if set(p.atoms) <= comp)
            + sum(b for rim, b, _be in net.term_rims if set(rim) <= comp)
            + sum(b for rim, b, _be in net.seam_rims if set(rim) <= comp)
        )
        if counting_residual(cpn, cb_comb, cchi) != 0:
            out.append(
                Finding(
                    "internal.euler",
                    Severity.ERROR,
                    f"combinatorial B inconsistent {'on a surface component' if not single else 'with the net'}",
                    where=where,
                    data=(("sheet", name),),
                )
            )
    return out


class Relaxed(NamedTuple):
    """A caller's own relax for :func:`geometry_findings` to judge: the
    coordinates, the final max force of that same relax, and a label
    (``"tethered"``) that ``geom.summary`` carries as ``relax``."""

    coords: np.ndarray
    max_force: float
    relax: str


def _geometry_findings(
    net: Net,
    profile: Profile,
    relaxed: Relaxed | None = None,
) -> list[Finding]:
    """Stick-model geometry check: bond lengths vs sigma, ring-corner
    angles vs the ring-ideal interior angle.  ``geom.summary`` always
    carries the statistics; individual findings are capped at the ten
    worst offenders so reports stay small and deterministic.  ``relaxed``
    is a caller's own relax (e.g. a tethered one); ``None`` relaxes the
    net here, untethered."""
    from .stick import stick_info

    if relaxed is not None:
        coords, max_force = relaxed.coords, relaxed.max_force
    else:
        coords, max_force = stick_info(net)
    out: list[Finding] = []
    sig = net.lattice.sigma_A
    sig_ch = net.lattice.sigma_CH_A
    elem = {a.ord: a.element for a in net.atoms}
    hyb = {a.ord: a.hyb for a in net.atoms}
    lat_el = set(net.lattice.elements)

    def bond_ideal(i: int, j: int) -> float:
        return sig if elem[i] in lat_el and elem[j] in lat_el else sig_ch

    bond_dev: list[tuple[float, int, int, float]] = []
    for i, j, _ in net.bonds:
        d = float(np.linalg.norm(coords[i] - coords[j]))
        bond_dev.append((d - bond_ideal(i, j), i, j, d))
    bond_bad = sorted(
        (x for x in bond_dev if abs(x[0]) > profile.bond_tol_A),
        key=lambda x: (-abs(x[0]), x[1], x[2]),
    )
    for dev, i, j, d in bond_bad[:10]:
        out.append(
            Finding(
                "geom.bond.long" if dev > 0 else "geom.bond.short",
                Severity.WARN,
                f"bond {i}-{j} length {d:.3f} A",
                where=str(i),
                data=(
                    ("bond", [i, j]),
                    ("length", round(d, 3)),
                    ("ideal", round(bond_ideal(i, j), 3)),
                ),
            )
        )

    # ring-corner angles: for each ring vertex, the angle between its two
    # ring edges vs that ring's ideal interior angle
    ang_dev: list[tuple[float, int, float, float]] = []
    for ring in net.rings:
        n = len(ring)
        for i in range(n):
            v = ring[i]
            ideal = ideal_angle_deg(n, hyb.get(v, ""))
            u = coords[ring[(i - 1) % n]] - coords[v]
            w = coords[ring[(i + 1) % n]] - coords[v]
            cos = float(u @ w) / (np.linalg.norm(u) * np.linalg.norm(w))
            ang = math.degrees(math.acos(max(-1.0, min(1.0, cos))))
            ang_dev.append((ang - ideal, v, ang, ideal))
    ang_bad = sorted(
        (x for x in ang_dev if abs(x[0]) > profile.angle_tol_deg),
        key=lambda x: (-abs(x[0]), x[1]),
    )
    for _dev, v, ang, ideal in ang_bad[:10]:
        out.append(
            Finding(
                "geom.angle.dev",
                Severity.WARN,
                f"angle at atom {v}: {ang:.1f} deg (ideal {ideal:.1f})",
                where=str(v),
                data=(
                    ("angle_deg", round(ang, 1)),
                    ("ideal_deg", round(ideal, 1)),
                ),
            )
        )

    inst = {a.ord: a.instance for a in net.atoms}
    elem = {a.ord: a.element for a in net.atoms}

    def _under_bar(found: list[tuple[float, int, int]]) -> list[tuple[float, int, int]]:
        # the pair search runs at the widest bar; each pair then answers to
        # its own (an H-H bay sits legitimately inside the carbon bar)
        return [
            (d, i, j) for d, i, j in found if d < profile.clash_bar(elem[i], elem[j])
        ]

    clashes = _under_bar(_clash_pairs(coords, net.bonds, profile.clash_reach_A))
    for d, i, j in clashes[:10]:
        # two bands: under clash_error_A no reading of the pair is a
        # geometry (overlapping atoms), between it and the pair's bar a
        # squeezed but possible one (a strained stick junction)
        overlap = d < profile.clash_error_A
        bar = profile.clash_bar(elem[i], elem[j])
        out.append(
            Finding(
                "geom.clash",
                Severity.ERROR if overlap else Severity.WARN,
                f"atoms {i} ({inst[i]}) and {j} ({inst[j]}) are {d:.2f} A apart "
                f"and not bonded ({elem[i]}-{elem[j]} bar {bar:.2f} A"
                + (
                    f", overlap under {profile.clash_error_A:.2f} A)"
                    if overlap
                    else ")"
                ),
                where=str(i),
                data=(
                    ("atoms", [i, j]),
                    ("instances", [inst[i], inst[j]]),
                    ("elements", [elem[i], elem[j]]),
                    ("distance", round(d, 3)),
                    ("bar", bar),
                ),
            )
        )

    # The seed tier: the same pair test on the placed seed, before stick
    # runs.  A seed with coincident atoms is wrong whatever the relaxer
    # makes of it afterwards (stick untangles some, a stiffer repulsion
    # would lift others out of geom.clash's ERROR band while the angles
    # get worse), so it is reported on its own and never cleared by the
    # relaxed numbers.
    seed_clashes = (
        _under_bar(
            _clash_pairs(np.asarray(net.seed3), net.bonds, profile.clash_reach_A)
        )
        if net.seed3 is not None
        else []
    )
    seed_overlaps = [x for x in seed_clashes if x[0] < profile.seed_overlap_A]
    for d, i, j in seed_overlaps[:10]:
        out.append(
            Finding(
                "geom.seed_overlap",
                Severity.ERROR,
                f"atoms {i} ({inst[i]}) and {j} ({inst[j]}) are seeded {d:.2f} A "
                f"apart (under {profile.seed_overlap_A:.2f} A): the placement put "
                "them on top of each other",
                where=str(i),
                data=(
                    ("atoms", [i, j]),
                    ("instances", [inst[i], inst[j]]),
                    ("distance", round(d, 3)),
                ),
            )
        )

    # a bonded pair is skipped by the overlap test above, but a bond seeded
    # far under any bond length is two atoms nearly on top of each other
    short_bonds: list[tuple[float, int, int]] = []
    if net.seed3 is not None and net.bonds:
        seed = np.asarray(net.seed3)
        bij = np.array([(i, j) for i, j, _o in net.bonds])
        blen = np.linalg.norm(seed[bij[:, 0]] - seed[bij[:, 1]], axis=1)
        short_bonds = sorted(
            (float(blen[k]), int(bij[k, 0]), int(bij[k, 1]))
            for k in np.flatnonzero(blen < profile.seed_short_bond_A)
        )
    for d, i, j in short_bonds[:10]:
        out.append(
            Finding(
                "geom.seed_short_bond",
                Severity.WARN,
                f"bonded atoms {i} ({inst[i]}) and {j} ({inst[j]}) are seeded "
                f"{d:.2f} A apart (under {profile.seed_short_bond_A:.2f} A): "
                "nearly stacked",
                where=str(i),
                data=(
                    ("atoms", [i, j]),
                    ("instances", [inst[i], inst[j]]),
                    ("distance", round(d, 3)),
                ),
            )
        )

    b_arr = np.array([x[0] for x in bond_dev]) if bond_dev else np.zeros(1)
    a_arr = np.array([x[0] for x in ang_dev]) if ang_dev else np.zeros(1)
    out.append(
        Finding(
            "geom.summary",
            Severity.INFO,
            f"bond rms {float(np.sqrt((b_arr**2).mean())):.3f} A "
            f"max {float(np.abs(b_arr).max()):.3f} A; "
            f"angle rms {float(np.sqrt((a_arr**2).mean())):.1f} deg "
            f"max {float(np.abs(a_arr).max()):.1f} deg; "
            f"{len(bond_bad)} bonds and {len(ang_bad)} angles past tolerance",
            data=(
                ("bond_rms", round(float(np.sqrt((b_arr**2).mean())), 3)),
                ("bond_max", round(float(np.abs(b_arr).max()), 3)),
                ("bond_count", len(bond_dev)),
                # every offender, not the ten the findings list: the cap
                # hides most of a strained region (240 corners on a ball top)
                ("bond_n_over", len(bond_bad)),
                ("angle_rms", round(float(np.sqrt((a_arr**2).mean())), 1)),
                ("angle_max", round(float(np.abs(a_arr).max()), 1)),
                ("angle_count", len(ang_dev)),
                ("angle_n_over", len(ang_bad)),
                ("clash_count", len(clashes)),
                ("clash_min", round(clashes[0][0], 3) if clashes else None),
                ("seed_clash_count", len(seed_clashes)),
                (
                    "seed_clash_min",
                    round(seed_clashes[0][0], 3) if seed_clashes else None,
                ),
                ("seed_short_bond_count", len(short_bonds)),
                (
                    "suppressed",
                    max(0, len(bond_bad) - 10)
                    + max(0, len(ang_bad) - 10)
                    + max(0, len(clashes) - 10)
                    + max(0, len(seed_overlaps) - 10)
                    + max(0, len(short_bonds) - 10),
                ),
                ("max_force_final", round(max_force, 4)),
                # a caller's relax names itself, so a stored summary never
                # shows a tethered geometry as the default stick's
                *((("relax", relaxed.relax),) if relaxed is not None else ()),
            ),
        )
    )
    return out


def _clash_pairs(
    coords: np.ndarray, bonds: tuple[tuple[int, int, int], ...] | list, bar: float
) -> list[tuple[float, int, int]]:
    """Every pair closer than ``bar`` that is neither bonded nor shares a
    bonded neighbour, closest first (ties by atom ids).  1-3 pairs are left
    to ``geom.angle.dev``: a squeezed angle already reports them, and a
    4-ring's diagonal sits near 2.0 A by construction.  A cell list keeps it
    linear in atom count; hexfold carries no scipy."""
    n = len(coords)
    nbrs: list[set[int]] = [set() for _ in range(n)]
    for i, j, *_ in bonds:
        nbrs[i].add(j)
        nbrs[j].add(i)
    cells: dict[tuple[int, int, int], list[int]] = {}
    keys = np.floor(np.asarray(coords) / bar).astype(np.int64)
    for i in range(n):
        cells.setdefault(
            (int(keys[i, 0]), int(keys[i, 1]), int(keys[i, 2])), []
        ).append(i)
    out: list[tuple[float, int, int]] = []
    offsets = [(a, b, c) for a in (-1, 0, 1) for b in (-1, 0, 1) for c in (-1, 0, 1)]
    for (cx, cy, cz), members in cells.items():
        for dx, dy, dz in offsets:
            other = cells.get((cx + dx, cy + dy, cz + dz))
            if not other:
                continue
            for i in members:
                near = nbrs[i]
                for j in other:
                    if j <= i or j in near or near & nbrs[j]:
                        continue
                    d = float(np.linalg.norm(coords[i] - coords[j]))
                    if d < bar:
                        out.append((d, i, j))
    out.sort()
    return out


def _gen_stale_finding(ast: Spec, generated_of: str) -> Finding | None:
    """``None`` if a fresh rebuild of ``ast``'s authored sections still
    hashes to ``generated_of``; otherwise the ``gen.stale`` WARN (SPEC
    13). The generated block is never reused either way -- ``check``
    always rebuilds ``ast`` itself for its findings."""
    expected = content_hash(ast)
    if expected == generated_of:
        return None
    return Finding(
        "gen.stale",
        Severity.WARN,
        "generated section's hash does not match a rebuild of its authored sections",
        data=(("expected", expected), ("found", generated_of)),
    )


def geometry_findings(
    net: Net,
    profile: Profile = Profile.DEFAULT,
    *,
    relaxed: Relaxed | None = None,
) -> list[Finding]:
    """The geometry tier (``geom.*``) over an already-built net — what
    ``check(spec, geometry=True)`` adds on top of the topological report.
    Public so a caller that already holds the ``Net`` (se's stick build)
    can put the same findings on the persisted record instead of only on
    the throwaway check echo (gr454488 residual 3).  ``relaxed`` (a
    :class:`Relaxed`: coordinates, their own final max force, a label)
    judges the caller's coordinates instead of
    re-relaxing: a tethered relax (docs/backlog/
    hexfold-ideal-surface-then-tile.md, S4) would otherwise be judged on a
    different, untethered geometry.  ``geom.seed_overlap`` still reads
    ``net.seed3``."""
    return _geometry_findings(net, profile, relaxed)


def check(
    spec: str | Spec,
    *,
    profile: Profile = Profile.DEFAULT,
    geometry: bool = False,
) -> Report:
    generated_of: str | None = None
    if isinstance(spec, str) and spec.lstrip().startswith("{"):
        doc = json.loads(spec)
        ast: Spec = spec_from_dict(doc)
        gen = doc.get("generated")
        if isinstance(gen, dict):
            generated_of = gen.get("of")
    elif isinstance(spec, str):
        ast = parse(spec)
    else:
        ast = spec
    findings: list[Finding] = []
    try:
        net = build(ast, profile=profile, strict=False)
    except BuildError as e:
        report = e.report
        if generated_of is not None and (f := _gen_stale_finding(ast, generated_of)):
            report = Report(report.findings + (f,)).sorted()
        return report
    findings.extend(_findings_from_net(net))
    # port.symmetry: collar order k must divide gcd(n,m) of the tube it binds.
    # Read the RESOLVED spec on the net, not the authored one: a roll-up
    # domain (``tube(fit in {…})``) is text until build resolves it.
    findings.extend(_port_symmetry(net.spec))
    if geometry:
        findings.extend(_geometry_findings(net, profile))
    if generated_of is not None and (f := _gen_stale_finding(ast, generated_of)):
        findings.append(f)
    return profile.apply_all(findings)


def _port_symmetry(spec: Spec) -> list[Finding]:
    out = []
    tubes = {
        i.name: (
            int(dict(i.params).get("0", dict(i.params).get("n", "0"))),
            int(dict(i.params).get("1", dict(i.params).get("m", "0"))),
        )
        for i in spec.instances
        if i.kind == "tube"
    }
    for c in spec.connects:
        if not c.menu:
            continue
        m = re.search(r"(\d+)\s*[x×]\s*(\d+)", c.menu)
        if not m:
            continue
        k = int(m.group(2))
        # the collar wraps the destination hole; only the dst's tube
        # symmetry matters
        for end in (c.dst,):
            inst = end.split(".", 1)[0].split(" ")[0].split("/")[0]
            if inst in tubes:
                n, mm = tubes[inst]
                g = math.gcd(n, mm)
                if g % k != 0:
                    out.append(
                        Finding(
                            "port.symmetry",
                            Severity.ERROR,
                            f"collar order {k} does not divide gcd({n},{mm})={g}",
                            where=c.src,
                            span=c.span,
                        )
                    )
    return out
