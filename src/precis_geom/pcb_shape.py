"""precis_geom.pcb_shape — the board's 3-D mechanical shape, **the one
derivation** (pcb-workspace-code slice 0b, "Board 3-D shape in the
container"; pcb-se-binding §Rough box / Follow-up v2).

Everything here is mm in the **pcb board frame** (outline coordinates as
authored, z = 0 at the bottom copper face, +z toward the top layer),
stdlib-only, and store-free — so it runs inside the ``--network none``
workspace container *and* underneath the trusted-side consumers, which
reuse it rather than re-derive:

- ``mechanical_profile(instances, features, ...)`` — the 0041-bridge
  profile dict (``{units, thickness_mm, outline, holes, blocks,
  keepouts}``). ``precis.pcb.export.mechanical_profile`` is a thin
  adapter over this (its ``model`` argument is just ``{"instances": …}``).
- ``derive_shape(profile, …)`` — the itemised shape: one
  :class:`Prism` per placed part (height + ``origin`` honesty flag),
  :class:`Hole` centres/diameters, :class:`Keepout` regions, the slab.
  ``precis_se.board.derive_board`` converts this into metres, centres
  x/y on the outline bbox, re-anchors z at the lowest extent, and wraps
  it in ``BoardDerived`` — that mm→m seam stays server-side
  (pcb-se-binding's single-crossing ruling).
- :meth:`BoardShape.fits` — the callable half: a fit check of the whole
  shape against an se part or an enclosure, both expressed as plain
  axis-aligned boxes in board-frame mm (``units='m'`` accepted as the
  declared crossing; ``pcb.box``/``pcb.enclosure`` build the specs).

**Heights stay honest.** A part's own ``height_mm`` wins
(``origin: 'user'``); absent, its footprint is looked up in
:data:`HEIGHT_TABLE` (``origin: 'proposed'``, source noted), with the
table's largest value as the fallback — the same order and the same
table the se binding's ``pcb_height_proposed`` finding reads.

**Holes are markers, never subtracted** — each carries centre + diameter
+ ``through_mm`` (the board thickness). Whether a bound board's holes
become derived child blocks or wait for a compound envelope is the
carried question (pcb-workspace-code §Slices note; shared with
cross-scale-single-assembly.md), deliberately not decided here.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: JLCPCB 4-layer default — the canonical home moved here with the
#: derivation; ``precis.pcb.export`` re-exports it for its callers.
DEFAULT_THICKNESS_MM = 1.6

#: Outline-fallback margin when no ``outline`` feature exists (the same
#: 2 mm ``mechanical_profile`` always used).
_BBOX_MARGIN_MM = 2.0


# --------------------------------------------------------------------------
# Package-class maximum body heights (moved verbatim from
# precis/pcb/package_heights.py so the container gets the same table).
# --------------------------------------------------------------------------

#: ``(regex, height_mm, source)`` — ordered, first match wins. Specific
#: prefixes before their general family (``SOT-223``/``SOT-89`` before
#: ``SOT-``); chip-passive imperial codes under one ``[RCL]`` row each
#: since resistor and MLCC body heights track the same imperial size,
#: with ``CHIP-`` and underscored (``R_0603``) spellings on the same
#: rows (gr513752) and the ``CAP-``/``RES-``/``IND-`` functional
#: prefixes the board examples use (gr513775). Each row's comment lists
#: spellings that match and near-misses that do not (gr513774): the
#: patterns are prefix-anchored, a trailing ``-`` suffix always matches
#: (``\b`` sits before a non-word char), and the chip rows also accept a
#: ``_`` tail — that is what ``R_0603_1608Metric`` rides. See
#: ``docs/backlog/pcb-se-binding.md`` §Rough box v0 / Change 1.
HEIGHT_TABLE: tuple[tuple[str, float, str], ...] = (
    # match: R402, R0402, C_0402, CHIP-0402, RES-0402, CAP-0402-100nF,
    #        R_0402_1005Metric
    # miss:  "0402" (no class prefix), R-0402 (dash after a lone letter)
    (
        r"^(?:CHIP[-_]?0?|[RCL]_?0?|(?:CAP|RES|IND)[-_]?0?)402(?:\b|_)",
        0.55,
        "0402 chip, package drawing body max 0.55 mm (Yageo RC0402 T)",
    ),
    # match: R603, R0603, C_0603, CHIP-0603, IND-0603, RES-0603-1uH,
    #        R_0603_1608Metric
    # miss:  "0603", R-0603
    (
        r"^(?:CHIP[-_]?0?|[RCL]_?0?|(?:CAP|RES|IND)[-_]?0?)603(?:\b|_)",
        0.9,
        "0603 chip, package drawing body max 0.9 mm (Yageo/KEMET 0603)",
    ),
    # match: R805, R0805, C_0805, CHIP_0805, CAP-0805
    # miss:  "0805", R-0805
    (
        r"^(?:CHIP[-_]?0?|[RCL]_?0?|(?:CAP|RES|IND)[-_]?0?)805(?:\b|_)",
        1.35,
        "0805 chip, package drawing body max 1.35 mm (Yageo RC0805 T)",
    ),
    # match: R1206, C_1206, CHIP-1206, CAP-1206, R_1206_3216Metric
    # miss:  R01206 (this row takes no ``0?`` lead-in), "1206"
    (
        r"^(?:CHIP[-_]?|[RCL]_?|(?:CAP|RES|IND)[-_]?)1206(?:\b|_)",
        1.8,
        "1206 chip, package drawing body max ~1.8 mm (Samsung CL31 series)",
    ),
    # match: R1210, C_1210, L1210
    # miss:  C_1210_3225Metric (the ``\b`` tail rejects a ``_`` suffix —
    #        unlike the rows above), CHIP-1210, CAP-1210
    (
        r"^[RCL]_?1210\b",
        2.8,
        "1210 MLCC, package drawing body max 2.8 mm (Samsung CL32 series)",
    ),
    # match: CAP-SMD, CAP-SMD-6.3x5.4
    # miss:  CAP-1206 (chip row wins it), CAP-ELEC-5P (THT can)
    (
        r"^CAP-SMD",
        6.3,
        "SMD aluminium electrolytic ~5 mm can, body max ~6.3 mm (Nichicon/Panasonic V-chip)",
    ),
    # match: LED-SMD, LED-SMD-0805
    # miss:  LED-3MM, LED-TH
    (
        r"^LED-SMD",
        1.1,
        "SMD LED ~2.0x1.3 mm class, body max ~1.1 mm (Kingbright APTD2012 family)",
    ),
    # match: HDR-SMD, HDR-SMD-2x5
    # miss:  HDR-TH (the next row)
    (
        r"^HDR-SMD",
        8.5,
        "2.54 mm SMD/female header, body ~8.5 mm (standard 2.54 header insulator)",
    ),
    # match: HDR-TH, HDR-TH-2.54 — also the table's largest value, which
    #        _FALLBACK reads
    # miss:  HDR-SMD
    (
        r"^HDR-TH",
        11.5,
        "2.54 mm through-hole pin header, overall ~11.5 mm (Samtec TSW class)",
    ),
    # match: SOT-223, SOT-223-5 — before ``^SOT-`` so it is not read as
    #        SOT-23 class
    # miss:  SOT-89 (the next row)
    (
        r"^SOT-223",
        1.8,
        "SOT-223, package drawing body max 1.8 mm (JEDEC TO-261)",
    ),
    # match: SOT-89, SOT-89-5 — before ``^SOT-`` for the same reason
    # miss:  SOT-223 (row above)
    (
        r"^SOT-89",
        1.6,
        "SOT-89, package drawing body max 1.6 mm (JEDEC TO-243)",
    ),
    # match: SOT-23, SOT-323, SOT-563
    # miss:  SOT-223/SOT-89 (rows above win), SOIC-8 (different prefix)
    (
        r"^SOT-",
        1.3,
        "SOT-23 class, package drawing body max ~1.3 mm (JEDEC TO-236 family)",
    ),
    # match: TSSOP, TSSOP-8, TSSOP20
    # miss:  SSOP-8 (SOIC-class row below), TQFP
    (
        r"^TSSOP",
        1.2,
        "TSSOP, package drawing body max 1.2 mm (JEDEC MO-153)",
    ),
    # match: SOIC-8, SO-8, SOP-8, QSOP-16, SSOP-28
    # miss:  SO8 (no dash form for the two-letter prefix), SOT-23
    (
        r"^(QSOP|SSOP|SOP|SOIC|SO-)",
        1.75,
        "SOP/SOIC/QSOP class, package drawing body max ~1.75 mm (JEDEC MS-012/013)",
    ),
    # match: QFN-32, TQFN, DFN-6, USON8, WSON, WSOF, HVSON
    # miss:  SON-8 (bare SON is not listed), QFP-64 (next row)
    (
        r"^(TQFN|QFN|HVSON|WSON|WSOF|DFN|USON)",
        1.0,
        "QFN/SON-class leadless, package drawing body max ~1.0 mm (JEDEC MO-220 family)",
    ),
    # match: QFP-64, LQFP-48, TQFP-100
    # miss:  QFN-32 (row above — different height class)
    (
        r"^(TQFP|LQFP|QFP)",
        1.6,
        "QFP class, package drawing body max ~1.6 mm (JEDEC MS-026)",
    ),
    # match: SW-SMD, SW-SMD-6x6
    # miss:  SW-TH
    (
        r"^SW-SMD",
        4.3,
        "SMD tact/DIP switch class, body max ~4.3 mm (C&K KMR/PTS families)",
    ),
)

_COMPILED: tuple[tuple[re.Pattern[str], float, str], ...] = tuple(
    (re.compile(pat), h, src) for pat, h, src in HEIGHT_TABLE
)

#: The fallback for a footprint no row matches: the table's largest
#: value, so an unmapped package never projects shorter than every
#: mapped class. ``(height_mm, source)``.
_FALLBACK: tuple[float, str] = (
    max(h for _, h, _ in HEIGHT_TABLE),
    "package table fallback — unmapped footprint, the table's largest value",
)


def height_mm(footprint: Any) -> tuple[float, str]:
    """The package-class maximum body height for ``footprint``, mm, and
    the source note a finding can quote. Total: an unmapped or empty
    footprint string takes the fallback, never ``None`` — a keep-out
    needs a number, and the caller records *that* this was one."""
    text = str(footprint or "")
    for pattern, h, source in _COMPILED:
        if pattern.search(text):
            return h, source
    return _FALLBACK


# --------------------------------------------------------------------------
# mechanical_profile — the 0041 bridge (mm profile dict)
# --------------------------------------------------------------------------


def placed_bbox(
    instances: Iterable[Mapping[str, Any]], *, margin_mm: float
) -> tuple[float, float, float, float]:
    """Bounding box (x0, y0, x1, y1) of the placed parts + a margin —
    the fallback board boundary when no explicit outline feature
    exists. ``precis.pcb.export.board_bbox`` is this over a model."""
    xs = [float(i["x"]) for i in instances if i.get("x") is not None]
    ys = [float(i["y"]) for i in instances if i.get("y") is not None]
    if not xs or not ys:
        return (0.0, 0.0, 50.0, 50.0)
    return (
        min(xs) - margin_mm,
        min(ys) - margin_mm,
        max(xs) + margin_mm,
        max(ys) + margin_mm,
    )


def mechanical_profile(
    instances: list[dict[str, Any]],
    features: list[dict[str, Any]],
    *,
    thickness_mm: float = DEFAULT_THICKNESS_MM,
    holes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Board outline + mounting holes + keepouts + component
    height-blocks as a 2.5D profile (the 0041 bridge), mm.

    ``instances`` are ``pcb_load``-shaped rows (``refdes``/``x``/``y``/
    ``layer``/``height_mm``). Outline: an explicit ``outline`` feature's
    ``geom.path`` if present, else the placed-parts bounding box. Holes:
    ``mounting_hole`` features with a ``geom.diameter`` — or, when
    ``holes`` is given (``{x, y, diameter}`` rows, e.g. the IR's
    mounting holes, which also carry every part's footprint holes at its
    current pose), exactly those. Keepouts: ``keepout`` features
    (``geom.poly`` + optional ``layers``/``reason``). Blocks: each
    placed instance's position × ``height_mm`` — a coarse keep-out
    volume for the lid."""
    feature_holes = []
    keepouts = []
    outline_path: list[list[float]] | None = None
    for f in features:
        ft = str(f.get("ftype") or "")
        geom = f.get("geom") or {}
        if ft == "outline" and isinstance(geom.get("path"), list):
            outline_path = [[float(p[0]), float(p[1])] for p in geom["path"]]
        elif ft == "mounting_hole":
            feature_holes.append(
                {
                    "x": f.get("x"),
                    "y": f.get("y"),
                    "diameter": geom.get("diameter"),
                }
            )
        elif ft == "keepout":
            keepouts.append(
                {
                    "poly": [
                        [float(p[0]), float(p[1])] for p in (geom.get("poly") or [])
                    ],
                    "layers": list(f.get("layers") or []),
                    "reason": f.get("reason"),
                }
            )
    if holes is None:
        holes = feature_holes
    if outline_path is None:
        x0, y0, x1, y1 = placed_bbox(instances, margin_mm=_BBOX_MARGIN_MM)
        outline_path = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    blocks = []
    for i in instances:
        if i.get("x") is None or i.get("y") is None:
            continue
        blocks.append(
            {
                "refdes": i["refdes"],
                "x": float(i["x"]),
                "y": float(i["y"]),
                "layer": i.get("layer") or "top",
                "height_mm": i.get("height_mm"),
            }
        )
    return {
        "units": "mm",
        "thickness_mm": thickness_mm,
        "outline": outline_path,
        "holes": holes,
        "blocks": blocks,
        "keepouts": keepouts,
    }


