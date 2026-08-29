"""Synthesized default land patterns — per-pin pad OFFSETS and SIZES when
no real footprint is cached.

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

**Size is a second, independent bound — added 2026-08-29, same defect
class as the offset one above.** Before :func:`sizes_for` existed, every
pad in the whole engine (router obstacle grid, DRC, the pre-fab-parts
gerber preview) was a flat 0.2mm-radius circle — a 2-pin passive's chip
pad, a 48-pin QFN's fine-pitch lead and a THT header pin all reserved the
literally identical disc on the routing grid, cleared the same DRC
distance, and flashed the same aperture. :func:`sizes_for` derives a
``(w, h, shape)`` per pin from the SAME package-family inference
:func:`offsets_for` already makes (:func:`_family_for` is the one place
that inference now lives, so the two functions cannot drift into
disagreeing about what family an ``n_pins``/``label`` pair is), so
different families get dimensionally different, non-circular pads. It is
still a BOUND, not a measurement — real per-footprint pad geometry
(``part_footprints.pads``, cached from EasyEDA) is the only ground truth,
and :mod:`precis.pcb.ir` prefers it per-pin when a caller supplies it,
falling back to this synthesis only where no real footprint is wired in
yet. ``synthesized=True`` from this module always means "dimensionally
plausible guess," never "measured."
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


#: Package-family tokens :func:`_family_for` chooses among. Both
#: :func:`offsets_for` and :func:`sizes_for` switch on this SAME token —
#: the one place "what kind of part is this" gets decided, so the two
#: functions cannot independently drift into disagreeing about it (the
#: exact failure mode named in the module docstring: two call sites
#: deriving the same fact and quietly diverging).
_SINGLE = "single"
_HEADER = "header"
_PASSIVE = "passive"
_ROW = "row"
_DUAL = "dual"
_QUAD = "quad"


def _family_for(n_pins: int, *, label: str | None = None) -> str:
    """The one package-family inference, shared by :func:`offsets_for` and
    :func:`sizes_for`. Deliberately crude — pin count and an optional
    ``label`` hint only — see :func:`offsets_for`'s own docstring for why
    that is an acceptable bound rather than a real footprint lookup."""
    if n_pins == 1:
        return _SINGLE
    hint = (label or "").lower()
    if any(k in hint for k in ("header", "conn", "jst", "pinhdr", "2.54")):
        return _HEADER
    if n_pins == 2:
        return _PASSIVE
    if n_pins <= 4:
        return _ROW
    if n_pins <= 16:
        return _DUAL
    return _QUAD


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
    ``label`` hint (:func:`_family_for`). This is deliberately crude: it
    produces geometry that is dimensionally sane and, critically, gives
    distinct pins DISTINCT coordinates. It does not claim to be the real
    part.
    """
    if n_pins <= 0:
        return [], True

    family = _family_for(n_pins, label=label)
    if family == _SINGLE:
        return [(0.0, 0.0)], True
    if family == _HEADER:
        return _single_row(n_pins, HEADER_PITCH_MM), True
    if family == _PASSIVE:
        return _two_pin(PASSIVE_PITCH_MM), True
    if family == _ROW:
        return _single_row(n_pins, DEFAULT_PITCH_MM * 2.0), True
    if family == _DUAL:
        return _dual_row(n_pins, DEFAULT_PITCH_MM), True
    return _quad(n_pins, DEFAULT_PITCH_MM), True


#: Pitch each family's :func:`sizes_for` pad is scaled off — the SAME
#: figure that family's :func:`offsets_for` branch places pins at (see
#: table below each function). Keeping the mapping here, keyed by the
#: shared :func:`_family_for` token, is what lets a pad's size and its
#: pin-to-pin spacing be derived from one number instead of two that could
#: drift apart.
_FAMILY_PITCH_MM: dict[str, float] = {
    _SINGLE: PASSIVE_PITCH_MM,
    _HEADER: HEADER_PITCH_MM,
    _PASSIVE: PASSIVE_PITCH_MM,
    _ROW: DEFAULT_PITCH_MM * 2.0,
    _DUAL: DEFAULT_PITCH_MM,
    _QUAD: DEFAULT_PITCH_MM,
}

#: Shape vocabulary :func:`sizes_for` emits — the same three tokens
#: :mod:`precis.pcb.gerber`'s aperture table accepts (module docstring's
#: ``_SHAPE_MAP`` in :mod:`precis.pcb.padplace`, mirrored here rather than
#: imported: this module has no other reason to depend on that one).
PAD_SHAPE_CIRCLE = "circle"
PAD_SHAPE_RECT = "rect"

