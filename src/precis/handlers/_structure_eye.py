"""The ``structure`` kind's spatial eye — ``st<id>~<focus>``
(structure-workspaces slice 2e, docs/backlog/structure-workspaces.md
§Structure fisheye).

A bare structure ref is one pseudo-block (block name ``''``) — the same
:mod:`precis.structure.spatial_eye` render the se eye uses, minus seams
and trace (a structure ref carries no construction hierarchy: ``frame:``
refuses naming the se form, and there is no ``Up:`` ladder). Focus
forms: ``<label>`` / ``#<label>`` (one atom), ``find:<pred>`` (the
``view='find'`` predicates — ``element=<El>``, ``undercoordinated``), and
``pi:<id>[/rings|/aromatic]`` (one perceived π system, read off the
``structure_pi`` row for the scene's current ``pi_key``).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import psycopg

from precis.errors import BadInput, Unsupported
from precis.structure import probe
from precis.structure import spatial_eye as se_eye
from precis.utils import handle_registry
from precis.workers.working_set import Extent

_EYE_NEXT = (
    "get(kind='structure', id='st<id>~<focus>', extent='fisheye') — "
    "focus: <label> | #<label> | find:<pred> | pi:<id>"
)
_RECALL_MSG = "an atom has no embedding — +recall needs a graph node"
_RECALL_NEXT = "search(kind='structure', q='<what it is>')"


def _pi_data(store: Any, ref_id: int, scene: Any) -> dict[str, Any] | None:
    """This scene's ``structure_pi`` row, keyed by its ``pi_key`` —
    ``None`` when the write path predates it or the row is missing."""
    from precis.structure.cache import pi_key

    try:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT data FROM structure_pi WHERE ref_id = %s AND pi_key = %s",
                (ref_id, pi_key(scene)),
            ).fetchone()
    except psycopg.errors.UndefinedTable:
        # Pre-0238 DB — no perception written yet.
        return None
    return dict(row[0]) if row else None


def _focus_keys(
    scene: Any, focus: str, base_handle: str
) -> tuple[str, list[tuple[str, str]], set[int], str]:
    """``(focus kind, keys, ordinals, head text)`` for a structure-eye
    focus string."""
    labels = list(scene.atoms)
    if focus.startswith("frame:"):
        raise BadInput(
            "a structure ref has no trace — frame: needs the se eye on "
            "the block that binds it",
            next="get(kind='se', id='<design>~frame:<key>', extent='fisheye')",
        )
    if focus.startswith("pi:"):
        return "pi", [], set(), focus  # resolved by the caller (needs data)
    if focus.startswith("find:"):
        preds = focus[len("find:") :].split(",")
        kw: dict[str, Any] = {}
        for p in preds:
            p = p.strip()
            if p.startswith("element="):
                kw["element"] = p[len("element=") :]
            elif p == "undercoordinated":
                kw["undercoordinated"] = True
            elif p:
                raise BadInput(
                    f"find: predicate {p!r} is not one of element=<El>, "
                    "undercoordinated",
                    next=_EYE_NEXT,
                )
        hits = probe.find(scene, **kw)
        if not hits:
            raise BadInput(f"find:{','.join(preds)} matches no atoms", next=_EYE_NEXT)
        return (
            "region",
            [("", la) for la in hits],
            {labels.index(la) for la in hits},
            f"{focus} → {len(hits)} atoms",
        )
    label = focus.lstrip("#")
    if label.isdigit():
        i = int(label)
        if i >= len(labels):
            raise BadInput(f"{len(labels)} atoms — no ordinal {i}", next=_EYE_NEXT)
        label = labels[i]
    if label not in scene.atoms:
        raise BadInput(
            f"no atom {label!r} on this structure (labels: {', '.join(labels[:8])}…)",
            next=_EYE_NEXT,
        )
    return "atom", [("", label)], {labels.index(label)}, focus


def _graph(
    scene: Any, base_handle: str
) -> tuple[dict[tuple[str, str], se_eye.EyeAtom], list[se_eye.EyeEdge]]:
    atoms: dict[tuple[str, str], se_eye.EyeAtom] = {}
    edges: list[se_eye.EyeEdge] = []
    for la, atom in scene.atoms.items():
        key = ("", la)
        atoms[key] = se_eye.EyeAtom(
            key=key,
            handle=f"{base_handle}~{la}",
            element=atom.element,
            hyb=atom.hybridization,
        )
    for b in scene.bonds:
        if b.i not in scene.atoms or b.j not in scene.atoms:
            continue
        d, _img = scene.cell.mic(
            scene.atoms[b.i].frac, scene.atoms[b.j].frac + np.array(b.image)
        )
        edges.append(
            se_eye.EyeEdge(
                a=("", b.i),
                b=("", b.j),
                order=b.order,
                kind=b.kind,
                image=tuple(b.image),
                length_A=d,
            )
        )
    return atoms, edges


def _relax_head(ref: Any) -> str | None:
    """``meta['last_relax']`` as one line — a structure's only relax
    provenance (no trace: no per-atom level/frozen, which the line says)."""
    lr = (getattr(ref, "meta", None) or {}).get("last_relax")
    if not isinstance(lr, dict) or not lr.get("rung"):
        return None
    state = "converged" if lr.get("converged") else "non-converged"
    line = f"relax: {lr['rung']} {state}"
    if lr.get("max_force") is not None:
        line += f" · max_force {lr['max_force']:g} eV/Å"
    return line + " · level — · frozen — (per-atom levels land with 4a)"


def render(
    store: Any,
    ref: Any,
    scene: Any,
    focus_text: str,
    ext: Extent,
    *,
    q: str | None = None,
    ring: Any = None,
) -> str:
    """The structure spatial eye body — :meth:`StructureHandler.eye`'s
    ``~<focus>`` half."""
    if ring is not None:
        if not isinstance(ring, dict):
            raise BadInput(f"eye: ring= takes a dict, got {ring!r}", next=_EYE_NEXT)
        if ring.get("space"):
            raise BadInput(
                "ring={'space': …} narrows a link eye's SPACE: tag axis — "
                "atoms carry no tags",
                next=_EYE_NEXT,
            )
    base_handle = handle_registry.try_format("structure", int(ref.id)) or str(
        ref.slug or ref.id
    )
    focus = focus_text.strip()
    if not focus:
        raise BadInput("the structure eye needs a focus after '~'", next=_EYE_NEXT)
    kind, keys, ords, head_text = _focus_keys(scene, focus, base_handle)
    labels = list(scene.atoms)
    pi_data = _pi_data(store, int(ref.id), scene)
    pi_prefix = f"{base_handle}~pi:"

    if kind == "pi":
        # pi:<id>[/rings|/aromatic] — one system's atom set.
        sys_s, slash, mod = focus[len("pi:") :].partition("/")
        if mod not in ("", "rings", "aromatic") or not sys_s.strip().isdigit():
            raise BadInput(
                f"pi selector is 'pi:<id>' (optionally '/rings' or "
                f"'/aromatic'), got {focus!r}",
                next=_EYE_NEXT,
            )
        sys_id = int(sys_s)
        systems = (pi_data or {}).get("systems") or []
        sys = next((s for s in systems if s.get("id") == sys_id), None)
        if pi_data is None or "skipped" in pi_data:
            raise BadInput(
                "no π perception stored for this structure"
                + (f" ({pi_data['skipped']})" if pi_data else ""),
                next=_EYE_NEXT,
            )
        if sys is None:
            raise BadInput(f"no π system {sys_id} (has {len(systems)})", next=_EYE_NEXT)
        members = {int(i) for i in sys.get("atoms") or []}
        if mod:
            rings = pi_data.get("rings") or []
            ring_ids = (
                set(sys.get("aromatic_rings") or [])
                if mod == "aromatic"
                else {
                    i
                    for i, r in enumerate(rings)
                    if set(r.get("atoms") or []) <= members
                }
            )
            members = {
                a
                for i in ring_ids
                for a in (rings[i].get("atoms") or [])
                if i < len(rings)
            }
        keys = [("", labels[i]) for i in sorted(members)]
        ords = members
        head_text = f"{focus} → {len(keys)} atoms"
        kind = "region"

    atoms, edges = _graph(scene, base_handle)
    head = [head_text]
    if kind == "atom":
        key = keys[0]
        a = atoms[key]
        n_bonds = sum(1 for e in edges if key in (e.a, e.b))
        rec = (pi_data or {}).get("atom", {}).get(str(labels.index(key[1])))
        rings_csv = "—"
        if rec is not None:
            rings = (pi_data or {}).get("rings") or []
            rings_csv = (
                ",".join(
                    str(len(rings[i]["atoms"]))
                    for i in rec.get("rings") or []
                    if i < len(rings)
                )
                or "—"
            )
        head = [
            f"{key[1]} {a.element} {a.hyb or '—'} ·{n_bonds} bonds· rings {rings_csv}"
        ]
        if ext == Extent.TOC:
            return "\n".join(head)
        partners = []
        for e in sorted(
            (e for e in edges if key in (e.a, e.b)),
            key=lambda e: e.length_A if e.length_A is not None else float("inf"),
        ):
            other = e.b if e.a == key else e.a
            ln = f" {e.length_A:.2f}Å" if e.length_A is not None else ""
            partners.append(f"{atoms[other].handle} {e.order:g}{ln}")
        head.append("bonds: " + " · ".join(partners))
        pi_line = se_eye.render_pi(labels.index(key[1]), pi_data, pi_prefix)
        if pi_line:
            head.append(pi_line)
        relax = _relax_head(ref)
        if relax:
            head.append(relax)
    elif ext == Extent.TOC:
        return "\n".join(head)

    if kind == "region" and ext == Extent.TOC:
        return "\n".join(head)
    if ext == Extent.SUMMARY:
        return "\n".join(head)

    if kind == "atom":
        relax_head = _relax_head(ref)
        card = se_eye.atom_card(
            scene,
            keys[0][1],
            handle=atoms[keys[0]].handle,
            pi_data=pi_data,
            pi_handle_prefix=pi_prefix,
            relax_lines=[relax_head] if relax_head else None,
        )
        head.extend(card[1:])
    else:
        head.append(f"resolved: {focus} → {len(keys)} atoms")
    if ext == Extent.FULL:
        return "\n".join(head)

    shell = se_eye.render_shell(atoms, edges, keys)
    if shell:
        head.extend(["shell:", *shell])
    head.extend(se_eye.pi_system_lines(pi_data, ords, pi_prefix))
    if ext == Extent.FIDELITY:
        return "\n".join(head)

    # ── +1hop: the ref ring — measures covering the focus, then links ──
    ring_lines = []
    for m in scene.measures:
        if set(m.operands) & {k[1] for k in keys}:
            ring_lines.append(f"  measure {m.kind} {m.name or ''}".rstrip())
    if ring_lines:
        head.append("ring:")
        head.extend(ring_lines[:8])
    if ext == Extent.HOP1:
        return "\n".join(head)

    # ── +2hop: link neighbourhood counts ──
    groups: dict[tuple[str, str], int] = {}
    try:
        nbh = store.neighbourhood("structure", int(ref.id))
        nodes = {
            (
                handle_registry.try_format(n["kind"], n["id"])
                or f"{n['kind']}:{n['id']}"
            ): n["kind"]
            for n in nbh.get("nodes", [])
        }
        for e in nbh.get("edges", []):
            other = e["dst"] if e.get("dir") == "out" else e["src"]
            okind = nodes.get(other)
            if okind is None:
                continue
            key = (okind, str(e.get("rel") or "related-to"))
            groups[key] = groups.get(key, 0) + 1
    except Exception:
        groups = {}
    head.extend(f"hop2: {n} {k} via {r}" for (k, r), n in sorted(groups.items()))
    return "\n".join(head)


def recall_refusal() -> Unsupported:
    """The ``+recall`` refusal — an atom has no embedding."""
    return Unsupported(_RECALL_MSG, next=_RECALL_NEXT)