# --------------------------------------------------------------------------
# The derived 3-D shape (mm, board frame)
# --------------------------------------------------------------------------


def _is_bottom(layer: Any) -> bool:
    return str(layer or "top").lower() in ("bottom", "bot", "b")


@dataclass(frozen=True)
class Prism:
    """One placed part's keep-out column: its position, board-frame
    AABB ``extent_mm`` (``[x0, y0, x1, y1]`` post-rotation; ``None`` when
    the footprint gives no courtyard or pads — a point column), resolved
    ``height_mm`` with the honesty pair ``origin`` (``'user'`` /
    ``'proposed'``) + ``source``, and ``z_mm`` (bottom copper face = 0;
    bottom-side prisms extend negative)."""

    refdes: str
    x_mm: float
    y_mm: float
    side: str  # 'top' | 'bottom'
    height_mm: float
    origin: str  # 'user' | 'proposed'
    source: str | None
    rot_deg: float
    extent_mm: tuple[float, float, float, float] | None
    z_mm: tuple[float, float]
    footprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "refdes": self.refdes,
            "x_mm": self.x_mm,
            "y_mm": self.y_mm,
            "side": self.side,
            "height_mm": self.height_mm,
            "origin": self.origin,
            "rot_deg": self.rot_deg,
            "z_mm": list(self.z_mm),
        }
        if self.source is not None:
            d["source"] = self.source
        if self.footprint is not None:
            d["footprint"] = self.footprint
        if self.extent_mm is not None:
            d["extent_mm"] = list(self.extent_mm)
        return d


