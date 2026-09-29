"""The symbolic chain solver with a stub geometry backend (SPEC 22.3,
SPEC 28 step 4; ``docs/backlog/hexfold-integration.md`` ruling step 4).

The LLM composes constraints, never geometry: *a capped tube on a sheet,
a spacer, a wider tube* is an ordered **chain** of parts sharing one
axis.  Every adjacency is one interface constraint -- this part's top rim
equals the next part's bottom rim (an integer equality on the dangling
count ``N``; SPEC 10: a fuse checks ``N`` only, zigzag onto armchair at
equal ``N`` is the 30° grain-boundary adapter and is *reported*, not
refused).  Pin either end and propagation resolves the middle; spacers
are the slack.

This module is the layer *above* the ``.hx`` text: a :class:`Part` is an
abstract record -- a kind, a roll-up that is pinned, a domain, or a
don't-care (the backend's catalogue), an axial length that is pinned
whole periods, free whole periods, or (spacers only) a real band -- and
never a hexfold instance.  A resolved se block becomes a pinned part
through :func:`part_from_payloads` from its typed port payloads (the
``{"kind": "rim", "N", "type"}`` the generator stamps on every
``GeneratedPort``), so blocks from different generators compose through
their ports alone.  Geometry questions go to a :class:`GeometryBackend`;
the :class:`StubBackend` answers from tables (``N = n + m``, ``6k``,
period pitch from the chiral indices) with no probe build.  That is the
interface claim SPEC 28.4 asks to prove before the chemistry is good:
every later swap -- the smooth layer, collars, sp³ seams, the step-5
block joiner -- is a backend or a part record; the solver never changes.

Solving is two passes.  **Rims**: arc consistency on ``N`` over the
adjacencies from the pinned ends, to a fixpoint (the same shape as
:mod:`hexfold.domains`, which does it inside one spec); an emptied domain
is ``chain.unsolvable`` naming the part, the constraint, what it needed
and what the domain offered.  **Lengths**: for every surviving roll-up
combination, the free whole periods and the spacer bands are searched for
the assignment whose total lands nearest the length wish (target ±
band); a wish nobody can reach is ``chain.length`` with the nearest
total.  Solutions rank by length deviation, then adapter count
(all-same-type seams beat 5-7 adapters), then total periods.  Tube
rotation steps (``C_g`` phase, the fuse ``k``) affect neither ``N`` nor
length and are not chain variables.  Integer and stdlib only: no numpy,
no ``hexfold.text`` -- the layer table in SPEC 2 says so.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field, replace
from math import gcd
from typing import Any, Protocol

from . import __version__
from .report import Finding, Severity

Value = tuple[int, int]

#: Kinds whose axial length is whole periods of a lattice pitch.
PERIODIC_KINDS: frozenset[str] = frozenset({"tube"})
#: The kind whose length is a real band, not periods: the chain's slack.
SPACER = "spacer"
#: An opaque pinned part: rims and length come from the record itself
#: (:func:`part_from_payloads`), never from the backend.
BLOCK = "block"
#: A ``@``-site destination: the hexagon hole the builder mints on a
#: sheet or wall (``hexfold.domains._MINTED_HOLE_N``).
HOLE = "hole"
HOLE_N = 6

#: Cap on the roll-up combinations the length pass enumerates; above it
#: the solver reports the propagated domains and ``chain.too_many``.
MAX_COMBINATIONS = 512
#: Cap on the free-period assignments per roll-up combination.
MAX_PERIOD_ASSIGNMENTS = 4096

_SIGMA_DEFAULT_A = 1.42
_A_DEFAULT = math.sqrt(3.0) * _SIGMA_DEFAULT_A


class ChainError(ValueError):
    """A malformed chain: unknown kind, a spacer at an end, a pinned
    end that is not a rim, two adjacent spacers."""


@dataclass(frozen=True)
class Rim:
    """One rim as the solver sees it: dangling count and SPEC 10 type
    (``"z"`` zigzag, ``"a"`` armchair, ``None`` mixed)."""

    N: int
    type: str | None = None

    def __str__(self) -> str:
        return f"{self.type or 'x'}{self.N}"

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Rim:
        """From a ``GeneratedPort.payload`` ``{"kind": "rim", "N", "type"}``
        (``type`` ``"z12"`` / ``"a10"`` / ``None``)."""
        if payload.get("kind") != "rim":
            raise ChainError(f"not a rim payload: {payload!r}")
        t = payload.get("type")
        return cls(int(payload["N"]), None if not t else str(t)[0])


@dataclass(frozen=True)
class Part:
    """One link of the chain (module docstring).

    ``value`` pinned, else ``domain`` (a tuple of roll-ups), else the
    backend catalogue for ``kind``.  ``periods`` pinned, else free in
    ``periods_range`` (periodic kinds only).  A spacer has ``length_A``
    as ``(min, max)``; a ``block`` carries its own ``rims`` and a fixed
    ``length_A``.  ``bottom``/``top`` name the two axial ports.
    """

    name: str
    kind: str
    value: Value | None = None
    domain: tuple[Value, ...] | None = None
    periods: int | None = None
    periods_range: tuple[int, int] = (1, 40)
    #: spacer: ``(min_A, max_A)``; block: a fixed length; else unused
    length_A: tuple[float, float] | float | None = None
    rims: dict[str, Rim] = field(default_factory=dict)
    bottom: str = "in"
    top: str = "out"

    @property
    def pinned(self) -> bool:
        return self.kind in (BLOCK, HOLE, SPACER) or self.value is not None


def part_from_payloads(
    name: str,
    bottom: dict[str, Any] | None,
    top: dict[str, Any] | None,
    length_A: float,
    *,
    bottom_name: str = "in",
    top_name: str = "out",
) -> Part:
    """A pinned opaque part from a resolved block's typed port payloads
    (``GeneratedPort.payload``) and its axial length (a ``<inst>_len``
    measure, or whatever the block knows).  A ``None`` payload is a
    closed end (no rim on that side)."""
    rims: dict[str, Rim] = {}
    if bottom is not None:
        rims[bottom_name] = Rim.from_payload(bottom)
    if top is not None:
        rims[top_name] = Rim.from_payload(top)
    return Part(
        name,
        BLOCK,
        length_A=float(length_A),
        rims=rims,
        bottom=bottom_name,
        top=top_name,
    )


# ---------- geometry backend ----------


class GeometryBackend(Protocol):
    """What the solver asks of a shape language.  Everything else is the
    solver's own arithmetic."""

    def catalogue(self, kind: str) -> tuple[Value, ...]:
        """Roll-ups a don't-care ``kind`` may take."""
        ...

    def rim(self, kind: str, value: Value, port: str) -> Rim | None:
        """The rim on ``port`` of ``kind`` rolled up as ``value``; ``None``
        when there is no such rim (a cap's top, a lid that does not
        exist)."""
        ...

    def pitch_A(self, kind: str, value: Value) -> float | None:
        """Axial length of one period; ``None`` when ``kind`` has no
        periodic length."""
        ...

    def fixed_length_A(self, kind: str, value: Value) -> float:
        """Axial length the part contributes regardless of periods."""
        ...