#: The pitch-constrained pad dimension, as a fraction of the family pitch
#: — the axis that (for a dual-row or quad package) sits ALONG the row a
#: pin marches down, so IT is what must stay clear of the next pin's own
#: pad. 0.25 leaves 75% of the pitch as clearance even at the tightest
#: (0.65mm, QFN/dual) family — comfortably above JLC's 0.09mm minimum
#: copper clearance with room to spare for the router's own dilation.
#:
#: **Measured, not guessed** — an earlier, still dimensionally-plausible
#: pair (0.35 / 0.65, see :func:`_LONG_FRACTION`'s sibling history) passed
#: every unit test here but cost the ESP32-C3 acceptance fixture
#: (``tests/test_pcb_reference_end_to_end.py``, seed 1) its zero-DRC/
#: full-route baseline: 10 findings (6 ``connectivity`` + 4 ``unrouted``,
#: read back from the DRC view) on GND/EN/RXD/TXD/SCL, all nets running
#: through a dense cluster of 2-pin decoupling caps whose pad radius had
#: nearly doubled. These two fractions are what restored 0 DRC errors /
#: 11 of 11 fanout>=2 nets routed at that same seed — still a real,
#: non-degenerate pad size (comfortably above JLC's manufacturing floor),
#: just the more conservative end of the plausible range rather than the
#: middle of it. A synthesized pattern is a BOUND (module docstring); this
#: is the bound that keeps the router's job possible.
_TIGHT_FRACTION = 0.25
#: The outward pad dimension (away from the part body), also expressed as
#: a fraction of pitch rather than a free constant: :func:`sizes_for` has
#: no per-pin orientation data (that is real per-footprint metadata a
#: synthesized bound does not have — see the module docstring), so it
#: cannot know which of a rect pad's two axes is actually "along the row"
#: for any GIVEN pin. Keeping this fraction under 1.0 too means BOTH
#: axes independently clear the same-family adjacent-pad spacing
#: regardless of which one turns out to be the row axis, rather than
#: only the intentionally-"tight" one. See :data:`_TIGHT_FRACTION`'s
#: docstring for why this is 0.45 and not a larger, equally-plausible
#: value.
_LONG_FRACTION = 0.45
#: Absolute clamps (mm) so an extreme pitch (a 1-pin part's fallback, or a
#: future family) never produces a vanishing or absurd pad.
_TIGHT_CLAMP = (0.20, 1.0)
_LONG_CLAMP = (0.25, 1.5)
#: The single-pin and header families get a round pad sized like a real
#: THT/testpoint pad, independent of their (much wider) pitch — a header's
#: pin spacing has nothing to do with how big its own annular ring is.
_ROUND_PAD_DIA_MM = 1.0


def _clamp(value: float, bounds: tuple[float, float]) -> float:
    lo, hi = bounds
    return max(lo, min(hi, value))


def sizes_for(
    n_pins: int, *, label: str | None = None
) -> tuple[list[tuple[float, float, str]], bool]:
    """Synthesized ``(w, h, shape)`` per pin, mm, plus a flag — the SIZE
    counterpart to :func:`offsets_for`, added 2026-08-29 (module
    docstring's "size is a second, independent bound" section).

    Returns ``(sizes, synthesized)`` with the same always-``True`` shape
    discipline as :func:`offsets_for` (a caller that has real per-pin
    footprint geometry must prefer it and mark that pin's ``synthesized``
    ``False`` itself — this function only ever produces a bound).

    Package family comes from the SAME :func:`_family_for` call
    :func:`offsets_for` makes, so a 48-pin QFN and a 2-pin 0603 resistor
    get differently-sized, non-circular pads instead of the one flat disc
    every pad used to reserve regardless of package (the defect this
    exists to close — see the module docstring's measured consequence).
    ``single``/``header`` get a round pad (a THT/testpoint annular ring
    doesn't scale with pin-to-pin pitch); every other family gets a
    rectangular pad sized off that family's own pitch.
    """
    if n_pins <= 0:
        return [], True
    family = _family_for(n_pins, label=label)
    if family in (_SINGLE, _HEADER):
        dia = _ROUND_PAD_DIA_MM
        return [(dia, dia, PAD_SHAPE_CIRCLE)] * n_pins, True
    pitch = _FAMILY_PITCH_MM[family]
    tight = _clamp(pitch * _TIGHT_FRACTION, _TIGHT_CLAMP)
    long = _clamp(pitch * _LONG_FRACTION, _LONG_CLAMP)
    return [(tight, long, PAD_SHAPE_RECT)] * n_pins, True


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
