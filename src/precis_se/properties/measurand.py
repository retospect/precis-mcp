"""Measurand resolution for se measures — a taxon node to the three facts
a measure row snapshots (:class:`Measurand`).

The pure half (:func:`measurand_from_meta`) decides the **se unit** a
node's numbers are stored in; the store half (:func:`measurand_resolver`)
resolves a caller's reference through the taxon kind's own resolver
(:meth:`precis.handlers.taxon.TaxonHandler._resolve_spec` — numeric id,
``tn<id>``, ``taxon:<id>``, a slug/name/alias, or a slash path) and
refuses anything that is not under the ``measurand`` start node.

**Unit rule** (first match wins):

1. a categorical node (``value_type`` categorical/boolean/text, or
   ``dimension_kind='categorical'``) has no unit — ``''`` — and carries
   no numbers on an se measure (:func:`precis_se.ops._apply_measurand`
   refuses value/min/max/relation-source for it);
2. a pure length (``dimension_kind='si'``, ``si_vector`` ``1,0,0,0,0,0,0``)
   is ``'m'`` whatever its ``canonical_unit`` says — se stores lengths as
   float64 metres (the package docstring's one-unit decision), while a
   legacy registry's ``canonical_unit`` is a display unit (the seeded
   ``Length`` node says ``mm``);
3. otherwise the node's ``canonical_unit`` verbatim;
4. no ``canonical_unit``: ``dimension_kind`` ``count`` → ``'count'``,
   ``dimensionless`` → ``'ratio'`` (the legacy enum's words);
5. anything else is refused — se cannot store a number for a quantity it
   has no unit for.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from precis_se.measures import MeasureError

#: The start node every measurand must reach (core migration 0174).
MEASURAND_ROOT = "measurand"

_LENGTH_VECTOR = "1,0,0,0,0,0,0"
_CATEGORICAL_VALUE_TYPES = frozenset({"categorical", "boolean", "text"})


@dataclass(frozen=True)
class Measurand:
    """A resolved measurand: what a measure row snapshots (``ref_id`` →
    ``MeasureSpec.measurand_ref``, ``slug`` → ``MeasureSpec.measurand``,
    ``unit`` → ``MeasureSpec.unit``) plus what write-time vetting reads."""

    ref_id: int
    slug: str
    unit: str
    categorical: bool = False
    allowed_values: tuple[str, ...] = ()


#: A store-bound resolver: caller's reference → :class:`Measurand`, raising
#: :class:`~precis_se.measures.MeasureError` with the candidate list on an
#: unknown, ambiguous or non-measurand reference.
MeasurandResolver = Callable[[Any], Measurand]


def measurand_from_meta(ref_id: int, meta: dict[str, Any]) -> Measurand:
    """The pure half: a taxon node's ``meta`` → :class:`Measurand` under
    the module docstring's unit rule. Raises :class:`MeasureError` when
    no se unit applies."""
    slug = str(meta.get("slug") or "").strip()
    if not slug:
        raise MeasureError(f"taxon tn{ref_id} has no slug — not a usable measurand")
    kind = meta.get("dimension_kind")
    value_type = meta.get("value_type")
    if kind == "categorical" or value_type in _CATEGORICAL_VALUE_TYPES:
        allowed = meta.get("allowed_values") or []
        return Measurand(
            ref_id=ref_id,
            slug=slug,
            unit="",
            categorical=True,
            allowed_values=tuple(str(v) for v in allowed),
        )
    if kind == "si" and "".join(str(meta.get("si_vector") or "").split()) == (
        _LENGTH_VECTOR
    ):
        return Measurand(ref_id=ref_id, slug=slug, unit="m")
    unit = str(meta.get("canonical_unit") or "").strip()
    if unit:
        return Measurand(ref_id=ref_id, slug=slug, unit=unit)
    if kind == "count":
        return Measurand(ref_id=ref_id, slug=slug, unit="count")
    if kind == "dimensionless":
        return Measurand(ref_id=ref_id, slug=slug, unit="ratio")
    raise MeasureError(
        f"measurand {slug!r} (tn{ref_id}) has no canonical_unit and no "
        f"dimension se can store a number in (dimension_kind={kind!r}) — "
        "pick a measurand with a unit, or use unit= (m | count | ratio | deg)"
    )


def measurand_resolver(store: Any) -> MeasurandResolver:
    """Bind :func:`measurand_from_meta` to ``store`` through the taxon
    kind's resolver. Memoized per returned closure (one put/edit/get call
    resolves the same reference once); the taxon handler is built on
    first use, so wiring a resolver costs nothing for a call that names
    no measurand."""
    from precis.dispatch import Hub
    from precis.errors import PrecisError
    from precis.handlers.taxon import TaxonHandler

    cache: dict[str, Measurand] = {}
    handlers: list[TaxonHandler] = []

    def resolve(ref: Any) -> Measurand:
        if isinstance(ref, bool) or not isinstance(ref, str | int):
            raise MeasureError(
                f"measurand must be a taxon reference (slug, name, path, "
                f"tn<id> or id), got {ref!r}"
            )
        key = str(ref).strip()
        if not key:
            raise MeasureError("measurand must be a non-empty taxon reference")
        hit = cache.get(key)
        if hit is not None:
            return hit
        if not handlers:
            handlers.append(TaxonHandler(hub=Hub(store=store)))
        try:
            ref_id = handlers[0]._resolve_spec(key)
        except PrecisError as exc:
            nxt = exc.next
            hint = "; ".join(nxt) if isinstance(nxt, list) else (nxt or "")
            raise MeasureError(
                f"measurand {key!r}: {exc}" + (f" ({hint})" if hint else "")
            ) from exc
        node = store.get_ref(kind="taxon", id=ref_id)
        if node is None:
            raise MeasureError(f"measurand {key!r}: no live taxon tn{ref_id}")
        starts: set[str] = set()
        for start_id in store.taxon_start_nodes_reached(ref_id):
            start = store.get_ref(kind="taxon", id=start_id)
            if start is not None:
                starts.add(str((start.meta or {}).get("slug")))
        if MEASURAND_ROOT not in starts:
            raise MeasureError(
                f"measurand {key!r} resolved to tn{ref_id} "
                f"({node.title!r}), which is not under the "
                f"{MEASURAND_ROOT!r} start node (reaches: "
                f"{', '.join(sorted(starts)) or 'none'}) — a measure needs "
                "a measurable quantity; search(kind='taxon', "
                "under='measurand', q='...') lists them"
            )
        out = measurand_from_meta(ref_id, dict(node.meta or {}))
        cache[key] = out
        return out

    return resolve