def _dr(n: int, m: int) -> int:
    return gcd(2 * n + m, 2 * m + n)


def _translation_vector(n: int, m: int) -> tuple[int, int]:
    d = _dr(n, m)
    return ((2 * m + n) // d, -(2 * n + m) // d)


def _metric_dot(p: tuple[int, int], q: tuple[int, int]) -> float:
    return p[0] * q[0] + p[1] * q[1] + 0.5 * (p[0] * q[1] + p[1] * q[0])


@dataclass(frozen=True)
class StubBackend:
    """Rims and rough lengths from tables and the chiral-index formulas
    (``hexfold.lattice`` restated without numpy; pinned equal by test):

    - ``tube(n,m)``: ``in``/``out`` rims ``N = n + m``, type ``z`` for
      ``m = 0``, ``a`` for ``n = m``, mixed otherwise; pitch ``|T|`` with
      ``T`` the translation vector; no fixed length.
    - ``cap(5,5)``: ``in`` rim ``a10``, fixed length the (5,5) radius (a
      hemisphere); ``cap(6k,0)`` flat lid: ``in`` rim ``z6k``, length 0.
    - ``hole``: ``N = 6`` both sides, length 0 (a wall the axis passes).
    - ``spacer``: pass-through, length from the part record.
    """

    a_A: float = _A_DEFAULT

    def catalogue(self, kind: str) -> tuple[Value, ...]:
        if kind == "tube":
            return tuple((n, 0) for n in range(3, 35)) + tuple(
                (n, n) for n in range(3, 35)
            )
        if kind == "cap":
            return ((5, 5),) + tuple((6 * k, 0) for k in range(1, 11))
        return ()

    def rim(self, kind: str, value: Value, port: str) -> Rim | None:
        n, m = value
        if kind == "tube":
            if port not in ("in", "out"):
                return None
            return Rim(n + m, "z" if m == 0 else "a" if n == m else None)
        if kind == "cap":
            if port != "in":
                return None
            if (n, m) == (5, 5):
                return Rim(10, "a")
            if m == 0 and n % 6 == 0 and n > 0:
                return Rim(n, "z")
            return None
        return None

    def pitch_A(self, kind: str, value: Value) -> float | None:
        if kind != "tube":
            return None
        t = _translation_vector(*value)
        return self.a_A * math.sqrt(_metric_dot(t, t))

    def fixed_length_A(self, kind: str, value: Value) -> float:
        if kind == "cap" and value == (5, 5):
            n, m = value
            return self.a_A * math.sqrt(n * n + n * m + m * m) / (2.0 * math.pi)
        return 0.0


class _CatalogueRead(Protocol):
    """The one method :class:`CachedBackend` needs from a
    ``hexfold.catalogue.CatalogueStore`` -- restated here (not imported)
    so this module stays numpy-free at import time; the real store type
    and ``EnvKey`` are only touched lazily, inside the methods below, when
    a caller actually asks a question (SPEC §26 "fill and read", (b))."""

    def get(self, key: Any) -> Any: ...


class CachedBackend:
    """:class:`GeometryBackend` over a catalogue store: ``rim``/
    ``catalogue`` (the roll-up domain) delegate straight to ``fallback``
    (the solver's rim/catalogue questions are combinatorial, not
    physical -- a measured row never changes them); ``pitch_A`` prefers a
    measured ``BulkCell`` row when one exists for ``(kind, value)`` under
    this backend's ``rung``/``relaxer``, falling back to ``fallback``
    otherwise (the gap a measured row closes: the geo rung's rest length
    1.52 A vs. sigma 1.42 A makes :class:`StubBackend`'s pitch ~7% short
    on that rung).  ``fixed_length_A`` has no bulk-cell equivalent (a
    tube's fixed length is always 0 in every backend so far) and always
    delegates.

    The constructor parameter is named ``catalogue`` (the store) even
    though :class:`GeometryBackend.catalogue` is a *method* name (the
    roll-up domain) -- no collision: the store lives under a private
    attribute, so ``self.catalogue(kind)`` calls the method, never shadows
    it with the constructor argument."""

    def __init__(
        self,
        catalogue: _CatalogueRead,
        fallback: GeometryBackend | None = None,
        rung: str = "stick",
        relaxer: str | None = None,
    ) -> None:
        self._store = catalogue
        self._fallback: GeometryBackend = fallback if fallback is not None else StubBackend()
        self._rung = rung
        self._relaxer = relaxer

    def catalogue(self, kind: str) -> tuple[Value, ...]:
        return self._fallback.catalogue(kind)

    def rim(self, kind: str, value: Value, port: str) -> Rim | None:
        return self._fallback.rim(kind, value, port)

    def pitch_A(self, kind: str, value: Value) -> float | None:
        row = self._bulk_row(kind, value)
        if row is not None:
            return float(row.pitch_A)
        return self._fallback.pitch_A(kind, value)

    def fixed_length_A(self, kind: str, value: Value) -> float:
        return self._fallback.fixed_length_A(kind, value)

    def _bulk_row(self, kind: str, value: Value) -> Any:
        if kind != "tube":
            return None
        from .catalogue import EnvKey  # lazy: keeps this module numpy-free at import

        relaxer = self._relaxer if self._relaxer is not None else f"{self._rung}@{__version__}"
        key = EnvKey(zone="bulk", kind=kind, nm=value, rung=self._rung, relaxer=relaxer)
        return self._store.get(key)


# ---------- results ----------


@dataclass(frozen=True)
class Assignment:
    part: str
    value: Value | None
    periods: int | None
    length_A: float
    bottom: Rim | None
    top: Rim | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "part": self.part,
            "value": None if self.value is None else list(self.value),
            "periods": self.periods,
            "length_A": round(self.length_A, 4),
            "bottom": None if self.bottom is None else str(self.bottom),
            "top": None if self.top is None else str(self.top),
        }


