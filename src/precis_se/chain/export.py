"""Interop export of the nucleic-acid domain — ``view='export'``.

Four formats, one way (no import):

- **scadnano** JSON — helix→helix, domain→domain, a loop with ``n > 0``
  nucleotides as a scadnano *loopout*, the longest strand flagged
  ``is_scaffold``. Lattice designs carry scadnano's ``grid`` +
  ``grid_position``; anything else is ``grid: none`` with a position in
  nm.
- **caDNAno** legacy (c2) JSON — lattice designs only, every helix on the
  same lattice; a waypoint helix is ``Unsupported`` naming it. The pointer
  arrays are ``[5' helix, 5' base, 3' helix, 3' base]`` per base, scaffold
  in ``scaf``, every other strand in ``stap``; a loop's nucleotides
  become a caDNAno insertion (``loop[offset] = n``) at the exit base,
  since caDNAno has no off-lattice nucleotide.
- **oxDNA** ``.top`` + ``.conf`` — one nucleotide per unit occupancy plus
  the loop nucleotides, positions from the helix frames (a placed loop's
  curve when ``relax_chain`` wrote one, else the chord between the two
  exits), in oxDNA length units (:data:`precis_se.chain.nucleic.OXDNA_UNIT_M`).
- **PDB** — every ``realize_chain``-bound segment's atoms, world-posed by
  the segment's own pose, one chain id per segment and a ``TER`` between.

**Helix indices are ours, not caDNAno's.** The chain domain reproduces
caDNAno's crossover offsets with the neighbour walk *reflected* (the
``se-nucleic-realize-export`` log, 2026-09-29): a viewing/handedness
convention nothing before this module could observe. The exports write
our ``row``/``col`` as given and make no claim of column-for-column
agreement with a caDNAno file of the same design until that handedness is
settled against a real one.
"""

from __future__ import annotations

import itertools
import json
import math
from collections.abc import Callable
from typing import Any

import numpy as np

from precis.errors import Unsupported
from precis_se.chain import nucleic
from precis_se.chain.layout import HelixGeometry, helix_geometry
from precis_se.chain.vocab import (
    HELIX_ROLE,
    SEGMENT_ROLE,
    STRAND_ROLE,
    ChainError,
    DomainSpec,
    chain_role,
    group_domains,
)
from precis_se.ops import SeTree

FORMATS = ("scadnano", "cadnano", "oxdna", "pdb")

#: scadnano's file-format version the JSON claims — the structural subset
#: written here (helices, grid, strands, domains, loopouts, sequence,
#: is_scaffold) has been stable across 0.17–0.19.
SCADNANO_VERSION = "0.19.4"

#: caDNAno requires every virtual helix the same length, a multiple of
#: the lattice repeat.
_CADNANO_REPEAT = {"square": 32, "honeycomb": 21}

#: oxDNA's nucleotide model: the backbone interaction site sits this far
#: from the centre of mass along ``-a1`` (oxDNA1's 0.4; the geometry
#: written here is a starting configuration, not an equilibrium one).
_OXDNA_BACKBONE_OFFSET_SU = 0.4

_STAPLE_COLOUR = 13369344  # caDNAno's default red


# ── the shared readout ──────────────────────────────────────────────────


def _helices(tree: SeTree) -> list[tuple[str, Any]]:
    """Helix blocks in tree order — the export's helix index is the
    position here."""
    return [
        (name, node)
        for name, node in tree.blocks.items()
        if chain_role(node) == HELIX_ROLE
    ]


def _strands(tree: SeTree) -> dict[str, list[DomainSpec]]:
    return group_domains(list(tree.domains)).by_strand


def _sequence(tree: SeTree, strand: str) -> str | None:
    node = tree.blocks.get(strand)
    if node is None or chain_role(node) != STRAND_ROLE:
        return None
    seq = (node.chain or {}).get("sequence")
    return str(seq) if seq else None


def _strand_nt(route: list[DomainSpec]) -> int:
    return sum(d.n_units + int(d.loop_before_nt or 0) for d in route)


