"""The ``config`` mini-DSL — compact typed shape specs (ADR 0041 §11).

A single string names a primitive and its dimensions in millimetres, e.g.

* ``box:w40d20h10``      — rectangular box (w × d × h)
* ``cyl:r3h12``          — cylinder (radius, height)
* ``cone:r4h8``          — cone (base radius → apex)
* ``tcone:rb4rt2h8``     — truncated cone (bottom → top radius)
* ``sphere:r5``          — sphere
* ``torus:R10r2``        — torus (major R, minor r)
* ``hex:r5h10``          — regular hexagonal prism (circumradius, height)
* ``ngon:n6r5h10``       — regular n-gon prism
* ``frustum:n6rb4rt2h5`` — regular n-gon frustum
* ``pyramid:n4r5h8``     — regular n-gon pyramid
* ``chamfer:1x45``       — planar bevel: size × angle° (resolved against an
                           anchor face at node-build time, not here)

Grammar: ``<alias>:<tokens>`` where each token is a ``<key><number>``
pair. Keys are matched longest-first so ``rb`` / ``rt`` win over ``r``;
``R`` (major radius) is distinct from ``r``. ``chamfer`` uses the special
``<size>x<angle>`` form.

This module stays kernel-pure (no precis imports). It raises
:class:`DslError`; the handler maps that onto ``BadInput``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from precis.cad.primitives import (
    CircularFrustum,
    Primitive,
    Sphere,
    Torus,
    box,
    pyramid,
    regular_frustum,
    regular_prism,
)


class DslError(ValueError):
    """A malformed ``config`` string."""


@dataclass(frozen=True)
class ShapeSpec:
    """A parsed shape: an ``alias`` and its numeric ``params`` (mm)."""

    alias: str
    params: dict[str, float]


#: ``<key><number>`` — keys longest-first so ``rb``/``rt`` beat ``r``.
_TOKEN_RE = re.compile(r"(rb|rt|R|r|w|d|h|n)(-?\d+(?:\.\d+)?)")
_CHAMFER_RE = re.compile(r"^(-?\d+(?:\.\d+)?)x(-?\d+(?:\.\d+)?)$")

#: Required keys per alias, in canonical output order (drives ``format_spec``).
_ALIAS_KEYS: dict[str, tuple[str, ...]] = {
    "box": ("w", "d", "h"),
    "cyl": ("r", "h"),
    "cone": ("r", "h"),
    "tcone": ("rb", "rt", "h"),
    "sphere": ("r",),
    "torus": ("R", "r"),
    "hex": ("r", "h"),
    "ngon": ("n", "r", "h"),
    "frustum": ("n", "rb", "rt", "h"),
    "pyramid": ("n", "r", "h"),
    "chamfer": ("size", "angle"),
}


def parse(config: str) -> ShapeSpec:
    """Parse a ``config`` string into a :class:`ShapeSpec`."""
    if not isinstance(config, str) or ":" not in config:
        raise DslError(
            f"config must be '<shape>:<dims>', got {config!r} "
            "(e.g. 'cyl:r3h12', 'box:w40d20h10')"
        )
    alias, _, rest = config.strip().partition(":")
    alias = alias.lower()
    rest = rest.strip()
    if alias not in _ALIAS_KEYS:
        known = ", ".join(sorted(_ALIAS_KEYS))
        raise DslError(f"unknown shape {alias!r}; known: {known}")

    if alias == "chamfer":
        m = _CHAMFER_RE.match(rest)
        if not m:
            raise DslError(
                "chamfer config must be '<size>x<angle>', e.g. 'chamfer:1x45'"
            )
        return ShapeSpec(alias, {"size": float(m.group(1)), "angle": float(m.group(2))})

    params: dict[str, float] = {}
    pos = 0
    for m in _TOKEN_RE.finditer(rest):
        if m.start() != pos:
            raise DslError(f"unexpected text in config near {rest[pos:]!r}")
        key, num = m.group(1), m.group(2)
        if key in params:
            raise DslError(f"duplicate key {key!r} in config {config!r}")
        params[key] = float(num)
        pos = m.end()
    if pos != len(rest):
        raise DslError(f"unexpected text in config near {rest[pos:]!r}")

    required = set(_ALIAS_KEYS[alias])
    missing = required - params.keys()
    if missing:
        raise DslError(
            f"{alias} needs {sorted(required)}, missing {sorted(missing)} in {config!r}"
        )
    extra = params.keys() - required
    if extra:
        raise DslError(f"{alias} got unexpected key(s) {sorted(extra)}")

    if "n" in params:
        n = params["n"]
        if n != int(n) or int(n) < 3:
            raise DslError(f"n must be an integer >= 3, got {n}")
    return ShapeSpec(alias, params)


def build(spec: ShapeSpec) -> Primitive:
    """Build a kernel :class:`Primitive` from a :class:`ShapeSpec`.

    ``chamfer`` is *not* buildable here — a half-space needs an anchor
    face (resolved at node-build time), so this raises for it.
    """
    p = spec.params
    a = spec.alias
    if a == "box":
        return box(p["w"], p["d"], p["h"])
    if a == "cyl":
        return CircularFrustum(rb=p["r"], rt=p["r"], h=p["h"])
    if a == "cone":
        return CircularFrustum(rb=p["r"], rt=0.0, h=p["h"])
    if a == "tcone":
        return CircularFrustum(rb=p["rb"], rt=p["rt"], h=p["h"])
    if a == "sphere":
        return Sphere(r=p["r"])
    if a == "torus":
        return Torus(R=p["R"], r=p["r"])
    if a == "hex":
        return regular_prism(6, p["r"], p["h"])
    if a == "ngon":
        return regular_prism(int(p["n"]), p["r"], p["h"])
    if a == "frustum":
        return regular_frustum(int(p["n"]), p["rb"], p["rt"], p["h"])
    if a == "pyramid":
        return pyramid(int(p["n"]), p["r"], p["h"])
    raise DslError(f"{a} cannot be built standalone (chamfer needs an anchor face)")


def build_config(config: str) -> Primitive:
    """Convenience: ``parse`` then ``build``."""
    return build(parse(config))


def _fmt_num(x: float) -> str:
    """Render a float compactly: 4.0 → '4', 4.5 → '4.5'."""
    if x == int(x):
        return str(int(x))
    return repr(round(x, 6)).rstrip("0").rstrip(".")


def format_spec(spec: ShapeSpec) -> str:
    """Render a :class:`ShapeSpec` back to canonical config text."""
    if spec.alias == "chamfer":
        return (
            f"chamfer:{_fmt_num(spec.params['size'])}x{_fmt_num(spec.params['angle'])}"
        )
    parts = "".join(
        f"{key}{_fmt_num(spec.params[key])}" for key in _ALIAS_KEYS[spec.alias]
    )
    return f"{spec.alias}:{parts}"