@dataclass(frozen=True)
class Solution:
    assignments: tuple[Assignment, ...]
    total_A: float
    #: signed distance of ``total_A`` from the wish target (0 without a wish)
    deviation_A: float
    #: ``(lower part, upper part, lower rim, upper rim)`` per z/a seam
    adapters: tuple[tuple[str, str, Rim, Rim], ...]

    @property
    def periods(self) -> int:
        return sum(a.periods or 0 for a in self.assignments)

    def rank_key(self) -> tuple[float, int, int]:
        return (abs(self.deviation_A), len(self.adapters), self.periods)

    def to_dict(self) -> dict[str, Any]:
        return {
            "assignments": [a.to_dict() for a in self.assignments],
            "total_A": round(self.total_A, 4),
            "deviation_A": round(self.deviation_A, 4),
            "adapters": [[lo, hi, str(a), str(b)] for lo, hi, a, b in self.adapters],
        }


@dataclass(frozen=True)
class Conflict:
    part: str
    needs: tuple[int, ...]
    offers: tuple[int, ...]
    constraint: str


@dataclass
class ChainResult:
    """Domains before/after the rim pass, what pruned each, the first rim
    conflict, the ranked solutions of the length pass and the findings
    that narrate both."""

    before: dict[str, tuple[Value, ...]]
    after: dict[str, tuple[Value, ...]]
    pruned_by: dict[str, list[str]] = field(default_factory=dict)
    conflict: Conflict | None = None
    solutions: list[Solution] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.conflict is None and bool(self.solutions)

    @property
    def best(self) -> Solution | None:
        return self.solutions[0] if self.solutions else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "domains": {k: [list(v) for v in vals] for k, vals in self.after.items()},
            "solutions": [s.to_dict() for s in self.solutions],
            "findings": [f.to_dict() for f in self.findings],
        }