def _scaffold(strands: dict[str, list[DomainSpec]]) -> str | None:
    """The longest strand by nucleotide count — scadnano's ``is_scaffold``
    and caDNAno's ``scaf`` array. Stated, not inferred from a name."""
    if not strands:
        return None
    return max(strands, key=lambda s: (_strand_nt(strands[s]), s))


def _lattice_site(node: Any) -> dict[str, Any] | None:
    path = ((node.chain or {}).get("path") or {}).get("lattice")
    return dict(path) if path else None


# ── scadnano ────────────────────────────────────────────────────────────


def to_scadnano(tree: SeTree, *, design: str) -> dict[str, Any]:
    helices = _helices(tree)
    if not helices:
        raise Unsupported(
            f"export: design {design!r} has no helix — declare_helix first",
            next="declare_helix + add_domain, then export",
        )
    sites = [_lattice_site(node) for _name, node in helices]
    kinds = {s["kind"] for s in sites if s is not None}
    on_grid = all(s is not None for s in sites) and len(kinds) == 1
    grid = next(iter(kinds)) if on_grid else "none"
    idx = {name: i for i, (name, _node) in enumerate(helices)}
    helix_rows: list[dict[str, Any]] = []
    for i, (name, node) in enumerate(helices):
        record = node.chain or {}
        row: dict[str, Any] = {"idx": i, "max_offset": int(record.get("n_units") or 0)}
        site = sites[i]
        if on_grid and site is not None:
            row["grid_position"] = [int(site["col"]), int(site["row"])]
        else:
            try:
                origin = helix_geometry(node).origin(0)
            except ChainError as exc:
                raise Unsupported(f"export: helix {name!r}: {exc}") from exc
            row["position"] = {
                "x": float(origin[0]) * 1e9,
                "y": float(origin[1]) * 1e9,
                "z": float(origin[2]) * 1e9,
            }
        helix_rows.append(row)
    strands = _strands(tree)
    scaffold = _scaffold(strands)
    strand_rows: list[dict[str, Any]] = []
    for strand in sorted(strands, key=lambda s: (s != scaffold, s)):
        route = strands[strand]
        domains: list[dict[str, Any]] = []
        for d in route:
            if d.helix not in idx:
                raise Unsupported(
                    f"export: strand {strand!r} domain {d.ord} names helix "
                    f"{d.helix!r}, which is not a helix block (chain_dangling_domain)"
                )
            n_loop = int(d.loop_before_nt or 0)
            if d.ord > 0 and n_loop > 0:
                domains.append({"loopout": n_loop})
            domains.append(
                {
                    "helix": idx[d.helix],
                    "forward": bool(d.forward),
                    "start": d.start,
                    "end": d.end,
                }
            )
        row = {"domains": domains, "is_scaffold": strand == scaffold}
        seq = _sequence(tree, strand)
        if seq is not None:
            row["sequence"] = seq
        strand_rows.append(row)
    return {
        "version": SCADNANO_VERSION,
        "grid": grid,
        "helices": helix_rows,
        "strands": strand_rows,
    }


# ── caDNAno ─────────────────────────────────────────────────────────────


