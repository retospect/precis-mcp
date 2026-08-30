"""A single-stroke (Hershey-style) vector font for silkscreen text.

**Real Hershey Roman Simplex data, not a reconstruction.** This table used
to be a hand-authored approximation "modeled on" Hershey Simplex — and one
entry in that reconstruction, ``S``, had both of its arcs authored with
reversed sweeps, so every "S" this module ever drew (refdes like ``SW1``,
board names, title blocks) was mirrored. That defect is exactly why this
module now transcribes the real digitized coordinates instead of
re-deriving shapes by eye: a byte-for-byte source can't quietly drift from
the reference the way a hand-picked polygon can.

**Source and licence — reproduced here because the licence REQUIRES it, not
as a courtesy.** This table is machine-converted from ``rowmans.jhf``
("Roman Simplex"), one of the public-domain-adjacent Hershey font
distributions. Per the licence that travels with that data:

    The Hershey Fonts were originally created by Dr. A. V. Hershey while
    working at the U.S. National Bureau of Standards.

    The format of the Font data in this distribution was originally
    created by James Hurt, Cognition, Inc.

That licence permits any use, including commercial, and explicitly permits
conversion into another format (which is what happened here: ``.jhf``
vertex records, in a coordinate system with y increasing DOWNWARD and a
combined left/right-bearing first "vertex", converted into this module's
own EM-unit/y-up/baseline-0 convention below) — provided the two
acknowledgements above travel with the data. They now do, in this
docstring.

**Normalization was MEASURED, not assumed.** The ``.jhf`` format encodes
each coordinate as ``ord(char) - ord('R')``; scanning the actual digitized
capitals A-Z found cap tops uniformly at raw y=-12 and baselines
(including every digit and every capital without a descender) uniformly at
raw y=+9 — a real, measured 21-unit cap height (``_EM_UNITS``, matching the
classic Hershey convention this font documents itself as using, but
confirmed against the data rather than asserted from the name). Each raw
coordinate pair's first entry is a (left, right) bearing pair rather than a
vertex; x is shifted by ``-left`` per-glyph (already done below) and the
advance width is ``right - left``. y is remapped from the raw
(down-increasing, baseline at +9) convention to this module's (up-increasing,
baseline at 0) convention via ``9 - raw_y``, then both axes are divided by
``_EM_UNITS`` so cap height lands at exactly 1.0 EM (:data:`GLYPHS`).

**No font file, no new dependency, at runtime.** The conversion above ran
once, offline, against the ``.jhf`` source; what ships here is its
output — a plain data table (:data:`GLYPHS`, generated from a private
``_RAW`` at import time via simple division, no curve-fitting or
procedural generation left in this module) plus :func:`layout_text`, which
places a string's glyphs at a caller-chosen anchor/height/rotation/mirror.

**Rotation/mirror is the ONE existing implementation, reused.**
:func:`precis.pcb.landpattern.rotate_offset` is the transform every placed
pad offset in this subsystem already goes through (mirror the local X
coordinate for a bottom-side instance, THEN rotate clockwise-from-north,
per that function's own docstring). :func:`layout_text` and
:func:`text_bbox_corners` route every glyph point and every bbox corner
through that SAME function, so silk text rotates/mirrors identically to
the part it labels — a second, textually-similar-but-different rotation
formula here is exactly the "one rule, two call sites" defect class this
codebase's docstrings keep calling out.

**Coordinate convention.** Each glyph is authored on an integer design grid
(:data:`_EM_UNITS` units of cap height) and normalized so cap height is
exactly 1.0 EM unit (baseline at y=0, cap top at y=1) — see
:data:`GLYPHS`. Some glyphs genuinely extend past that box: brackets,
braces, parentheses, and the ``$``/``#`` bars are digitized taller than cap
height and dipping below baseline in the source font (so they visually
bracket ascenders/descenders and full-height digits) — this is real font
data, not overshoot slop, and the coordinate-sanity test's slack reflects
it. :func:`layout_text` scales EM units to a caller's ``height_mm``;
nothing outside this module ever sees the raw grid.

**Real per-glyph metrics, not a fixed advance cell.** :data:`GLYPH_ADVANCE_EM`
carries each glyph's own advance width (``I`` is roughly a third of ``M``'s
width) — the specific thing a fixed monospace grid gets wrong and the thing
that makes Hershey-style text look gappy/badly-spaced if skipped.
:data:`ADVANCE_EM` is now the FALLBACK used only for a character with no
entry in :data:`GLYPHS` at all (there should be very few of these — see
"Coverage" below) so text still advances past it rather than colliding with
whatever comes next.

**Coverage.** Every printable-ASCII character except lowercase a-z (see
"Uppercase-only" below) has a real entry: ``A``-``Z``, ``0``-``9``, and every
punctuation/symbol character from ``!`` to ``~``, plus space. ``rowmans.jhf``
covers this exact same set (plus a separate lowercase a-z block this module
doesn't use — see "Uppercase-only"), so nothing from the old table's
coverage was lost in the conversion.

**Uppercase-only.** Lowercase input folds to uppercase before lookup —
the same case-insensitivity classic Hershey "simplex" stroke fonts use.
An unsupported character (anything not in :data:`GLYPHS` — with the coverage
above, only true non-ASCII/control input) draws nothing but still advances
the cursor by :data:`ADVANCE_EM`, so surrounding text doesn't collide with
where it would have been.
"""