@dataclass(frozen=True)
class Hole:
    """A mounting hole as data: centre, diameter, ``through_mm`` (the
    slab thickness). A marker — nothing is subtracted (the subtraction
    question is carried, not decided)."""

    name: str
    x_mm: float
    y_mm: float
    diameter_mm: float
    through_mm: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "x_mm": self.x_mm,
            "y_mm": self.y_mm,
            "diameter_mm": self.diameter_mm,
            "through_mm": self.through_mm,
        }


@dataclass(frozen=True)
class Keepout:
    """A ``keepout`` feature: a layer-scoped polygon (board frame, mm).
    Recorded, not enforced — the fit check reports overlaps as warnings,
    which is exactly the not-yet-binding status the spec gives it."""

    poly: tuple[tuple[float, float], ...]
    layers: tuple[str, ...]
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "poly": [list(p) for p in self.poly],
            "layers": list(self.layers),
        }
        if self.reason is not None:
            d["reason"] = self.reason
        return d


@dataclass(frozen=True)
class FitFinding:
    """One fit-check row: ``severity`` ``'error'`` fails the check,
    ``'warn'`` is recorded context (a keepout is not yet binding; a
    covered hole is the carried subtraction question)."""

    severity: str
    code: str
    subject: str
    detail: str


@dataclass(frozen=True)
class FitResult:
    """The verdict of :meth:`BoardShape.fits`: ``ok`` (no error-tier
    findings — True only when every part that has a prism was checked),
    the finding list, a one-line ``summary()``, and the
    ``checked_parts``/``unchecked_parts`` counts behind it."""

    ok: bool
    findings: list[FitFinding]
    checked_parts: int = 0
    unchecked_parts: int = 0

    def summary(self) -> str:
        if self.unchecked_parts:
            head = (
                f"fit NOT CHECKED: {self.checked_parts} of "
                f"{self.checked_parts + self.unchecked_parts} parts checked "
                f"({len(self.findings)} finding(s))"
            )
        else:
            head = (
                f"fit {'OK' if self.ok else 'FAIL'} ({len(self.findings)} finding(s))"
            )
        if not self.findings:
            return head
        return (
            head
            + ":\n"
            + "\n".join(
                f"  {f.severity} {f.code} {f.subject}: {f.detail}"
                for f in self.findings
            )
        )


