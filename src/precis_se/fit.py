"""``view='fit'`` — the server side of the built-in fit check
(se-fit-check-and-probe slice 2, gr514325).

The same engine the offline ``fit.check`` runs (:mod:`precis.cad.fit`),
fed from the live tree instead of a run dir:

- Poses are world-frame already (``ops.compose_world_pose`` at
  ``persist.load_tree``); instances and ``linear``/``polar`` arrays
  expand through :func:`precis.cad.fit.array_member_transforms` — one
  implementation shared with ``precis_geom.fit``.
- Geometry per part is the bound cad design's stored ``SceneSpec``
  (``store.cad_load`` — ``tree.cad_designs`` resolves a built
  ``graph.Design``, which the engine cannot tessellate). A block with no
  cad falls back to its ``effective_envelope`` and the header says so.
- Ruling C: the view recomputes live while the projected cost (≈0.2 ms
  per pair plus a fresh-tessellation term) stays under
  ``_LIVE_BUDGET_MS``; past that it serves the report the last
  ``import_workspace`` stored at ``meta.workspaces[<ws>].fit``, labelled
  with its build and age, and names ``args={'recompute': True}`` as the
  override.
- The diff lists new / cleared / changed findings — keyed by
  ``(rule, sorted pair)``, never by value (Decision 8) — plus the mass
  delta when both reports weighed the design.
- Decision 9: a manifest ``fit`` payload is untrusted input. The
  importer recomputes and stores the server's own report; this module
  never renders the script's verdict as verified.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np

from precis.cad import fit as _engine
from precis.cad.scene import NodeSpec, SceneSpec
from precis.cad.vec import Transform, identity
from precis.cad.vec import pose as vec_pose
from precis.utils.units import parse_quantity

#: Ruling C's live budget: projected cost above this serves the stored
#: report instead.
_LIVE_BUDGET_MS = 200.0

#: Seconds a design's ``updated_at`` may trail the stored report's ``at``
#: before the stored report counts as stale (see ``_edited_after``).
_STALE_GRACE_S = 2.0
#: Cost model (the spec's measured numbers): ~0.2 ms per measured pair,
#: ~5 ms per component with no cached tessellation.
_PAIR_MS = 0.2
_FRESH_COMP_MS = 5.0
#: Decision 4's server cache: in-process LRU keyed by geometry hash —
#: no DB, no out/.fitcache.
_MESH_CACHE_MAX = 512
_MESH_CACHE: OrderedDict[str, Any] = OrderedDict()
#: Pair-measurement memo handed to ``fit(pair_cache=…)`` — the same
#: (hash a, hash b, rel transform, rule) keys the offline check uses, so
#: a recompute re-runs only pairs that moved.
_PAIR_CACHE_MAX = 8192
_PAIR_CACHE: OrderedDict[str, dict[str, float]] = OrderedDict()

#: ``spec_of(owner_node) → (SceneSpec | None, ref meta | None)`` — the
#: geometry resolver the adapters inject; the default resolves the
#: block's ``bound`` cad slug through the store, the import path answers
#: from the manifest's freshly parsed specs (its ``cad_save``s are still
#: uncommitted at that point, so ``cad_load`` could not see them).
SpecOf = Callable[[Any], tuple[SceneSpec | None, dict[str, Any] | None]]
#: ``site_of(frame_id) → 'file:line' | None`` — made-by labels. The view
#: resolves them through ``tree.traces``; the import passes the trace it
#: just loaded. The ``sites`` map a caller hands to :func:`compute`
#: (block name → label, resolved against the run's trace the offline
#: check's way) wins over both.
SiteOf = Callable[[Any], str | None]


@dataclass
class FitRun:
    """One compute pass: the engine report plus the provenance the view
    header prints (which blocks were checked on real cad vs posed
    envelopes, and how long it took)."""

    report: _engine.FitReport
    n_cad: int
    n_env: int
    elapsed_ms: float


# ---------------------------------------------------------------------------
# geometry sources
# ---------------------------------------------------------------------------


def _cad_spec_of(store: Any) -> SpecOf:
    """Resolve ``bound_kind='cad'`` slugs to ``(SceneSpec, ref meta)``
    through the store; memoized per call-site closure. ``(None, meta)``
    means the ref exists but failed to load — a named not-fit-checked
    row, never a silent envelope."""
    memo: dict[str, tuple[Any, dict[str, Any] | None] | None] = {}

    def spec_of(owner: Any) -> tuple[SceneSpec | None, dict[str, Any] | None]:
        if owner.bound_kind != "cad" or not owner.bound:
            return None, None
        if owner.bound not in memo:
            got: tuple[Any, dict[str, Any] | None] | None = None
            ref = store.get_ref(kind="cad", id=owner.bound)
            if ref is not None:
                try:
                    spec, _handles = store.cad_load(ref.id)
                    got = (spec, dict(ref.meta or {}))
                except Exception:
                    got = (None, dict(ref.meta or {}))
            memo[owner.bound] = got
        return memo[owner.bound] or (None, None)

    return spec_of


def _envelope_spec(env: str) -> SceneSpec | None:
    """A stored ``se_blocks.envelope`` (canonical bare metres) as a
    one-component spec the engine can mesh. ``None`` when the string is
    not a parseable shape."""
    from precis.cad.dsl import parse as parse_shape

    try:
        parse_shape(env)
    except Exception:
        return None
    return SceneSpec(
        nodes=[NodeSpec(name="envelope", op="add", config=env, component="envelope")],
        components=["envelope"],
        meta={"units": "m"},
    )


def _mesh(spec: SceneSpec, comp: str, chord: float) -> Any:
    """The Decision-4 in-process LRU — tessellate once per
    (spec, component, chord), keyed by ``geometry_hash`` so an edited
    template re-meshes itself."""
    gh = _engine.geometry_hash(spec, comp, chord)
    if gh in _MESH_CACHE:
        _MESH_CACHE.move_to_end(gh)
        return _MESH_CACHE[gh]
    solid = _engine.tessellate_component(spec, comp, chord)
    _MESH_CACHE[gh] = solid
    while len(_MESH_CACHE) > _MESH_CACHE_MAX:
        _MESH_CACHE.popitem(last=False)
    return solid


def _pair_cache() -> OrderedDict[str, dict[str, float]]:
    while len(_PAIR_CACHE) > _PAIR_CACHE_MAX:
        _PAIR_CACHE.popitem(last=False)
    return _PAIR_CACHE


# ---------------------------------------------------------------------------
# labels
# ---------------------------------------------------------------------------


def _site_of(
    tree: Any, meta: Mapping[str, Any] | None, site_of: SiteOf | None
) -> str | None:
    """The block's made-by: ``meta.generated.frame`` resolved to a
    ``file:line`` — by the caller's ``site_of`` (import path), else via
    the stored trace the ref's ``generated.trace`` record points at."""
    gen = (meta or {}).get("generated") or {}
    frame = gen.get("frame")
    if frame is None:
        return None
    if site_of is not None:
        return site_of(frame)
    traces = getattr(tree, "traces", None)
    rec = gen.get("trace")
    if traces is None or not rec:
        return None
    doc = traces(rec)
    if not doc:
        return None
    from precis_se import traceview

    try:
        return traceview.site(
            traceview.Trace({"run": doc.get("run"), "frames": doc.get("frames")}),
            int(frame),
        )
    except (TypeError, ValueError, KeyError):
        return None