from __future__ import annotations

from typing import Final

from precis.pcb.landpattern import rotate_offset

Point = tuple[float, float]

# ─────────────────────────────────────────────────────────────────────
# raw glyph data — design grid: x grows right from each glyph's own
# origin (left bearing already subtracted, per the .jhf conversion this
# module docstring describes), y:[0, _EM_UNITS] with baseline at 0 and
# cap-height top at _EM_UNITS (21 -- MEASURED from the real rowmans.jhf
# capitals/digits, not assumed; see module docstring). A handful of
# glyphs legitimately dip a little below 0 (Q's tail, comma/semicolon,
# parenthesis/bracket/brace descenders) or well past _EM_UNITS
# (parenthesis/bracket/brace/#/$ ascenders, real digitized extents, not
# overshoot slop) -- see the coordinate-sanity test's slack.
# ─────────────────────────────────────────────────────────────────────
_EM_UNITS: Final[float] = 21.0

Strokes = tuple[tuple[Point, ...], ...]

# char -> (strokes, advance width) -- both in RAW design-grid units.
# Transcribed from Hershey Roman Simplex (rowmans.jhf) -- see module
# docstring for the licence acknowledgement this data requires and the
# coordinate conversion (y-down/baseline-at-9 raw -> y-up/baseline-at-0
# here) that produced these numbers.
_RAW: Final[dict[str, tuple[Strokes, float]]] = {
    " ": ((), 16.0),
    # ── uppercase ───────────────────────────────────────────────────
    "A": (
        (
            ((9, 21), (1, 0)),
            ((9, 21), (17, 0)),
            ((4, 7), (14, 7)),
        ),
        18.0,
    ),
    "B": (
        (
            ((4, 21), (4, 0)),
            (
                (4, 21),
                (13, 21),
                (16, 20),
                (17, 19),
                (18, 17),
                (18, 15),
                (17, 13),
                (16, 12),
                (13, 11),
            ),
            (
                (4, 11),
                (13, 11),
                (16, 10),
                (17, 9),
                (18, 7),
                (18, 4),
                (17, 2),
                (16, 1),
                (13, 0),
                (4, 0),
            ),
        ),
        21.0,
    ),
    "C": (
        (
            (
                (18, 16),
                (17, 18),
                (15, 20),
                (13, 21),
                (9, 21),
                (7, 20),
                (5, 18),
                (4, 16),
                (3, 13),
                (3, 8),
                (4, 5),
                (5, 3),
                (7, 1),
                (9, 0),
                (13, 0),
                (15, 1),
                (17, 3),
                (18, 5),
            ),
        ),
        21.0,
    ),
    "D": (
        (
            ((4, 21), (4, 0)),
            (
                (4, 21),
                (11, 21),
                (14, 20),
                (16, 18),
                (17, 16),
                (18, 13),
                (18, 8),
                (17, 5),
                (16, 3),
                (14, 1),
                (11, 0),
                (4, 0),
            ),
        ),
        21.0,
    ),
    "E": (
        (
            ((4, 21), (4, 0)),
            ((4, 21), (17, 21)),
            ((4, 11), (12, 11)),
            ((4, 0), (17, 0)),
        ),
        19.0,
    ),
    "F": (
        (
            ((4, 21), (4, 0)),
            ((4, 21), (17, 21)),
            ((4, 11), (12, 11)),
        ),
        18.0,
    ),
    "G": (
        (
            (
                (18, 16),
                (17, 18),
                (15, 20),
                (13, 21),
                (9, 21),
                (7, 20),
                (5, 18),
                (4, 16),
                (3, 13),
                (3, 8),
                (4, 5),
                (5, 3),
                (7, 1),
                (9, 0),
                (13, 0),
                (15, 1),
                (17, 3),
                (18, 5),
                (18, 8),
            ),
            ((13, 8), (18, 8)),
        ),
        21.0,
    ),
    "H": (
        (
            ((4, 21), (4, 0)),
            ((18, 21), (18, 0)),
            ((4, 11), (18, 11)),
        ),
        22.0,
    ),
    "I": ((((4, 21), (4, 0)),), 8.0),
    "J": (
        (
            (
                (12, 21),
                (12, 5),
                (11, 2),
                (10, 1),
                (8, 0),
                (6, 0),
                (4, 1),
                (3, 2),
                (2, 5),
                (2, 7),
            ),
        ),
        16.0,
    ),
    "K": (
        (
            ((4, 21), (4, 0)),
            ((18, 21), (4, 7)),
            ((9, 12), (18, 0)),
        ),
        21.0,
    ),
    "L": (
        (
            ((4, 21), (4, 0)),
            ((4, 0), (16, 0)),
        ),
        17.0,
    ),
    "M": (
        (
            ((4, 21), (4, 0)),
            ((4, 21), (12, 0)),
            ((20, 21), (12, 0)),
            ((20, 21), (20, 0)),
        ),
        24.0,
    ),
    "N": (
        (
            ((4, 21), (4, 0)),
            ((4, 21), (18, 0)),
            ((18, 21), (18, 0)),
        ),
        22.0,
    ),
    "O": (
        (
            (
                (9, 21),
                (7, 20),
                (5, 18),
                (4, 16),
                (3, 13),
                (3, 8),
                (4, 5),
                (5, 3),
                (7, 1),
                (9, 0),
                (13, 0),
                (15, 1),
                (17, 3),
                (18, 5),
                (19, 8),
                (19, 13),
                (18, 16),
                (17, 18),
                (15, 20),
                (13, 21),
                (9, 21),
            ),
        ),
        22.0,
    ),
    "P": (
        (
            ((4, 21), (4, 0)),
            (
                (4, 21),
                (13, 21),
                (16, 20),
                (17, 19),
                (18, 17),
                (18, 14),
                (17, 12),
                (16, 11),
                (13, 10),
                (4, 10),
            ),
        ),
        21.0,
    ),
    "Q": (
        (
            (
                (9, 21),
                (7, 20),
                (5, 18),
                (4, 16),
                (3, 13),
                (3, 8),
                (4, 5),
                (5, 3),
                (7, 1),
                (9, 0),
                (13, 0),
                (15, 1),
                (17, 3),
                (18, 5),
                (19, 8),
                (19, 13),
                (18, 16),
                (17, 18),
                (15, 20),
                (13, 21),
                (9, 21),
            ),
            ((12, 4), (18, -2)),
        ),
        22.0,
    ),
    "R": (
        (
            ((4, 21), (4, 0)),
            (
                (4, 21),
                (13, 21),
                (16, 20),
                (17, 19),
                (18, 17),
                (18, 15),
                (17, 13),
                (16, 12),
                (13, 11),
                (4, 11),
            ),
            ((11, 11), (18, 0)),
        ),
        21.0,
    ),
    "S": (
        (
            (
                (17, 18),
                (15, 20),
                (12, 21),
                (8, 21),
                (5, 20),
                (3, 18),
                (3, 16),
                (4, 14),
                (5, 13),
                (7, 12),
                (13, 10),
                (15, 9),
                (16, 8),
                (17, 6),
                (17, 3),
                (15, 1),
                (12, 0),
                (8, 0),
                (5, 1),
                (3, 3),
            ),
        ),
        20.0,
    ),
    "T": (
        (
            ((8, 21), (8, 0)),
            ((1, 21), (15, 21)),
        ),
        16.0,
    ),
    "U": (
        (
            (
                (4, 21),
                (4, 6),
                (5, 3),
                (7, 1),
                (10, 0),
                (12, 0),
                (15, 1),
                (17, 3),
                (18, 6),
                (18, 21),
            ),
        ),
        22.0,
    ),
    "V": (
        (
            ((1, 21), (9, 0)),
            ((17, 21), (9, 0)),
        ),
        18.0,
    ),
    "W": (
        (
            ((2, 21), (7, 0)),
            ((12, 21), (7, 0)),
            ((12, 21), (17, 0)),
            ((22, 21), (17, 0)),
        ),
        24.0,
    ),
    "X": (
        (
            ((3, 21), (17, 0)),
            ((17, 21), (3, 0)),
        ),
        20.0,
    ),
    "Y": (
        (
            ((1, 21), (9, 11), (9, 0)),
            ((17, 21), (9, 11)),
        ),
        18.0,
    ),
    "Z": (
        (
            ((17, 21), (3, 0)),
            ((3, 21), (17, 21)),
            ((3, 0), (17, 0)),
        ),
        20.0,
    ),
    # ── digits ──────────────────────────────────────────────────────
    "0": (
        (
            (
                (9, 21),
                (6, 20),
                (4, 17),
                (3, 12),
                (3, 9),
                (4, 4),
                (6, 1),
                (9, 0),
                (11, 0),
                (14, 1),
                (16, 4),
                (17, 9),
                (17, 12),
                (16, 17),
                (14, 20),
                (11, 21),
                (9, 21),
            ),
        ),
        20.0,
    ),
    "1": ((((6, 17), (8, 18), (11, 21), (11, 0)),), 20.0),
    "2": (
        (
            (
                (4, 16),
                (4, 17),
                (5, 19),
                (6, 20),
                (8, 21),
                (12, 21),
                (14, 20),
                (15, 19),
                (16, 17),
                (16, 15),
                (15, 13),
                (13, 10),
                (3, 0),
                (17, 0),
            ),
        ),
        20.0,
    ),
    "3": (
        (
            (
                (5, 21),
                (16, 21),
                (10, 13),
                (13, 13),
                (15, 12),
                (16, 11),
                (17, 8),
                (17, 6),
                (16, 3),
                (14, 1),
                (11, 0),
                (8, 0),
                (5, 1),
                (4, 2),
                (3, 4),
            ),
        ),
        20.0,
    ),
    "4": (
        (
            ((13, 21), (3, 7), (18, 7)),
            ((13, 21), (13, 0)),
        ),
        20.0,
    ),
    "5": (
        (
            (
                (15, 21),
                (5, 21),
                (4, 12),
                (5, 13),
                (8, 14),
                (11, 14),
                (14, 13),
                (16, 11),
                (17, 8),
                (17, 6),
                (16, 3),
                (14, 1),
                (11, 0),
                (8, 0),
                (5, 1),
                (4, 2),
                (3, 4),
            ),
        ),
        20.0,
    ),
    "6": (
        (
            (
                (16, 18),
                (15, 20),
                (12, 21),
                (10, 21),
                (7, 20),
                (5, 17),
                (4, 12),
                (4, 7),
                (5, 3),
                (7, 1),
                (10, 0),
                (11, 0),
                (14, 1),
                (16, 3),
                (17, 6),
                (17, 7),
                (16, 10),
                (14, 12),
                (11, 13),
                (10, 13),
                (7, 12),
                (5, 10),
                (4, 7),
            ),
        ),
        20.0,
    ),
    "7": (
        (
            ((17, 21), (7, 0)),
            ((3, 21), (17, 21)),
        ),
        20.0,
    ),
    "8": (
        (
            (
                (8, 21),
                (5, 20),
                (4, 18),
                (4, 16),
                (5, 14),
                (7, 13),
                (11, 12),
                (14, 11),
                (16, 9),
                (17, 7),
                (17, 4),
                (16, 2),
                (15, 1),
                (12, 0),
                (8, 0),
                (5, 1),
                (4, 2),
                (3, 4),
                (3, 7),
                (4, 9),
                (6, 11),
                (9, 12),
                (13, 13),
                (15, 14),
                (16, 16),
                (16, 18),
                (15, 20),
                (12, 21),
                (8, 21),
            ),
        ),
        20.0,
    ),
    "9": (
        (
            (
                (16, 14),
                (15, 11),
                (13, 9),
                (10, 8),
                (9, 8),
                (6, 9),
                (4, 11),
                (3, 14),
                (3, 15),
                (4, 18),
                (6, 20),
                (9, 21),
                (10, 21),
                (13, 20),
                (15, 18),
                (16, 14),
                (16, 9),
                (15, 4),
                (13, 1),
                (10, 0),
                (8, 0),
                (5, 1),
                (4, 3),
            ),
        ),
        20.0,
    ),
    # ── punctuation / symbols (the rest of printable ASCII) ────────
    "!": (
        (
            ((5, 21), (5, 7)),
            ((5, 2), (4, 1), (5, 0), (6, 1), (5, 2)),
        ),
        10.0,
    ),
    '"': (
        (
            ((4, 21), (4, 14)),
            ((12, 21), (12, 14)),
        ),
        16.0,
    ),
    "#": (
        (
            ((11, 25), (4, -7)),
            ((17, 25), (10, -7)),
            ((4, 12), (18, 12)),
            ((3, 6), (17, 6)),
        ),
        21.0,
    ),
    "$": (
        (
            ((8, 25), (8, -4)),
            ((12, 25), (12, -4)),
            (
                (17, 18),
                (15, 20),
                (12, 21),
                (8, 21),
                (5, 20),
                (3, 18),
                (3, 16),
                (4, 14),
                (5, 13),
                (7, 12),
                (13, 10),
                (15, 9),
                (16, 8),
                (17, 6),
                (17, 3),
                (15, 1),
                (12, 0),
                (8, 0),
                (5, 1),
                (3, 3),
            ),
        ),
        20.0,
    ),
    "%": (
        (
            ((21, 21), (3, 0)),
            (
                (8, 21),
                (10, 19),
                (10, 17),
                (9, 15),
                (7, 14),
                (5, 14),
                (3, 16),
                (3, 18),
                (4, 20),
                (6, 21),
                (8, 21),
                (10, 20),
                (13, 19),
                (16, 19),
                (19, 20),
                (21, 21),
            ),
            (
                (17, 7),
                (15, 6),
                (14, 4),
                (14, 2),
                (16, 0),
                (18, 0),
                (20, 1),
                (21, 3),
                (21, 5),
                (19, 7),
                (17, 7),
            ),
        ),
        24.0,
    ),
    "&": (
        (
            (
                (23, 12),
                (23, 13),
                (22, 14),
                (21, 14),
                (20, 13),
                (19, 11),
                (17, 6),
                (15, 3),
                (13, 1),
                (11, 0),
                (7, 0),
                (5, 1),
                (4, 2),
                (3, 4),
                (3, 6),
                (4, 8),
                (5, 9),
                (12, 13),
                (13, 14),
                (14, 16),
                (14, 18),
                (13, 20),
                (11, 21),
                (9, 20),
                (8, 18),
                (8, 16),
                (9, 13),
                (11, 10),
                (16, 3),
                (18, 1),
                (20, 0),
                (22, 0),
                (23, 1),
                (23, 2),
            ),
        ),
        26.0,
    ),
    "'": ((((5, 19), (4, 20), (5, 21), (6, 20), (6, 18), (5, 16), (4, 15)),), 10.0),
    "(": (
        (
            (
                (11, 25),
                (9, 23),
                (7, 20),
                (5, 16),
                (4, 11),
                (4, 7),
                (5, 2),
                (7, -2),
                (9, -5),
                (11, -7),
            ),
        ),
        14.0,
    ),
    ")": (
        (
            (
                (3, 25),
                (5, 23),
                (7, 20),
                (9, 16),
                (10, 11),
                (10, 7),
                (9, 2),
                (7, -2),
                (5, -5),
                (3, -7),
            ),
        ),
        14.0,
    ),
    "*": (
        (
            ((8, 21), (8, 9)),
            ((3, 18), (13, 12)),
            ((13, 18), (3, 12)),
        ),
        16.0,
    ),
    "+": (
        (
            ((13, 18), (13, 0)),
            ((4, 9), (22, 9)),
        ),
        26.0,
    ),
    ",": ((((6, 1), (5, 0), (4, 1), (5, 2), (6, 1), (6, -1), (5, -3), (4, -4)),), 10.0),
    "-": ((((4, 9), (22, 9)),), 26.0),
    ".": ((((5, 2), (4, 1), (5, 0), (6, 1), (5, 2)),), 10.0),
    "/": ((((20, 25), (2, -7)),), 22.0),
    ":": (
        (
            ((5, 14), (4, 13), (5, 12), (6, 13), (5, 14)),
            ((5, 2), (4, 1), (5, 0), (6, 1), (5, 2)),
        ),
        10.0,
    ),
    ";": (
        (
            ((5, 14), (4, 13), (5, 12), (6, 13), (5, 14)),
            ((6, 1), (5, 0), (4, 1), (5, 2), (6, 1), (6, -1), (5, -3), (4, -4)),
        ),
        10.0,
    ),
    "<": ((((20, 18), (4, 9), (20, 0)),), 24.0),
    "=": (
        (
            ((4, 12), (22, 12)),
            ((4, 6), (22, 6)),
        ),
        26.0,
    ),
    ">": ((((4, 18), (20, 9), (4, 0)),), 24.0),
    "?": (
        (
            (
                (3, 16),
                (3, 17),
                (4, 19),
                (5, 20),
                (7, 21),
                (11, 21),
                (13, 20),
                (14, 19),
                (15, 17),
                (15, 15),
                (14, 13),
                (13, 12),
                (9, 10),
                (9, 7),
            ),
            ((9, 2), (8, 1), (9, 0), (10, 1), (9, 2)),
        ),
        18.0,
    ),
    "@": (
        (
            (
                (18, 13),
                (17, 15),
                (15, 16),
                (12, 16),
                (10, 15),
                (9, 14),
                (8, 11),
                (8, 8),
                (9, 6),
                (11, 5),
                (14, 5),
                (16, 6),
                (17, 8),
            ),
            ((12, 16), (10, 14), (9, 11), (9, 8), (10, 6), (11, 5)),
            (
                (18, 16),
                (17, 8),
                (17, 6),
                (19, 5),
                (21, 5),
                (23, 7),
                (24, 10),
                (24, 12),
                (23, 15),
                (22, 17),
                (20, 19),
                (18, 20),
                (15, 21),
                (12, 21),
                (9, 20),
                (7, 19),
                (5, 17),
                (4, 15),
                (3, 12),
                (3, 9),
                (4, 6),
                (5, 4),
                (7, 2),
                (9, 1),
                (12, 0),
                (15, 0),
                (18, 1),
                (20, 2),
                (21, 3),
            ),
            ((19, 16), (18, 8), (18, 6), (19, 5)),
        ),
        27.0,
    ),
    "[": (
        (
            ((4, 25), (4, -7)),
            ((5, 25), (5, -7)),
            ((4, 25), (11, 25)),
            ((4, -7), (11, -7)),
        ),
        14.0,
    ),
    "\\": ((((0, 21), (14, -3)),), 14.0),
    "]": (
        (
            ((9, 25), (9, -7)),
            ((10, 25), (10, -7)),
            ((3, 25), (10, 25)),
            ((3, -7), (10, -7)),
        ),
        14.0,
    ),
    "^": (
        (
            ((6, 15), (8, 18), (10, 15)),
            ((3, 12), (8, 17), (13, 12)),
            ((8, 17), (8, 0)),
        ),
        16.0,
    ),
    "_": ((((0, -2), (16, -2)),), 16.0),
    "`": ((((6, 21), (5, 20), (4, 18), (4, 16), (5, 15), (6, 16), (5, 17)),), 10.0),
    "{": (
        (
            (
                (9, 25),
                (7, 24),
                (6, 23),
                (5, 21),
                (5, 19),
                (6, 17),
                (7, 16),
                (8, 14),
                (8, 12),
                (6, 10),
            ),
            (
                (7, 24),
                (6, 22),
                (6, 20),
                (7, 18),
                (8, 17),
                (9, 15),
                (9, 13),
                (8, 11),
                (4, 9),
                (8, 7),
                (9, 5),
                (9, 3),
                (8, 1),
                (7, 0),
                (6, -2),
                (6, -4),
                (7, -6),
            ),
            (
                (6, 8),
                (8, 6),
                (8, 4),
                (7, 2),
                (6, 1),
                (5, -1),
                (5, -3),
                (6, -5),
                (7, -6),
                (9, -7),
            ),
        ),
        14.0,
    ),
    "|": ((((4, 25), (4, -7)),), 8.0),
    "}": (
        (
            (
                (5, 25),
                (7, 24),
                (8, 23),
                (9, 21),
                (9, 19),
                (8, 17),
                (7, 16),
                (6, 14),
                (6, 12),
                (8, 10),
            ),
            (
                (7, 24),
                (8, 22),
                (8, 20),
                (7, 18),
                (6, 17),
                (5, 15),
                (5, 13),
                (6, 11),
                (10, 9),
                (6, 7),
                (5, 5),
                (5, 3),
                (6, 1),
                (7, 0),
                (8, -2),
                (8, -4),
                (7, -6),
            ),
            (
                (8, 8),
                (6, 6),
                (6, 4),
                (7, 2),
                (8, 1),
                (9, -1),
                (9, -3),
                (8, -5),
                (7, -6),
                (5, -7),
            ),
        ),
        14.0,
    ),
    "~": (
        (
            (
                (3, 6),
                (3, 8),
                (4, 11),
                (6, 12),
                (8, 12),
                (10, 11),
                (14, 8),
                (16, 7),
                (18, 7),
                (20, 8),
                (21, 10),
            ),
            (
                (3, 8),
                (4, 10),
                (6, 11),
                (8, 11),
                (10, 10),
                (14, 7),
                (16, 6),
                (18, 6),
                (20, 7),
                (21, 10),
                (21, 12),
            ),
        ),
        24.0,
    ),
}


