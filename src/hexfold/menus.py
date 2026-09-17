"""Nanobud menus (SPEC section 12): expand menu lines into concrete AST.

Each menu is a pure function from the parsed AST to expanded AST lines:
the fullerene-side holes, any generated neck instances (``source`` set to
the menu name) and the fuse/bond connects that realise the attachment.
Expansion happens before build; the original menu line is kept on the spec
with ``expanded`` recording what it produced.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from .fullerene import C60Data
from .lattice import Site, neighbors
from .text import Connect, Hole, Instance, Spec

_MENU_ARG = re.compile(r"^(?P<name>[A-Za-z0-9+\-]+)(?:\((?P<arg>[^)]*)\))?$")


def _host_ref(dst: str) -> tuple[str, Site, int | None]:
    """Parse a menu dst 'inst/(u,v,s)[:d]' -> (instance, site, dir)."""
    inst, rest = dst.split("/", 1)
    site_s, _, d = rest.partition(":")
    return inst, Site.parse(site_s), int(d) if d else None


def _ring_key(ring: list[Any]) -> tuple[str, ...]:
    ss = [str(v) for v in ring]
    return min(tuple(ss[i:] + ss[:i]) for i in range(len(ss)))


def _lexmin_ring(rings: list[list[Any]]) -> list[Any]:
    return min(rings, key=_ring_key)


def _c60_min_site(ring: list[Any]) -> Site:
    """Smallest Site label on a C60 ring (vids are already Sites)."""
    return min((v for v in ring if isinstance(v, Site)), key=lambda v: (v.u, v.v, v.s))


def _hole_name(inst: Instance) -> str:
    return "hole" if not inst.holes else f"hole{len(inst.holes)}"


def _gen_name(spec_insts: list[Instance], base: str) -> str:
    names = {i.name for i in spec_insts}
    name = base
    i = 1
    while name in names:
        name = f"{base}{i}"
        i += 1
    return name


def _add_hole(insts: list[Instance], name: str, hole: Hole) -> str:
    """Append ``hole`` to instance ``name``; return its port name."""
    for i, inst in enumerate(insts):
        if inst.name == name:
            pname = _hole_name(inst)
            insts[i] = replace(inst, holes=(*inst.holes, hole))
            return pname
    raise KeyError(name)


def expand(spec: Spec) -> Spec:
    """Expand every ``verb == 'menu'`` connect into concrete lines."""
    insts = list(spec.instances)
    connects: list[Connect] = []
    data: C60Data | None = None

    def c60() -> C60Data:
        nonlocal data
        if data is None:
            data = C60Data()
        return data

    for c in spec.connects:
        if c.verb != "menu" or c.expanded is not None:
            # already expanded (a Net's spec, or a spec read back from the
            # sectioned JSON): its generated instances, holes and connects
            # are present in the spec already -- expanding again would
            # duplicate them.  Idempotent by construction.
            connects.append(c)
            continue
        menu = c.menu or ""
        m = _MENU_ARG.match(menu)
        name = m["name"] if m else menu
        arg = m["arg"] if m else None
        host, hsite, hdir = _host_ref(c.dst)
        bud = c.src
        exp: dict[str, Any] = {"menu": menu}

        if name == "2+2":
            d = c60()
            sites = d.sites()
            b66 = min(
                (tuple(sorted((sites[a], sites[b]), key=str)) for a, b in d.bonds66()),
                key=lambda p: (str(p[0]), str(p[1])),
            )
            nb = neighbors(hsite)[(hdir or 0) % 3]
            gen = [
                Connect(
                    src=f"{bud}/{b66[0]}",
                    dst=f"{host}/{hsite}",
                    verb="bond",
                    span=c.span,
                ),
                Connect(
                    src=f"{bud}/{b66[1]}",
                    dst=f"{host}/{nb}",
                    verb="bond",
                    span=c.span,
                ),
            ]
            exp["connects"] = [f"{g.src} --bond--> {g.dst}" for g in gen]
        elif name in ("9-6", "8-7"):
            # bond-mode attachment onto an intact host (Wang & Li 2009):
            # the C54 bud hole's six dangling atoms are bonded onto the C3
            # orbits around one host hexagon.  The registration that gives
            # the named seam ({9,6} or {8,7}) is solved at build time.
            d = c60()
            sites = d.sites()
            rings = [[sites[a] for a in ring] for ring in d.hexagons]
            hsite_b = _c60_min_site(_lexmin_ring(rings))
            _add_hole(insts, bud, Hole(6, hsite_b))
            gen = []
            exp["holes"] = {bud: [str(hsite_b)]}
            exp["connects"] = [f"{bud}.hole --{name}--> {host}/{hsite}:{hdir}"]
        elif name in ("DA-neck", "DB-neck"):
            length = int(arg) if arg else 2
            d = c60()
            sites = d.sites()
            bud_holes: list[Hole] = []
            if name == "DA-neck":
                rings = [[sites[a] for a in r] for r in d.pentagons]
                bud_holes.append(Hole(5, _c60_min_site(_lexmin_ring(rings))))
                neck_nm = (5, 0)
            else:
                rings6 = [[sites[a] for a in r] for r in d.hexagons]
                # fused opening: hexagon hole (6 dangling).  Its excision
                # also destroys the three adjacent pentagons -> P5 = 9.
                bud_holes.append(Hole(6, _c60_min_site(_lexmin_ring(rings6))))
                neck_nm = (6, 0)
            hole_names = [f"{bud}.{_add_hole(insts, bud, h)}" for h in bud_holes]
            host_hole_name = None
            if name == "DA-neck":
                # 5-dangling host opening: a connected 3-atom path
                # (3|S| - 2 e_S = 5 for |S|=3, e_S=2) — Baowan, Cox &
                # Hill's (5,0) neck seats on it
                host_hole_name = (
                    f"{host}.{_add_hole(insts, host, Hole(-2, hsite, hdir))}"
                )
            nname = _gen_name(insts, "neck")
            insts.append(
                Instance(
                    name=nname,
                    kind="tube",
                    params=(
                        ("0", str(neck_nm[0])),
                        ("1", str(neck_nm[1])),
                        ("len", str(length)),
                    ),
                    source=menu,
                )
            )
            dst_site = f"{hsite}:{hdir}" if hdir is not None else f"{hsite}"
            if name == "DA-neck":
                assert host_hole_name is not None
                dst = host_hole_name
                host_fuse_menu = None
                host_k = -1  # fit over k=0..4 by smallest max seam ring
            else:
                # Baowan's three heptagons are the bud-side seam's; the
                # host join gets its native {7:6} — no collar (a {7x3}
                # collar over-curves the net, euler.residual -3)
                dst = f"{host} @ {host}/{dst_site}"
                host_fuse_menu = None
                host_k = 0
            gen = [
                Connect(
                    src=hole_names[0],
                    dst=f"{nname}.in",
                    verb="fuse",
                    k=0,
                    span=c.span,
                ),
                Connect(
                    src=f"{nname}.out",
                    dst=dst,
                    verb="fuse",
                    k=host_k,
                    menu=host_fuse_menu,
                    span=c.span,
                ),
            ]
            exp["instances"] = [
                f"{nname}: tube({neck_nm[0]},{neck_nm[1]}, len={length})"
            ]
            exp["holes"] = {bud: [str(h.site) for h in bud_holes]}
            if name == "DA-neck":
                exp["holes"][host] = ["path3"]
            exp["connects"] = [
                f"{g.src} --{g.verb}"
                + (f"{{{g.menu}}}" if g.menu else "")
                + (f" k={g.k}" if g.k is not None else "")
                + f"--> {g.dst}"
                for g in gen
            ]
        else:
            gen = []

        connects.append(replace(c, expanded=exp))
        connects.extend(gen)
    return replace(spec, instances=tuple(insts), connects=tuple(connects))