def to_cadnano(tree: SeTree, *, design: str) -> dict[str, Any]:
    helices = _helices(tree)
    if not helices:
        raise Unsupported(
            f"export: design {design!r} has no helix — declare_helix first",
            next="declare_helix + add_domain, then export",
        )
    sites: list[dict[str, Any]] = []
    for name, node in helices:
        site = _lattice_site(node)
        if site is None:
            raise Unsupported(
                f"export: caDNAno is lattice-only and helix {name!r} follows "
                "waypoints, not a lattice site",
                next="export format='scadnano' (positions in nm) or 'oxdna' instead",
            )
        sites.append(site)
    kinds = {s["kind"] for s in sites}
    if len(kinds) != 1:
        raise Unsupported(
            f"export: caDNAno needs one lattice for the whole design, this one mixes "
            f"{', '.join(sorted(kinds))}",
            next="export format='scadnano' instead",
        )
    kind = next(iter(kinds))
    repeat = _CADNANO_REPEAT[kind]
    longest = max(int((node.chain or {}).get("n_units") or 0) for _n, node in helices)
    length = max(repeat, math.ceil(longest / repeat) * repeat)
    idx = {name: i for i, (name, _node) in enumerate(helices)}
    vstrands: list[dict[str, Any]] = []
    for i, (_name, _node) in enumerate(helices):
        vstrands.append(
            {
                "num": i,
                "row": int(sites[i]["row"]),
                "col": int(sites[i]["col"]),
                "scaf": [[-1, -1, -1, -1] for _ in range(length)],
                "stap": [[-1, -1, -1, -1] for _ in range(length)],
                "loop": [0] * length,
                "skip": [0] * length,
                "scafLoop": [],
                "stapLoop": [],
                "stap_colors": [],
            }
        )
    strands = _strands(tree)
    scaffold = _scaffold(strands)
    for strand, route in strands.items():
        array = "scaf" if strand == scaffold else "stap"
        positions: list[tuple[int, int]] = []
        for d in route:
            if d.helix not in idx:
                raise Unsupported(
                    f"export: strand {strand!r} domain {d.ord} names helix "
                    f"{d.helix!r}, which is not a helix block (chain_dangling_domain)"
                )
            n_loop = int(d.loop_before_nt or 0)
            if d.ord > 0 and n_loop > 0 and positions:
                h, b = positions[-1]
                vstrands[h]["loop"][b] += n_loop
            positions.extend((idx[d.helix], offset) for offset in d.offsets())
        for (h, b), (h2, b2) in itertools.pairwise(positions):
            vstrands[h][array][b][2] = h2
            vstrands[h][array][b][3] = b2
            vstrands[h2][array][b2][0] = h
            vstrands[h2][array][b2][1] = b
        if array == "stap" and positions:
            h, b = positions[0]
            vstrands[h]["stap_colors"].append([b, _STAPLE_COLOUR])
    return {"name": f"{design}.json", "vstrands": vstrands}


# ── oxDNA ───────────────────────────────────────────────────────────────


def _geoms(tree: SeTree) -> dict[str, HelixGeometry]:
    out: dict[str, HelixGeometry] = {}
    for name, node in _helices(tree):
        try:
            out[name] = helix_geometry(node)
        except ChainError as exc:
            raise Unsupported(f"export: helix {name!r}: {exc}") from exc
    return out


def _radial(geom: HelixGeometry, offset: int, forward: bool) -> np.ndarray:
    """Unit vector from the axis to the strand's backbone at ``offset``."""
    v = geom.exit(offset, forward) - geom.origin(offset)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else np.array([1.0, 0.0, 0.0])


def _tangent(geom: HelixGeometry, offset: int) -> np.ndarray:
    return np.asarray(geom.units.frames[offset][:, 0], dtype=float)


def _perp(t: np.ndarray, seed: np.ndarray) -> np.ndarray:
    n = seed - np.dot(seed, t) * t
    norm = float(np.linalg.norm(n))
    if norm < 1e-9:
        least = int(np.argmin(np.abs(t)))
        axis = np.zeros(3)
        axis[least] = 1.0
        n = axis - np.dot(axis, t) * t
        norm = float(np.linalg.norm(n))
    return n / norm


def _loop_points(
    curve: list[list[float]] | None, p: np.ndarray, q: np.ndarray, n: int
) -> list[np.ndarray]:
    """``n`` backbone positions between exit ``p`` and entry ``q`` (metres):
    along the placed curve when there is one, else the chord."""
    if n <= 0:
        return []
    if curve:
        pts = np.asarray(curve, dtype=float)
        seg = np.diff(pts, axis=0)
        seg_len = np.linalg.norm(seg, axis=1)
        cum = np.concatenate([[0.0], np.cumsum(seg_len)])
        total = float(cum[-1])
        if total > 0:
            out: list[np.ndarray] = []
            for i in range(n):
                s = total * (i + 1) / (n + 1)
                j = min(
                    max(int(np.searchsorted(cum, s, side="right") - 1), 0), len(seg) - 1
                )
                frac = 0.0 if seg_len[j] <= 0 else (s - cum[j]) / seg_len[j]
                out.append(pts[j] + frac * seg[j])
            return out
    return [p + (q - p) * (i + 1) / (n + 1) for i in range(n)]