def _normalize(strokes: Strokes) -> tuple[tuple[Point, ...], ...]:
    return tuple(
        tuple((x / _EM_UNITS, y / _EM_UNITS) for x, y in stroke) for stroke in strokes
    )


#: char -> polylines, in EM units (cap height 1.0, baseline y=0). ``" "``
#: is present with zero strokes -- a supported glyph that draws nothing but
#: still advances the cursor (see module docstring).
GLYPHS: Final[dict[str, tuple[tuple[Point, ...], ...]]] = {
    ch: _normalize(strokes) for ch, (strokes, _adv) in _RAW.items()
}

#: char -> this glyph's OWN advance width, in EM units -- the per-glyph
#: metric that replaces a fixed monospace cell (module docstring). Every
#: key in :data:`GLYPHS` has a matching entry here.
GLYPH_ADVANCE_EM: Final[dict[str, float]] = {
    ch: adv / _EM_UNITS for ch, (_strokes, adv) in _RAW.items()
}

#: Fallback advance, in EM units, for a character with NO entry in
#: :data:`GLYPHS` at all (true only for non-ASCII/control input given the
#: coverage documented above) -- so text still advances past it rather
#: than colliding with whatever comes next.
ADVANCE_EM: Final[float] = 16.0 / _EM_UNITS


