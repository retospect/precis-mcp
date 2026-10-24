"""The shared scene-level spatial eye (structure-workspaces slice 2e,
docs/backlog/structure-workspaces.md §Structure fisheye).

One merged bond graph — a block's own ``scene.bonds`` plus the ``seam``
edges a ``kind='bond'`` connect adds — rendered the same way for the se
eye (:mod:`precis_se.eyeview`) and the structure eye
(:mod:`precis.handlers._structure_eye`). This module is core ``precis``
and deliberately knows nothing about se: nodes are ``(block, label)``
keys, handles arrive pre-rendered, and relax / trace annotations ride
plain string fields the callers fill in.

* ``merged_shell`` — graduated BFS over the merged graph (hop 0 = the
  focus set), returning the edge that reached each key so hop-1 lines can
  name the bond kind/order/length.
* ``render_shell`` — the spec's graduated listing: one line per bond at
  hop 1 (capped 8), ``(element, hybridization, relax)`` groups at hops
  2–3, counts only at hops 4–6, then ``beyond:`` for what the BFS left.
* ``relax_mix`` — the ``relax mix:`` line and the ``WARN mixed methods
  within 2 bonds`` marker (the eye never hides a method change in a
  fold).
* ``render_pi`` / ``atom_card`` — the per-atom π block and the verbatim
  atom card, shared verbatim between the two eyes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from . import elements
from .scene import Scene

#: Cap on hop-1 bond lines (a 12-coordinated metal overflows with
#: ``… +N more — view='neighborhood'``).
_HOP1_CAP = 8
#: Sample handles on a grouped hop-2/3 line.
_GROUP_SAMPLES = 3


@dataclass
class EyeAtom:
    """One node in the merged bond graph."""

    key: tuple[str, str]  # (block_name, label); block '' for a bare structure
    handle: str  # canonical eye handle, e.g. 'hcol-a2~p#aC12' or 'st7~aC12'
    element: str = ""
    hyb: str | None = None  # declared hybridization (None → render —)
    relax: str | None = None  # e.g. 'xtb interfaces' / 'gfnff unit u123'
    level: str | None = None  # 4a contract — absent until then
    frozen: str | None = None  # 4a contract — 'frozen (pass 2)' / 'free'
    maker_line: str | None = None  # short creator-frame text, e.g. 'junction#8'


@dataclass
class EyeEdge:
    """One bond (or seam) in the merged graph."""

    a: tuple[str, str]
    b: tuple[str, str]
    order: float = 1.0
    kind: str = "pairwise"  # 'seam' allowed for cross-block connects
    image: tuple = (0, 0, 0)
    length_A: float | None = None


def merged_shell(
    atoms: dict[tuple[str, str], EyeAtom],
    edges: list[EyeEdge],
    focus_keys: list[tuple[str, str]],
    *,
    max_hop: int = 6,
) -> dict[int, list[tuple[tuple[str, str], EyeEdge | None]]]:
    """BFS over the merged bond graph from ``focus_keys`` —
    ``{hop: [(key, the edge that reached it, None for the focus set)]}``.
    Hop order first, bond length second (the browser shells' ordering):
    each hop's keys are sorted by (length of the reaching bond, key)."""
    adj: dict[tuple[str, str], list[tuple[tuple[str, str], EyeEdge]]] = {
        k: [] for k in atoms
    }
    for e in edges:
        if e.a in adj and e.b in adj:
            adj[e.a].append((e.b, e))
            adj[e.b].append((e.a, e))
    out: dict[int, list[tuple[tuple[str, str], EyeEdge | None]]] = {
        0: [(k, None) for k in focus_keys if k in atoms]
    }
    depth = {k: 0 for k in focus_keys if k in atoms}
    queue = list(depth)
    while queue:
        cur = queue.pop(0)
        d = depth[cur]
        if d >= max_hop:
            continue
        for nxt, edge in adj[cur]:
            if nxt in depth:
                continue
            depth[nxt] = d + 1
            out.setdefault(d + 1, []).append((nxt, edge))
            queue.append(nxt)
    for d, items in out.items():
        items.sort(
            key=lambda t: (
                float("inf")
                if t[1] is None or t[1].length_A is None
                else t[1].length_A,
                t[0],
            )
        )
    return out


def _relax_method(relax: str | None) -> str | None:
    """The method token of a relax string (``'xtb interfaces'`` → ``xtb``)."""
    return relax.split()[0] if relax else None


def render_shell(
    atoms: dict[tuple[str, str], EyeAtom],
    edges: list[EyeEdge],
    focus_keys: list[tuple[str, str]],
    *,
    max_hop: int = 6,
) -> list[str]:
    """The bonded-shell listing (spec §Bonded shell): hop 1 one line per
    bond, hops 2–3 as ``(element, hyb, relax)`` groups with a relax-differing
    group on its own line, hops 4–6 as counts, ``beyond:`` for what the BFS
    left, closed by the ``relax mix:`` line and the mixed-method WARN."""
    focus_relax = atoms[focus_keys[0]].relax if len(focus_keys) == 1 else None
    shell = merged_shell(atoms, edges, focus_keys, max_hop=max_hop)
    lines: list[str] = []

    hop1 = shell.get(1) or []
    for key, edge in hop1[:_HOP1_CAP]:
        a = atoms[key]
        e = edge or EyeEdge(a=key, b=key)
        kind = f"{e.kind} {e.order:g}".strip()
        length = f" {e.length_A:.2f} Å" if e.length_A is not None else ""
        img = (
            f" [img {e.image[0]},{e.image[1]},{e.image[2]}]"
            if e.image != (0, 0, 0)
            else ""
        )
        relax = f" · {a.relax}" if a.relax and a.relax != focus_relax else ""
        lines.append(
            f"  {a.handle} {a.element} {a.hyb or '—'} · {kind}{length}{img}{relax}"
        )
    if len(hop1) > _HOP1_CAP:
        lines.append(f"  … +{len(hop1) - _HOP1_CAP} more — view='neighborhood'")

    for hop in (2, 3):
        items = shell.get(hop) or []
        if not items:
            continue
        same_relax = [k for k, _e in items if atoms[k].relax == focus_relax]
        diff_relax = [k for k, _e in items if atoms[k].relax != focus_relax]
        if same_relax:
            groups: dict[tuple[str, str | None], list[tuple[str, str]]] = {}
            for k in same_relax:
                a = atoms[k]
                groups.setdefault((a.element, a.hyb), []).append(k)
            counts = ", ".join(
                f"{len(ks)} {el} {hyb or '—'}"
                for (el, hyb), ks in sorted(groups.items())
            )
            samples = ", ".join(atoms[k].handle for k in same_relax[:_GROUP_SAMPLES])
            lines.append(f"hop {hop}: {len(same_relax)} atoms — {counts} · {samples}")
        by_relax: dict[str, list[tuple[str, str]]] = {}
        for k in diff_relax:
            by_relax.setdefault(atoms[k].relax or "—", []).append(k)
        for relax, ks in sorted(by_relax.items()):
            groups = {}
            for k in ks:
                a = atoms[k]
                groups.setdefault((a.element, a.hyb), []).append(k)
            counts = ", ".join(
                f"{len(v)} {el} {hyb or '—'}" for (el, hyb), v in sorted(groups.items())
            )
            samples = ", ".join(atoms[k].handle for k in ks[:_GROUP_SAMPLES])
            lines.append(f"hop {hop}: {len(ks)} atoms — {counts} · {relax} · {samples}")

    tail = [len(shell.get(h) or []) for h in range(4, max_hop + 1)]
    if any(tail):
        lines.append(f"hop 4–{max_hop}: {' / '.join(str(n) for n in tail)}")
    visited = {k for items in shell.values() for k, _e in items}
    remaining = [k for k in atoms if k not in visited]
    if remaining:
        in_block = [k for k in remaining if k[0] in {f[0] for f in focus_keys}]
        across = len(remaining) - len(in_block)
        lines.append(f"beyond: {len(in_block)} atoms in block, {across} across seams")
    lines.extend(relax_mix(atoms, shell, focus_keys))
    return lines


def relax_mix(
    atoms: dict[tuple[str, str], EyeAtom],
    shell: dict[int, list[tuple[tuple[str, str], EyeEdge | None]]],
    focus_keys: list[tuple[str, str]],
    *,
    strain_frame: str | None = None,
) -> list[str]:
    """The ``relax mix:`` line closing the shell (spec §Relaxation
    awareness): the focus's own relax, then per-hop relax groups that
    differ from it — and ``WARN mixed methods within 2 bonds`` when
    different methods appear at hop ≤ 2, where a relaxed-looking number
    misleads."""
    if len(focus_keys) == 1:
        focus_relax = atoms[focus_keys[0]].relax
        head = f"this atom: {focus_relax or '—'}"
    else:
        focus_relax = None
        head = "this set:"
    parts = [head]
    methods = {_relax_method(focus_relax)}
    for hop in sorted(h for h in shell if h >= 1):
        groups: dict[str, int] = {}
        for k, _e in shell[hop]:
            r = atoms[k].relax
            if r != focus_relax:
                groups[r or "—"] = groups.get(r or "—", 0) + 1
            if hop <= 2:
                methods.add(_relax_method(r))
        label = f"hop {hop}" if hop <= 3 else "hop 4+"
        for r, n in sorted(groups.items()):
            parts.append(f"{label}: {n} atoms {r}")
    out = ["relax mix: " + " · ".join(parts)]
    if len({m for m in methods if m is not None}) > 1:
        out.append(
            "WARN mixed methods within 2 bonds — bond lengths across the "
            "seam are not comparable"
            + (f"; 'assembly.strain' on frame {strain_frame}" if strain_frame else "")
        )
    return out


def render_pi(
    atom_ord: int,
    pi_data: dict[str, Any] | None,
    handle_prefix: str,
) -> str | None:
    """The per-atom π line (spec §Shared π electrons): ring sizes, the
    system selector ``<handle_prefix>pi:<block>#<id>``, its atom and
    aromatic-ring counts, and the RDKit cross-check verdict when it
    differed. ``handle_prefix`` carries the block for the se eye
    (``<slug>~pi:p``); for a bare structure it is ``<id>~pi`` with the
    selector taking the plain ``pi:<id>`` form — pass the whole prefix.
    ``None`` when the atom sits in no ring or system."""
    if not pi_data or "skipped" in pi_data:
        return None
    rec = (pi_data.get("atom") or {}).get(str(atom_ord))
    if rec is None:
        return None
    rings = pi_data.get("rings") or []
    sizes = ",".join(
        str(len(rings[i]["atoms"])) for i in (rec.get("rings") or []) if i < len(rings)
    )
    parts = [f"rings {sizes or '—'}"]
    sys_id = rec.get("system")
    if sys_id is not None:
        sys = next(
            (s for s in pi_data.get("systems") or [] if s.get("id") == sys_id),
            None,
        )
        parts.append(f"sys {handle_prefix}{sys_id}")
        if sys is not None:
            parts.append(
                f"{len(sys.get('atoms') or [])} atoms · "
                f"{len(sys.get('aromatic_rings') or [])} aromatic rings"
            )
    line = "pi: " + " · ".join(parts)
    check = pi_data.get("check") or {}
    if check.get("rdkit") == "differs":
        line += " · pi.perception_differs"
    return line


def pi_system_lines(
    pi_data: dict[str, Any] | None,
    atom_ords: set[int],
    handle_prefix: str,
) -> list[str]:
    """The π systems a focus SET touches — ``<sys handle> · N atoms · M
    aromatic rings (K in focus)`` per system holding any focus ordinal;
    for a single-atom focus :func:`render_pi` is the block instead."""
    if not pi_data or "skipped" in pi_data:
        return []
    out = []
    for sys in pi_data.get("systems") or []:
        members = set(sys.get("atoms") or [])
        inside = members & atom_ords
        if not inside:
            continue
        out.append(
            f"  pi: {handle_prefix}{sys.get('id')} · {len(members)} atoms · "
            f"{len(sys.get('aromatic_rings') or [])} aromatic rings "
            f"({len(inside)} in focus)"
        )
    if (pi_data.get("check") or {}).get("rdkit") == "differs":
        out.append("  pi.perception_differs (RDKit cross-check)")
    return out


def infer_hybridization(atom: Any, bonds: list[Any]) -> str:
    """Hybridization inferred from the bond orders on this atom — the
    declared ``atom.hybridization``'s check, never its replacement:
    ``sp`` on any order ≥ 2.5 or two order ≥ 2 bonds, ``sp2`` on any
    order ≥ 1.4 (aromatic/double), else ``sp3``."""
    orders = [float(b.order) for b in bonds]
    if any(o >= 2.5 for o in orders) or sum(1 for o in orders if o >= 2) >= 2:
        return "sp"
    if any(o >= 1.4 for o in orders):
        return "sp2"
    return "sp3"


def atom_card(
    scene: Scene,
    label: str,
    *,
    handle: str,
    world_A: Any = None,
    pi_data: dict[str, Any] | None = None,
    pi_handle_prefix: str = "pi:",
    relax_lines: list[str] | None = None,
    made_by: str | None = None,
) -> list[str]:
    """The verbatim atom card (spec rungs table): element, declared and
    inferred hybridization (flagged when they differ), charge, fixed mask,
    position (block-frame Å + world when given), coordination vs valence,
    the bonds, the π block, the relax block, the made-by line."""
    atom = scene.atoms[label]
    labels = list(scene.atoms)
    bonds = scene.bonds_of(label)
    inferred = infer_hybridization(atom, bonds)
    declared = atom.hybridization or "—"
    hyb = f"{declared} (inferred {inferred}"
    hyb += " · differs)" if declared != "—" and declared != inferred else ")"
    lines = [f"atom {handle}: {atom.element} · {hyb}"]
    lines.append(f"charge {atom.charge} · fixed {atom.fixed:03b}")
    cart = scene.cell.frac_to_cart(atom.frac)
    pos = f"position: ({cart[0]:.3f}, {cart[1]:.3f}, {cart[2]:.3f}) Å"
    if world_A is not None:
        w = np.asarray(world_A, dtype=float)
        pos += f" · world ({w[0]:.3f}, {w[1]:.3f}, {w[2]:.3f}) Å"
    lines.append(pos)
    val, _known = elements.effective_valence(atom.element, int(atom.charge))
    lines.append(
        f"coordination: {len(bonds)} / valence {val if val is not None else '—'}"
    )
    if bonds:
        lines.append("bonds:")
        for b in sorted(bonds, key=lambda b: b.j if b.i == label else b.i):
            other = b.j if b.i == label else b.i
            length, _img = scene.cell.mic(
                atom.frac, scene.atoms[other].frac + np.array(b.image)
            )
            lines.append(f"  {other} {b.kind} {b.order:g} {length:.2f} Å")
    pi_line = render_pi(labels.index(label), pi_data, pi_handle_prefix)
    if pi_line:
        lines.append(pi_line)
    lines.extend(relax_lines or [])
    if made_by:
        lines.append(f"made-by: {made_by}")
    return lines