def to_oxdna(tree: SeTree, *, design: str) -> tuple[str, str]:
    """``(.top text, .conf text)``. Nucleotides are listed strand by
    strand, each strand 3'→5' (oxDNA's convention); an unsequenced
    nucleotide is written ``N`` — oxDNA itself needs a real base, so a
    design exported before its sequence exists is a geometry, not a
    runnable input, and says so by that letter."""
    geoms = _geoms(tree)
    if not geoms:
        raise Unsupported(
            f"export: design {design!r} has no helix — declare_helix first",
            next="declare_helix + add_domain, then export",
        )
    strands = _strands(tree)
    unit = nucleic.OXDNA_UNIT_M
    p_radius = nucleic.P_RADIUS_M["B-DNA"]
    rows: list[
        tuple[int, str, np.ndarray, np.ndarray, np.ndarray]
    ] = []  # strand, base, com, a1, a3
    strand_ids = {name: i + 1 for i, name in enumerate(sorted(strands))}
    for strand in sorted(strands):
        route = strands[strand]
        seq = _sequence(tree, strand)
        pos = 0
        nts: list[tuple[str, np.ndarray, np.ndarray, np.ndarray]] = []  # 5'→3'
        prev: DomainSpec | None = None
        for d in route:
            geom = geoms.get(d.helix)
            if geom is None:
                raise Unsupported(
                    f"export: strand {strand!r} domain {d.ord} names helix "
                    f"{d.helix!r}, which is not a helix block (chain_dangling_domain)"
                )
            n_loop = int(d.loop_before_nt or 0) if d.ord > 0 else 0
            if prev is not None and n_loop > 0:
                prev_geom = geoms[prev.helix]
                p = prev_geom.exit(prev.exit_offset, prev.forward)
                q = geom.exit(d.entry_offset, d.forward)
                loop_pts = _loop_points(d.loop_curve, p, q, n_loop)
                for i, bb in enumerate(loop_pts):
                    letter = (
                        seq[pos + i] if seq is not None and pos + i < len(seq) else "N"
                    )
                    nxt = loop_pts[i + 1] if i + 1 < len(loop_pts) else q
                    prv = loop_pts[i - 1] if i > 0 else p
                    t = nxt - prv
                    t = t / (float(np.linalg.norm(t)) or 1.0)
                    a1 = _perp(t, _radial(prev_geom, prev.exit_offset, prev.forward))
                    com = bb + a1 * _OXDNA_BACKBONE_OFFSET_SU * unit
                    nts.append((letter, com, a1, -t))
            pos += n_loop
            for offset in d.offsets():
                letter = seq[pos] if seq is not None and pos < len(seq) else "N"
                pos += 1
                radial = _radial(geom, offset, d.forward)
                backbone = geom.origin(offset) + radial * p_radius
                a1 = -radial
                com = backbone + a1 * _OXDNA_BACKBONE_OFFSET_SU * unit
                t = _tangent(geom, offset)
                a3 = -t if d.forward else t
                nts.append((letter, com, a1, a3))
            prev = d
        # oxDNA lists 3'→5'
        for letter, com, a1, a3 in reversed(nts):
            rows.append((strand_ids[strand], letter, com, a1, a3))
    n = len(rows)
    top_lines = [f"{n} {len(strand_ids)}"]
    # neighbours: in a 3'→5' listing, the 3' neighbour of row i is row i-1
    # of the same strand and the 5' neighbour is row i+1.
    for i, (sid, letter, _com, _a1, _a3) in enumerate(rows):
        three = i - 1 if i > 0 and rows[i - 1][0] == sid else -1
        five = i + 1 if i + 1 < n and rows[i + 1][0] == sid else -1
        top_lines.append(f"{sid} {letter} {three} {five}")
    coms = np.array([r[2] for r in rows]) / unit if rows else np.zeros((0, 3))
    extent = float(np.max(np.abs(coms))) if coms.size else 1.0
    box = 2.0 * extent + 2.0e-9 / unit
    conf_lines = ["t = 0", f"b = {box:.4f} {box:.4f} {box:.4f}", "E = 0 0 0"]
    for _sid, _letter, com, a1, a3 in rows:
        c = com / unit
        conf_lines.append(
            f"{c[0]:.6f} {c[1]:.6f} {c[2]:.6f} "
            f"{a1[0]:.6f} {a1[1]:.6f} {a1[2]:.6f} "
            f"{a3[0]:.6f} {a3[1]:.6f} {a3[2]:.6f} "
            "0 0 0 0 0 0"
        )
    return "\n".join(top_lines) + "\n", "\n".join(conf_lines) + "\n"


