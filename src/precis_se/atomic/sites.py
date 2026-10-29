"""``view='sites'`` — the valve's attachment-site enumerator, and the
shape-and-polarity complementarity stub that scores it (tools 3–4 of
``docs/backlog/rotary-ratchet-valve.md`` "Design tool set", hexfold spec
§28.8).

**Sites.** (The pure enumeration and taxonomy below live in
:mod:`precis.structure.sites`, which ships in the wheel; this module
renders them.) Every carbon that can take a substituent, classified three
ways:

- *ring taxonomy* — ``sp3`` when the carbon is four-or-more-coordinated
  on single bonds (a graft junction — no free valence, counted but not a
  site), ``substituted`` when it already carries a non-C non-H group,
  ``open-edge`` under three carbon neighbours (a rim dangling bond —
  the H cap, if any, is replaceable), else the C–C rings it sits on:
  every defect (non-6) ring size it closes on the bond graph, joined
  ``+`` (``5+7-ring`` where two defects share the atom), ``6-ring`` for
  a pure-hexagon carbon, ``non-ring`` past that;
- *face* — which side of the local surface the substituent would grow
  on: the free direction's sign against the block's envelope axis
  (``out`` = away from the axis, ``in`` = toward the bore, ``axial`` =
  along it, the rim dangling bond — axial only for a zigzag rim; an
  armchair rim's dangling bond has a radial component). A sp² wall
  carbon is a site on each free face — one row per (atom, face);
- *steric exposure* — the free fraction of a probe sphere of radius
  ``exposure_A`` (default 3.0 Å) centred one radius out along the free
  direction, sampled by a deterministic Fibonacci lattice — 1.0 is wide
  open.

Each row also carries ``(z, θ, r)`` about the block's own envelope axis
in :mod:`precis_se.atomic.clearance`'s conventions (z from the anchor on
the axis line, θ off a fixed world basis); a block with no axis-bearing
envelope still lists ring/face=``n/a``/exposure with the cylindrical
columns blank. The pair form ``args={'a','b'}`` keeps only sites that
face the *other* block within ``pocket_gap_A`` — the valve doc's "the
rim's sites travel, the shell's sites are fixed".

**Stored record.** The single-block form also renders the bound
structure's own build record (gr503027): ``generated.sites`` — the
generator's measured site list in its authored frame, ordinals and
z/θ/r plus free-direction normals and the two-point pendant pairs — and
the ``seam.geometry``/``junction.strain``/``seam.strain`` finding
``data`` (the measured seam/junction angles and residuals), which
otherwise only ever surfaced as ``view='report'`` INFO prose. Nothing is
recomputed; both sections are verbatim stored rows, labelled as such.

**Complementarity stub.** ``args={'a','b','lining':{...}}`` on
``view='clearance'``: a per-block list of ``{'site': <ordinal> |
'ring:<class>', 'group': 'OH'|'NH3+'|'COO-'|'CH3'|'H'}`` assignments
decorates site carbons with a polarity class (OH polar-donor, NH3+
cationic, COO- anionic, CH3 apolar, H and undecorated bare). Over the
same read-only sweep as the clearance table, each pocket is scored per
angle: a shape term (volume vs ``cargo_A3``) plus a polarity term —
COMPATIBLE decorated contact pairs (+1: any polar with any polar,
cationic–anionic, apolar–apolar, and polar–charged ion–dipole pairs)
minus unlike ones (−1: polar or charged against apolar, same-sign
charges); ``bare`` never counts. When the term's sign flips across the
sweep the verdict names the admitting and rejecting arcs — contiguous
runs of the SWEEP ANGLE, wrapping at 360°; otherwise it says "no
rectification". It is a stub BY DESIGN — no energetics; the ladder above
it is the se atomic-mode relax/charge work.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

from precis.blocktree.types import no_block_msg
from precis.errors import BadInput, NotFound
from precis.format import render_agent_table
from precis.handlers._interpret import PREFIX as _INTERPRETED
from precis.handlers._slice_args import slice_window
from precis.structure import Scene as StructScene
from precis.structure import sites as _core
from precis_se.atomic import clearance as se_atomic_clearance
from precis_se.atomic.clearance import AtomCloud, SweepAxis
from precis_se.ops import SeTree

if TYPE_CHECKING:  # pragma: no cover - typing only
    from precis.store import Store

# The pure enumeration and taxonomy live in precis.structure.sites (wheel-safe);
# the names this module renders with, and its tests import, stay here.
Site = _core.Site
ring_classes = _core.ring_classes
is_closed_cage = _core.is_closed_cage
_bond_graph = _core.bond_graph
_hyb_of = _core.hyb_of
_sites_block = _core.sites_block
_SITE_CLASSES = _core.SITE_CLASSES
_EXCLUDED_CLASSES = _core.EXCLUDED_CLASSES


#: A site "faces" the other block when the cross-block nearest atom lies
#: inside this half-cone of the free direction (and the pair's gap is
#: under pocket_gap_A — the same contact scale the clearance view uses).
_FACING_COS = 0.5

#: Substituent-group → polarity class for the complementarity stub.
GROUP_POLARITY: dict[str, str] = {
    "OH": "polar-donor",
    "NH3+": "cationic",
    "COO-": "anionic",
    "CH3": "apolar",
    "H": "bare",
}

#: The polarity classes that behave polar — donor and acceptor both; any
#: two of them pair compatible (donor–donor, donor–acceptor,
#: acceptor–acceptor).
_POLAR = frozenset({"polar-donor", "polar-acceptor"})

_GROUP_NAMES = tuple(GROUP_POLARITY)


def _facing_sites(
    sites: list[Site],
    cloud_a: AtomCloud,
    cloud_b: AtomCloud,
    pocket_gap_A: float,
) -> list[Site]:
    """The pair-view subset: sites whose free direction points at a
    partner-block atom within ``pocket_gap_A`` — the valve doc's
    travelling-rim half of the surface list."""
    field = se_atomic_clearance.gap_field(cloud_a, cloud_b)
    out: list[Site] = []
    for s in sites:
        if s.block == cloud_a.name:
            j = int(field.near_a[s.ordinal])
            gap = field.gaps_a[s.ordinal]
            partner = cloud_b.pts_A[j]
            atom = cloud_a.pts_A[s.ordinal]
        else:
            j = int(field.near_b[s.ordinal])
            gap = field.gaps_b[s.ordinal]
            partner = cloud_a.pts_A[j]
            atom = cloud_b.pts_A[s.ordinal]
        to = partner - atom
        n = float(np.linalg.norm(to))
        if n < 1e-9:
            continue
        if gap < pocket_gap_A and float(np.dot(s.normal, to / n)) > _FACING_COS:
            out.append(s)
    return out


def _vet_exposure_A(args: dict[str, Any]) -> float:
    raw = args.get("exposure_A", 3.0)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise BadInput(
            f"view='sites' 'exposure_A' must be a number (Å); got {raw!r}"
        ) from exc
    if not (0.5 <= value <= 20.0):
        raise BadInput(f"view='sites' 'exposure_A' must be in [0.5, 20] Å; got {value}")
    return value


def _vet_face(args: dict[str, Any]) -> str:
    face = str(args.get("face") or "both").strip().lower()
    if face not in ("in", "out", "axial", "both"):
        raise BadInput(
            f"view='sites' 'face' must be 'in', 'out', 'axial' or 'both'; got {face!r}"
        )
    return face


#: Default cap on listed site rows — the table is sorted by exposure, so
#: the head is the most open sites; the summary always counts them all.
_LIMIT_DEFAULT = 50


def _vet_limit(args: dict[str, Any]) -> tuple[int, int, list[str]]:
    """The row window: canonical ``page``/``page_size``, ``limit`` the
    deprecated ``page_size`` alias (gr514469). Returns ``(offset,
    limit, readings)`` — ``limit`` 0 lists every row."""
    offset, size, readings = slice_window(
        args, where="get(kind='se', view='sites')", default_size=_LIMIT_DEFAULT
    )
    return offset, _LIMIT_DEFAULT if size is None else size, readings


def _finish(lines: list[str], readings: list[str]) -> str:
    """The joined view body, leading with the ``interpreted:`` line when
    a deprecated slice spelling was used."""
    body = "\n".join(lines) + "\n"
    if readings:
        body = f"{_INTERPRETED}{'; '.join(readings)}\n{body}"
    return body


def _fmt_sites(
    sites: list[Site],
    classes: dict[str, list[str | None]],
    limit: int = 0,
    offset: int = 0,
) -> list[str]:
    """The site table (``limit`` rows by exposure from ``offset``; 0 =
    all) plus
    the per-(class, face) summary over EVERY row — the summary covers the
    EXCLUDED classes too (``sp3``/``substituted`` count under a ``—``
    face) so a graft junction is accounted for, not dropped."""
    lines: list[str] = []
    shown = sites[offset : offset + limit] if limit else sites[offset:]
    if len(shown) < len(sites):
        lines.append(
            f"showing {len(shown)} of {len(sites)} site rows, most exposed "
            "first — args={'page_size': 0} lists every row; the summary below "
            "counts all of them"
        )
    if not sites:
        lines.append("(no sites)")
    else:
        rows = [
            {
                "block": s.block,
                "ordinal": s.ordinal,
                "label": s.label,
                "ring": s.ring_class,
                "face": s.face,
                "exposure": f"{s.exposure:.2f}",
                "z (Å)": f"{s.z_A:.2f}" if s.z_A is not None else "—",
                "θ": f"{s.theta_deg:.1f}°" if s.theta_deg is not None else "—",
                "r (Å)": f"{s.r_A:.2f}" if s.r_A is not None else "—",
            }
            for s in shown
        ]
        lines.append(
            render_agent_table(
                rows,
                schema=[
                    "block",
                    "ordinal",
                    "label",
                    "ring",
                    "face",
                    "exposure",
                    "z (Å)",
                    "θ",
                    "r (Å)",
                ],
            )
        )
    counts: dict[tuple[str, str], int] = {}
    for s in sites:
        key = (s.ring_class, s.face)
        counts[key] = counts.get(key, 0) + 1
    for cls in classes.values():
        for ring in cls:
            if ring in _EXCLUDED_CLASSES:
                key = (str(ring), "—")
                counts[key] = counts.get(key, 0) + 1
    lines.append("")
    extra = sorted(
        {ring for ring, _face in counts} - set(_SITE_CLASSES) - set(_EXCLUDED_CLASSES)
    )
    count_rows = [
        {"ring": ring, "face": face, "count": counts.get((ring, face), 0)}
        for ring in (*_SITE_CLASSES, *extra)
        for face in ("in", "out", "axial", "n/a")
        if counts.get((ring, face), 0)
    ]
    count_rows += [
        {"ring": ring, "face": "—", "count": counts.get((ring, "—"), 0)}
        for ring in _EXCLUDED_CLASSES
        if counts.get((ring, "—"), 0)
    ]
    lines.append(render_agent_table(count_rows, schema=["ring", "face", "count"]))
    return lines


def _block_scene(store: Store, node: Any) -> StructScene:
    scene = se_atomic_clearance._load_scene(store, node.bound or "")
    if scene is None:
        raise BadInput(
            f"block {node.name!r} is bound to structure {node.bound!r}, "
            "which no longer resolves — the binding is dangling"
        )
    if not scene.atoms:
        raise BadInput(
            f"block {node.name!r}'s bound structure {node.bound!r} has "
            "no atoms — nothing to enumerate sites over"
        )
    return scene


def _require_atomic(
    store: Store, tree: SeTree, name: str
) -> tuple[Any, AtomCloud, StructScene]:
    node = tree.blocks.get(name)
    if node is None:
        raise NotFound(no_block_msg(tree, name))
    if not (node.bound_kind == "structure" and node.bound):
        raise BadInput(
            f"view='sites' needs structure-bound blocks; {name!r} is not "
            "bound — bind_structure or generate it first"
        )
    scene = _block_scene(store, node)
    return node, se_atomic_clearance.atom_cloud(node, scene), scene


def _carbon_note(name: str, cloud: AtomCloud, classes: list[str | None]) -> str:
    n = sum(1 for c in classes if c is not None)
    note = f"{name}: {n} carbon(s)"
    if is_closed_cage(cloud, classes):
        note += (
            " — closed cage: no open edge, so the 'in' face is a sealed "
            "interior; its sites list at exposure 0"
        )
    return note


def _axis_note(axis: SweepAxis | None) -> str:
    if axis is not None:
        return f"axis: {axis.note}"
    return (
        "axis: none — the block's envelope carries no axis; ring class, "
        "face n/a and exposure still report, z/θ/r stay blank"
    )


#: stored-report codes whose ``data`` fields hold the generator's
#: measured seam/junction angles and residuals — rendered verbatim under
#: view='sites' (gr503027: they only ever surfaced as report INFO prose).
_MEASURED_SEAM_CODES = ("seam.geometry", "seam.strain", "junction.strain")


def _data_str(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.4g}"
    if isinstance(v, list):
        if all(isinstance(x, str) for x in v):
            return "".join(v) if all(len(x) == 1 for x in v) else ", ".join(v)
        if all(isinstance(x, int | float) for x in v):
            return "[" + ", ".join(_data_str(x) for x in v) + "]"
    return str(v)


def _stored_generated_lines(
    store: Store, node: Any, limit: int, offset: int = 0
) -> list[str]:
    """The bound structure's stored build-record detail that only ever
    surfaced as report INFO prose (gr503027): ``generated.sites`` — the
    generator's own measured site list (ordinal, region, seam, sign,
    z/θ/r, free-direction normal) plus its two-point pendant pairs — and
    the measured seam/junction angles carried in ``seam.geometry`` /
    ``junction.strain`` / ``seam.strain`` finding ``data``, one row per
    ``junctions``/``seam_atoms`` chain where the record keeps them."""
    ref = store.get_ref(kind="structure", id=node.bound)
    rec = (getattr(ref, "meta", None) or {}).get("generated") if ref else None
    if not isinstance(rec, dict):
        return []
    lines: list[str] = []
    sites_rec = rec.get("sites")
    entries = (
        [e for e in sites_rec["sites"] if isinstance(e, dict)]
        if isinstance(sites_rec, dict) and isinstance(sites_rec.get("sites"), list)
        else []
    )
    if entries and isinstance(sites_rec, dict):
        lines.append("")
        lines.append(
            f"## generated sites (stored build record — {len(entries)} site(s))"
        )
        frame = sites_rec.get("frame")
        if isinstance(frame, dict):
            lines.append(
                f"frame: {frame.get('axis')}; θ0 {frame.get('theta_zero')}; "
                f"{frame.get('measured_on')}; frame.to_stored maps onto "
                "stored ordinals"
            )
        shown = entries[offset : offset + limit] if limit else entries[offset:]
        if len(shown) < len(entries):
            lines.append(
                f"showing {len(shown)} of {len(entries)} stored site rows — "
                "args={'page_size': 0} lists every row"
            )
        lines.append(
            render_agent_table(
                [
                    {
                        "ordinal": e.get("ordinal"),
                        "region": e.get("region"),
                        "seam": e.get("seam"),
                        "sign": e.get("sign", "—"),
                        "z (Å)": _data_str(e.get("z_A")),
                        "θ": _data_str(e.get("theta_deg")),
                        "r (Å)": _data_str(e.get("r_A")),
                        "normal": _data_str(e.get("normal")),
                    }
                    for e in shown
                ],
                schema=[
                    "ordinal",
                    "region",
                    "seam",
                    "sign",
                    "z (Å)",
                    "θ",
                    "r (Å)",
                    "normal",
                ],
            )
        )
        pairs = sites_rec.get("pairs")
        if isinstance(pairs, list) and pairs:
            lines.append(
                f"pendant pairs: {len(pairs)} two-point — "
                + ", ".join(str(p) for p in pairs)
            )
    junctions = rec.get("junctions")
    seam_atoms = rec.get("seam_atoms")
    findings = (
        rec.get("report", {}).get("findings")
        if isinstance(rec.get("report"), dict)
        else None
    )
    seam_findings = [
        f
        for f in (findings if isinstance(findings, list) else [])
        if isinstance(f, dict) and f.get("code") in _MEASURED_SEAM_CODES
    ]
    if (
        (isinstance(junctions, list) and junctions)
        or (isinstance(seam_atoms, list) and seam_atoms)
        or seam_findings
    ):
        lines.append("")
        lines.append("## generated seams (stored build record)")
        if isinstance(junctions, list):
            for j in junctions:
                if not isinstance(j, dict):
                    continue
                lines.append(
                    f"{j.get('name')}: {len(j.get('atoms') or [])} atoms, "
                    f"{len(j.get('faces') or [])} faces, "
                    f"{len(j.get('fused') or [])} fused bonds"
                )
        if isinstance(seam_atoms, list) and seam_atoms:
            lines.append(f"seam_atoms: {seam_atoms}")
        for f in seam_findings:
            lines.append(
                f"{f.get('code')} @ {f.get('where') or '—'}: {f.get('message', '')}"
            )
            data = f.get("data")
            if isinstance(data, dict) and data:
                lines.append(
                    "  " + " · ".join(f"{k}={_data_str(v)}" for k, v in data.items())
                )
    return lines


def render_sites(store: Store, tree: SeTree, args: dict[str, Any] | None) -> str:
    """``view='sites'`` — single-block form ``{'block': ...}`` or the
    pair form ``{'a','b'}`` (sites facing the partner within
    ``pocket_gap_A``). The single-block form also shows the bound
    structure's stored build record — ``generated.sites`` and the
    measured seam/junction angles — when the generator kept them
    (:func:`_stored_generated_lines`)."""
    args = args or {}
    face = _vet_face(args)
    exposure_A = _vet_exposure_A(args)
    offset, limit, readings = _vet_limit(args)
    a_name = args.get("a")
    b_name = args.get("b")
    block_name = args.get("block")

    if block_name and (a_name or b_name):
        raise BadInput("view='sites' takes either 'block' or the pair {'a','b'}")
    if not block_name and not (a_name and b_name):
        raise BadInput(
            "view='sites' needs args={'block': <name>} for one block, or "
            "{'a','b'} for the pair view"
        )

    lines: list[str] = []
    if block_name:
        name = str(block_name).strip()
        node, cloud, scene = _require_atomic(store, tree, name)
        axis = se_atomic_clearance._envelope_axis(tree, node)
        sites, classes = _sites_block(cloud, scene, axis, exposure_A)
        if face != "both":
            sites = [s for s in sites if s.face == face]
        lines.append(
            f"# sites: {name!r} — {len(sites)} site row(s), "
            f"rung {se_atomic_clearance._rung_of(store, node)} — "
            "stick/preview geometry unless relaxed"
        )
        lines.append(_axis_note(axis))
        lines.append(_carbon_note(name, cloud, classes))
        lines += _fmt_sites(sites, {name: classes}, limit, offset)
        lines += _stored_generated_lines(store, node, limit, offset)
        return _finish(lines, readings)

    a_name, b_name = str(a_name).strip(), str(b_name).strip()
    if a_name == b_name:
        raise BadInput("view='sites': 'a' and 'b' must differ")
    a_node, cloud_a, scene_a = _require_atomic(store, tree, a_name)
    b_node, cloud_b, scene_b = _require_atomic(store, tree, b_name)
    pocket_gap_A = se_atomic_clearance._pocket_gap_A(args)
    axis_a = se_atomic_clearance._envelope_axis(tree, a_node)
    axis_b = se_atomic_clearance._envelope_axis(tree, b_node)
    sites, classes_a = _sites_block(
        cloud_a, scene_a, axis_a, exposure_A, occl=cloud_b.pts_A
    )
    more, classes_b = _sites_block(
        cloud_b, scene_b, axis_b, exposure_A, occl=cloud_a.pts_A
    )
    sites += more
    facing = _facing_sites(sites, cloud_a, cloud_b, pocket_gap_A)
    if face != "both":
        facing = [s for s in facing if s.face == face]
    lines.append(
        f"# sites: {a_name!r} vs {b_name!r} — {len(facing)} facing site "
        f"row(s) within pocket_gap_A={pocket_gap_A:g} Å"
    )
    lines.append(
        f"rungs: {a_name}={se_atomic_clearance._rung_of(store, a_node)}, "
        f"{b_name}={se_atomic_clearance._rung_of(store, b_node)} — "
        "stick/preview geometry unless relaxed"
    )
    lines.append(
        "each (z, θ, r) is measured against the site's OWN block axis — "
        "see view='clearance' for the pair axis"
    )
    lines.append(_carbon_note(a_name, cloud_a, classes_a))
    lines.append(_carbon_note(b_name, cloud_b, classes_b))
    lines += _fmt_sites(facing, {a_name: classes_a, b_name: classes_b}, limit, offset)
    return _finish(lines, readings)


# ── complementarity stub ──────────────────────────────────────────────


def _label_ring_set(label: str) -> frozenset[int]:
    """The ring sizes a ring-family class names — ``'5+7-ring'`` →
    ``{5, 7}``, ``'6-ring'`` → ``{6}``. Defect labels drop the implicit
    hexagon, so ``ring:6-ring`` selects only the pure-hexagon class."""
    return frozenset(int(p) for p in label[: -len("-ring")].split("+"))


def _polarity_map(
    lining: Any,
    cloud_by_name: dict[str, AtomCloud],
    classes_by_name: dict[str, list[str | None]],
) -> dict[str, dict[int, str]]:
    """``lining`` → per-block ``{ordinal: polarity class}``; undecorated
    atoms are ``bare``. ``site`` is an atom ordinal or ``'ring:<class>'``
    (every carbon classified under that class — ``ring:7-ring`` matches
    every atom that sits on a 7-ring, including defect-shared
    ``5+7-ring`` ones; ``non-ring`` excluded: a carbon that closes no
    small ring isn't a nameable site class)."""
    if not isinstance(lining, dict) or not lining:
        raise BadInput(
            "clearance 'lining' must be an object like "
            "{'rotor': [{'site': 137 | 'ring:6-ring', 'group': 'OH'}]}"
        )
    unknown_blocks = sorted(set(lining) - set(cloud_by_name))
    if unknown_blocks:
        raise BadInput(
            f"lining names block(s) {unknown_blocks} outside the pair "
            f"{sorted(cloud_by_name)}"
        )
    selectable = tuple(c for c in _SITE_CLASSES if c != "non-ring")
    out: dict[str, dict[int, str]] = {name: {} for name in cloud_by_name}
    for name, entries in lining.items():
        if not isinstance(entries, list) or not entries:
            raise BadInput(
                f"lining[{name!r}] must be a non-empty list of "
                "{'site','group'} entries"
            )
        cloud, classes = cloud_by_name[name], classes_by_name[name]
        for entry in entries:
            if not isinstance(entry, dict):
                raise BadInput(f"lining[{name!r}] entries must be objects")
            unknown = sorted(set(entry) - {"site", "group"})
            if unknown:
                raise BadInput(
                    f"lining[{name!r}] unknown key(s) {unknown}; accepted: site, group"
                )
            group = str(entry.get("group", ""))
            if group not in GROUP_POLARITY:
                raise BadInput(
                    f"lining[{name!r}] group must be one of {_GROUP_NAMES}; "
                    f"got {group!r}"
                )
            site = entry.get("site")
            if site is None:
                raise BadInput(f"lining[{name!r}] entry lacks 'site'")
            if isinstance(site, str) and site.startswith("ring:"):
                ring = site[5:]
                if ring == "non-ring":
                    raise BadInput(
                        f"lining[{name!r}] site 'ring:non-ring' is not a "
                        "nameable site class — list ordinals instead"
                    )
                if ring not in _SITE_CLASSES:
                    raise BadInput(
                        f"lining[{name!r}] site 'ring:' class must be one of "
                        f"{selectable}; got {site!r}"
                    )
                if ring == "open-edge":
                    matched = [i for i, cls in enumerate(classes) if cls == ring]
                else:
                    size = int(ring[: -len("-ring")])
                    matched = [
                        i
                        for i, cls in enumerate(classes)
                        if cls is not None
                        and cls.endswith("-ring")
                        and cls != "non-ring"
                        and size in _label_ring_set(cls)
                    ]
                if not matched:
                    present: dict[str, int] = {}
                    for cls in classes:
                        if cls in selectable or (
                            cls is not None
                            and cls.endswith("-ring")
                            and cls != "non-ring"
                        ):
                            present[cls] = present.get(cls, 0) + 1
                    have = (
                        ", ".join(f"{c} {n}" for c, n in sorted(present.items()))
                        or "none"
                    )
                    raise BadInput(
                        f"lining[{name!r}] site {site!r} matches no carbon on "
                        f"{name!r} (classes present: {have}). A carbon's "
                        "class names every defect ring it sits on — a "
                        "'5+7-ring' atom matches both 'ring:5-ring' and "
                        "'ring:7-ring'; 'ring:6-ring' is the pure-hexagon "
                        "class. List ordinals from view='sites' for finer"
                    )
                for i in matched:
                    out[name][i] = GROUP_POLARITY[group]
                continue
            try:
                ordinal = int(site)
            except (TypeError, ValueError) as exc:
                raise BadInput(
                    f"lining[{name!r}] site must be an atom ordinal or "
                    f"'ring:<class>'; got {site!r}"
                ) from exc
            if not (0 <= ordinal < len(cloud.labels)):
                raise BadInput(
                    f"lining[{name!r}] site {ordinal} out of range "
                    f"(0..{len(cloud.labels) - 1})"
                )
            if cloud.elements[ordinal] != "C":
                raise BadInput(
                    f"lining[{name!r}] site {ordinal} is "
                    f"{cloud.elements[ordinal]}, not a carbon — sites are "
                    "carbons only"
                )
            out[name][ordinal] = GROUP_POLARITY[group]
    return out


def build_polarity(
    cloud_a: AtomCloud,
    cloud_b: AtomCloud,
    scene_a: StructScene,
    scene_b: StructScene,
    lining: Any,
) -> dict[str, dict[int, str]]:
    """Validate ``lining`` and return the per-block polarity map — runs
    BEFORE the sweep so a bad lining fails fast (one bond-graph build
    per block feeds the ring classes here and nothing else)."""
    classes_by_name: dict[str, list[str | None]] = {}
    for cloud, scene in (
        (cloud_a, scene_a),
        (cloud_b, scene_b),
    ):
        adj, unsat = _bond_graph(scene, cloud.labels, cloud.pts_A)
        classes_by_name[cloud.name] = ring_classes(
            cloud, adj, unsat, _hyb_of(scene, cloud)
        )
    return _polarity_map(
        lining,
        {cloud_a.name: cloud_a, cloud_b.name: cloud_b},
        classes_by_name,
    )


def vet_cargo_A3(args: dict[str, Any]) -> float:
    raw = args.get("cargo_A3", 100.0)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise BadInput(
            f"clearance 'cargo_A3' must be a number (Å³); got {raw!r}"
        ) from exc
    if value <= 0:
        raise BadInput(f"clearance 'cargo_A3' must be > 0 Å³; got {value}")
    return value


def _pair_score(pa: str, pb: str) -> int:
    """Compatibility of two decorated contact partners — +1 like, −1
    unlike, 0 whenever either side is bare.

    Like (the reviewer's ruling: "like" means COMPATIBLE): any polar
    class with any polar class (donor–donor, donor–acceptor,
    acceptor–acceptor), cationic–anionic, apolar–apolar, and
    polar–charged ion–dipole pairs. Unlike: polar or charged against
    apolar, and same-sign charges (cationic–cationic,
    anionic–anionic)."""
    if pa == "bare" or pb == "bare":
        return 0
    if pa == "apolar" or pb == "apolar":
        return 1 if pa == pb == "apolar" else -1
    if pa in _POLAR and pb in _POLAR:
        return 1
    if {pa, pb} == {"cationic", "anionic"}:
        return 1
    if pa in _POLAR or pb in _POLAR:
        return 1  # ion–dipole: polar next to a charge is favourable
    return -1  # cationic–cationic / anionic–anionic


def _pocket_contacts(
    cloud_a: AtomCloud,
    cloud_b: AtomCloud,
    atoms_a: list[int],
    atoms_b: list[int],
    threshold_A: float,
) -> list[tuple[int, int]]:
    """Cross-block contact pairs inside one pocket — every (a, b) atom
    pair whose vdW gap is under the pocket threshold."""
    if not atoms_a or not atoms_b:
        return []
    pa = cloud_a.pts_A[atoms_a]
    pb = cloud_b.pts_A[atoms_b]
    d = np.linalg.norm(pa[:, None, :] - pb[None, :, :], axis=2)
    g = d - cloud_a.radii_A[atoms_a][:, None] - cloud_b.radii_A[atoms_b][None, :]
    ii, jj = np.nonzero(g < threshold_A)
    return [(atoms_a[int(i)], atoms_b[int(j)]) for i, j in zip(ii, jj)]


def _score_pockets(
    cloud_a: AtomCloud,
    cloud_b: AtomCloud,
    pocket_list: list[Any],
    polarity: dict[str, dict[int, str]],
    threshold_A: float,
    cargo_A3: float,
) -> tuple[list[int], int]:
    """``(score per pocket, aggregate polarity term)`` — score = shape
    (pocket hull volume vs cargo) + polarity (compatible minus unlike
    decorated contact pairs)."""
    scores: list[int] = []
    term_all = 0
    for pocket in pocket_list:
        contacts = _pocket_contacts(
            cloud_a, cloud_b, pocket.atoms_a, pocket.atoms_b, threshold_A
        )
        term = 0
        for i, j in contacts:
            pa = polarity[cloud_a.name].get(i, "bare")
            pb = polarity[cloud_b.name].get(j, "bare")
            term += _pair_score(pa, pb)
        term_all += term
        shape = (
            1 if pocket.volume_A3 is not None and pocket.volume_A3 >= cargo_A3 else -1
        )
        scores.append(shape + term)
    return scores, term_all


def _arc_runs(
    terms: list[int],
    angles: list[float],
    wrap: bool,
) -> list[tuple[int, float, float]]:
    """Contiguous same-sign runs over the sweep, each ``(sign, first,
    last)`` — when ``wrap`` (a full 360° sweep) a run spanning the
    range end merges across the seam and its ``last`` exceeds 360
    (``300°–420°`` reads as one unbroken arc)."""
    runs: list[list[Any]] = []
    for angle, term in zip(angles, terms):
        sign = 1 if term > 0 else (-1 if term < 0 else 0)
        if runs and runs[-1][0] == sign:
            runs[-1][2] = angle
        else:
            runs.append([sign, angle, angle])
    if wrap and len(runs) > 1 and runs[0][0] == runs[-1][0]:
        first = runs.pop(0)
        runs[-1][2] = first[2] + 360.0
    return [(int(s), float(a), float(b)) for s, a, b in runs]


def render_complementarity(
    cloud_a: AtomCloud,
    cloud_b: AtomCloud,
    rows: list[Any],
    polarity: dict[str, dict[int, str]],
    cargo_A3: float,
    threshold_A: float,
    range_deg: float,
) -> list[str]:
    """The STUB scorer's section — the validated polarity map +
    per-angle best/worst pocket score + the arc verdict over SWEEP-ANGLE
    runs. ``rows`` are the sweep rows (each carrying its pockets)."""
    n_dec = {name: len(m) for name, m in polarity.items()}
    lines = [
        "\nlining (STUB — shape + polarity match, no energetics): "
        + ", ".join(f"{name} {n} decorated carbon(s)" for name, n in n_dec.items())
        + f"; cargo {cargo_A3:g} Å³",
        "polarity: OH=polar-donor, NH3+=cationic, COO-=anionic, CH3=apolar, "
        "undecorated=bare; compatible: polar–polar, cationic–anionic, "
        "apolar–apolar, polar–charged",
    ]
    table_rows = []
    angles: list[float] = []
    terms: list[int] = []
    for row in rows:
        scores, term = _score_pockets(
            cloud_a,
            row.cloud_b,
            row.pockets,
            polarity,
            threshold_A,
            cargo_A3,
        )
        angles.append(row.angle_deg)
        terms.append(term)
        table_rows.append(
            {
                "angle": f"{row.angle_deg:g}°",
                "best score": max(scores) if scores else "—",
                "worst score": min(scores) if scores else "—",
                "polarity term": term,
            }
        )
    lines.append(
        render_agent_table(
            table_rows, schema=["angle", "best score", "worst score", "polarity term"]
        )
    )
    runs = _arc_runs(terms, angles, wrap=abs(range_deg - 360.0) < 1e-9)
    pos = [r for r in runs if r[0] > 0]
    neg = [r for r in runs if r[0] < 0]
    if pos and neg:
        # all runs when few enough to read; else the longest of each sign.
        show_pos = (
            pos if len(pos) + len(neg) <= 4 else [max(pos, key=lambda r: r[2] - r[1])]
        )
        show_neg = (
            neg if len(pos) + len(neg) <= 4 else [max(neg, key=lambda r: r[2] - r[1])]
        )
        fmt_pos = ", ".join(f"[{a:g}°–{b:g}°]" for _, a, b in show_pos)
        fmt_neg = ", ".join(f"[{a:g}°–{b:g}°]" for _, a, b in show_neg)
        word_pos = "arcs" if len(show_pos) > 1 else "arc"
        word_neg = "arcs" if len(show_neg) > 1 else "arc"
        lines.append(
            f"verdict: admits on {word_pos} {fmt_pos}, rejects on {word_neg} "
            f"{fmt_neg} — the polarity term flips across the sweep (STUB)"
        )
    else:
        lines.append(
            "verdict: no rectification — polarity does not alternate "
            "around the shell (STUB)"
        )
    return lines
