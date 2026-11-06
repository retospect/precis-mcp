"""Named, bounded parameters over a design recipe's feature attributes.

A convenience layer for continuous optimisers (se-flow-kernel slice 4b
§Design representation item 3): a :class:`Space` declares named dimensions
with bounds, optional feasibility constraints, and ``bindings`` from each
parameter to a ``<feature id>.<param>`` recipe attribute. It samples,
clips and validates :class:`Params`, and applies them to a recipe through
the duck-typed ``recipe.modify`` door — so a fixed-topology search can
drive the feature graph without a parameter vector of its own.

``Params`` is a frozen dataclass of sorted ``(name, value)`` pairs with the
:class:`~collections.abc.Mapping` interface, so
:func:`precis_geom.steps._pcanon` keys it as a step argument (and refuses a
bare ``MappingProxyType``: a script passes a ``Params``, not a raw proxy).
The feature-graph recipe and ``build`` are item 1; this module needs only
an object exposing ``modify(id, **params)``.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from precis.errors import BadInput


@runtime_checkable
class Rng(Protocol):
    """The draws a :class:`Space` needs — satisfied by ``np.random.Generator``."""

    def uniform(self, low: float, high: float) -> Any: ...

    def integers(self, low: int, high: int, *, endpoint: bool = False) -> Any: ...


@dataclass(frozen=True)
class Params(Mapping[str, Any]):
    """An immutable point in a :class:`Space`: sorted ``(name, value)`` pairs.

    Equality and hashing are by the sorted pairs, so two points with the same
    contents are one key. Construct from a mapping with :meth:`of`.
    """

    pairs: tuple[tuple[str, Any], ...] = ()

    @classmethod
    def of(cls, values: Mapping[str, Any]) -> Params:
        """Freeze a mapping into sorted pairs."""
        return cls(tuple(sorted(values.items())))

    def __getitem__(self, name: str) -> Any:
        for key, value in self.pairs:
            if key == name:
                return value
        raise KeyError(name)

    def __iter__(self) -> Iterator[str]:
        return (name for name, _ in self.pairs)

    def __len__(self) -> int:
        return len(self.pairs)


class Dim(Protocol):
    """One named dimension: how to sample, clip to the feasible range, and
    reject a value that is out of range or the wrong type."""

    def sample(self, rng: Rng) -> Any: ...

    def clip(self, value: Any) -> Any: ...

    def validate(self, name: str, value: Any) -> None: ...


def _refuse(name: str, reason: str) -> None:
    raise BadInput(
        f"parameter {name!r} {reason}",
        next=f"set {name} within its declared range, or clip() first",
    )


@dataclass(frozen=True)
class Continuous:
    """A real parameter in ``[lo, hi]``."""

    lo: float
    hi: float

    def __post_init__(self) -> None:
        if not (math.isfinite(self.lo) and math.isfinite(self.hi)):
            raise BadInput(
                f"Continuous bounds must be finite, got ({self.lo}, {self.hi})"
            )
        if self.lo > self.hi:
            raise BadInput(f"Continuous lo {self.lo} exceeds hi {self.hi}")

    def sample(self, rng: Rng) -> float:
        return float(rng.uniform(self.lo, self.hi))

    def clip(self, value: Any) -> float:
        return float(min(max(float(value), self.lo), self.hi))

    def validate(self, name: str, value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _refuse(name, f"must be a real number, got {value!r}")
        x = float(value)
        if not math.isfinite(x):
            _refuse(name, f"must be finite, got {value!r}")
        if not (self.lo <= x <= self.hi):
            _refuse(name, f"= {x} is outside [{self.lo}, {self.hi}]")


@dataclass(frozen=True)
class Integer:
    """An integer parameter in ``[lo, hi]`` inclusive; :meth:`clip` rounds."""

    lo: int
    hi: int

    def __post_init__(self) -> None:
        if isinstance(self.lo, bool) or isinstance(self.hi, bool):
            raise BadInput("Integer bounds must be ints, not bools")
        if self.lo > self.hi:
            raise BadInput(f"Integer lo {self.lo} exceeds hi {self.hi}")

    def sample(self, rng: Rng) -> int:
        return int(rng.integers(self.lo, self.hi, endpoint=True))

    def clip(self, value: Any) -> int:
        return int(min(max(round(float(value)), self.lo), self.hi))

    def validate(self, name: str, value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            _refuse(name, f"must be an int, got {value!r}")
        if not (self.lo <= value <= self.hi):
            _refuse(name, f"= {value} is outside [{self.lo}, {self.hi}]")


@dataclass(frozen=True)
class Boolean:
    """A boolean parameter."""

    def sample(self, rng: Rng) -> bool:
        return bool(int(rng.integers(0, 1, endpoint=True)))

    def clip(self, value: Any) -> bool:
        return bool(value)

    def validate(self, name: str, value: Any) -> None:
        if not isinstance(value, bool):
            _refuse(name, f"must be a bool, got {value!r}")


@dataclass(frozen=True)
class Space:
    """Named dimensions with bounds, feasibility constraints and recipe bindings.

    ``constraints`` are ``g(params) <= 0`` predicates (feasible when at or
    below zero). ``bindings`` map a parameter name to a ``<feature id>.<param>``
    recipe attribute; :meth:`apply` drives ``recipe.modify`` per binding, so
    only bound parameters touch the recipe.
    """

    dims: Mapping[str, Dim]
    constraints: Sequence[Any] = ()
    bindings: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        unknown = sorted(set(self.bindings) - set(self.dims))
        if unknown:
            raise BadInput(
                f"bindings name parameters not in the space: {unknown}",
                next=f"declare {unknown} as dims, or drop the binding",
            )
        for name, target in self.bindings.items():
            fid, dot, attr = target.partition(".")
            if not dot or not fid.strip() or not attr.strip():
                raise BadInput(
                    f"binding {name!r} -> {target!r} must be '<feature id>.<param>'",
                    next="write the binding as e.g. 'h7.d'",
                )

    def sample(self, rng: Rng) -> Params:
        """A uniform draw of every dimension."""
        return Params.of({name: dim.sample(rng) for name, dim in self.dims.items()})

    def clip(self, params: Mapping[str, Any]) -> Params:
        """Each declared dimension clipped to its range (``Integer`` rounds);
        any extra key is carried through unchanged."""
        out = dict(params)
        for name, dim in self.dims.items():
            if name in out:
                out[name] = dim.clip(out[name])
        return Params.of(out)

    def validate(self, params: Mapping[str, Any]) -> None:
        """Refuse a missing, out-of-range or wrong-type parameter naming it,
        then any infeasible constraint. Returns ``None`` when feasible."""
        for name, dim in self.dims.items():
            if name not in params:
                _refuse(name, "is missing from the point")
            dim.validate(name, params[name])
        for i, g in enumerate(self.constraints):
            value = float(g(params))
            if value > 0.0:
                label = getattr(g, "__name__", None) or f"#{i}"
                raise BadInput(
                    f"constraint {label} is infeasible: g(params) = {value} > 0",
                    next="adjust the parameters until every constraint is <= 0",
                )

    def apply(self, recipe: Any, params: Mapping[str, Any]) -> None:
        """Write every bound parameter onto the recipe via ``recipe.modify``."""
        for name, target in self.bindings.items():
            fid, _, attr = target.partition(".")
            recipe.modify(fid, **{attr: params[name]})


__all__ = [
    "Boolean",
    "Continuous",
    "Dim",
    "Integer",
    "Params",
    "Rng",
    "Space",
]