def _thread_size(tree: Any, c: Any) -> str | None:
    """The scripted fastener's ``annotations.thread`` (Decision 6 — never
    inferred) resolved on the endpoint's effective port."""
    from precis_se.ops import effective_ports

    for blk, port in ((c.a_block, c.a_port), (c.b_block, c.b_port)):
        node = tree.blocks.get(blk)
        if node is None:
            continue
        p = effective_ports(tree, node).get(port)
        ann = getattr(p, "annotations", None) or {}
        if isinstance(ann.get("thread"), str):
            return ann["thread"]
    return None


def _densities(store: Any, slugs: set[str]) -> dict[str, float] | None:
    """material slug → kg/m³ from the material kind's sourced density
    (the ``_render_mass`` pattern). A slug with no ref or no density is
    left out — the engine's mass section then names the blocks loudly
    instead of defaulting a number."""
    if not slugs:
        return None
    out: dict[str, float] = {}
    for slug in sorted(slugs):
        try:
            ref = store.get_ref(kind="material", id=slug)
            if ref is None:
                continue
            for v in store.material_values_for_ref(ref.id):
                if v.get("property_id") == "density" and v.get("value_num") is not None:
                    out[slug] = float(v["value_num"])
                    break
        except Exception:
            continue
    return out


def min_clearance_of(
    fit_rules: Mapping[str, Any] | None,
) -> dict[tuple[str, str], float] | None:
    """``fit_rules`` (manifest key / stored record) → the engine's
    per-pair ``min_clearance`` map. Values are unit strings; bare numbers
    read as metres."""
    rows = (fit_rules or {}).get("min_clearance") or []
    out: dict[tuple[str, str], float] = {}
    for row in rows:
        pair = tuple(sorted(str(x) for x in row["pair"]))
        if len(pair) != 2:
            continue
        out[(pair[0], pair[1])] = parse_quantity(
            row["value"], "length", require_unit=False
        )
    return out or None


