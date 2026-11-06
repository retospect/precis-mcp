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

This module is the recipe layer only — pure Python, no cad or step-cache
dependency. ``build(recipe) -> Built`` (the ``SceneSpec`` product,
``shape_sha`` over :func:`precis_geom.steps._digest_spec`, ``Built.solid()``
and the region-change report) is the build layer and lands on top of this.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from precis.errors import BadInput

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
    return tuple(sorted(params.items()))


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
        return dict(self.params)


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
        """Features in insertion order."""
        return tuple(self._features.values())

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
        return feat

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
        self._features = {f.id: f for f in snap.features}
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
                    "params": dict(f.params),
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


__all__ = ["Feature", "Recipe", "Snapshot"]
