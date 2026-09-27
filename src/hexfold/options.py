"""``options(handle, wish)`` -- SPEC 25.3's one genuinely new verb.

The LLM states an underdetermined wish at a fit site (SPEC 25.2: a target
with a tolerance band, or an explicit don't-care); hexfold enumerates the
realisable values nearby, checks each by building the whole spec, and
returns a bounded, ranked list -- plus the in-band values that do *not*
build and why, which is where the solver's integer refusal becomes a
design conversation.  No new solver: this drives the existing ``len`` and
``k`` fit families (SPEC 12.1, ``build.py``) with a band instead of
"smallest clean", and ranks by distance to the wish first -- cost (1) of
SPEC 12.1, the hook ``_rank_fit`` leaves at 0 for a plain ``build``.

Handles (0.2): ``"<inst>.len"`` -- a tube's length in whole translation
periods (the wish may be in Angstrom, converted through the tube's period
``a * |T|``); ``"<src port>.k"`` -- a fuse's rotational phase in steps of
the rim symmetry N (distance is modular).  Collars ``{Rxk @fit}`` and
``sheet(W,H)`` extents come with domain fits (integration step 3).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from . import menus
from .build import _FIT_CAP, _lattice_from_spec, _param, build
from .lattice import _lattice_metric_dot, translation_vector
from .report import BuildError, Report, Severity
from .text import Spec, parse

#: Default half-width when a target is given without a band: the nearest
#: realisable integer on either side, nothing more.
_DEFAULT_BAND = 0.5
#: Don't-care wishes cap the family like ``build`` does (SPEC 12.1).
_DONT_CARE_CAP = 8


@dataclass(frozen=True)
class Wish:
    """A target with a tolerance band, in the handle's native unit
    (periods for ``len``, steps for ``k``) or in Angstrom for ``len``
    (``target_A``/``band_A``, converted through the tube period).  All
    ``None`` is the explicit don't-care: the plain fit family."""

    target: float | None = None
    band: float | None = None
    target_A: float | None = None
    band_A: float | None = None

    @property
    def dont_care(self) -> bool:
        return self.target is None and self.target_A is None


@dataclass(frozen=True)
class Option:
    value: int
    #: |value - target| in the native unit (modular for ``k``); 0 for a
    #: don't-care wish
    distance: float
    #: SPEC 12.1 cost tuple (max seam ring, |seam residual|, lattice index)
    cost: tuple[float, float, float]
    clean: bool
    #: ERROR codes when not clean
    errors: tuple[str, ...]
    #: the value in Angstrom for a ``len`` handle
    value_A: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "distance": self.distance,
            "cost": list(self.cost),
            "clean": self.clean,
            "errors": list(self.errors),
            "value_A": None if self.value_A is None else round(self.value_A, 3),
        }


@dataclass(frozen=True)
class OptionsResult:
    handle: str
    kind: str  # "len" | "k"
    unit: str  # "periods" | "steps"
    wish: Wish
    #: what a plain ``build`` applies today (the fit winner, or the
    #: authored value); ``None`` when the plain build is unsolvable
    applied: int | None
    #: clean candidates within the band, best first
    options: tuple[Option, ...]
    #: in-band candidates that do not build, with their ERROR codes
    rejected: tuple[Option, ...]
    #: the tube's translation period for a ``len`` handle
    period_A: float | None = None
    #: rim symmetry N for a ``k`` handle
    period_steps: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "kind": self.kind,
            "unit": self.unit,
            "wish": {
                "target": self.wish.target,
                "band": self.wish.band,
                "target_A": self.wish.target_A,
                "band_A": self.wish.band_A,
            },
            "applied": self.applied,
            "options": [o.to_dict() for o in self.options],
            "rejected": [o.to_dict() for o in self.rejected],
            "period_A": None if self.period_A is None else round(self.period_A, 4),
            "period_steps": self.period_steps,
        }


