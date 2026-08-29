"""Synthesized default land patterns — per-pin pad offsets when no real
footprint is cached.

**Why this exists.** :class:`precis.pcb.ir.PcbIR` carries ``inst_x``/
``inst_y`` per INSTANCE and nothing per PIN. Every geometric consumer
therefore reads the instance centroid for every one of that part's pins:
``realize._instance_point``, ``cost``'s crossing/coupling/loop terms, and
``ir.same_layer_crossing_count``. So a 14-pin MCU emits 14 tracks on 14
different nets that all START AT THE SAME COORDINATE.

Measured consequences (2026-08-28 acceptance run):

* ~600 ``clearance`` DRC errors at an **exact 0.000mm** gap — not
  near-misses a better router could close. Two nets' tracks are coincident
  before any routing happens, so no routing algorithm can separate them.
* ``crossings`` is computed between centroids, so it cannot see whether two
  nets leaving one part actually cross — the topological objective is
  measured on a degenerate graph.
* Three of eight move classes are provably cost-neutral for want of this
  data: ``ROTATE`` (``optimize.py``: "its ``total()`` delta is a true,
  provable zero"), ``SIDE_FLIP`` ("structurally blind" at centroid
  granularity), and ``PIN_SWAP`` (its payoff is computed by
  :mod:`precis.pcb.pinswap`'s own evaluator and never read by ``total()``).

**Why synthesize rather than require a real footprint.** Real pad geometry
lives in ``part_footprints``, populated from EasyEDA per C-number. A design
authored with ``label`` only — which the whole netlist-first workflow
encourages, and which the acceptance fixture uses — has none. Refusing to
place pins without it would make pin geometry available only to designs
that had already chosen concrete parts, i.e. never during early layout,
which is exactly when placement decisions are made.

**These are BOUNDS, not measurements**, in the same sense as
:attr:`precis.pcb.cost.TermValue.is_bound`: a synthesized pattern is
dimensionally plausible for its pin count but is NOT the real part. It
must never be exported to fabrication, and a caller that has a real
footprint must always prefer it. :func:`offsets_for` returns
``(offsets, synthesized)`` so the flag cannot be dropped by accident.
"""

from __future__ import annotations

import math

#: Default centre-to-centre pad pitch (mm) for a synthesized multi-pin
#: package. 0.65mm is a common fine-pitch value (SOP/QFN family) and sits
#: mid-range: fine enough that a synthesized part is not absurdly large,
#: coarse enough that adjacent pads do not land inside one clearance
#: envelope and manufacture a wall of false clearance errors.
DEFAULT_PITCH_MM = 0.65

#: Pitch for a 2-pin passive — chip-package pads sit further apart than a
#: fine-pitch IC's. ~1.0mm matches an 0402/0603 land pattern closely enough
#: for placement purposes.
PASSIVE_PITCH_MM = 1.0

#: Header pitch, the one dimension that is a de-facto standard rather than
#: a guess (2.54mm = 0.1in). Applied when a part is recognisably a header.
HEADER_PITCH_MM = 2.54


def _two_pin(pitch: float) -> list[tuple[float, float]]:
    """A passive: two pads either side of the origin on the X axis."""
    half = pitch / 2.0
    return [(-half, 0.0), (half, 0.0)]


def _dual_row(n: int, pitch: float) -> list[tuple[float, float]]:
    """SOIC/DIP-style: two rows, pin 1 top-left, counterclockwise.

    Counterclockwise from pin 1 is the IC numbering convention, and it
    matters here rather than being cosmetic: pad ORDER determines which
    pins are adjacent, which is what makes a pin swap reduce or increase
    crossings. A wrong order yields plausible-looking geometry that
    optimises toward the wrong swaps.
    """
    per_row = (n + 1) // 2
    span = (per_row - 1) * pitch
    row_gap = max(2.0, span * 0.6)
    half_gap = row_gap / 2.0
    out: list[tuple[float, float]] = []
    for i in range(per_row):  # left column, top to bottom
        out.append((-half_gap, span / 2.0 - i * pitch))
    for i in range(n - per_row):  # right column, bottom to top
        out.append((half_gap, -span / 2.0 + i * pitch))
    return out


def _quad(n: int, pitch: float) -> list[tuple[float, float]]:
    """QFN/QFP-style: pads on four sides, counterclockwise from top-left."""
    per_side = max(1, (n + 3) // 4)
    span = (per_side - 1) * pitch
    half = max(1.5, span / 2.0 + pitch)
    out: list[tuple[float, float]] = []
    for i in range(per_side):  # left, top to bottom
        out.append((-half, span / 2.0 - i * pitch))
    for i in range(per_side):  # bottom, left to right
        out.append((-span / 2.0 + i * pitch, -half))
    for i in range(per_side):  # right, bottom to top
        out.append((half, -span / 2.0 + i * pitch))
    for i in range(per_side):  # top, right to left
        out.append((span / 2.0 - i * pitch, half))
    return out[:n]


def _single_row(n: int, pitch: float) -> list[tuple[float, float]]:
    """Header-style: one row along X, pin 1 leftmost."""
    span = (n - 1) * pitch
    return [(-span / 2.0 + i * pitch, 0.0) for i in range(n)]


def offsets_for(
    n_pins: int, *, label: str | None = None
) -> tuple[list[tuple[float, float]], bool]:
    """Synthesized ``(dx, dy)`` per pin, footprint-local mm, plus a flag.

    Returns ``(offsets, synthesized)``. ``synthesized`` is always ``True``
    here — the tuple shape exists so a caller that MAY have a real
    footprint keeps one code path and cannot silently lose the
    distinction. Offsets are centred on the origin, so the instance
    centroid stays the rotation pivot (matching ``padplace``'s convention:
    mirror, then rotate clockwise-from-north, then translate).

    Package family is inferred only from pin count and an optional
    ``label`` hint. This is deliberately crude: it produces geometry that
    is dimensionally sane and, critically, gives distinct pins DISTINCT
    coordinates. It does not claim to be the real part.
    """
    if n_pins <= 0:
        return [], True

    hint = (label or "").lower()
    is_header = any(k in hint for k in ("header", "conn", "jst", "pinhdr", "2.54"))

    if n_pins == 1:
        return [(0.0, 0.0)], True
    if is_header:
        return _single_row(n_pins, HEADER_PITCH_MM), True
    if n_pins == 2:
        return _two_pin(PASSIVE_PITCH_MM), True
    if n_pins <= 4:
        return _single_row(n_pins, DEFAULT_PITCH_MM * 2.0), True
    if n_pins <= 16:
        return _dual_row(n_pins, DEFAULT_PITCH_MM), True
    return _quad(n_pins, DEFAULT_PITCH_MM), True


def rotate_offset(
    dx: float, dy: float, rot_deg: float, *, mirrored: bool = False
) -> tuple[float, float]:
    """Place a footprint-local offset into board space for one instance.

    Mirror BEFORE rotate — the order ``padplace`` fixed and pinned with a
    test. Reversing it yields a bottom-side part that is subtly wrong in a
    way that looks plausible in a render.

    ``rot_deg`` follows the board frame's convention: **clockwise from
    north**, per ``precis-pcb-help``. That is not the mathematical
    convention, so the sign here is deliberate rather than a bug.
    """
    if mirrored:
        dx = -dx
    theta = math.radians(rot_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    # Clockwise rotation: [[cos, sin], [-sin, cos]].
    return (dx * cos_t + dy * sin_t, -dx * sin_t + dy * cos_t)