def entry_sites(
    manifest: Mapping[str, Any], frames: Iterable[Mapping[str, Any]] | None
) -> dict[str, str]:
    """block/instance name → ``file:line`` — each manifest entry's
    ``frame`` stamp resolved against the run's trace the offline check's
    way (``precis_geom.fit._sites``: raw frame id → file:line, no kind
    walk, no ``(frame N)`` suffix), so the server report's made-by
    columns match ``fit.check(run_dir)`` verbatim."""
    by_id = {
        int(f["id"]): f"{f.get('file') or '?'}:{f.get('line') or '?'}"
        for f in frames or []
        if isinstance(f, Mapping) and f.get("id") is not None
    }

    def put(entry: Any, out: dict[str, str]) -> None:
        if isinstance(entry, Mapping):
            name, fid = entry.get("name"), entry.get("frame")
        else:
            name, fid = getattr(entry, "name", None), getattr(entry, "frame", None)
        if name is None or fid is None:
            return
        site = by_id.get(int(fid))
        if site is not None:
            out[str(name)] = site

    out: dict[str, str] = {}
    for e in manifest.get("blocks") or []:
        put(e, out)
    for e in manifest.get("instances") or []:
        put(e, out)
    return out


# ---------------------------------------------------------------------------
# the compute pass
# ---------------------------------------------------------------------------


def compute(
    store: Any,
    tree: Any,
    *,
    spec_of: SpecOf | None = None,
    site_of: SiteOf | None = None,
    sites: Mapping[str, str] | None = None,
    min_clearance: dict[tuple[str, str], float] | None = None,
) -> FitRun:
    """Run the mesh fit check over the tree's placed parts. ``sites``
    (block name → ``file:line``, :func:`entry_sites`) supplies made-by
    labels first; ``site_of``/the stored trace fills what it misses."""
    from precis_se import ops as se_ops

    t0 = time.monotonic()
    se_ops.compose_world_pose(tree)  # idempotent; covers caller-built trees
    spec_of = spec_of or _cad_spec_of(store)
    templates = se_ops.template_names(tree)
    placed: list[_engine.Placed] = []
    slugs: set[str] = set()
    n_cad = 0
    n_env = 0

    for name in sorted(tree.blocks):
        node = tree.blocks[name]
        if name in templates:
            continue
        owner = tree.blocks.get(node.template or "") or node
        spec: SceneSpec | None = None
        meta: dict[str, Any] | None = None
        source = "envelope"
        fail: str | None = None
        if owner.bound_kind == "structure" and owner.bound:
            fail = "atomic (structure-bound) — fit is macro only"
        elif owner.bound_kind == "cad" and owner.bound:
            spec, meta = spec_of(owner)
            if spec is None:
                fail = f"cad {owner.bound!r} failed to load"
            else:
                source = "cad"
        if spec is None and fail is None:
            env = se_ops.effective_envelope(tree, node)
            spec = _envelope_spec(env) if env else None
            if spec is None:
                fail = "no bound cad, no usable envelope"
        xf = vec_pose(np.asarray(node.pose), np.asarray(node.rot))
        made_by = (
            (sites or {}).get(name)
            or _site_of(tree, meta, site_of)
            or (
                f"cad {owner.bound}" if spec is not None and source == "cad" else source
            )
        )
        members = (
            _engine.array_member_transforms(node.array) if node.array else [identity()]
        )
        for k, mxf in enumerate(members):
            label = f"{name}#{k + 1}" if len(members) > 1 else name
            _place_part(
                placed,
                label,
                spec,
                xf.compose(mxf),
                base=name if node.template is not None else None,
                parent=node.parent,
                made_by=made_by,
                fail=fail,
            )
        if fail is None:
            if source == "cad":
                n_cad += 1
            else:
                n_env += 1
            if spec is not None:
                slugs.update(
                    str(s) for s in (spec.meta.get("materials") or {}).values()
                )

    connects = [
        _engine.Connect(
            a=c.a_block,
            b=c.b_block,
            joint=c.joint,
            a_port=c.a_port or None,
            b_port=c.b_port or None,
            thread_size=_thread_size(tree, c),
        )
        for c in tree.connects
    ]
    from precis_geom import fit as geom_fit

    report = _engine.fit(
        placed,
        connects,
        _engine.default_rules(),
        sizes=geom_fit._thread_sizes(),
        materials=_densities(store, slugs),
        min_clearance=min_clearance,
        pair_cache=_pair_cache(),
    )
    return FitRun(report, n_cad, n_env, (time.monotonic() - t0) * 1000.0)