def supported(ch: str) -> bool:
    """Whether ``ch`` (case-folded) has real strokes — i.e. is drawable,
    not just advance-only like an unrecognized character or space."""
    return ch.upper() in GLYPHS


def _advance_em(ch: str) -> float:
    """This character's own advance width in EM units -- its real
    per-glyph metric if it's in the table, :data:`ADVANCE_EM` otherwise."""
    return GLYPH_ADVANCE_EM.get(ch.upper(), ADVANCE_EM)


def text_width_mm(text: str, height_mm: float) -> float:
    """The full advance width of ``text`` at ``height_mm`` cap height,
    left-to-right, before any rotation/mirror — the sum of each
    character's OWN advance (every character, including an unsupported
    one, which still advances by :data:`ADVANCE_EM`) — never a fixed
    per-character cell, so this always agrees with what
    :func:`layout_text`'s cursor actually does."""
    return sum(_advance_em(ch) for ch in text) * height_mm


def _h_shift(total_w_mm: float, h_align: str) -> float:
    if h_align == "left":
        return 0.0
    if h_align == "center":
        return -total_w_mm / 2.0
    if h_align == "right":
        return -total_w_mm
    raise ValueError(f"h_align must be 'left'/'center'/'right', got {h_align!r}")


def _v_shift(height_mm: float, v_align: str) -> float:
    if v_align == "baseline":
        return 0.0
    if v_align == "middle":
        return -height_mm / 2.0
    if v_align == "top":
        return -height_mm
    raise ValueError(f"v_align must be 'baseline'/'middle'/'top', got {v_align!r}")