def _error_codes(report: Report) -> tuple[str, ...]:
    return tuple(
        sorted({f.code for f in report.findings if f.severity == Severity.ERROR})
    )


def _try_build(spec: Spec) -> tuple[Any | None, tuple[str, ...]]:
    """Build leniently; ``(net, ())`` when clean, ``(net or None, codes)``
    when the report carries ERRORs (a nested ``fit.unsolvable`` raises)."""
    try:
        net = build(spec, strict=False)
    except BuildError as e:
        return None, _error_codes(e.report) or ("build.error",)
    codes = _error_codes(net.report)
    return net, codes


def _rank(options: list[Option]) -> list[Option]:
    return sorted(options, key=lambda o: (o.distance, o.cost[0], o.cost[1], o.cost[2]))


def _band_values(
    target: float | None, band: float | None, lo: int, hi: int
) -> list[int]:
    if target is None:
        return list(range(lo, hi + 1))
    half = _DEFAULT_BAND if band is None else band
    start = max(lo, math.ceil(target - half))
    stop = min(hi, math.floor(target + half))
    return list(range(start, stop + 1))


# ---------- len ----------


def _len_key(inst: Any) -> str:
    """``len`` is positional param ``"2"`` or keyword ``"len"``."""
    return "len" if "len" in dict(inst.params) else "2"


def _set_len(spec: Spec, name: str, value: int) -> Spec:
    out = []
    for inst in spec.instances:
        if inst.name != name:
            out.append(inst)
            continue
        key = _len_key(inst)
        params = tuple((k, str(value) if k == key else v) for k, v in inst.params)
        if key not in dict(params):
            params = params + ((key, str(value)),)
        out.append(replace(inst, params=params))
    return replace(spec, instances=tuple(out))


def _len_options(spec: Spec, name: str, wish: Wish, handle: str) -> OptionsResult:
    inst = spec.instance(name)
    if inst is None or inst.kind != "tube":
        raise ValueError(f"options: {handle!r} is not a tube instance's len")
    n = int(_param(inst, "0", _param(inst, "n", "5")))
    m = int(_param(inst, "1", _param(inst, "m", "5")))
    lat = _lattice_from_spec(spec)
    t = translation_vector(n, m)
    period_a = lat.a * math.sqrt(_lattice_metric_dot(t, t))

    target = wish.target
    band = wish.band
    if wish.target_A is not None:
        target = wish.target_A / period_a
    if wish.band_A is not None:
        band = wish.band_A / period_a

    # what build() applies today
    net0, _codes0 = _try_build(spec)
    applied: int | None = None
    if net0 is not None:
        applied_inst = net0.spec.instance(name)
        if applied_inst is not None:
            raw = _param(applied_inst, _len_key(applied_inst), "")
            applied = int(raw) if raw.isdigit() else None

    values = _band_values(target, band, 1, _FIT_CAP)
    found: list[Option] = []
    rejected: list[Option] = []
    for v in values:
        net, codes = _try_build(_set_len(spec, name, v))
        clean = net is not None and not codes
        dist = 0.0 if target is None else abs(v - target)
        opt = Option(
            value=v,
            distance=dist,
            # seam ring and residual are constant across a len family
            # absent a smooth: section (SPEC 12.1, build.py) -- lattice
            # index breaks the tie, as in build
            cost=(0.0, 0.0, float(v)),
            clean=clean,
            errors=codes,
            value_A=v * period_a,
        )
        (found if clean else rejected).append(opt)
        if target is None and len(found) >= _DONT_CARE_CAP:
            break
    return OptionsResult(
        handle=handle,
        kind="len",
        unit="periods",
        wish=wish,
        applied=applied,
        options=tuple(_rank(found)),
        rejected=tuple(rejected),
        period_A=period_a,
    )


# ---------- k ----------