def _place_part(
    placed: list[_engine.Placed],
    name: str,
    spec: SceneSpec | None,
    xf: Transform,
    *,
    base: str | None,
    parent: str | None,
    made_by: str | None,
    fail: str | None,
) -> None:
    if fail is not None or spec is None:
        placed.append(
            _engine.place(
                {},
                xf,
                name=name,
                chord=_engine.CHORD_MAX_M,
                base=base,
                parent=parent,
                made_by=made_by,
                note=fail or "no geometry",
            )
        )
        return
    materials = dict(spec.meta.get("materials") or {})
    solids: dict[str, Any] = {}
    reasons: list[str] = []
    chords: list[float] = []
    gh_parts: list[str] = []
    for comp in spec.components or []:
        reason = _engine.unmeshable_reason(spec, comp)
        if reason is not None:
            reasons.append(f"{comp}: {reason}")
            continue
        chord = _engine.component_chord(spec, comp)
        chords.append(chord)
        solid = _mesh(spec, comp, chord)
        if solid is None:
            reasons.append(f"{comp}: empty fold")
            continue
        solids[comp] = solid
        gh_parts.append(_engine.geometry_hash(spec, comp, chord))
    chord = max(chords) if chords else _engine.CHORD_MAX_M
    if not solids:
        placed.append(
            _engine.place(
                {},
                xf,
                name=name,
                chord=chord,
                base=base,
                parent=parent,
                made_by=made_by,
                material=materials,
                note="; ".join(reasons) or "no meshable component",
            )
        )
        return
    import hashlib

    comps = list(solids)
    m34 = [[float(xf.R[i, j]) for j in range(3)] + [float(xf.t[i])] for i in range(3)]

    def refine(finer: float) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for comp in comps:
            s = _mesh(spec, comp, finer)
            if s is not None:
                out[comp] = s.transform(m34)
        return out

    placed.append(
        _engine.place(
            solids,
            xf,
            name=name,
            chord=chord,
            base=base,
            parent=parent,
            made_by=made_by,
            refine=refine,
            material=materials,
            geo_hash=hashlib.sha256("|".join(gh_parts).encode()).hexdigest(),
        )
    )


# ---------------------------------------------------------------------------
# the stored report + diff (Decision 8)
# ---------------------------------------------------------------------------


def _finding_key(f: Mapping[str, Any]) -> tuple[Any, tuple[str, ...]]:
    return (f.get("rule"), tuple(sorted(str(x) for x in f.get("pair") or [])))