# ── PDB ─────────────────────────────────────────────────────────────────

LoadStructure = Callable[[str], tuple[Any, dict[str, Any]] | None]


def to_pdb(tree: SeTree, *, design: str, load_structure: LoadStructure) -> str:
    """Every ``realize_chain``-bound segment, world-posed, one chain id per
    segment. ``load_structure(slug)`` returns the bound design's
    ``(scene, meta)`` or ``None``; the residue naming comes from
    ``meta['chain_atoms']`` (a plain bound scene without it is written as
    ``UNK`` residues, element names)."""
    from precis.cad.vec import as_vec3, pose
    from precis_chain.pdb import write_pdb
    from precis_se.atomic.validate import A_to_m

    m_to_A = 1.0 / A_to_m(1.0)
    elements: list[str] = []
    coords: list[list[float]] = []
    names: list[str] = []
    resnames: list[str] = []
    resseq: list[int] = []
    chain_ids: list[str] = []
    n_chains = 0
    for name, node in tree.blocks.items():
        if (
            chain_role(node) != SEGMENT_ROLE
            or node.bound_kind != "structure"
            or not node.bound
        ):
            continue
        loaded = load_structure(str(node.bound))
        if loaded is None:
            continue
        scene, meta = loaded
        chain_atoms = (meta or {}).get("chain_atoms") or {}
        placed = pose(
            as_vec3([float(v) for v in node.pose]),
            as_vec3([float(v) for v in node.rot]),
        )
        cid = chr(ord("A") + n_chains) if n_chains < 26 else str(n_chains % 10)
        n_chains += 1
        atom_names = chain_atoms.get("names") or []
        atom_res = chain_atoms.get("resnames") or []
        atom_seq = chain_atoms.get("resseq") or []
        for i, atom in enumerate(scene.atoms.values()):
            cart_A = scene.cell.frac_to_cart(atom.frac)
            world = placed.to_world_point(
                as_vec3([float(c) * A_to_m(1.0) for c in cart_A])
            )
            coords.append([float(c) * m_to_A for c in world])
            elements.append(atom.element)
            names.append(str(atom_names[i]) if i < len(atom_names) else atom.element)
            resnames.append(str(atom_res[i]) if i < len(atom_res) else "UNK")
            resseq.append(int(atom_seq[i]) if i < len(atom_seq) else 1)
            chain_ids.append(cid)
    if not coords:
        raise Unsupported(
            f"export: design {design!r} has no realized region — nothing to write as PDB",
            next="realize_chain a region first (it binds a structure to the segment)",
        )
    return write_pdb(
        elements, np.asarray(coords, dtype=float), names, resnames, resseq, chain_ids
    )


# ── the view ────────────────────────────────────────────────────────────


def render_export(
    tree: SeTree, fmt: str | None, *, design: str, load_structure: LoadStructure
) -> str:
    """``view='export'``'s body for ``args={'format': fmt}``."""
    key = str(fmt or "").strip().lower()
    if key not in FORMATS:
        raise Unsupported(
            f"export: format {key!r} — one of {', '.join(FORMATS)}",
            next="get(kind='se', id=…, view='export', args={'format': 'scadnano'})",
        )
    if key == "scadnano":
        return json.dumps(to_scadnano(tree, design=design), indent=1) + "\n"
    if key == "cadnano":
        return json.dumps(to_cadnano(tree, design=design)) + "\n"
    if key == "oxdna":
        top, conf = to_oxdna(tree, design=design)
        return f"## {design}.top\n{top}## {design}.conf\n{conf}"
    return to_pdb(tree, design=design, load_structure=load_structure)