def _seam_cost(net: Any, span: tuple[int, int]) -> tuple[float, float]:
    """(max seam ring, |sum(6 - n)|) from the connect's own ``seam.rings``."""
    for f in net.report.findings:
        if f.code == "seam.rings" and f.span == span:
            rings = dict(f.data).get("rings", {})
            if not rings:
                return 0.0, 0.0
            mx = max(int(s) for s in rings)
            resid = abs(sum((6 - int(s)) * int(c) for s, c in rings.items()))
            return float(mx), float(resid)
    return 0.0, 0.0


def _k_options(spec: Spec, src: str, wish: Wish, handle: str) -> OptionsResult:
    if wish.target_A is not None or wish.band_A is not None:
        raise ValueError(f"options: {handle!r} takes steps, not Angstrom")
    cis = [i for i, c in enumerate(spec.connects) if c.verb == "fuse" and c.src == src]
    if len(cis) != 1:
        raise ValueError(f"options: {handle!r} names {len(cis)} fuse connects, need 1")
    ci = cis[0]
    conn = spec.connects[ci]

    # rim symmetry N: the src port's dangling count on the net built
    # without this connect
    without = replace(
        spec, connects=tuple(c for i, c in enumerate(spec.connects) if i != ci)
    )
    net_w, _codes_w = _try_build(without)
    if net_w is None or src not in dict(net_w.ports):
        raise ValueError(f"options: port {src!r} not found for {handle!r}")
    n = dict(net_w.ports)[src].size

    net0, _codes0 = _try_build(spec)
    applied: int | None = None
    if net0 is not None:
        c0 = net0.spec.connects[ci]
        if c0.expanded and "k" in c0.expanded:
            applied = int(c0.expanded["k"])
        elif c0.k is not None and c0.k >= 0:
            applied = int(c0.k)

    target = wish.target
    if target is None:
        values = list(range(n))
    else:
        half = _DEFAULT_BAND if wish.band is None else wish.band
        values = [
            v
            for v in range(n)
            if min(abs(v - target) % n, n - abs(v - target) % n) <= half
        ]
    found: list[Option] = []
    rejected: list[Option] = []
    for v in values:
        spec2 = replace(
            spec,
            connects=tuple(
                replace(c, k=v) if i == ci else c for i, c in enumerate(spec.connects)
            ),
        )
        net, codes = _try_build(spec2)
        clean = net is not None and not codes
        if target is None:
            dist = 0.0
        else:
            d = abs(v - target) % n
            dist = min(d, n - d)
        seam, resid = _seam_cost(net, conn.span) if net is not None else (0.0, 0.0)
        opt = Option(
            value=v,
            distance=dist,
            cost=(seam, resid, float(v)),
            clean=clean,
            errors=codes,
        )
        (found if clean else rejected).append(opt)
    return OptionsResult(
        handle=handle,
        kind="k",
        unit="steps",
        wish=wish,
        applied=applied,
        options=tuple(_rank(found)),
        rejected=tuple(rejected),
        period_steps=n,
    )


# ---------- entry ----------


def options(
    spec_text_or_ast: str | Spec, handle: str, wish: Wish | None = None
) -> OptionsResult:
    """Realisable values near a wish at one fit site (module docstring).

    ``handle``: ``"<inst>.len"`` or ``"<src port>.k"``.  ``wish``: a
    :class:`Wish`; ``None`` is the don't-care.  Raises ``ValueError`` for a
    handle that names nothing this verb can vary.
    """
    wish = wish or Wish()
    spec = (
        parse(spec_text_or_ast)
        if isinstance(spec_text_or_ast, str)
        else spec_text_or_ast
    )
    spec = menus.expand(spec)
    if "." not in handle:
        raise ValueError(f"options: handle {handle!r} needs '<name>.len' or '<port>.k'")
    name, key = handle.rsplit(".", 1)
    if key == "len":
        return _len_options(spec, name, wish, handle)
    if key == "k":
        return _k_options(spec, name, wish, handle)
    raise ValueError(f"options: unknown handle kind {key!r} in {handle!r}")