def diff_reports(
    cur: Mapping[str, Any] | None, prev: Mapping[str, Any] | None
) -> dict[str, Any]:
    """New / cleared / changed findings between two ``report.to_json()``
    payloads — keyed by ``(rule, sorted pair)``, never by value — plus the
    mass delta when both sides weighed."""
    cur_f = {_finding_key(f): f for f in (cur or {}).get("findings") or []}
    prev_f = {_finding_key(f): f for f in (prev or {}).get("findings") or []}
    new = [k for k in cur_f if k not in prev_f]
    cleared = [k for k in prev_f if k not in cur_f]
    changed = [
        k
        for k in cur_f
        if k in prev_f
        and any(
            cur_f[k].get(field) != prev_f[k].get(field)
            for field in ("gap_m", "overlap_m3", "thickness_m")
        )
    ]
    cur_mass = (cur or {}).get("mass_kg")
    prev_mass = (prev or {}).get("mass_kg")
    return {
        "new": [cur_f[k] for k in sorted(new)],
        "cleared": [prev_f[k] for k in sorted(cleared)],
        "changed": [cur_f[k] for k in sorted(changed)],
        "mass_delta_kg": (
            round(cur_mass - prev_mass, 9)
            if isinstance(cur_mass, (int, float))
            and isinstance(prev_mass, (int, float))
            else None
        ),
        "mass_kg": cur_mass,
        "prev_mass_kg": prev_mass,
    }


