"""Built-in catalog atoms — the procurable building blocks of machines.

``part <name> <family>:<code>`` in the design language places one of these:
a standard part rendered as an **envelope + ports**, generated here as cad
source text and inlined by :func:`precis.cad.scene.expand_instances` exactly
like a ``use`` instance — except the "resolver" is this module, so a
parts-only design needs no store access (`precis.cad` imports nothing from
the DB, and must stay that way).

Deliberately envelopes, never true geometry: a bearing is two cylinders, a
bolt has no helix. What matters is the honest outer shape (for clearance /
sweep / mass sampling), the mount frames (``port`` lines, so parts mate with
zero world coordinates), and the **procurement identity** — ``part_slug`` is
the length-free identity a `component` ref is expected under (`bearing-6202`,
`extrusion-2020`), which the handler resolves into ``realized-by`` links and
the design's ``view='bom'``.

Codes are case-insensitive; **designation** dimensions (the numbers inside
a code string — ``bolt:m3x12``'s ``12``, ``extrusion:2020x400``'s ``400``)
stay mm by catalogue convention, same as the real-world standard's own
part numbers — they are a designation string, not a free quantity, so the
units-policy-cutover ingest boundary does not apply to them. The three
**ISO fastener** families take their dimensions from
`precis/data/component_series.json` (see :data:`_FASTENER_SERIES`) rather
than a second transcription of the same standard; the rest are tabulated
here, having no series counterpart. That keeps the no-DB property — the
series loader is stdlib-only over packaged data — while there is exactly
one place to fix a number.

The *emitted geometry* (the ``cyl:r..h..`` envelope + port source text
below) is generated cad-DSL source, expanded in-memory exactly once per
lookup (never persisted — the stored reference is just
``part:<family>:<code>``). Every dimension it emits carries an explicit
``mm`` unit (:func:`_g`), so it flows through the same strict boundary as
hand-authored geometry (``scene.py``'s ``@x,y,z``/``config:`` tokens) and
lands in the same SI-metres internal representation — a design mixing
catalog parts and hand-authored nodes stays in scale.

Families:

- ``bearing:6202`` — deep-groove ball bearings (ISO 15 + the 60x minis).
- ``bolt:m6x20`` / ``nut:m6`` / ``washer:m6`` — ISO 4017 / 4032 / 7089.
- ``extrusion:2020x400`` — T-slot profile × cut length.
- ``rail:mgn12x200`` — MGN miniature linear rail × cut length.
- ``nema:17`` — stepper motor envelope (face, body, boss, shaft).
- ``gear:m1z20`` (optional ``w8``) — spur gear blank, OD = m·(z+2).

Unknown families/codes raise ``ValueError`` naming what *is* known — the
scene parser wraps that with the line number, so a typo'd part refuses at
``put``, never at expansion.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any

from precis import component_series

__all__ = ["PartInfo", "known_families", "resolve_part"]


@dataclass(frozen=True)
class PartInfo:
    """One resolved catalog part.

    ``code`` is the canonical ``family:code`` spelling; ``part_slug`` is the
    procurement identity (length-free for cut-to-length families) a
    ``component`` ref is looked up under; ``source`` is the cad source text
    for the envelope + ports.
    """

    family: str
    code: str
    part_slug: str
    designation: str
    standard: str
    source: str


def _g(v: float) -> str:
    """mm value → shortest exact token with an explicit unit
    (``17.5mm``, not ``17.500000``).

    Catalog dimensions are tabulated in mm (real-world part-standard
    convention); emitting the unit explicitly means the generated source
    text flows through the same strict boundary
    (``scene.parse_source`` / ``cad.dsl.parse(require_units=True)``) as
    hand-authored geometry — the unit converts the value to SI metres
    exactly once, at expansion."""
    return f"{v:g}mm"


# --- family tables (mm) ----------------------------------------------------

#: code -> (bore d, OD, width B) — deep-groove ball bearings.
_BEARINGS: dict[str, tuple[float, float, float]] = {
    "608": (8, 22, 7),
    "623": (3, 10, 4),
    "625": (5, 16, 5),
    "626": (6, 19, 6),
    "688": (8, 16, 5),
    "6000": (10, 26, 8),
    "6001": (12, 28, 8),
    "6002": (15, 32, 9),
    "6003": (17, 35, 10),
    "6004": (20, 42, 12),
    "6005": (25, 47, 12),
    "6200": (10, 30, 9),
    "6201": (12, 32, 10),
    "6202": (15, 35, 11),
    "6203": (17, 40, 12),
    "6204": (20, 47, 14),
    "6205": (25, 52, 15),
    "6300": (10, 35, 11),
    "6301": (12, 37, 12),
    "6302": (15, 42, 13),
}

#: The three ISO fastener families read their dimensions from the **one**
#: transcription in `precis/data/component_series.json`
#: (:mod:`precis.component_series`) rather than carrying a second copy.
#:
#: Consolidated 2026-09-06 (Reto's call). Three tables of the same
#: standard had grown up independently — here, the series file, and
#: `fit_classes.json` — and they agreed only because a test said so. A
#: drift guard is a good answer to duplication; not duplicating is a
#: better one.
#:
#: **This does not weaken the no-DB rule.** `precis.component_series` is
#: a pure JSON loader over packaged data: stdlib only, no store, no
#: network. A parts-only design still resolves with no DB access, which
#: is the property that mattered — the constraint was never "no data
#: file".
_FASTENER_SERIES: dict[str, str] = {
    "bolt": "iso-4017",
    "nut": "iso-4032",
    "washer": "iso-7089",
}


@cache
def _fastener_sizes(family: str) -> dict[int, dict[str, Any]]:
    """``{6: {'across_flats': 10.0, 'head_height': 4.0, …}}`` — one
    family's size table, keyed by M-size, dimensions in mm.

    Empty when the series is missing, which the callers below turn into
    the ordinary "unknown code" refusal rather than a crash: a truncated
    data file should narrow what resolves, never break the parser."""
    series = component_series.find_series(_FASTENER_SERIES[family])
    if series is None:  # pragma: no cover — packaged data, always present
        return {}
    out: dict[int, dict[str, Any]] = {}
    for size in series.sizes:
        m = _MSIZE_RE.match(str(size.key).strip().lower())
        if m:
            out[int(m[1])] = dict(size.specs)
    return out


#: profile -> (w, d) cross-section — T-slot aluminium extrusions.
_EXTRUSIONS: dict[str, tuple[float, float]] = {
    "2020": (20, 20),
    "2040": (20, 40),
    "4040": (40, 40),
}

#: series -> (rail width W, rail height H) — MGN miniature linear rails.
_RAILS: dict[str, tuple[float, float]] = {
    "mgn7": (7, 4.8),
    "mgn9": (9, 6.5),
    "mgn12": (12, 8),
    "mgn15": (15, 10),
}

#: size -> (face F, body length L, shaft d, shaft len, boss d, boss h).
_NEMAS: dict[str, tuple[float, float, float, float, float, float]] = {
    "11": (28.2, 32, 5, 20, 22, 2),
    "14": (35.2, 28, 5, 20, 22, 2),
    "17": (42.3, 40, 5, 24, 22, 2),
    "23": (57.2, 56, 6.35, 21, 38.1, 1.6),
    "34": (86, 80, 14, 37, 73, 1.9),
}

_HEX_OVER_FLATS = 2 / 3**0.5  # across-corners e = s / cos30 — the envelope

_BOLT_RE = re.compile(r"^m(\d+)x(\d+(?:\.\d+)?)$")
_MSIZE_RE = re.compile(r"^m(\d+)$")
_CUT_RE = re.compile(r"^([a-z0-9]+)x(\d+(?:\.\d+)?)$")
_GEAR_RE = re.compile(r"^m(\d+(?:\.\d+)?)z(\d+)(?:w(\d+(?:\.\d+)?))?$")

_MAX_CUT_LENGTH = 4000.0  # longest stock cut we'll pretend is one part


def known_families() -> tuple[str, ...]:
    return ("bearing", "bolt", "extrusion", "gear", "nema", "nut", "rail", "washer")


def _known(mapping: Mapping[Any, object]) -> str:
    return ", ".join(str(k) for k in sorted(mapping, key=str))


def _msize(family: str, code: str) -> int:
    """Parse ``m6`` against **that family's own** size table.

    Each family validates against its own series now — before
    consolidation all three checked the bolt table, so a nut size was
    legal because a *bolt* of that size existed. They happen to cover the
    same sizes today; relying on that was luck."""
    sizes = _fastener_sizes(family)
    m = _MSIZE_RE.match(code)
    if not m or int(m[1]) not in sizes:
        raise ValueError(
            f"unknown {family} size {code!r} — known: "
            + ", ".join(f"m{k}" for k in sorted(sizes))
        )
    return int(m[1])


def _cut(
    family: str, code: str, table: dict[str, tuple[float, float]]
) -> tuple[str, float]:
    m = _CUT_RE.match(code)
    if not m or m[1] not in table:
        raise ValueError(
            f"unknown {family} code {code!r} — expected '<profile>x<length>' "
            f"with profile one of: {_known(table)}"
        )
    length = float(m[2])
    if not 1 <= length <= _MAX_CUT_LENGTH:
        raise ValueError(
            f"{family} length {length:g} mm out of range (1..{_MAX_CUT_LENGTH:g})"
        )
    return m[1], length


def _bearing(code: str) -> PartInfo:
    if code not in _BEARINGS:
        raise ValueError(f"unknown bearing code {code!r} — known: {_known(_BEARINGS)}")
    d, od, b = _BEARINGS[code]
    desig = f"{code} deep-groove ball bearing d{_g(d)} D{_g(od)} B{_g(b)}"
    src = (
        f"desc: {desig} (envelope)\n"
        "component body\n"
        f"ring add cyl:r{_g(od / 2)}h{_g(b)}\n"
        f"bore cut cyl:r{_g(d / 2)}h{_g(b + 2)} @0mm,0mm,-1mm\n"
        f"port bore @0mm,0mm,{_g(b / 2)} of:body\n"
        f"port face @0mm,0mm,{_g(b)} of:body\n"
    )
    return PartInfo(
        "bearing", f"bearing:{code}", f"bearing-{code}", desig, "ISO 15", src
    )


def _bolt(code: str) -> PartInfo:
    sizes = _fastener_sizes("bolt")
    m = _BOLT_RE.match(code)
    if not m or int(m[1]) not in sizes:
        raise ValueError(
            f"unknown bolt code {code!r} — expected 'm<size>x<length>' with "
            "size one of: " + ", ".join(f"m{k}" for k in sorted(sizes))
        )
    d, length = int(m[1]), float(m[2])
    if not 2 <= length <= 300:
        raise ValueError(f"bolt length {length:g} mm out of range (2..300)")
    k = float(sizes[d]["head_height"])
    s = float(sizes[d]["across_flats"])
    e = s * _HEX_OVER_FLATS
    desig = f"hex head bolt M{d}x{_g(length)}"
    src = (
        f"desc: {desig} (ISO 4017 envelope) — origin at the under-head plane\n"
        "component body\n"
        f"head add cyl:r{_g(e / 2)}h{_g(k)} @0mm,0mm,-{_g(k)}\n"
        f"shank add cyl:r{_g(d / 2)}h{_g(length)}\n"
        "port head @0mm,0mm,0mm of:body\n"
        f"port tip @0mm,0mm,{_g(length)} of:body\n"
    )
    return PartInfo(
        # part_slug is a bare identifier (matches the pre-cutover convention
        # every other family's slug uses — nut-m6, washer-m6, nema-17 — none
        # of which run their code's numbers through `_g`'s unit-suffixed DSL
        # formatter); only the DSL `src` text needs the unit.
        "bolt",
        f"bolt:{code}",
        f"bolt-m{d}x{length:g}",
        desig,
        "ISO 4017",
        src,
    )


def _nut(code: str) -> PartInfo:
    d = _msize("nut", code)
    specs = _fastener_sizes("nut")[d]
    # ISO 4032 states the nut's own across-flats; it equals the bolt's at
    # every shared size, but reading it from the nut's row is the honest
    # source rather than a coincidence held in place by a test.
    m_h = float(specs["height"])
    s = float(specs["across_flats"])
    e = s * _HEX_OVER_FLATS
    desig = f"hex nut M{d}"
    src = (
        f"desc: {desig} (ISO 4032 envelope)\n"
        "component body\n"
        f"hex add cyl:r{_g(e / 2)}h{_g(m_h)}\n"
        f"bore cut cyl:r{_g(d / 2)}h{_g(m_h + 2)} @0mm,0mm,-1mm\n"
        "port face @0mm,0mm,0mm of:body\n"
    )
    return PartInfo("nut", f"nut:{code}", f"nut-m{d}", desig, "ISO 4032", src)


def _washer(code: str) -> PartInfo:
    d = _msize("washer", code)
    specs = _fastener_sizes("washer")[d]
    d1 = float(specs["inner_diameter"])
    d2 = float(specs["outer_diameter"])
    h = float(specs["thickness"])
    desig = f"plain washer M{d}"
    src = (
        f"desc: {desig} (ISO 7089 envelope)\n"
        "component body\n"
        f"disc add cyl:r{_g(d2 / 2)}h{_g(h)}\n"
        f"bore cut cyl:r{_g(d1 / 2)}h{_g(h + 2)} @0mm,0mm,-1mm\n"
        "port face @0mm,0mm,0mm of:body\n"
    )
    return PartInfo("washer", f"washer:{code}", f"washer-m{d}", desig, "ISO 7089", src)


def _extrusion(code: str) -> PartInfo:
    profile, length = _cut("extrusion", code, _EXTRUSIONS)
    w, d = _EXTRUSIONS[profile]
    desig = f"{profile} T-slot aluminium extrusion, {_g(length)}"
    src = (
        f"desc: {desig} (envelope, length along z)\n"
        "component body\n"
        f"profile add box:w{_g(w)}d{_g(d)}h{_g(length)}\n"
        "port end_a @0mm,0mm,0mm of:body\n"
        f"port end_b @0mm,0mm,{_g(length)} of:body\n"
    )
    return PartInfo(
        "extrusion", f"extrusion:{code}", f"extrusion-{profile}", desig, "T-slot", src
    )


def _rail(code: str) -> PartInfo:
    series, length = _cut("rail", code, _RAILS)
    w, h = _RAILS[series]
    desig = f"{series.upper()} miniature linear rail, {_g(length)}"
    src = (
        f"desc: {desig} (envelope, length along z)\n"
        "component body\n"
        f"rail add box:w{_g(w)}d{_g(h)}h{_g(length)}\n"
        "port end_a @0mm,0mm,0mm of:body\n"
        f"port end_b @0mm,0mm,{_g(length)} of:body\n"
    )
    return PartInfo("rail", f"rail:{code}", f"rail-{series}", desig, "MGN", src)


def _nema(code: str) -> PartInfo:
    if code not in _NEMAS:
        raise ValueError(f"unknown nema size {code!r} — known: {_known(_NEMAS)}")
    f, body_l, sd, sl, bd, bh = _NEMAS[code]
    desig = f"NEMA {code} stepper motor"
    src = (
        f"desc: {desig} (envelope) — face plate at z=0, shaft along +z\n"
        "component body\n"
        f"frame add box:w{_g(f)}d{_g(f)}h{_g(body_l)} @0mm,0mm,-{_g(body_l)}\n"
        f"boss add cyl:r{_g(bd / 2)}h{_g(bh)}\n"
        f"shaft add cyl:r{_g(sd / 2)}h{_g(sl)}\n"
        "port face @0mm,0mm,0mm of:body\n"
        f"port shaft @0mm,0mm,{_g(sl)} of:body\n"
    )
    return PartInfo("nema", f"nema:{code}", f"nema-{code}", desig, "NEMA ICS 16", src)


def _gear(code: str) -> PartInfo:
    m = _GEAR_RE.match(code)
    if not m:
        raise ValueError(
            f"unknown gear code {code!r} — expected 'm<module>z<teeth>[w<width>]' "
            "(e.g. 'gear:m1z20', 'gear:m2z30w12')"
        )
    module, teeth = float(m[1]), int(m[2])
    if not 0.3 <= module <= 10:
        raise ValueError(f"gear module {module:g} out of range (0.3..10)")
    if not 6 <= teeth <= 300:
        raise ValueError(f"gear tooth count {teeth} out of range (6..300)")
    width = float(m[3]) if m[3] else 10 * module
    od = module * (teeth + 2)
    desig = f"spur gear module {_g(module)}, {teeth} teeth, width {_g(width)}"
    src = (
        f"desc: {desig} (blank envelope, OD = m·(z+2))\n"
        "component body\n"
        f"blank add cyl:r{_g(od / 2)}h{_g(width)}\n"
        f"port axis @0mm,0mm,{_g(width / 2)} of:body\n"
    )
    return PartInfo("gear", f"gear:{code}", f"gear-{code}", desig, "", src)


_FAMILIES = {
    "bearing": _bearing,
    "bolt": _bolt,
    "nut": _nut,
    "washer": _washer,
    "extrusion": _extrusion,
    "rail": _rail,
    "nema": _nema,
    "gear": _gear,
}


def resolve_part(spec: str) -> PartInfo:
    """``'bearing:6202'`` (any case) → its :class:`PartInfo`.

    Raises ``ValueError`` naming the known families/codes on any miss — the
    scene parser wraps this with the offending line number.
    """
    text = spec.strip().lower()
    family, sep, code = text.partition(":")
    if not sep or not code:
        raise ValueError(
            f"catalog part {spec!r} must be '<family>:<code>' — families: "
            + ", ".join(known_families())
        )
    builder = _FAMILIES.get(family)
    if builder is None:
        raise ValueError(
            f"unknown catalog family {family!r} — families: "
            + ", ".join(known_families())
        )
    return builder(code)