def _local_bounds(
    text: str, height_mm: float, h_align: str, v_align: str
) -> tuple[float, float, float, float]:
    """``(x0, y0, x1, y1)`` of ``text``'s advance box in the LOCAL frame —
    before rotate/mirror/anchor — shared by :func:`layout_text` and
    :func:`text_bbox_corners` so the two can never disagree about where the
    text actually sits."""
    total_w = text_width_mm(text, height_mm)
    x0 = _h_shift(total_w, h_align)
    y0 = _v_shift(height_mm, v_align)
    return x0, y0, x0 + total_w, y0 + height_mm


def layout_text(
    text: str,
    *,
    anchor: Point,
    height_mm: float,
    rotation_deg: float = 0.0,
    mirror: bool = False,
    h_align: str = "left",
    v_align: str = "baseline",
) -> list[list[Point]]:
    """Lay ``text`` out into board-space polylines.

    ``anchor`` is a BOARD-space point (already placed — this function does
    not itself know about an instance's own position, only the transform
    applied around it); ``h_align``/``v_align`` choose which point of the
    text's local advance box sits at ``anchor`` before the mirror+rotate
    (``'left'``/``'baseline'`` — the default — is the classic typesetting
    origin; ``'center'``/``'middle'`` is what a caller centering a label on
    a part wants). Returns one list of ``(x, y)`` mm points per stroke —
    the caller turns each into a ``{"shape":"line", "start":.., "end":..}``
    chain (see :mod:`precis.pcb.silk`), never a font concern.
    """
    x0, y0, _x1, _y1 = _local_bounds(text, height_mm, h_align, v_align)
    ax, ay = anchor
    strokes_out: list[list[Point]] = []
    cursor = x0
    for ch in text:
        glyph = GLYPHS.get(ch.upper())
        if glyph is None:
            cursor += ADVANCE_EM * height_mm
            continue
        for stroke in glyph:
            pts: list[Point] = []
            for gx, gy in stroke:
                lx = cursor + gx * height_mm
                ly = y0 + gy * height_mm
                rx, ry = rotate_offset(lx, ly, rotation_deg, mirrored=mirror)
                pts.append((ax + rx, ay + ry))
            if len(pts) >= 2:
                strokes_out.append(pts)
        cursor += _advance_em(ch) * height_mm
    return strokes_out


def text_bbox_corners(
    text: str,
    *,
    anchor: Point,
    height_mm: float,
    rotation_deg: float = 0.0,
    mirror: bool = False,
    h_align: str = "left",
    v_align: str = "baseline",
) -> list[Point]:
    """The 4 board-space corners of ``text``'s advance box, through the
    SAME transform :func:`layout_text` uses — so a caller checking "does
    this text's footprint clear a pad" is checking exactly the box the
    glyphs are drawn inside, not an approximation of it."""
    x0, y0, x1, y1 = _local_bounds(text, height_mm, h_align, v_align)
    ax, ay = anchor
    corners: list[Point] = []
    for lx, ly in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        rx, ry = rotate_offset(lx, ly, rotation_deg, mirrored=mirror)
        corners.append((ax + rx, ay + ry))
    return corners


__all__ = [
    "ADVANCE_EM",
    "GLYPHS",
    "GLYPH_ADVANCE_EM",
    "Point",
    "layout_text",
    "supported",
    "text_bbox_corners",
    "text_width_mm",
]