def _rotated_aabb(
    extent: tuple[float, float, float, float],
    rot_deg: float,
    x: float,
    y: float,
) -> tuple[float, float, float, float]:
    """A footprint-local bbox placed at ``(x, y)`` and turned ``rot_deg``
    about the part origin → its board-frame AABB. Exact at multiples of
    90°, conservative (the rotated-corners hull) otherwise."""
    x0, y0, x1, y1 = extent
    if rot_deg % 360.0:
        rad = math.radians(rot_deg)
        c, s = math.cos(rad), math.sin(rad)
        pts = [(x0 * c - y0 * s, x0 * s + y0 * c)]
        pts += [(x1 * c - y0 * s, x1 * s + y0 * c)]
        pts += [(x1 * c - y1 * s, x1 * s + y1 * c)]
        pts += [(x0 * c - y1 * s, x0 * s + y1 * c)]
        x0 = min(p[0] for p in pts)
        x1 = max(p[0] for p in pts)
        y0 = min(p[1] for p in pts)
        y1 = max(p[1] for p in pts)
    return (x0 + x, y0 + y, x1 + x, y1 + y)


@dataclass(frozen=True)
class BoardShape:
    """The board's 3-D shape as data — outline polygon, slab, one
    :class:`Prism` per placed part, :class:`Hole` centres/diameters,
    :class:`Keepout` regions — all mm, board frame, z = 0 at the bottom
    copper face. ``profile`` is the :func:`mechanical_profile` dict the
    shape derives from (``shape.profile`` IS the byte-shape the trusted
    side serves). ``unplaced`` names parts with no pose — they have no
    prism, and honesty says to say so."""

    outline: list[list[float]] = field(default_factory=list)
    thickness_mm: float = 0.0
    prisms: list[Prism] = field(default_factory=list)
    holes: list[Hole] = field(default_factory=list)
    keepouts: list[Keepout] = field(default_factory=list)
    unplaced: list[str] = field(default_factory=list)
    profile: dict[str, Any] = field(default_factory=dict)
    why_not: str | None = None
    units: str = "mm"

    @property
    def ok(self) -> bool:
        return self.why_not is None

    @property
    def centre_mm(self) -> tuple[float, float]:
        xs = [p[0] for p in self.outline]
        ys = [p[1] for p in self.outline]
        return ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)

    @property
    def bottom_extent_mm(self) -> float:
        """Tallest bottom-side prism — the depth the shape reaches below
        the bottom copper face."""
        return max(
            (p.height_mm for p in self.prisms if p.side == "bottom"),
            default=0.0,
        )

    @property
    def top_extent_mm(self) -> float:
        return max(
            (p.height_mm for p in self.prisms if p.side == "top"),
            default=0.0,
        )

    def envelope_mm(self) -> dict[str, float]:
        """The one conservative box over slab + prisms (outline bbox ×
        (bottom extent + thickness + top extent)) — the same envelope the
        se side emits as a ``box:`` DSL spec, in board-frame mm."""
        xs = [p[0] for p in self.outline]
        ys = [p[1] for p in self.outline]
        return {
            "w": max(xs) - min(xs),
            "d": max(ys) - min(ys),
            "h": self.bottom_extent_mm + self.thickness_mm + self.top_extent_mm,
            "z0": -self.bottom_extent_mm,
            "z1": self.thickness_mm + self.top_extent_mm,
        }

    def solids_mm(
        self,
    ) -> list[tuple[str, tuple[float, float, float, float, float, float]]]:
        """Every solid as ``(name, (x0, y0, z0, x1, y1, z1))`` board-frame
        AABBs — the slab (outline bbox × [0, t]) plus each prism (extent
        or point column). The multi-solid list the fit check iterates."""
        out: list[tuple[str, tuple[float, float, float, float, float, float]]] = []
        xs = [p[0] for p in self.outline]
        ys = [p[1] for p in self.outline]
        out.append(
            ("slab", (min(xs), min(ys), 0.0, max(xs), max(ys), self.thickness_mm))
        )
        for p in self.prisms:
            x0, y0, x1, y1 = p.extent_mm or (p.x_mm, p.y_mm, p.x_mm, p.y_mm)
            out.append((p.refdes, (x0, y0, p.z_mm[0], x1, y1, p.z_mm[1])))
        return out

    def fits(
        self,
        other: Mapping[str, Any] | Iterable[Mapping[str, Any]],
        *,
        clearance_mm: float = 0.0,
    ) -> FitResult:
        """The fit-check callable: this shape against an se part or an
        enclosure — one box spec or a list of them (the multi-solid
        call). Each spec is ``{w, d, h, at=(cx, cy, z_base), role}``
        (helpers :func:`box`/:func:`enclosure` build them; mm, ``units=
        'm'`` converts at this declared seam). ``role='enclosure'`` asks
        "does the whole board fit inside this interior cavity";
        ``role='part'`` (default) asks "does this solid stay clear of the
        board's solids". Parts with no pose (``self.unplaced``) have no
        prism and were never checked — an error-tier
        ``fit.parts_unplaced`` finding names them and fails ``ok``, so a
        bare-slab pass can never read as "fits". Keepout and hole
        coverage are warn-tier findings, never errors —
        recorded-not-enforced and the carried subtraction question
        respectively. A malformed spec raises ``PcbBuilderError`` naming
        the problem."""
        solids = [_as_box(o) for o in _as_specs(other)]
        findings: list[FitFinding] = []
        if self.why_not is not None:
            findings.append(FitFinding("error", "fit.no_shape", "board", self.why_not))
            return FitResult(ok=False, findings=findings)
        if self.unplaced:
            shown = ", ".join(self.unplaced[:8])
            more = len(self.unplaced) - 8
            if more > 0:
                shown += f", +{more} more"
            findings.append(
                FitFinding(
                    "error",
                    "fit.parts_unplaced",
                    "board",
                    f"{len(self.unplaced)} of "
                    f"{len(self.prisms) + len(self.unplaced)} parts have no "
                    f"pose and were NOT checked: {shown} — next: place parts "
                    "with part.at(...) or run pcb.local.place / the place "
                    "service, then re-run fits()",
                )
            )
        for spec in solids:
            if spec["role"] == "enclosure":
                _check_containment(self, spec, clearance_mm, findings)
            else:
                _check_interference(self, spec, clearance_mm, findings)
        return FitResult(
            ok=not any(f.severity == "error" for f in findings),
            findings=findings,
            checked_parts=len(self.prisms),
            unchecked_parts=len(self.unplaced),
        )

    def to_dict(self) -> dict[str, Any]:
        """The JSON-able data surface a workspace script can print."""
        d: dict[str, Any] = {
            "units": self.units,
            "outline": self.outline,
            "thickness_mm": self.thickness_mm,
            "slab_z_mm": [0.0, self.thickness_mm],
            "prisms": [p.to_dict() for p in self.prisms],
            "holes": [h.to_dict() for h in self.holes],
            "keepouts": [k.to_dict() for k in self.keepouts],
        }
        if self.unplaced:
            d["unplaced"] = list(self.unplaced)
        if self.why_not is not None:
            d["why_not"] = self.why_not
        else:
            d["envelope_mm"] = self.envelope_mm()
        return d


