"""check(spec) -> Report (SPEC section 9).

Never raises for structural problems.  Phase-1 codes:
euler.chi, euler.residual, euler.closed_unreachable, internal.euler,
valence.over, valence.under, cut.overlap, ring.size.unusual,
port.symmetry, and the geometry tier (geom.*) when ``geometry=True``.
Phase-2 codes: seam.rings, annot.sublattice, fit.unsolvable, fit.alternatives,
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


def _geometry_findings(net: Net, profile: Profile) -> list[Finding]:
    """Stick-model geometry check: bond lengths vs sigma, ring-corner
    angles vs the ring-ideal interior angle.  ``geom.summary`` always
    carries the statistics; individual findings are capped at the ten
    worst offenders so reports stay small and deterministic."""
    from .stick import stick_info

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

    b_arr = np.array([x[0] for x in bond_dev]) if bond_dev else np.zeros(1)
    a_arr = np.array([x[0] for x in ang_dev]) if ang_dev else np.zeros(1)
    out.append(
        Finding(
            "geom.summary",
            Severity.INFO,
            f"bond rms {float(np.sqrt((b_arr**2).mean())):.3f} A "
            f"max {float(np.abs(b_arr).max()):.3f} A; "
            f"angle rms {float(np.sqrt((a_arr**2).mean())):.1f} deg "
            f"max {float(np.abs(a_arr).max()):.1f} deg",
            data=(
                ("bond_rms", round(float(np.sqrt((b_arr**2).mean())), 3)),
                ("bond_max", round(float(np.abs(b_arr).max()), 3)),
                ("bond_count", len(bond_dev)),
                ("angle_rms", round(float(np.sqrt((a_arr**2).mean())), 1)),
                ("angle_max", round(float(np.abs(a_arr).max()), 1)),
                ("angle_count", len(ang_dev)),
                ("suppressed", max(0, len(bond_bad) - 10) + max(0, len(ang_bad) - 10)),
                ("max_force_final", round(max_force, 4)),
            ),
        )
    )
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
    # port.symmetry: collar order k must divide gcd(n,m) of the tube it binds
    findings.extend(_port_symmetry(ast))
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