# ---------- the solver ----------

_End = tuple[str, str] | int


def _validate(parts: list[Part], backend: GeometryBackend) -> None:
    if not parts:
        raise ChainError("empty chain")
    names = [p.name for p in parts]
    if len(set(names)) != len(names):
        raise ChainError(f"duplicate part names: {names}")
    for i, p in enumerate(parts):
        if p.kind == SPACER:
            if i in (0, len(parts) - 1):
                raise ChainError(f"{p.name}: a spacer cannot end the chain")
            if parts[i - 1].kind == SPACER:
                raise ChainError(f"{p.name}: two adjacent spacers -- merge them")
            if not (isinstance(p.length_A, tuple) and len(p.length_A) == 2):
                raise ChainError(f"{p.name}: a spacer needs length_A=(min_A, max_A)")
            continue
        if p.kind == BLOCK:
            if not isinstance(p.length_A, int | float):
                raise ChainError(f"{p.name}: a block needs a fixed length_A")
            continue
        if p.kind == HOLE:
            continue
        if p.value is None and p.domain is None and not backend.catalogue(p.kind):
            raise ChainError(f"{p.name}: unknown kind {p.kind!r} with no domain")
        if p.kind in PERIODIC_KINDS and p.periods is None:
            lo, hi = p.periods_range
            if lo < 1 or hi < lo:
                raise ChainError(f"{p.name}: periods_range {p.periods_range} is empty")


