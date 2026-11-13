"""The editable design *recipe*: a feature graph with stable ids.

se-flow-kernel slice 4b §Design representation item 1. A :class:`Recipe` is
the mutable, snapshot-able thing an optimiser edits; built geometry is a pure
function of it and is immutable (item 0), so a *move* is a recipe edit plus a
rebuild, never an in-place transform. Growing and pruning features does not
fit a fixed-length parameter vector, so the feature graph — not a vector — is
the primary interface; :mod:`precis_geom.params` is the thin bound-parameter
convenience over it, driving :meth:`Recipe.modify` through its ``bindings``.

A :class:`Feature` carries a stable ``id`` (minted once, never reused), an
``op`` (a ``FEATURE_OPS`` key in the build layer), sorted plain-valued
``params``, parent feature ids (the graph is a DAG), an optional interchangeable
``group``, and a ``pinned`` flag that refuses :meth:`~Recipe.modify` and
:meth:`~Recipe.remove`. One evaluation is one transaction: snapshot, edit,
build/evaluate, then accept or :meth:`~Recipe.restore`. :meth:`~Recipe.restore`
leaves the id counter alone, so a rejected move never frees an id for reuse.
Nested plain parameter values are copied at edit, read and transaction
boundaries: a caller's list or dict must not bypass pinning or change the
state an optimiser will restore.

**Build layer.** :func:`build` turns a recipe into a :class:`Built`: a cad
``SceneSpec`` (what ``out.block`` writes), its :func:`~precis_geom.steps.shape_sha`
and ``Built.solid()`` — the ``(Design, Expr)`` pair ``evaluate`` takes. Every
feature is one :data:`FEATURE_OPS` step (``(parent, **params) -> SceneSpec``,
cached by :mod:`precis_geom.steps`) stacked on its parents' output. Group
members are leaf steps with no parent, gathered by one ``combine`` step in
parameter order and named ``<group>#<rank>``; other nodes are named by their
content. Feature ids reach neither step params nor node names, so two hole
orders, or equal geometry under different ids, give one ``shape_sha``, and a
hole edit rebuilds that leaf and its ``combine`` only. Several sinks (two
independent branches) meet in one ``union`` step. A region whose ``patch:``
anchor names a feature the recipe no longer has refuses at build; the
rasterised region-change report (``one→many`` …) is still to come with the
``precis.cad.patch`` resolver move.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

from precis.cad.dsl import _ALIAS_KEYS, ShapeSpec, format_spec
from precis.cad.fold import Expr
from precis.cad.graph import Design
from precis.cad.scene import NodeSpec, SceneSpec, build_design
from precis.errors import BadInput
from precis_geom import steps

# A feature id reads as ``<prefix><n>`` where ``n`` is a global monotonic
# counter (so ids are unique whatever the prefix) and the prefix hints the op
# — a drilled hole is ``h7``, matching the recipe_key convention (``h7.x``) the
# build/trace layer stamps on creator frames. Unmapped ops fall back to the
# op's first letter, or ``f`` for an empty op.
_PREFIX: Mapping[str, str] = {
    "drill": "h",
    "box": "b",
    "ngon": "t",
    "combine": "c",
    "extrude": "e",
    "fillet": "r",
    "wrap": "w",
    "boolean": "x",
}


def _prefix(op: str) -> str:
    return _PREFIX.get(op) or (op[:1] if op else "f")


def _canon_params(params: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    """Sorted ``(name, value)`` pairs; values must be plain SI data."""
    return deepcopy(tuple(sorted(params.items())))


@dataclass(frozen=True)
class Feature:
    """One node of the recipe DAG. Frozen: an edit replaces the node, keeping
    its ``id`` — there are no per-move traces on an object (item 0)."""

    id: str
    op: str
    params: tuple[tuple[str, Any], ...] = ()
    parents: tuple[str, ...] = ()
    group: str | None = None
    pinned: bool = False

    def param_map(self) -> dict[str, Any]:
        return deepcopy(dict(self.params))


@dataclass(frozen=True)
class Snapshot:
    """A detached, frozen copy of a recipe's features and regions.

    Plain copy because designs are small; it must survive later proposals and
    restores (the annealer's :class:`~precis_geom.optimize.anneal.Moves`
    contract), so it shares no mutable alias with the live recipe.
    """

    features: tuple[Feature, ...] = ()
    regions: tuple[tuple[str, str], ...] = ()


class Recipe:
    """The mutable feature graph. Edits mutate the recipe; geometry does not."""

    def __init__(self) -> None:
        self._features: dict[str, Feature] = {}
        self._regions: dict[str, str] = {}
        self._counter = 0

    # ── reads ────────────────────────────────────────────────────────────
    @property
    def features(self) -> tuple[Feature, ...]:
        """Detached features in insertion order."""
        return deepcopy(tuple(self._features.values()))

    @property
    def regions(self) -> tuple[tuple[str, str], ...]:
        """Named regions as sorted ``(name, selector)`` pairs."""
        return tuple(sorted(self._regions.items()))

    def feature(self, id: str) -> Feature:
        feat = self._features.get(id)
        if feat is None:
            raise BadInput(
                f"no feature {id!r} in the recipe",
                next=f"add it first, or name one of {sorted(self._features)}",
            )
        return deepcopy(feat)

    def __contains__(self, id: object) -> bool:
        return id in self._features

    # ── edits ────────────────────────────────────────────────────────────
    def add(
        self,
        op: str,
        *,
        parents: tuple[str, ...] = (),
        group: str | None = None,
        pinned: bool = False,
        **params: Any,
    ) -> str:
        """Mint a feature and return its id. Parents must already exist."""
        if not isinstance(op, str) or not op.strip():
            raise BadInput("feature op must be a non-empty string")
        for parent in parents:
            if parent not in self._features:
                raise BadInput(
                    f"parent {parent!r} of a new {op!r} feature does not exist",
                    next=f"add {parent!r} first, or drop it from parents",
                )
        self._counter += 1
        fid = f"{_prefix(op)}{self._counter}"
        self._features[fid] = Feature(
            id=fid,
            op=op,
            params=_canon_params(params),
            parents=tuple(parents),
            group=group,
            pinned=pinned,
        )
        return fid

    def modify(self, id: str, **params: Any) -> None:
        """Merge ``params`` into a feature, re-canonicalising. Refuses a pinned
        feature (user-placed geometry is not the optimiser's to move)."""
        feat = self.feature(id)
        if feat.pinned:
            raise BadInput(
                f"feature {id!r} is pinned; modify refuses it",
                next="unpin it in the recipe source, or edit a non-pinned feature",
            )
        merged = feat.param_map()
        merged.update(params)
        self._features[id] = replace(feat, params=_canon_params(merged))

    def remove(self, id: str) -> None:
        """Delete a feature. Refuses a pinned feature, or one a child names."""
        feat = self.feature(id)
        if feat.pinned:
            raise BadInput(
                f"feature {id!r} is pinned; remove refuses it",
                next="unpin it in the recipe source, or remove a non-pinned feature",
            )
        children = [f.id for f in self._features.values() if id in f.parents]
        if children:
            raise BadInput(
                f"feature {id!r} still has child feature(s) {sorted(children)}",
                next=f"remove {sorted(children)} first, or re-parent them",
            )
        del self._features[id]
        # The counter is deliberately left alone: a removed id is never reused.

    def region(self, name: str, selector: str) -> None:
        """Name a region by a ``patch:`` selector. Regions name a stable feature
        id, not a face, because a build is free to re-raster the geometry."""
        if not isinstance(name, str) or not name.strip():
            raise BadInput("region name must be a non-empty string")
        if not isinstance(selector, str) or not selector.strip():
            raise BadInput(f"region {name!r} selector must be a non-empty string")
        self._regions[name] = selector

    # ── transactions ──────────────────────────────────────────────────────
    def snapshot(self) -> Snapshot:
        """A detached copy for the one-evaluation-one-transaction loop."""
        return Snapshot(features=self.features, regions=self.regions)

    def restore(self, snap: Snapshot) -> None:
        """Replace features and regions in place; the id counter is untouched,
        so a rejected move never frees an id for reuse."""
        self._features = {f.id: f for f in deepcopy(snap.features)}
        self._regions = dict(snap.regions)

    # ── serialisation ──────────────────────────────────────────────────────
    def to_json(self) -> dict[str, Any]:
        """The full recipe: everything ``build`` needs, plus the id counter so a
        round trip keeps minting unused ids."""
        return {
            "counter": self._counter,
            "features": [
                {
                    "id": f.id,
                    "op": f.op,
                    "params": f.param_map(),
                    "parents": list(f.parents),
                    "group": f.group,
                    "pinned": f.pinned,
                }
                for f in self._features.values()
            ],
            "regions": dict(self._regions),
        }

    @classmethod
    def from_json(cls, doc: Mapping[str, Any]) -> Recipe:
        """Rebuild a recipe from :meth:`to_json`. The counter is advanced past
        every id so no stored id can be re-minted."""
        recipe = cls()
        for entry in doc.get("features", ()):
            params = dict(entry.get("params", {}))
            fid = entry["id"]
            recipe._features[fid] = Feature(
                id=fid,
                op=entry["op"],
                params=_canon_params(params),
                parents=tuple(entry.get("parents", ())),
                group=entry.get("group"),
                pinned=bool(entry.get("pinned", False)),
            )
        recipe._regions = dict(doc.get("regions", {}))
        recipe._counter = max(
            int(doc.get("counter", 0)),
            *(_id_number(fid) for fid in recipe._features),
            0,
        )
        return recipe


def _id_number(fid: str) -> int:
    """The trailing integer of ``<prefix><n>`` (0 when there is none)."""
    tail = ""
    for ch in reversed(fid):
        if ch.isdigit():
            tail = ch + tail
        else:
            break
    return int(tail) if tail else 0


# ── build layer ──────────────────────────────────────────────────────────

#: DSL aliases a feature op may name directly (the loader-free solids).
_SHAPES = (
    "box",
    "cyl",
    "cone",
    "tcone",
    "sphere",
    "torus",
    "hex",
    "ngon",
    "frustum",
    "pyramid",
)
#: Placement and boolean params every shape op takes besides its dims.
_PLACE = ("x", "y", "z", "rx", "ry", "rz", "mode", "component")
_MODES = ("add", "cut", "intersect")


def _node(alias: str, params: Mapping[str, Any]) -> NodeSpec:
    """One shape node from plain SI params, named by its content."""
    dims = _ALIAS_KEYS[alias]
    missing = [k for k in dims if k not in params]
    unknown = sorted(set(params) - set(dims) - set(_PLACE))
    if missing or unknown:
        raise BadInput(
            f"feature op {alias!r}: missing {missing}, unknown {unknown}",
            next=f"pass {list(dims)} plus optional {list(_PLACE)}",
        )
    mode = params.get("mode", "add")
    if mode not in _MODES:
        raise BadInput(f"feature op {alias!r}: mode {mode!r} is not one of {_MODES}")
    config = format_spec(ShapeSpec(alias, {k: float(params[k]) for k in dims}))
    loc = tuple(float(params.get(k, 0.0)) for k in ("x", "y", "z"))
    rot = tuple(float(params.get(k, 0.0)) for k in ("rx", "ry", "rz"))
    component = str(params.get("component", "part"))
    digest = steps._sha(json.dumps([config, loc, rot, mode, component]))
    return NodeSpec(
        name=f"{alias}.{digest[:12]}",
        op=mode,
        config=config,
        component=component,
        loc=(loc[0], loc[1], loc[2]),
        rot=(rot[0], rot[1], rot[2]),
    )


def _stack(parent: SceneSpec | None, nodes: Iterable[NodeSpec]) -> SceneSpec:
    """A new spec: ``parent``'s nodes then ``nodes``. An identical node is
    already there (every boolean is idempotent); a name held by different
    content refuses."""
    out = list(parent.nodes) if parent is not None else []
    have = {n.name: n for n in out}
    for node in nodes:
        old = have.get(node.name)
        if old is None:
            out.append(node)
            have[node.name] = node
        elif old != node:
            raise BadInput(
                f"build: two different nodes are both named {node.name!r}",
                next="give the clashing group a distinct name",
            )
    components = list(dict.fromkeys(n.component for n in out)) or ["part"]
    meta = dict(parent.meta) if parent is not None else {"units": "mm"}
    return SceneSpec(nodes=out, components=components, meta=meta)


def _shape_op(alias: str) -> Callable[..., SceneSpec]:
    def op(parent: SceneSpec | None, **params: Any) -> SceneSpec:
        return _stack(parent, [_node(alias, params)])

    return steps.step(op, name=f"feature.{alias}")


@steps.step(name="feature.drill")
def _drill(
    parent: SceneSpec | None,
    *,
    x: float,
    y: float,
    d: float,
    depth: float,
    z: float = 0.0,
    mode: str = "add",
    component: str = "part",
) -> SceneSpec:
    """A vertical cylinder of diameter ``d`` reaching ``depth`` below ``z``.
    ``mode='add'`` by default: a gap/fluid solid (``domain_from_solid``) is
    the void, so a hole in its floor *adds* to it."""
    node = _node(
        "cyl",
        {
            "r": d / 2.0,
            "h": depth,
            "x": x,
            "y": y,
            "z": z - depth,
            "mode": mode,
            "component": component,
        },
    )
    return _stack(parent, [node])


@steps.step(name="feature.combine")
def _combine(
    parent: SceneSpec | None, leaves: list[SceneSpec], group: str
) -> SceneSpec:
    """Stack a group's leaves (already in canonical order) on ``parent``,
    naming each leaf node ``<group>#<rank>``."""
    nodes = [
        replace(node, name=f"{group}#{rank}")
        for rank, leaf in enumerate(leaves)
        for node in leaf.nodes
    ]
    return _stack(parent, nodes)


@steps.step(name="feature.union")
def _union(specs: list[SceneSpec]) -> SceneSpec:
    """Merge branch outputs (shared ancestry nodes appear once)."""
    out: SceneSpec | None = None
    for spec in specs:
        out = _stack(out, spec.nodes)
    assert out is not None
    return out


#: ``op`` → a ``@steps.step`` ``(parent: SceneSpec | None, **params) ->
#: SceneSpec``. Shape ops take the alias dims (``_ALIAS_KEYS``) plus
#: ``x/y/z`` (loc), ``rx/ry/rz`` (rot), ``mode`` and ``component``; all SI.
FEATURE_OPS: dict[str, Callable[..., SceneSpec]] = {
    **{alias: _shape_op(alias) for alias in _SHAPES},
    "drill": _drill,
}


@dataclass(frozen=True)
class Built:
    """One immutable build of a recipe."""

    spec: SceneSpec
    shape_sha: str
    #: ``(feature id, step key)`` in build order; the key is the cache entry's
    #: (``content:spec:<sha>`` with the cache off).
    step_keys: tuple[tuple[str, str], ...]

    def solid(self) -> tuple[Design, Expr]:
        """The ``(Design, Expr)`` pair ``evaluate`` and ``domain_from_solid``
        take."""
        design = build_design(self.spec)
        return design, design.whole()


def _step_key(spec: SceneSpec) -> str:
    inst = steps._live_inst(spec, "spec")
    return inst.key if inst is not None else f"content:spec:{steps.shape_sha(spec)}"


def _order(features: tuple[Feature, ...]) -> list[Feature]:
    """Parents before children, insertion order otherwise."""
    by_id = {f.id: f for f in features}
    for f in features:
        for p in f.parents:
            if p not in by_id:
                raise BadInput(f"feature {f.id!r} names missing parent {p!r}")
    done: dict[str, Feature] = {}
    while len(done) < len(features):
        ready = [
            f
            for f in features
            if f.id not in done and all(p in done for p in f.parents)
        ]
        if not ready:
            raise BadInput("the recipe's parent links form a cycle")
        done.update((f.id, f) for f in ready)
    return list(done.values())


def _check_regions(recipe: Recipe) -> None:
    for name, selector in recipe.regions:
        if not selector.startswith("patch:"):
            continue
        anchor = selector[len("patch:") :].split("@", 1)[0].split(".", 1)[0]
        if anchor not in recipe:
            raise BadInput(
                f"region {name!r} is anchored on feature {anchor!r}, which the "
                "recipe no longer has",
                next=f"re-anchor region {name!r} on a live feature, or drop it",
            )


def build(recipe: Recipe) -> Built:
    """Build ``recipe`` into an immutable :class:`Built`; never mutates it."""
    _check_regions(recipe)
    features = _order(recipe.features)
    if not features:
        raise BadInput("build: the recipe has no features", next="add one first")
    members = {f.id: f for f in features if f.group is not None}
    outputs: dict[str, SceneSpec] = {}
    keys: list[tuple[str, str]] = []
    has_child: set[str] = set()
    groups: dict[str, list[Feature]] = {}

    def _input(parent_ids: tuple[str, ...]) -> SceneSpec | None:
        specs = sorted((outputs[p] for p in parent_ids), key=steps.shape_sha)
        if not specs:
            return None
        return specs[0] if len(specs) == 1 else _union(specs)

    for f in features:
        if f.op not in FEATURE_OPS:
            raise BadInput(
                f"feature {f.id!r}: unknown op {f.op!r}",
                next=f"use one of {sorted(FEATURE_OPS)}",
            )
        leaf_parents = [p for p in f.parents if p in members]
        if leaf_parents:
            raise BadInput(
                f"feature {f.id!r} stacks on group member(s) {leaf_parents}; "
                "group members are leaves",
                next="parent it on the group's own parent instead",
            )
        has_child.update(f.parents)
        if f.group is not None:
            groups.setdefault(f.group, []).append(f)
            continue
        outputs[f.id] = FEATURE_OPS[f.op](_input(f.parents), **f.param_map())
        keys.append((f.id, _step_key(outputs[f.id])))

    sinks = [outputs[fid] for fid in outputs if fid not in has_child]
    for group, feats in groups.items():
        parents = {f.parents for f in feats}
        if len(parents) != 1:
            raise BadInput(
                f"group {group!r} members have different parents {sorted(parents)}",
                next="group members are siblings: give them one parent set",
            )
        feats.sort(
            key=lambda f: json.dumps(
                [f.op, f.param_map()], sort_keys=True, default=repr
            )
        )
        leaves = []
        for f in feats:
            leaf = FEATURE_OPS[f.op](None, **f.param_map())
            keys.append((f.id, _step_key(leaf)))
            leaves.append(leaf)
        sinks.append(_combine(_input(feats[0].parents), leaves, group))
    sinks.sort(key=steps.shape_sha)
    spec = sinks[0] if len(sinks) == 1 else _union(sinks)
    return Built(spec=spec, shape_sha=steps.shape_sha(spec), step_keys=tuple(keys))


__all__ = ["FEATURE_OPS", "Built", "Feature", "Recipe", "Snapshot", "build"]
