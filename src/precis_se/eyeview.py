"""The se spatial eye — ``<slug>~<focus>`` on the fisheye ladder
(structure-workspaces slice 2e, docs/backlog/structure-workspaces.md
§Structure fisheye).

``SeHandler.eye`` splits ``id`` on the first ``~`` and lands here: the
focus is one atom (``<block>#<label>``, ``<block>/<AtomPath>``,
``#uid:<path>``, ``<block>#<ord>``), a trace frame (``frame:<key>``,
with ``!`` / ``/rest`` / ``/touched`` as in ``view='trace'``), or any
region selector (``pi:<block>#<id>``, ``atoms:``, ``sites:``, ``mol:``,
… — the ``precis-se-regions-help`` grammar resolved by
:mod:`precis_se.datums`).

The merged bond graph the shell walks is every structure-bound block's
scene plus the ``seam`` edges of ``kind='bond'`` connects (port →
``bound_atom`` on each side); the pure BFS/graduated renderer lives in
:mod:`precis.structure.spatial_eye`, this module only maps design facts
— trace makers, relax provenance, pockets, findings — onto it.

**Relax scoping contract** (read here; slice 4a owns the real writer):
a ``kind='service'`` frame's ``touched.atoms`` ordinal ranges index the
scene of the block its ``args.block`` names (or EVERY bound block when
``args.block`` is absent). Per-atom ``level``/``frozen`` come from
``args['levels']``/``args['fixed']`` ordinal-range entries — the 4a
contract; absent today, so those slots render ``—`` honestly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from precis.errors import BadInput, Unsupported
from precis.structure import spatial_eye as se_eye
from precis.utils import handle_registry
from precis.utils.eye_render import _ordered_body, _rings
from precis.workers.working_set import Extent
from precis_se import (
    datums,
    persist,  # noqa: F401  (referenced by callers' trees)
)
from precis_se import traceview as tv
from precis_se.ops import effective_ports

#: ``next=`` every focus-parse refusal carries — the eye's own forms.
_EYE_NEXT = (
    "get(kind='se', id='<slug>~<focus>', extent='fisheye') — focus: "
    "<block>#<label> | <block>/<path> | frame:<key> | pi:<block>#<id> | "
    "any precis-se-regions-help selector"
)
_RECALL_MSG = "an atom has no embedding — +recall needs a graph node"
_RECALL_NEXT = "search(kind='structure', q='<what it is>')"


def _ranges_cover(ranges: Any, ordinal: int) -> bool:
    for r in ranges or []:
        if (
            isinstance(r, (list, tuple))
            and len(r) >= 2
            and int(r[0]) <= ordinal <= int(r[1])
        ):
            return True
    return False


def _relax_of(
    name: str, bt: Any, ordinal: int
) -> tuple[str | None, str | None, str | None]:
    """``(relax text, level, frozen)`` for one atom from the trace's
    service frames — the scoping contract in the module docstring.
    ``level``/``frozen`` are the 4a contract: ``args['levels']`` /
    ``args['fixed']`` ordinal ranges; absent → ``None`` (rendered ``—``)."""
    doc = bt.doc
    if not doc:
        return None, None, None
    best = None
    for f in doc.get("frames") or []:
        if f.get("kind") != "service":
            continue
        args = f.get("args") or {}
        if args.get("block") not in (None, name):
            continue
        if _ranges_cover((f.get("touched") or {}).get("atoms"), ordinal):
            best = f
    if best is None:
        return None, None, None
    args = best.get("args") or {}
    text = " ".join(str(args[k]) for k in ("method", "scope") if args.get(k))
    if args.get("unit"):
        text += f" {args['unit']}"
    level = None
    levels = args.get("levels")
    if isinstance(levels, dict):
        level = next(
            (lev for lev, ranges in levels.items() if _ranges_cover(ranges, ordinal)),
            None,
        )
    frozen = None
    if _ranges_cover(args.get("fixed"), ordinal):
        frozen = f"frozen (pass {args['pass']})" if args.get("pass") else "frozen"
    return text or None, level, frozen


def _seam_key(
    tree: Any, block: str, port: str, bts: dict[str, Any]
) -> tuple[str, str] | None:
    """``(block, bound_atom label)`` for one end of a ``kind='bond'``
    connect — the port's ``bound_atom`` on the block's bound scene."""
    node = tree.blocks.get(block)
    if node is None:
        return None
    spec = effective_ports(tree, node).get(port)
    label = getattr(spec, "bound_atom", None)
    if not label or block not in bts or label not in bts[block].scene.atoms:
        return None
    return (block, str(label))


def _seam_length(tree: Any, ka: tuple[str, str], kb: tuple[str, str]) -> float | None:
    """Seam length in Å — world-frame positions of the two bound atoms
    (:func:`precis_se.datums.posed_structure_atoms`), ``None`` when either
    side fails to resolve."""
    try:
        pa = datums.posed_structure_atoms(tree, ka[0])
        pb = datums.posed_structure_atoms(tree, kb[0])
        if pa is None or pb is None:
            return None
        ia, ib = pa[0].index(ka[1]), pb[0].index(kb[1])
        return float(np.linalg.norm(np.asarray(pa[2][ia]) - np.asarray(pb[2][ib])))
    except Exception:
        return None


def _merged(store: Any, tree: Any, slug: str) -> tuple[Any, Any, Any, Any]:
    """``(atoms, edges, bts, scenes)`` — the merged bond graph over every
    structure-bound block (traced or not) plus seam edges."""
    from precis_se import handler as _h

    ctx = _h._trace_context(tree)
    bts: dict[str, Any] = dict(ctx)
    resolver = getattr(tree, "structures", None)
    if resolver is not None:
        for name, node in tree.blocks.items():
            owner = tree.blocks.get(node.template) if node.template else node
            owner = owner or node
            if owner.bound_kind != "structure" or not owner.bound or name in bts:
                continue
            bound = resolver(owner.bound)
            if bound is None:
                continue
            gen = (bound.meta or {}).get("generated") or {}
            traces = getattr(tree, "traces", None)
            doc = traces(gen.get("trace")) if traces is not None else None
            bts[name] = _h._BlockTrace(
                bound=owner.bound,
                scene=bound.scene,
                meta=dict(bound.meta or {}),
                doc=doc,
            )
    atoms: dict[tuple[str, str], se_eye.EyeAtom] = {}
    edges: list[se_eye.EyeEdge] = []
    for name, bt in bts.items():
        scene = bt.scene
        ord_of = {la: i for i, la in enumerate(scene.atoms)}
        for la, atom in scene.atoms.items():
            relax, level, frozen = _relax_of(name, bt, ord_of[la])
            key = (name, la)
            atoms[key] = se_eye.EyeAtom(
                key=key,
                handle=f"{slug}~{name}#{la}",
                element=atom.element,
                hyb=atom.hybridization,
                relax=relax,
                level=level,
                frozen=frozen,
            )
        for b in scene.bonds:
            if b.i not in scene.atoms or b.j not in scene.atoms:
                continue
            d, _img = scene.cell.mic(
                scene.atoms[b.i].frac,
                scene.atoms[b.j].frac + np.array(b.image),
            )
            edges.append(
                se_eye.EyeEdge(
                    a=(name, b.i),
                    b=(name, b.j),
                    order=b.order,
                    kind=b.kind,
                    image=tuple(b.image),
                    length_A=d,
                )
            )
    for c in tree.connects:
        if c.kind != "bond":
            continue
        ka = _seam_key(tree, c.a_block, c.a_port, bts)
        kb = _seam_key(tree, c.b_block, c.b_port, bts)
        if ka is None or kb is None:
            continue
        edges.append(
            se_eye.EyeEdge(
                a=ka,
                b=kb,
                order=1.0,
                kind="seam",
                length_A=_seam_length(tree, ka, kb),
            )
        )
    return atoms, edges, bts, {n: bt.scene for n, bt in bts.items()}


@dataclass
class _Focus:
    kind: str  # 'atom' | 'frame' | 'region'
    keys: list[tuple[str, str]]
    block: str
    ordinals: set[int]
    frame: dict[str, Any] | None = None
    tr: Any = None
    bt: Any = None
    selector_text: str = ""


def _trace_of(bt: Any) -> Any:
    return tv.Trace(bt.doc) if bt is not None and bt.doc else None


def _atom_focus(tree: Any, focus: str, atoms: Any, bts: dict[str, Any]) -> _Focus:
    from precis_se import handler as _h

    blk, ref = _h._split_atom_ref(tree, focus)
    hit = _h._atom_lookup(bts, tree, blk, ref)
    if isinstance(hit, str):
        raise BadInput(hit, next=_EYE_NEXT)
    name, ordinal, _atom = hit
    label = list(bts[name].scene.atoms)[ordinal]
    bt = bts.get(name)
    return _Focus(
        kind="atom",
        keys=[(name, label)],
        block=name,
        ordinals={ordinal},
        tr=_trace_of(bt),
        bt=bt,
        selector_text=focus,
    )


def _frame_focus(
    tree: Any, slug: str, focus: str, atoms: Any, bts: dict[str, Any]
) -> _Focus:
    """``frame:<key>[!|/rest|/touched]`` — the frame's resolved atom set
    on the block whose trace holds it."""
    try:
        sel = datums.parse_selector(focus)
    except Exception as exc:
        raise BadInput(str(exc), next=_EYE_NEXT) from exc
    for name, bt in bts.items():
        if bt.doc is None:
            continue
        tr = tv.Trace(bt.doc)
        keys: list[str] = []
        if sel.kind == "trace_frame" and sel.name in tr.by_key:
            keys = [sel.name]
        elif sel.kind == "trace_qual":
            keys = [f["key"] for f in tr.frames if f.get("qual") == sel.name]
        if not keys:
            continue
        node = tree.blocks.get(name)
        rdat = datums.resolve(tree, node, sel)
        if rdat.error:
            raise BadInput(rdat.error, next=_EYE_NEXT)
        labels = list(bt.scene.atoms)
        fkeys = [(name, labels[i]) for i in rdat.atoms if i < len(labels)]
        if sel.kind == "trace_frame":
            fr = tr.by_key.get(sel.name or "")
        else:  # trace_qual — the LCA of the qualifying frames' subtree
            ids = [f["id"] for f in tr.frames if f.get("key") in set(keys)]
            fr = tr.lca(ids) if ids else None
        return _Focus(
            kind="frame",
            keys=fkeys,
            block=name,
            ordinals=set(int(i) for i in rdat.atoms),
            frame=fr,
            tr=tr,
            bt=bt,
            selector_text=focus,
        )
    raise BadInput(
        f"frame {focus[len('frame:') :]!r} is not in any of this design's traces",
        next=_EYE_NEXT,
    )


def _region_focus(
    tree: Any, slug: str, focus: str, sel: Any, atoms: Any, bts: dict[str, Any]
) -> _Focus:
    """Any other selector — ``pi:``, ``atoms:``, ``sites:``, ``mol:``, …
    — resolved by :func:`precis_se.datums.resolve` to the atom set."""
    candidates = []
    if sel.instance and sel.instance in bts:
        candidates.append(tree.blocks[sel.instance])
    candidates.extend(
        tree.blocks[n] for n in bts if tree.blocks.get(n) not in candidates
    )
    if not candidates:
        raise BadInput(f"no structure-bound block resolves {focus!r}", next=_EYE_NEXT)
    last_error = f"selector {focus!r} did not resolve"
    for node in candidates:
        rdat = datums.resolve(tree, node, sel)
        if rdat.error:
            last_error = rdat.error
            continue
        block = sel.instance if sel.kind in ("pi", "atoms", "sites") else node.name
        bt = bts.get(block or node.name) or bts.get(node.name)
        if bt is None:
            continue
        labels = list(bt.scene.atoms)
        ords = {int(i) for i in rdat.atoms if 0 <= int(i) < len(labels)}
        if not ords:
            last_error = f"{focus!r} resolved to no atoms on {bt and node.name!r}"
            continue
        return _Focus(
            kind="region",
            keys=[(block or node.name, labels[i]) for i in sorted(ords)],
            block=block or node.name,
            ordinals=ords,
            tr=_trace_of(bt),
            bt=bt,
            selector_text=focus,
        )
    raise BadInput(last_error, next=_EYE_NEXT)


def _parse_focus(
    store: Any, tree: Any, slug: str, focus_text: str, atoms: Any, bts: Any
) -> _Focus:
    focus = focus_text.strip()
    if not focus:
        raise BadInput("the se eye needs a focus after '~'", next=_EYE_NEXT)
    if focus.startswith("frame:"):
        return _frame_focus(tree, slug, focus, atoms, bts)
    try:
        sel = datums.parse_selector(focus)
    except Exception:
        sel = None
    if sel is not None and sel.kind in ("trace_frame", "trace_qual"):
        return _frame_focus(tree, slug, focus, atoms, bts)
    if sel is not None and ":" in focus:
        return _region_focus(tree, slug, focus, sel, atoms, bts)
    return _atom_focus(tree, focus, atoms, bts)


def _up_ladder(
    tr: Any,
    fr: dict[str, Any] | None,
    *,
    block: str,
    slug: str,
    bt: Any,
) -> str | None:
    """The ``Up:`` hierarchy ladder — leaf→root joined `` ← `` (spec
    §Up and down the hierarchy): the creator frame with its ``frame:``
    handle, parents as frame lines (``kind='user'`` as ``<qual>
    (<file>:<line> · ws-<ws>::<qual>)``), the run, then block and design."""
    if tr is None or fr is None:
        return None
    rung: list[str] = []
    for i, f in enumerate(reversed(tr.path(fr))):
        if f.get("kind") == "root":
            continue
        if i == 0:
            rung.append(f"{tv.line(f, tr.ws)} (frame:{f['key']})")
        elif f.get("kind") == "user":
            ws = f" · ws-{tr.ws}::{f.get('qual')}" if tr.ws else ""
            rung.append(f"{f.get('qual')} ({f.get('file')}:{f.get('line')}{ws})")
        else:
            rung.append(tv.line(f, tr.ws))
    gen = (bt.meta.get("generated") or {}) if bt is not None else {}
    run_job = (gen.get("trace") or {}).get("run_job")
    rung.append(f"run {run_job}" if run_job is not None else "run —")
    ladder = " ← ".join(rung)
    return f"Up: {ladder} · block {block} · design {slug}" if ladder else None


def _down_lines(tree: Any, slug: str, tr: Any, fr: dict[str, Any]) -> list[str]:
    kids = tr.children.get(fr["key"], [])
    lines = ["Down:"]
    for c in kids[:8]:
        made = tv._counts_text(c.get("made")) or "—"
        lines.append(f"  {slug}~frame:{c['key']} {tv.line(c, tr.ws)} · made {made}")
    if len(kids) > 8:
        lines.append(f"  … +{len(kids) - 8} more")
    return lines


def _maker_line(foc: _Focus) -> str | None:
    if foc.tr is None or foc.bt is None or not foc.ordinals:
        return None
    labels = list(foc.bt.scene.atoms)
    maker = foc.bt.scene.atoms[labels[next(iter(foc.ordinals))]].maker
    fr = foc.tr.maker_frame(maker)
    if fr is None:
        return None
    return f"{str(fr.get('qual')).split('.')[-1]}#{fr['id']}"


def _lca_frame(foc: _Focus) -> dict[str, Any] | None:
    """The maker frame (atom focus), the LCA of the set's makers
    (region focus), or the frame itself."""
    if foc.tr is None or foc.bt is None:
        return None
    if foc.frame is not None:
        return foc.frame
    labels = list(foc.bt.scene.atoms)
    makers = [
        int(foc.bt.scene.atoms[labels[i]].maker)
        for i in sorted(foc.ordinals)
        if foc.bt.scene.atoms[labels[i]].maker is not None
    ]
    if not makers:
        return None
    if len(makers) == 1:
        return foc.tr.maker_frame(makers[0])
    return foc.tr.lca(makers)


def _relax_lines(foc: _Focus, atoms: Any) -> list[str]:
    """The relax block (spec §Relaxation awareness): the atom's relax
    row, its level/frozen slots (``—`` until 4a), the block's
    ``meta.last_relax`` context."""
    out = []
    if foc.kind == "atom":
        key = foc.keys[0]
        a = atoms[key]
        level = a.level or "—"
        frozen = a.frozen or "—"
        pending = "" if (a.level or a.frozen) else " (4a)"
        out.append(
            f"relax: {a.relax or '—'} · level {level} · frozen {frozen}{pending}"
        )
    else:
        hist: dict[str, int] = {}
        for k in foc.keys:
            r = atoms[k].relax or "—"
            hist[r] = hist.get(r, 0) + 1
        out.append(
            "relax: " + " · ".join(f"{n} atoms {r}" for r, n in sorted(hist.items()))
        )
    lr = ((foc.bt.meta if foc.bt else {}) or {}).get("last_relax")
    if isinstance(lr, dict) and lr.get("rung"):
        state = "converged" if lr.get("converged") else "non-converged"
        out.append(
            f"  context: {lr['rung']} {state}"
            + (
                f", max_force {lr['max_force']:g} eV/Å"
                if lr.get("max_force") is not None
                else ""
            )
        )
    return out


def _block_ring(
    store: Any,
    tree: Any,
    slug: str,
    foc: _Focus,
    sections: set[str] | None,
) -> dict[str, list[str]]:
    """The containing block's ring (``fisheye+1hop``): relax runs and
    service frames, pockets holding the focus, findings naming it,
    measures covering it, and the maker's workspace/run/python id lines.
    Computed lazily — only this rung pays the validate cost."""
    out: dict[str, list[str]] = {}
    name = foc.block
    bt = foc.bt
    node = tree.blocks.get(name)
    ords = foc.ordinals

    if sections is None or "relax" in sections:
        rows = []
        lr = ((bt.meta if bt else {}) or {}).get("last_relax")
        if isinstance(lr, dict) and lr.get("rung"):
            rows.append(
                f"last_relax {lr['rung']} "
                f"{'converged' if lr.get('converged') else 'non-converged'}"
            )
        n_service = (
            sum(1 for f in (bt.doc.get("frames") or []) if f.get("kind") == "service")
            if bt and bt.doc
            else 0
        )
        if n_service:
            rows.append(f"{n_service} service frame(s)")
        if rows:
            out["relax"] = rows

    if (
        node is not None
        and node.pockets
        and (sections is None or "pockets" in sections)
    ):
        rows = []
        for pname, pocket in node.pockets.items():
            covered: set[int] = set()
            for region in pocket.regions:
                try:
                    rdat = datums.resolve(tree, node, region)
                except Exception:
                    continue
                if rdat.error:
                    continue
                covered.update(int(i) for i in rdat.atoms)
            if ords & covered:
                rows.append(f"pocket {pname} ({len(covered)} atoms)")
        if rows:
            out["pockets"] = rows

    if sections is None or "findings" in sections:
        rows = []
        labels = list(bt.scene.atoms) if bt else []
        names = {f"{name}/{labels[i]}" for i in ords if i < len(labels)}
        creator = ""
        fr = _lca_frame(foc)
        if foc.tr is not None and fr is not None:
            creator = tv.creator_text(foc.tr, fr)
        try:
            from precis_se import validate as se_validate

            for f in se_validate.validate(tree):
                if names & {s for s in names if s in f.subject} or (
                    creator and creator in f.subject
                ):
                    rows.append(f"{f.severity} {f.rule} {f.subject} — {f.detail}")
        except Exception:
            rows = []
        if rows:
            out["findings"] = rows[:8]

    if sections is None or "measures" in sections:
        rows = []
        for m in tree.measures:
            if m.block != name or not m.datum:
                continue
            try:
                rdat = datums.resolve(tree, node, m.datum)
            except Exception:
                continue
            if rdat.error or not rdat.atoms:
                continue
            if ords & {int(i) for i in rdat.atoms}:
                rows.append(f"{m.name} ({m.datum})")
        if rows:
            out["measures"] = rows

    if foc.tr is not None and (sections is None or "workspace" in sections):
        rows = []
        gen = (bt.meta.get("generated") or {}) if bt else {}
        ws = foc.tr.ws
        run_job = (gen.get("trace") or {}).get("run_job")
        if ws:
            rows.append(f"workspace: {ws}")
        if run_job is not None:
            rows.append(f"run {run_job}")
        fr = _lca_frame(foc)
        user = tv.nearest_user(foc.tr, fr) if fr is not None else None
        if ws and user is not None:
            rows.append(f"python: ws-{ws}::{user.get('qual')}")
        if rows:
            out["workspace"] = rows
    return out


def _hop2_lines(
    store: Any, tree: Any, foc: _Focus, ref_id: int | None, q: str | None
) -> list[str]:
    """``fisheye+2hop``: the second hop as ``hop2: <count> <kind> via
    <rel>`` count lines — the link neighbourhood of the design and its
    bound structure refs; ``q='<label>'`` expands one group."""
    groups: dict[tuple[str, str], list[str]] = {}
    targets = [("se", ref_id)] if ref_id else []
    if foc.bt is not None and getattr(foc.bt, "bound", None):
        ref = store.get_ref(kind="structure", id=foc.bt.bound)
        if ref is not None:
            targets.append(("structure", int(ref.id)))
    for kind, rid in targets:
        try:
            nbh = store.neighbourhood(kind, rid)
        except Exception:
            continue
        kinds_of = {(n["kind"], n["id"]): n["kind"] for n in nbh.get("nodes", [])}
        handle_of = {
            (n["kind"], n["id"]): handle_registry.try_format(n["kind"], n["id"])
            or f"{n['kind']}:{n['id']}"
            for n in nbh.get("nodes", [])
        }
        for e in nbh.get("edges", []):
            other = e["dst"] if e.get("dir") == "out" else e["src"]
            okind = None
            for (nk, nid), k in kinds_of.items():
                if handle_of.get((nk, nid)) == other:
                    okind = nk
                    break
            if okind is None:
                continue
            groups.setdefault((okind, str(e.get("rel") or "related-to")), []).append(
                other
            )
    if q:
        hits = [
            (pair, refs)
            for pair, refs in groups.items()
            if pair[0] == q or pair[1] == q or f"{pair[0]}:{pair[1]}" == q
        ]
        if not hits:
            raise BadInput(
                f"no second-hop group {q!r} on this eye",
                next="one of the hop2 count lines' labels",
            )
        out = []
        for (kind, rel), refs in hits:
            for h in refs[:20]:
                out.append(f"  {h} ({kind} via {rel})")
        return out
    return [
        f"hop2: {len(refs)} {kind} via {rel}"
        for (kind, rel), refs in sorted(groups.items())
    ]


def _ring_sections(ring: Any) -> set[str] | None:
    """``ring=`` for the spatial eye: ``{'kinds': <csv|list>}`` narrows
    the +1hop sections by name; ``{'space': …}`` refuses — there is no
    SPACE axis on atoms."""
    if ring is None:
        return None
    if not isinstance(ring, dict):
        raise BadInput(f"eye: ring= takes a dict, got {ring!r}", next=_EYE_NEXT)
    if ring.get("space"):
        raise BadInput(
            "ring={'space': …} narrows a link eye's SPACE: tag axis — "
            "atoms carry no tags",
            next=_EYE_NEXT,
        )
    raw = ring.get("kinds")
    if raw is None:
        return None
    vals = raw if isinstance(raw, (list, tuple, set)) else str(raw).split(",")
    return {str(v).strip() for v in vals if str(v).strip()} or None


def render(
    store: Any,
    tree: Any,
    slug: str,
    focus_text: str,
    ext: Extent,
    *,
    q: str | None = None,
    ring: Any = None,
) -> str:
    """The se spatial eye body — :meth:`SeHandler.eye`'s ``~<focus>``
    half. ``ext`` is already parsed by the caller."""
    sections_filter = _ring_sections(ring)
    atoms, edges, bts, scenes = _merged(store, tree, slug)
    if not atoms:
        raise BadInput(
            "no structure-bound block on this design — the spatial eye needs atoms",
            next=f"get(kind='se', id={slug!r}, view='block', args={{'block':…}})",
        )
    foc = _parse_focus(store, tree, slug, focus_text, atoms, bts)
    if foc.bt is None:
        raise BadInput(
            f"{focus_text!r} resolves on no bound block of this design",
            next=_EYE_NEXT,
        )
    bt, scene = foc.bt, foc.bt.scene
    labels = list(scene.atoms) if scene is not None else []
    pi_data = (
        tree.structure_pi(bt.bound) if bt is not None and tree.structure_pi else None
    )
    pi_prefix = f"{slug}~pi:{foc.block}#"
    ref = store.get_ref(kind="se", id=slug)

    # ── kwd / summary ──
    if foc.kind == "atom":
        key = foc.keys[0]
        a = atoms[key]
        n_bonds = sum(1 for e in edges if key in (e.a, e.b))
        rec = (
            (pi_data or {}).get("atom", {}).get(str(labels.index(key[1])))
            if pi_data
            else None
        )
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
            f"{foc.block}#{key[1]} {a.element} {a.hyb or '—'} ·{n_bonds} bonds· "
            f"rings {rings_csv} · {a.relax or '—'} · {_maker_line(foc) or '—'}"
        ]
        if ext == Extent.TOC:
            return "\n".join(head)
        # summary: bond list + pi + relax line
        partner_lines = []
        for e in sorted(
            (e for e in edges if key in (e.a, e.b)),
            key=lambda e: e.length_A if e.length_A is not None else float("inf"),
        ):
            other = e.b if e.a == key else e.a
            oa = atoms[other]
            ln = f" {e.length_A:.2f}Å" if e.length_A is not None else ""
            partner_lines.append(f"{oa.handle} {e.order:g}{ln}")
        head.append("bonds: " + " · ".join(partner_lines))
        pi_line = se_eye.render_pi(labels.index(key[1]), pi_data, pi_prefix)
        if pi_line:
            head.append(pi_line)
        head.extend(_relax_lines(foc, atoms))
    else:
        head = [f"{foc.selector_text} → {len(foc.keys)} atoms on {foc.block}"]
        if foc.frame is not None:
            head[0] = (
                f"{slug}~{foc.selector_text} {tv.line(foc.frame, foc.tr.ws)} → "
                f"{len(foc.keys)} atoms"
            )
            made = tv._counts_text(foc.frame.get("made"))
            touched = tv._counts_text(foc.frame.get("touched"))
            counts = " · ".join(
                f"{k} {v}" for k, v in (("made", made), ("touched", touched)) if v
            )
            if counts:
                head[0] += f" · {counts}"
        if ext == Extent.TOC:
            return "\n".join(head)
        head.extend(_relax_lines(foc, atoms))
        if foc.frame is not None:
            kids = foc.tr.children.get(foc.frame["key"], [])
            if kids:
                head.append(
                    "children: " + " · ".join(tv.line(c, foc.tr.ws) for c in kids[:6])
                )

    if ext == Extent.SUMMARY:
        return "\n".join(head)

    # ── verbatim: the atom card / frame card / resolved selector ──
    if foc.kind == "atom":
        key = foc.keys[0]
        a = atoms[key]
        extra = []
        for e in edges:
            if e.kind != "seam" or key not in (e.a, e.b):
                continue
            other = e.b if e.a == key else e.a
            extra.append((atoms[other].handle, e.kind, e.order, e.length_A, e.image))
        card = se_eye.atom_card(
            scene,
            key[1],
            handle=a.handle,
            pi_data=pi_data,
            pi_handle_prefix=pi_prefix,
            relax_lines=_relax_lines(foc, atoms),
            made_by=_made_by(foc, slug),
        )
        if extra:
            seen = {ln.split()[1] for ln in card if ln.startswith("  ")}
            add = [
                f"  {h} {k} {o:g} {f'{d:.2f} Å' if d is not None else ''}".rstrip()
                for h, k, o, d, _img in extra
                if h not in seen
            ]
            if add:
                idx = card.index("bonds:") + 1 if "bonds:" in card else len(card)
                card[idx:idx] = add
        head.extend(card[1:])  # head already carries the kwd line
    elif foc.kind == "frame" and foc.frame is not None:
        head.append(tv.card(foc.tr, foc.frame["key"]))
    else:
        head.append(f"resolved: {foc.selector_text} → {len(foc.keys)} atoms")
    if ext == Extent.FULL:
        return "\n".join(head)

    # ── fisheye: the shell + hierarchy + π beyond ──
    shell = se_eye.render_shell(atoms, edges, foc.keys)
    if shell:
        head.extend(["shell:", *shell])
    ladder = _up_ladder(foc.tr, _lca_frame(foc), block=foc.block, slug=slug, bt=bt)
    if ladder:
        head.append(ladder)
    if foc.kind == "frame" and foc.frame is not None:
        head.extend(_down_lines(tree, slug, foc.tr, foc.frame))
    head.extend(se_eye.pi_system_lines(pi_data, foc.ordinals, pi_prefix))
    if ext == Extent.FIDELITY:
        return "\n".join(head)

    # ── +1hop: the containing block's ring ──
    ring_secs = _block_ring(store, tree, slug, foc, sections_filter)
    if ring_secs:
        head.append(f"ring — block {foc.block}:")
        for name in ("relax", "pockets", "findings", "measures", "workspace"):
            for line in ring_secs.get(name, []):
                head.append(f"  {line}" if not line.startswith(" ") else f" {line}")
    if ext == Extent.HOP1:
        return "\n".join(head)

    # ── +2hop: second hop as counts ──
    hop2 = _hop2_lines(store, tree, foc, int(ref.id) if ref else None, q)
    if hop2:
        head.extend(hop2)
    return "\n".join(head)


def _made_by(foc: _Focus, slug: str) -> str | None:
    """The ``made-by:`` line — the maker frame's python id first, then
    the frame text (slice 2p's block-eye shape)."""
    fr = _lca_frame(foc)
    if foc.tr is None or fr is None:
        return None
    user = tv.nearest_user(foc.tr, fr)
    if foc.tr.ws and user is not None:
        pyid = f"ws-{foc.tr.ws}::{user.get('qual')}"
        return f"{pyid} ({user.get('file')}:{user.get('line')}) → {tv.line(fr)}"
    return tv.line(fr)


def note_eye(
    store: Any,
    ref: Any,
    ext: Extent,
    *,
    recall: bool = False,
    q: str | None = None,
    ring: Any = None,
) -> str:
    """The bare-design fallback — an se ref has no code, so its eye is the
    note-eye shape (head + card body + link ring) without
    :func:`precis.utils.eye_render._head`'s file-kind formatting."""
    if recall:
        raise Unsupported(_RECALL_MSG, next=_RECALL_NEXT)
    hid = handle_registry.try_format("se", int(ref.id)) or f"se:{ref.slug}"
    title = " ".join(str(ref.title or "").split())
    head = f"{hid} [se] {title}".rstrip()
    if ext <= Extent.TOC:
        return f"· {head}"
    body, _total = _ordered_body(store, int(ref.id), cap=4000)
    block = f"{head}\n{body}" if body else head
    if ext < Extent.HOP1:
        return block
    sections = _rings(store, int(ref.id), "se", ext, recall=False, expand=q, ring=ring)
    return "\n\n".join([block, *sections])