def stored_record(
    report_json: Mapping[str, Any],
    *,
    workspace: str,
    run_job: int | None,
    fit_rules: Mapping[str, Any] | None,
    prev: Mapping[str, Any] | None,
    sites: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The ``meta.workspaces[<ws>].fit`` record the importer writes —
    the server's own recomputed report plus the diff against whatever
    this workspace stored last (Decision 8/9). ``sites`` keeps the
    made-by labels the import resolved, so a live recompute prints the
    same table instead of the bound ref's coarser
    ``generated.frame``."""
    return {
        "schema": 1,
        "workspace": workspace,
        "run_job": run_job,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "report": dict(report_json),
        "fit_rules": dict(fit_rules) if fit_rules else None,
        "sites": dict(sites) if sites else None,
        "diff": diff_reports(report_json, (prev or {}).get("report")),
    }


def _latest_stored(ref: Any) -> dict[str, Any] | None:
    """Newest ``workspaces[*].fit`` record across the design's
    workspaces — ISO-8601 UTC stamps sort lexically."""
    ws = (ref.meta or {}).get("workspaces") or {}
    recs = [r["fit"] for r in ws.values() if isinstance(r, dict) and r.get("fit")]
    return max(recs, key=lambda r: str(r.get("at") or ""), default=None)


def _age(at: Any) -> str:
    try:
        then = datetime.fromisoformat(str(at))
    except ValueError:
        return "age ?"
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    s = max(0.0, (datetime.now(UTC) - then).total_seconds())
    for cut, unit in ((3600.0, "h"), (60.0, "m")):
        if s >= cut:
            return f"{s / cut:.0f}{unit} ago"
    return f"{s:.0f}s ago"


def _fmt_finding(f: Mapping[str, Any]) -> str:
    pair = " / ".join(str(x) for x in f.get("pair") or [])
    gap = f.get("gap_m")
    thick = f.get("thickness_m")
    meas = (
        f"overlap thickness {float(thick) * 1e3:.3f} mm"
        if thick
        else f"gap {float(gap or 0.0) * 1e3:+.3f} mm"
    )
    return f"{f.get('rule')} {pair} ({meas})"


def _diff_text(diff: Mapping[str, Any], *, against: str) -> str:
    lines = [f"# diff vs {against}"]
    for f in diff.get("new") or []:
        lines.append(f"new: {_fmt_finding(f)}")
    for f in diff.get("cleared") or []:
        lines.append(f"cleared: {_fmt_finding(f)}")
    for f in diff.get("changed") or []:
        lines.append(f"changed: {_fmt_finding(f)}")
    delta = diff.get("mass_delta_kg")
    cur, prev = diff.get("mass_kg"), diff.get("prev_mass_kg")
    if delta is not None:
        lines.append(
            f"mass: {'+' if delta >= 0 else ''}{delta:.3f} kg "
            f"({prev:.3f} → {cur:.3f} kg)"
        )
    if not (diff.get("new") or diff.get("cleared") or diff.get("changed")):
        lines.append("no finding changes")
    return "\n".join(lines) + "\n"


def _projected_ms(tree: Any, spec_of: SpecOf) -> float:
    """Ruling C's live check: n-choose-2 pairs at the measured ~0.2 ms
    plus ~5 ms per component with no cached tessellation."""
    from precis_se import ops as se_ops

    templates = se_ops.template_names(tree)
    owners: dict[str, Any] = {}
    n_parts = 0
    for name, node in tree.blocks.items():
        if name in templates:
            continue
        n_parts += int(node.array["count"]) if node.array else 1
        owner = tree.blocks.get(node.template or "") or node
        owners[owner.name] = owner
    fresh = 0
    for owner in owners.values():
        spec, _meta = spec_of(owner)
        if spec is None:
            env = se_ops.effective_envelope(tree, owner)
            spec = _envelope_spec(env) if env else None
        if spec is None:
            continue
        for comp in spec.components or []:
            if _engine.unmeshable_reason(spec, comp) is not None:
                continue
            gh = _engine.geometry_hash(spec, comp, _engine.component_chord(spec, comp))
            if gh not in _MESH_CACHE:
                fresh += 1
    return n_parts * (n_parts - 1) / 2 * _PAIR_MS + fresh * _FRESH_COMP_MS


def _edited_after(rec: Mapping[str, Any], edited_at: Any) -> bool:
    """True when the design was edited after the stored report's ``at``
    (a missing or unparseable stamp on either side reads as not stale)."""
    if not isinstance(edited_at, datetime):
        return False
    try:
        then = datetime.fromisoformat(str(rec.get("at")))
    except ValueError:
        return False
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    if edited_at.tzinfo is None:
        edited_at = edited_at.replace(tzinfo=UTC)
    # ``at`` is truncated to whole seconds and the import's own writes
    # stamp ``updated_at`` from the same transaction, so the import itself
    # lands within the grace window and is not "an edit after the report".
    return (edited_at - then).total_seconds() > _STALE_GRACE_S


def _render_stored(
    rec: Mapping[str, Any], projected_ms: float, edited_at: Any = None
) -> str:
    at = rec.get("at") or "?"
    head = [
        f"# view='fit' — stored report · workspace {rec.get('workspace')!r} · "
        f"run_job {rec.get('run_job')} · {at} ({_age(at)})",
    ]
    stale = _edited_after(rec, edited_at)
    if stale:
        head.append("# stale: design edited after this report")
    head.append(
        f"# live recompute projected ≈{projected_ms:.0f} ms "
        f"(budget {_LIVE_BUDGET_MS:.0f} ms) — "
        + ("this report predates your edits; recompute: " if stale else "force it: ")
        + "args={'recompute': true}"
    )
    body = str((rec.get("report") or {}).get("report") or "")
    return (
        "\n".join(head)
        + "\n"
        + body
        + _diff_text(rec.get("diff") or {}, against="the previous import")
    )


def render_fit(store: Any, tree: Any, ref: Any, args: Mapping[str, Any] | None) -> str:
    """``view='fit'``: live recompute while the projected cost fits the
    budget (or when nothing is stored yet), else the last import's stored
    report — either way with the new/cleared/changed diff."""
    stored = _latest_stored(ref)
    spec_of = _cad_spec_of(store)
    force = bool((args or {}).get("recompute"))
    if stored is not None and not force:
        projected = _projected_ms(tree, spec_of)
        if projected > _LIVE_BUDGET_MS:
            return _render_stored(stored, projected, getattr(ref, "updated_at", None))
    run = compute(
        store,
        tree,
        spec_of=spec_of,
        sites=(stored or {}).get("sites"),
        min_clearance=min_clearance_of((stored or {}).get("fit_rules")),
    )
    head = [
        f"# view='fit' — live · {run.elapsed_ms:.0f} ms · geometry: "
        f"cad×{run.n_cad} envelope×{run.n_env}"
    ]
    out = "\n".join(head) + "\n" + run.report.text()
    if stored is None:
        return out + "# diff: no earlier report on record\n"
    # The stored record's diff is the last import's delta against ITS
    # predecessor — the answer "what did that build change"; live-vs-
    # stored adds post-import edit drift on top when it is non-empty.
    out += _diff_text(
        stored.get("diff") or {},
        against=f"the previous import (run_job {stored.get('run_job')}, "
        f"{stored.get('at')})",
    )
    drift = diff_reports(run.report.to_json(), stored.get("report"))
    if drift["new"] or drift["cleared"] or drift["changed"]:
        out += _diff_text(drift, against="the stored report (post-import edits)")
    return out
