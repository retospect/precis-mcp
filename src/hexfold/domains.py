"""``fit`` on domain sets and chain propagation from the pinned ends
(SPEC 12.1 0.2 "fit on more parameters", SPEC 22.3).

A primitive's roll-up parameter may be a **domain** instead of a value:
``tube(fit in {(5,5),(6,6)}, len=3)`` (a set literal) or ``tube(fit)`` /
``cap(fit)`` (the primitive's catalogue, :data:`CATALOGUES`).  Every
``fuse`` is an integer equality between two rims' dangling counts ``N``
(SPEC 10: a fuse checks ``N`` only -- zigzag onto armchair at equal ``N``
is the 30° grain-boundary adapter, not a mismatch), and every k≥3 ``seam``
identifies its rims pairwise.  For a domain value ``N`` is arithmetic --
a tube end has ``n+m`` dangling atoms, ``cap(5,5)`` has 10, ``cap(6k,0)``
has ``6k`` -- and for a pinned instance (literal parameters) it is read off
one probe build.  :func:`propagate` runs arc consistency over those
equalities to a fixpoint: a pinned end prunes its neighbour's domain, the
survivors prune the next, in both directions along the chain ("pin either
end and propagation resolves the middle", SPEC 22.3).  An emptied domain is
the conflict the caller reports as ``fit.unsolvable``: which instance,
which connect, what it needed and what the domain offered.

Propagation is arithmetic; it does not build.  The remaining Cartesian
product is what the builder enumerates and ranks (SPEC 12.1 cost order) --
``build.py`` owns that loop, this module only says which combinations are
worth building.  Numpy-free, ``precis``-free.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from .text import Instance, Spec

#: The primitives whose roll-up parameter may carry a domain.
DOMAIN_KINDS: tuple[str, ...] = ("tube", "cap")

#: ``fit`` alone on a tube: the two pure rim types (SPEC 10 rim standard)
#: at every radius from ``n=3`` up -- 64 members, the ``_FIT_CAP`` of
#: SPEC 12.1.  Chiral roll-ups are authored explicitly, never guessed.
#: ``fit`` alone on a cap: the C60 hemisphere and the flat-lid family.
CATALOGUES: dict[str, tuple[tuple[int, int], ...]] = {
    "tube": tuple((n, 0) for n in range(3, 35)) + tuple((n, n) for n in range(3, 35)),
    "cap": ((5, 5),) + tuple((6 * k, 0) for k in range(1, 11)),
}

_DOMAIN_RE = re.compile(r"^fit(?:\s+in\s*\{(?P<set>[^}]*)\})?$")
_PAIR_RE = re.compile(r"\(\s*(\d+)\s*,\s*(\d+)\s*\)")

#: A fuse onto ``inst @ inst/site`` mints a hexagon hole on the
#: destination (``build.py`` ``hole_requests`` ring 6): six dangling atoms.
_MINTED_HOLE_N = 6


class DomainError(ValueError):
    """Malformed domain syntax, or a domain on a primitive that has none."""


def domain_of(inst: Instance) -> tuple[tuple[int, int], ...] | None:
    """The instance's domain when its roll-up parameter is ``fit`` or
    ``fit in {...}``; ``None`` for a pinned instance."""
    raw = dict(inst.params).get("0")
    if raw is None or not raw.startswith("fit"):
        return None
    if inst.kind not in DOMAIN_KINDS:
        raise DomainError(
            f"{inst.name}: {inst.kind}({raw}) -- a domain fit is defined for "
            f"{', '.join(DOMAIN_KINDS)} only"
        )
    m = _DOMAIN_RE.match(raw.strip())
    if m is None:
        raise DomainError(
            f"{inst.name}: cannot read domain {raw!r}; write 'fit' or "
            "'fit in {(n,m),(n,m),...}'"
        )
    if m["set"] is None:
        return CATALOGUES[inst.kind]
    pairs = [(int(a), int(b)) for a, b in _PAIR_RE.findall(m["set"])]
    if not pairs:
        raise DomainError(f"{inst.name}: empty domain {raw!r}")
    seen: list[tuple[int, int]] = []
    for p in pairs:
        if p not in seen:
            seen.append(p)
    return tuple(seen)


def has_domains(spec: Spec) -> bool:
    return any(domain_of(i) is not None for i in spec.instances)


def rim_n(kind: str, value: tuple[int, int], port: str) -> int | None:
    """Dangling count of ``port`` on a ``kind`` instance rolled up as
    ``value``; ``None`` when the value has no such rim (an unsupported cap,
    a port name the primitive does not have)."""
    n, m = value
    if kind == "tube":
        return n + m if port in ("in", "out") else None
    if kind == "cap":
        if port != "in":
            return None
        if (n, m) == (5, 5):
            return 10
        if m == 0 and n % 6 == 0 and n > 0:
            return n
        return None
    return None


def set_value(spec: Spec, name: str, value: tuple[int, int]) -> Spec:
    """Replace instance ``name``'s domain parameter by the literal
    ``(n, m)`` -- positional ``0``/``1``, the rest of the params kept."""
    out = []
    for inst in spec.instances:
        if inst.name != name:
            out.append(inst)
            continue
        params: list[tuple[str, str]] = []
        for k, v in inst.params:
            if k == "0":
                params.append(("0", str(value[0])))
                params.append(("1", str(value[1])))
            elif k == "1":
                continue
            else:
                params.append((k, v))
        out.append(replace(inst, params=tuple(params)))
    return replace(spec, instances=tuple(out))


# ---------- propagation ----------

#: One end of an equality: a domain instance's port, or a pinned ``N``.
_End = tuple[str, str] | int


@dataclass(frozen=True)
class Conflict:
    instance: str
    #: what the constraint needed (the other end's N set) ...
    needs: tuple[int, ...]
    #: ... and what the domain still offered before this constraint
    offers: tuple[int, ...]
    #: the connect/seam that emptied it (``"fuse a.out -> b.in"``)
    constraint: str
    span: tuple[int, int] | None


@dataclass
class Propagation:
    """Result of :func:`propagate`: the domains before and after, which
    constraints pruned each, the equalities whose pinned end could not be
    read (no probe port), and the first conflict, if any."""

    before: dict[str, tuple[tuple[int, int], ...]]
    after: dict[str, tuple[tuple[int, int], ...]]
    pruned_by: dict[str, list[str]] = field(default_factory=dict)
    unpinned: list[str] = field(default_factory=list)
    conflict: Conflict | None = None

    @property
    def combinations(self) -> int:
        n = 1
        for vals in self.after.values():
            n *= len(vals)
        return n


def _split_port(ref: str) -> tuple[str, str]:
    inst, _, port = ref.rpartition(".")
    return inst, port


def _equalities(
    spec: Spec, doms: dict[str, tuple[tuple[int, int], ...]]
) -> list[tuple[str, str, str, tuple[int, int] | None]]:
    """``(label, src ref, dst ref, span)`` for every fuse and every
    consecutive seam rim pair.  A ``@``-site destination is the literal
    ``"@hole"`` (its N is :data:`_MINTED_HOLE_N`)."""
    out: list[tuple[str, str, str, tuple[int, int] | None]] = []
    for c in spec.connects:
        if c.verb != "fuse":
            continue
        dst = "@hole" if "@" in c.dst else c.dst
        out.append((f"fuse {c.src} -> {c.dst}", c.src, dst, c.span))
    for s in spec.seams:
        for a, b in zip(s.rims, s.rims[1:]):
            out.append((f"seam {s.name}: {a} == {b}", a, b, s.span))
    return out


def propagate(spec: Spec, probe: Callable[[Spec], Any] | None = None) -> Propagation:
    """Arc consistency over the rim equalities (module docstring).

    ``probe(spec) -> Net`` builds the spec with every domain replaced by
    its first member **and every fuse and seam removed** (a fuse consumes
    both rims, so a fused pinned port would not be on the net) so the
    *pinned* ports' ``N`` can be read from real rims (``net.ports``); the
    domain instances' own ports are never read from it.  With
    ``probe=None`` (or a probe that raises), equalities against pinned
    instances are skipped and listed in ``unpinned``.
    """
    doms: dict[str, tuple[tuple[int, int], ...]] = {}
    kinds: dict[str, str] = {}
    for inst in spec.instances:
        d = domain_of(inst)
        if d is not None:
            doms[inst.name] = d
            kinds[inst.name] = inst.kind
    result = Propagation(before=dict(doms), after=dict(doms))
    if not doms:
        return result

    pinned: dict[str, int] = {}
    if probe is not None:
        probe_spec = replace(
            spec,
            connects=tuple(c for c in spec.connects if c.verb != "fuse"),
            seams=(),
        )
        for name, vals in doms.items():
            probe_spec = set_value(probe_spec, name, vals[0])
        try:
            net = probe(probe_spec)
        except Exception:
            net = None
        if net is not None:
            for pname, port in net.ports:
                owner, _ = _split_port(pname)
                if owner not in doms:
                    pinned[pname] = port.size

    def end_of(ref: str) -> _End | None:
        if ref == "@hole":
            return _MINTED_HOLE_N
        inst, port = _split_port(ref)
        if inst in doms:
            return (inst, port)
        if ref in pinned:
            return pinned[ref]
        return None

    def allowed(end: _End) -> set[int]:
        if isinstance(end, int):
            return {end}
        inst, port = end
        return {n for v in doms[inst] if (n := rim_n(kinds[inst], v, port)) is not None}

    eqs = []
    for label, a, b, span in _equalities(spec, doms):
        ea, eb = end_of(a), end_of(b)
        if ea is None or eb is None:
            if isinstance(ea, tuple) or isinstance(eb, tuple):
                result.unpinned.append(label)
            continue
        if isinstance(ea, int) and isinstance(eb, int):
            continue  # two pinned rims: the builder's port.mismatch, not ours
        eqs.append((label, ea, eb, span))

    changed = True
    while changed:
        changed = False
        for label, ea, eb, span in eqs:
            for me, other in ((ea, eb), (eb, ea)):
                if isinstance(me, int):
                    continue
                iname, port = me
                ok = allowed(other)
                before = doms[iname]
                offers = tuple(
                    sorted(
                        {
                            n
                            for v in before
                            if (n := rim_n(kinds[iname], v, port)) is not None
                        }
                    )
                )
                kept = tuple(
                    v for v in before if (rim_n(kinds[iname], v, port) or -1) in ok
                )
                if len(kept) == len(before):
                    continue
                doms[iname] = kept
                labels = result.pruned_by.setdefault(iname, [])
                if label not in labels:
                    labels.append(label)
                changed = True
                if not kept:
                    result.after = dict(doms)
                    result.conflict = Conflict(
                        instance=iname,
                        needs=tuple(sorted(ok)),
                        offers=offers,
                        constraint=label,
                        span=span,
                    )
                    return result
    result.after = dict(doms)
    return result