def _rim_of(
    p: Part, value: Value | None, port: str, backend: GeometryBackend
) -> Rim | None:
    if p.kind == HOLE:
        return Rim(HOLE_N, None)
    if p.kind == BLOCK:
        return p.rims.get(port)
    if value is None:
        return None
    return backend.rim(p.kind, value, port)


def _candidates(
    p: Part, doms: dict[str, tuple[Value, ...]]
) -> tuple[Value | None, ...]:
    return doms.get(p.name, (None,))


def _orient_ends(
    parts: list[Part], doms: dict[str, tuple[Value, ...]], backend: GeometryBackend
) -> list[Part]:
    """Face a single-rim end part into the chain: a cap (or a block with
    one rim) written first has its rim on top, written last has it at the
    bottom.  Only the two ends are touched, only when the facing side has
    no rim on any candidate and the other side has one."""
    if len(parts) < 2:
        return parts
    out = list(parts)

    def has(p: Part, port: str) -> bool:
        return any(
            _rim_of(p, v, port, backend) is not None for v in _candidates(p, doms)
        )

    first, last = out[0], out[-1]
    if first.kind != SPACER and not has(first, first.top) and has(first, first.bottom):
        out[0] = replace(first, bottom=first.top, top=first.bottom)
    if last.kind != SPACER and not has(last, last.bottom) and has(last, last.top):
        out[-1] = replace(last, bottom=last.top, top=last.bottom)
    return out