def derive_shape(
    profile: Mapping[str, Any],
    *,
    details: Mapping[str, Mapping[str, Any]] | None = None,
    unplaced: Iterable[str] = (),
) -> BoardShape:
    """One board's :func:`mechanical_profile` (mm) → its itemised 3-D
    shape (mm, board frame, z = 0 at the bottom copper face).

    ``details`` maps refdes → the extras the profile's blocks don't
    carry: ``footprint`` (the free-text name the height table reads —
    the se path passes the ``pcb_load`` row, the builder the authored
    ``part.footprint``), ``extent_mm`` (footprint-local courtyard/pads
    bbox ``[x0, y0, x1, y1]``), ``rot_deg``. All optional: a missing
    footprint means the table fallback; a missing extent means a point
    column.

    Never raises on shape: a profile with no outline reports
    ``why_not`` instead of inventing a board — the same honesty
    ``precis_se.board.derive_board`` gives its callers."""
    outline = [[float(p[0]), float(p[1])] for p in (profile.get("outline") or [])]
    if not outline:
        return BoardShape(
            why_not="board has no outline and no placed parts",
            profile=dict(profile),
            unplaced=list(unplaced),
        )
    xs = [p[0] for p in outline]
    ys = [p[1] for p in outline]
    w_mm, d_mm = max(xs) - min(xs), max(ys) - min(ys)
    t_mm = float(profile.get("thickness_mm") or 0.0)
    if w_mm <= 0.0 or d_mm <= 0.0 or t_mm <= 0.0:
        return BoardShape(
            outline=outline,
            thickness_mm=t_mm,
            profile=dict(profile),
            unplaced=list(unplaced),
            why_not=(
                "board outline or thickness is degenerate "
                f"(w={w_mm:g} mm, d={d_mm:g} mm, t={t_mm:g} mm)"
            ),
        )

    details = details or {}
    prisms: list[Prism] = []
    for block in profile.get("blocks") or []:
        refdes = str(block.get("refdes"))
        side = "bottom" if _is_bottom(block.get("layer")) else "top"
        detail = details.get(refdes) or {}
        own = block.get("height_mm")
        if own is not None:
            h_mm, origin, source = float(own), "user", None
        else:
            h_mm, source = height_mm(detail.get("footprint"))
            origin = "proposed"
        # The local extent is used AS-IS for bottom-side parts: the
        # mechanical profile mirrors the silk/fiducial convention
        # (``silk.world_courtyard_rings`` keeps ``mirror=False`` — "a
        # soft preference, not a clearance guarantee"), not the
        # occupancy courtyard, which mirrors since 2026-10-25
        # (``ir.world_courtyard_polygons``).
        local = detail.get("extent_mm")
        rot = float(detail.get("rot_deg") or 0.0)
        bx, by = float(block["x"]), float(block["y"])
        extent = (
            _rotated_aabb(
                (float(local[0]), float(local[1]), float(local[2]), float(local[3])),
                rot,
                bx,
                by,
            )
            if local is not None
            else None
        )
        prisms.append(
            Prism(
                refdes=refdes,
                x_mm=bx,
                y_mm=by,
                side=side,
                height_mm=h_mm,
                origin=origin,
                source=source,
                rot_deg=rot,
                extent_mm=extent,
                z_mm=(t_mm, t_mm + h_mm) if side == "top" else (-h_mm, 0.0),
                footprint=(
                    str(detail["footprint"])
                    if detail.get("footprint") is not None
                    else None
                ),
            )
        )

    holes: list[Hole] = []
    for n, hole in enumerate(profile.get("holes") or [], start=1):
        if hole.get("x") is None or hole.get("y") is None:
            continue
        holes.append(
            Hole(
                name=f"H{n}",
                x_mm=float(hole["x"]),
                y_mm=float(hole["y"]),
                diameter_mm=float(hole.get("diameter") or 0.0),
                through_mm=t_mm,
            )
        )

    keepouts = [
        Keepout(
            poly=tuple((float(p[0]), float(p[1])) for p in (k.get("poly") or [])),
            layers=tuple(str(l) for l in (k.get("layers") or [])),
            reason=k.get("reason"),
        )
        for k in (profile.get("keepouts") or [])
    ]
    return BoardShape(
        outline=outline,
        thickness_mm=t_mm,
        prisms=prisms,
        holes=holes,
        keepouts=keepouts,
        unplaced=list(unplaced),
        profile=dict(profile),
    )