def solve(
    parts: list[Part],
    *,
    backend: GeometryBackend | None = None,
    bottom: Rim | int | None = None,
    top: Rim | int | None = None,
    wish_A: tuple[float, float] | None = None,
) -> ChainResult:
    """Solve the chain (module docstring).

    ``bottom``/``top`` pin the chain's outer rims (a :class:`Rim` or a
    bare ``N``); ``wish_A`` is ``(target, band)`` on the total axial
    length.  Raises :class:`ChainError` for a malformed chain; every
    unsatisfiable *constraint* is a finding, never an exception.
    """
    backend = backend or StubBackend()
    _validate(parts, backend)
    by_name = {p.name: p for p in parts}

    # domains: pinned parts are singletons; don't-care takes the catalogue
    doms: dict[str, tuple[Value, ...]] = {}
    for p in parts:
        if p.kind in (SPACER, BLOCK, HOLE):
            continue
        if p.value is not None:
            doms[p.name] = (p.value,)
        elif p.domain is not None:
            if not p.domain:
                raise ChainError(f"{p.name}: empty domain")
            doms[p.name] = tuple(dict.fromkeys(p.domain))
        else:
            doms[p.name] = backend.catalogue(p.kind)
    parts = _orient_ends(parts, doms, backend)
    by_name = {p.name: p for p in parts}
    free = {n for n, vals in doms.items() if len(vals) > 1}
    result = ChainResult(
        before={n: doms[n] for n in free}, after={n: doms[n] for n in free}
    )

    def end_of(p: Part, port: str) -> _End | None:
        if p.name in doms:
            if p.name in free:
                return (p.name, port)
            r = _rim_of(p, doms[p.name][0], port, backend)
            return None if r is None else r.N
        r = _rim_of(p, None, port, backend)
        return None if r is None else r.N

    # adjacencies, spacers transparent
    eqs: list[tuple[str, Part, str, Part, str]] = []
    solid = [p for p in parts if p.kind != SPACER]
    for lo, hi in itertools.pairwise(solid):
        eqs.append(
            (f"{lo.name}.{lo.top} == {hi.name}.{hi.bottom}", lo, lo.top, hi, hi.bottom)
        )

    pinned_ends: list[tuple[str, Part, str, int]] = []
    for label, p, port, pin in (
        ("bottom", solid[0], solid[0].bottom, bottom),
        ("top", solid[-1], solid[-1].top, top),
    ):
        if pin is None:
            continue
        n = pin.N if isinstance(pin, Rim) else int(pin)
        pinned_ends.append((f"chain.{label} == {p.name}.{port}", p, port, n))

    def allowed(end: _End) -> set[int]:
        if isinstance(end, int):
            return {end}
        name, port = end
        p = by_name[name]
        return {
            r.N for v in doms[name] if (r := _rim_of(p, v, port, backend)) is not None
        }

    def offers_of(name: str, port: str) -> tuple[int, ...]:
        p = by_name[name]
        return tuple(
            sorted(
                {
                    r.N
                    for v in doms[name]
                    if (r := _rim_of(p, v, port, backend)) is not None
                }
            )
        )

    arcs: list[tuple[str, _End, _End]] = []
    for label, lo, lport, hi, hport in eqs:
        ea, eb = end_of(lo, lport), end_of(hi, hport)
        if ea is None or eb is None:
            missing = lo.name if ea is None else hi.name
            result.findings.append(
                Finding(
                    "chain.no_rim",
                    Severity.ERROR,
                    f"{label}: {missing} has no rim on that side",
                    where=missing,
                )
            )
            continue
        arcs.append((label, ea, eb))
    for label, p, port, n in pinned_ends:
        e = end_of(p, port)
        if e is None:
            result.findings.append(
                Finding(
                    "chain.no_rim",
                    Severity.ERROR,
                    f"{label}: {p.name} has no rim on that side",
                    where=p.name,
                )
            )
            continue
        arcs.append((label, e, n))

    # two pinned rims: a plain mismatch, reported and fatal
    for label, ea, eb in arcs:
        if isinstance(ea, int) and isinstance(eb, int) and ea != eb:
            result.findings.append(
                Finding(
                    "chain.mismatch",
                    Severity.ERROR,
                    f"{label}: {ea} != {eb} (both pinned)",
                    data=(("needs", ea), ("offers", eb)),
                )
            )
    if any(f.severity == Severity.ERROR for f in result.findings):
        return result

    # arc consistency to a fixpoint
    changed = True
    while changed:
        changed = False
        for label, ea, eb in arcs:
            for me, other in ((ea, eb), (eb, ea)):
                if isinstance(me, int):
                    continue
                name, port = me
                ok = allowed(other)
                p = by_name[name]
                before = doms[name]
                offers = offers_of(name, port)
                kept = tuple(
                    v
                    for v in before
                    if (r := _rim_of(p, v, port, backend)) is not None and r.N in ok
                )
                if len(kept) == len(before):
                    continue
                doms[name] = kept
                labels = result.pruned_by.setdefault(name, [])
                if label not in labels:
                    labels.append(label)
                changed = True
                if not kept:
                    result.after = {n: doms[n] for n in free}
                    result.conflict = Conflict(name, tuple(sorted(ok)), offers, label)
                    result.findings.append(
                        Finding(
                            "chain.unsolvable",
                            Severity.ERROR,
                            f"{name}: {label} needs N in {sorted(ok)} but the domain "
                            f"offered {list(offers)}",
                            where=name,
                            data=(
                                ("needs", list(sorted(ok))),
                                ("offers", list(offers)),
                                ("constraint", label),
                            ),
                            fix="widen the domain, or pin the other end differently",
                        )
                    )
                    return result
    result.after = {n: doms[n] for n in free}
    for name in free:
        if len(doms[name]) < len(result.before[name]):
            result.findings.append(
                Finding(
                    "chain.propagated",
                    Severity.INFO,
                    f"{name}: {len(result.before[name])} -> {len(doms[name])} roll-ups "
                    f"({'; '.join(result.pruned_by.get(name, []))})",
                    where=name,
                    data=(
                        ("before", [list(v) for v in result.before[name]]),
                        ("after", [list(v) for v in doms[name]]),
                    ),
                )
            )

    # ---- length pass over the surviving product ----
    combos = 1
    for vals in doms.values():
        combos *= len(vals)
    if combos > MAX_COMBINATIONS:
        result.findings.append(
            Finding(
                "chain.too_many",
                Severity.WARN,
                f"{combos} roll-up combinations survive propagation (cap "
                f"{MAX_COMBINATIONS}); pin an end or narrow a domain",
                data=(("combinations", combos),),
            )
        )
        return result

    names = list(doms)
    target, band = wish_A if wish_A is not None else (0.0, 0.0)
    nearest: Solution | None = None
    for choice in itertools.product(*(doms[n] for n in names)):
        values = dict(zip(names, choice))
        sol = _solve_lengths(
            parts,
            values,
            backend,
            target,
            band,
            wish_A is not None,
            ends=(
                bottom if isinstance(bottom, Rim) else None,
                top if isinstance(top, Rim) else None,
            ),
        )
        if sol is None:
            continue
        if wish_A is None or abs(sol.deviation_A) <= band + 1e-9:
            result.solutions.append(sol)
        elif nearest is None or abs(sol.deviation_A) < abs(nearest.deviation_A):
            nearest = sol
    result.solutions.sort(key=Solution.rank_key)
    if not result.solutions:
        if nearest is None:
            result.findings.append(
                Finding(
                    "chain.length",
                    Severity.ERROR,
                    "no roll-up combination survives with a rim on every side",
                )
            )
        else:
            result.findings.append(
                Finding(
                    "chain.length",
                    Severity.ERROR,
                    f"no assignment lands in {target:g} ± {band:g} A; nearest total "
                    f"{nearest.total_A:.2f} A ({nearest.deviation_A:+.2f} A)",
                    data=(("nearest", nearest.to_dict()),),
                    fix="widen the band, free a tube's periods, or add a spacer",
                )
            )
        return result
    best = result.solutions[0]
    for lower, upper, a, b in best.adapters:
        result.findings.append(
            Finding(
                "chain.adapter",
                Severity.INFO,
                f"{lower} -> {upper}: {a} onto {b} is the 30° grain-boundary adapter (5-7 seam)",
                where=upper,
            )
        )
    result.findings.append(
        Finding(
            "chain.solved",
            Severity.INFO,
            f"{len(result.solutions)} solution(s); best total {best.total_A:.2f} A"
            + (f" ({best.deviation_A:+.2f} A from {target:g})" if wish_A else "")
            + ", "
            + ", ".join(
                f"{a.part}={'x'.join(map(str, a.value)) if a.value else a.part}"
                + (f" len={a.periods}" if a.periods is not None else "")
                for a in best.assignments
            ),
        )
    )
    return result