# --------------------------------------------------------------------------
# The fit check — box specs in board-frame mm
# --------------------------------------------------------------------------

#: The declared unit seam for counterparty specs (``units=``). pcb is
#: the mm enclave; an se-side number arrives in metres and converts
#: exactly once, here — never silently.
_UNIT_TO_MM = {"mm": 1.0, "m": 1000.0}

#: ``role`` vocabulary for :meth:`BoardShape.fits` counterparty specs.
_SPEC_ROLES = ("part", "enclosure")

#: Required keys on a fit spec.
_SPEC_DIMS = ("w", "d", "h")


def box(
    w: float,
    d: float,
    h: float,
    *,
    at: tuple[float, float, float] | None = None,
    role: str = "part",
    units: str = "mm",
    name: str | None = None,
) -> dict[str, Any]:
    """One counterparty solid for :meth:`BoardShape.fits`: an
    axis-aligned box ``w × d × h`` whose ``at`` is its x/y centre +
    z base in the board frame (the cad ``box`` anchor convention).
    ``role='part'`` (default) checks non-interference, ``role=
    'enclosure'`` checks the board fits inside it."""
    return {
        "w": w,
        "d": d,
        "h": h,
        "at": list(at) if at is not None else None,
        "role": role,
        "units": units,
        "name": name or role,
    }


def enclosure(
    w: float,
    d: float,
    h: float,
    *,
    at: tuple[float, float, float] | None = None,
    units: str = "mm",
    name: str | None = None,
) -> dict[str, Any]:
    """``role='enclosure'`` shorthand for :func:`box` — the interior
    cavity the whole board (slab + every prism) must fit inside."""
    return box(w, d, h, at=at, role="enclosure", units=units, name=name)