def _solve_lengths(
    parts: list[Part],
    values: dict[str, Value],
    backend: GeometryBackend,
    target: float,
    band: float,
    wished: bool,
    ends: tuple[Rim | None, Rim | None] = (None, None),
) -> Solution | None:
    """One roll-up combination: pick free periods and spacer lengths so the
    total lands nearest ``target``.  ``None`` when a part has no rim it
    needs (the combination is not buildable).  ``ends`` are the pinned
    outer rims, so a z/a seam against a pinned end counts as an adapter
    too."""
    fixed = 0.0
    free_periods: list[
        tuple[int, float, tuple[int, int]]
    ] = []  # (part idx, pitch, range)
    spacers: list[tuple[int, float, float]] = []
    lengths: list[float] = []
    for i, p in enumerate(parts):
        v = values.get(p.name)
        if p.kind == SPACER:
            assert isinstance(p.length_A, tuple)
            lo, hi = p.length_A
            spacers.append((i, float(lo), float(hi)))
            lengths.append(float(lo))
            continue
        if p.kind == BLOCK:
            assert isinstance(p.length_A, int | float)
            length = float(p.length_A)
            fixed += length
            lengths.append(length)
            continue
        if p.kind == HOLE or v is None:
            lengths.append(0.0)
            continue
        length = backend.fixed_length_A(p.kind, v)
        pitch = backend.pitch_A(p.kind, v)
        if pitch is not None:
            if p.periods is not None:
                length += p.periods * pitch
            else:
                free_periods.append((i, pitch, p.periods_range))
        fixed += length
        lengths.append(length)

    spacer_min = sum(lo for _, lo, _ in spacers)
    spacer_max = sum(hi for _, _, hi in spacers)

    def deviation(periodic: float) -> tuple[float, float]:
        """(signed deviation, spacer total) for a periodic contribution."""
        base = fixed + periodic
        if not wished:
            return 0.0, spacer_min
        want = target - base
        spacer_total = min(max(want, spacer_min), spacer_max)
        return base + spacer_total - target, spacer_total

    best: tuple[tuple[float, int], dict[int, int], float] | None = None
    ranges = [range(lo, hi + 1) for _, _, (lo, hi) in free_periods]
    count = 1
    for r in ranges:
        count *= len(r)
    if count > MAX_PERIOD_ASSIGNMENTS:
        # coarse: the nearest whole-period count per free tube, independently
        ranges = []
        for _, pitch, (lo, hi) in free_periods:
            share = (
                (target - fixed - spacer_min) / max(1, len(free_periods))
                if wished
                else 0.0
            )
            k = min(max(round(share / pitch), lo), hi)
            ranges.append(range(max(lo, k - 2), min(hi, k + 2) + 1))
    for ks in itertools.product(*ranges) if free_periods else [()]:
        periodic = sum(k * pitch for k, (_, pitch, _) in zip(ks, free_periods))
        dev, spacer_total = deviation(periodic)
        key = (abs(dev), sum(ks))
        if best is None or key < best[0]:
            best = (key, {i: k for k, (i, _, _) in zip(ks, free_periods)}, spacer_total)
        if not wished:
            break
    if best is None:
        return None
    _key, ks_by_idx, spacer_total = best
    for i, pitch, _ in free_periods:
        lengths[i] += ks_by_idx[i] * pitch
    # spacer slack shared in proportion to each spacer's own band
    if spacers:
        room = spacer_max - spacer_min
        extra = spacer_total - spacer_min
        for i, lo, hi in spacers:
            lengths[i] = lo + (extra * (hi - lo) / room if room > 0 else 0.0)

    assignments: list[Assignment] = []
    for i, p in enumerate(parts):
        v = values.get(p.name)
        periods = p.periods if p.kind in PERIODIC_KINDS else None
        if p.kind in PERIODIC_KINDS and p.periods is None:
            periods = ks_by_idx.get(i)
        assignments.append(
            Assignment(
                p.name,
                v,
                periods,
                lengths[i],
                _rim_of(p, v, p.bottom, backend) if p.kind != SPACER else None,
                _rim_of(p, v, p.top, backend) if p.kind != SPACER else None,
            )
        )
    solid = [a for p, a in zip(parts, assignments) if p.kind != SPACER]
    adapters: list[tuple[str, str, Rim, Rim]] = []
    for la, ha in itertools.pairwise(solid):
        if la.top is None or ha.bottom is None:
            return None
        if la.top.N != ha.bottom.N:
            return None
        if la.top.type and ha.bottom.type and la.top.type != ha.bottom.type:
            adapters.append((la.part, ha.part, la.top, ha.bottom))
    end_bottom, end_top = ends
    first, last = solid[0], solid[-1]
    if end_bottom is not None and first.bottom is not None and end_bottom.type:
        if first.bottom.type and first.bottom.type != end_bottom.type:
            adapters.insert(0, ("chain.bottom", first.part, end_bottom, first.bottom))
    if end_top is not None and last.top is not None and end_top.type:
        if last.top.type and last.top.type != end_top.type:
            adapters.append((last.part, "chain.top", last.top, end_top))
    total = sum(lengths)
    return Solution(
        tuple(assignments),
        total,
        total - target if wished else 0.0,
        tuple(adapters),
    )