def _as_specs(
    other: Mapping[str, Any] | Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    if isinstance(other, Mapping):
        return [other]
    return list(other)


def _as_box(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise one spec to a resolved board-frame box:
    ``{w, d, h, cx, cy, z_base, role, name}`` in mm. A malformed spec —
    a missing dimension, an unknown ``role`` or ``units``, a non-numeric
    or non-finite or non-positive dimension, a bad ``at`` — raises
    ``PcbBuilderError`` (the same refusal vocabulary the builder uses)
    naming the allowed values, never a bare ``KeyError``."""
    from precis_geom.pcb import PcbBuilderError  # deferred: pcb imports this module

    problems: list[str] = []
    missing = [k for k in _SPEC_DIMS if spec.get(k) is None]
    if missing:
        problems.append(f"missing keys {missing} (required: {list(_SPEC_DIMS)})")
    role = str(spec.get("role") or "part")
    if role not in _SPEC_ROLES:
        problems.append(f"role must be one of {list(_SPEC_ROLES)}, got {role!r}")
    units = str(spec.get("units") or "mm")
    if units not in _UNIT_TO_MM:
        problems.append(f"units must be one of {sorted(_UNIT_TO_MM)}, got {units!r}")
    dims: dict[str, float] = {}
    for k in _SPEC_DIMS:
        if spec.get(k) is None:
            continue
        try:
            dims[k] = float(spec[k])
        except (TypeError, ValueError):
            problems.append(f"{k} must be a number, got {spec[k]!r}")
    bad = {k: v for k, v in dims.items() if not math.isfinite(v) or v <= 0.0}
    if bad:
        problems.append(f"dimensions must be finite and positive, got {bad}")
    at = spec.get("at")
    if at is not None:
        try:
            at3 = tuple(float(c) for c in at)
        except (TypeError, ValueError):
            problems.append(f"at must be three numbers (cx, cy, z_base), got {at!r}")
        else:
            if len(at3) != 3 or any(not math.isfinite(c) for c in at3):
                problems.append(
                    f"at must be three finite numbers (cx, cy, z_base), got {at!r}"
                )
    if problems:
        raise PcbBuilderError(f"fit spec {dict(spec)!r}: " + "; ".join(problems))

    scale = _UNIT_TO_MM[units]
    cx, cy, z_base = (
        (float(at[0]) * scale, float(at[1]) * scale, float(at[2]) * scale)
        if at is not None
        else (None, None, None)
    )
    return {
        "w": dims["w"] * scale,
        "d": dims["d"] * scale,
        "h": dims["h"] * scale,
        "cx": cx,
        "cy": cy,
        "z_base": z_base,
        "role": role,
        "name": str(spec.get("name") or spec.get("role") or "part"),
    }


def _spec_aabb(
    shape: BoardShape, spec: Mapping[str, Any]
) -> tuple[float, float, float, float, float, float]:
    """The spec's resolved ``(x0, y0, z0, x1, y1, z1)``: ``at`` defaults
    to the outline-bbox centre x/y and, for an enclosure, the lowest
    solid's underside as the cavity base; a part defaults to the slab's
    top face."""
    cx = spec["cx"] if spec["cx"] is not None else shape.centre_mm[0]
    cy = spec["cy"] if spec["cy"] is not None else shape.centre_mm[1]
    if spec["z_base"] is not None:
        z0 = spec["z_base"]
    else:
        z0 = (
            -shape.bottom_extent_mm
            if spec["role"] == "enclosure"
            else shape.thickness_mm
        )
    return (
        cx - spec["w"] / 2.0,
        cy - spec["d"] / 2.0,
        z0,
        cx + spec["w"] / 2.0,
        cy + spec["d"] / 2.0,
        z0 + spec["h"],
    )


def _assumed_height_notes(shape: BoardShape) -> dict[str, str]:
    """refdes → the honesty tail a fit finding appends when that prism's
    height is the table fallback (gr513752): the fallback was silent, so
    any finding naming the part must say the height was assumed — and
    name the fix (``height_mm=`` on ``b.part``)."""
    out: dict[str, str] = {}
    for p in shape.prisms:
        if p.source != _FALLBACK[1]:
            continue
        why = (
            f"no height for footprint {p.footprint!r}"
            if p.footprint
            else "no footprint and no height"
        )
        out[p.refdes] = (
            f"height assumed {p.height_mm:g} mm ({why}; pass height_mm= on b.part)"
        )
    return out


def _check_containment(
    shape: BoardShape,
    spec: Mapping[str, Any],
    clearance_mm: float,
    findings: list[FitFinding],
) -> None:
    assumed = _assumed_height_notes(shape)
    bx0, by0, bz0, bx1, by1, bz1 = _spec_aabb(shape, spec)
    for name, (x0, y0, z0, x1, y1, z1) in shape.solids_mm():
        over = [
            (
                "x",
                bx0 + clearance_mm - x0,
                f"left {x0:.4g} < {bx0 + clearance_mm:.4g}",
            ),
            (
                "x",
                x1 - (bx1 - clearance_mm),
                f"right {x1:.4g} > {bx1 - clearance_mm:.4g}",
            ),
            (
                "y",
                by0 + clearance_mm - y0,
                f"front {y0:.4g} < {by0 + clearance_mm:.4g}",
            ),
            (
                "y",
                y1 - (by1 - clearance_mm),
                f"back {y1:.4g} > {by1 - clearance_mm:.4g}",
            ),
            (
                "z",
                bz0 + clearance_mm - z0,
                f"bottom {z0:.4g} < {bz0 + clearance_mm:.4g}",
            ),
            (
                "z",
                z1 - (bz1 - clearance_mm),
                f"top {z1:.4g} > {bz1 - clearance_mm:.4g}",
            ),
        ]
        bad = [(ax, amt, why) for ax, amt, why in over if amt > 0.0]
        if bad:
            detail = (
                f"{name} exceeds enclosure {spec['name']!r}: "
                + ", ".join(f"{ax} by {amt:.3g} mm ({why})" for ax, amt, why in bad)
                + f" (cavity {bx1 - bx0:.4g}×"
                f"{by1 - by0:.4g}×{bz1 - bz0:.4g} mm)"
            )
            if name in assumed:
                detail += f"; {assumed[name]}"
            findings.append(FitFinding("error", "fit.outside_enclosure", name, detail))


def _overlap(
    a: tuple[float, float, float, float, float, float],
    b: tuple[float, float, float, float, float, float],
    clearance_mm: float,
) -> float | None:
    """The least single-axis separation shortfall between two AABBs —
    ``None`` when they clear by ≥ ``clearance_mm`` on some axis."""
    gap = max(
        a[0] - b[3],
        b[0] - a[3],
        a[1] - b[4],
        b[1] - a[4],
        a[2] - b[5],
        b[2] - a[5],
    )
    shortfall = clearance_mm - gap
    return shortfall if shortfall > 0.0 else None


def _check_interference(
    shape: BoardShape,
    spec: Mapping[str, Any],
    clearance_mm: float,
    findings: list[FitFinding],
) -> None:
    """``role='part'`` half of the fit check: AABB interference vs the
    slab and every prism, plus the two warn-tier coverage reports — a
    covered mounting hole, and a keepout overlap tested against the
    keepout polygon's own bounding box (layers and z are ignored: a
    keepout is recorded-not-enforced data, and the rough box is
    deliberately 2.5-D)."""
    assumed = _assumed_height_notes(shape)
    s = _spec_aabb(shape, spec)
    outline_xs = [p[0] for p in shape.outline]
    outline_ys = [p[1] for p in shape.outline]
    if (
        s[0] < min(outline_xs)
        or s[3] > max(outline_xs)
        or s[1] < min(outline_ys)
        or s[4] > max(outline_ys)
    ):
        findings.append(
            FitFinding(
                "warn",
                "fit.overhanging_part",
                spec["name"],
                f"{spec['name']!r}'s footprint reaches past the board "
                "outline (edge-mating parts overhang legitimately)",
            )
        )
    for name, solid in shape.solids_mm():
        shortfall = _overlap(solid, s, clearance_mm)
        if shortfall is not None:
            detail = (
                f"{spec['name']!r} comes within {shortfall:.3g} mm of "
                f"{name} (clearance {clearance_mm:g} mm)"
            )
            if name in assumed:
                detail += f"; {assumed[name]}"
            findings.append(FitFinding("error", "fit.interference", name, detail))
    for hole in shape.holes:
        if s[0] <= hole.x_mm <= s[3] and s[1] <= hole.y_mm <= s[4]:
            findings.append(
                FitFinding(
                    "warn",
                    "fit.hole_covered",
                    hole.name,
                    f"{spec['name']!r} covers mounting hole {hole.name} "
                    f"(Ø{hole.diameter_mm:g} mm) — fasteners need it",
                )
            )
    for n, keepout in enumerate(shape.keepouts, start=1):
        if not keepout.poly:
            continue
        kxs = [p[0] for p in keepout.poly]
        kys = [p[1] for p in keepout.poly]
        if not (
            s[3] < min(kxs) or s[0] > max(kxs) or s[4] < min(kys) or s[1] > max(kys)
        ):
            findings.append(
                FitFinding(
                    "warn",
                    "fit.keepout",
                    f"keepout#{n}",
                    f"{spec['name']!r}'s footprint overlaps keepout#{n}"
                    + (f" ({keepout.reason})" if keepout.reason else "")
                    + " — recorded, not enforced (keepout binding is open)",
                )
            )


def fit(
    shape: BoardShape,
    other: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    *,
    clearance_mm: float = 0.0,
) -> FitResult:
    """Module-level spelling of :meth:`BoardShape.fits` — the multi-solid
    call the spec names (one call, a box spec or a list of them)."""
    return shape.fits(other, clearance_mm=clearance_mm)


__all__ = [
    "DEFAULT_THICKNESS_MM",
    "HEIGHT_TABLE",
    "BoardShape",
    "FitFinding",
    "FitResult",
    "Hole",
    "Keepout",
    "Prism",
    "box",
    "derive_shape",
    "enclosure",
    "fit",
    "height_mm",
    "mechanical_profile",
    "placed_bbox",
]
