"""Package-class maximum body heights — pure data, mm, for the pcb→se
rough box (``docs/backlog/pcb-se-binding.md`` §Rough box v0).

A placed instance's own ``height_mm`` (filled by
:mod:`precis.pcb.catalog` from the catalog row) always wins. When it is
absent — JLC parametrics often carry none — the se-side board derivation
needs *something* to bound the keep-out volume, and an honest guess is a
**package-class maximum**: the tallest published body height for the
footprint's package family. Each row is ``(regex, height_mm, source)``,
matched **in order, first match wins** against the instance's
``footprint`` string (the same free-text EasyEDA-style name
``pcb_components.footprint`` carries). The ``source`` note is kept
verbatim so the ``pcb_height_proposed`` finding can name where a guessed
height came from — a table row and a datasheet answer must never read
the same.

The classes are seeded from ``heater-base-test``'s footprint histogram —
its six largest (``R0402``, ``C0603``, the 2.54 mm SMD header, the
``LED-SMD`` 2.0×1.3, ``WSOF-6``, ``SOT-23-3``) cover 113 of its 140
parts — and generalized to the neighbouring classes of the same
families. Heights are published *maxima* (package drawings / vendor
tolerance columns), erring tall on purpose: an over-tall keep-out is a
conservative clearance answer, an under-tall one is a lie. Connectors
and electromechanical parts vary too widely for a class number — they
stay unmapped and take :func:`height_mm`'s fallback (the table's largest
value), still conservative.
"""

from __future__ import annotations

import re
from typing import Any

#: ``(regex, height_mm, source)`` — ordered, first match wins. Specific
#: prefixes before their general family (``SOT-223``/``SOT-89`` before
#: ``SOT-``); chip-passive imperial codes under one ``[RCL]`` row each
#: since resistor and MLCC body heights track the same imperial size.
HEIGHT_TABLE: tuple[tuple[str, float, str], ...] = (
    (
        r"^[RCL]0?402\b",
        0.55,
        "0402 chip, package drawing body max 0.55 mm (Yageo RC0402 T)",
    ),
    (
        r"^[RCL]0?603\b",
        0.9,
        "0603 chip, package drawing body max 0.9 mm (Yageo/KEMET 0603)",
    ),
    (
        r"^[RCL]0?805\b",
        1.35,
        "0805 chip, package drawing body max 1.35 mm (Yageo RC0805 T)",
    ),
    (
        r"^[RCL]1210\b",
        2.8,
        "1210 MLCC, package drawing body max 2.8 mm (Samsung CL32 series)",
    ),
    (
        r"^CAP-SMD",
        6.3,
        "SMD aluminium electrolytic ~5 mm can, body max ~6.3 mm (Nichicon/Panasonic V-chip)",
    ),
    (
        r"^LED-SMD",
        1.1,
        "SMD LED ~2.0x1.3 mm class, body max ~1.1 mm (Kingbright APTD2012 family)",
    ),
    (
        r"^HDR-SMD",
        8.5,
        "2.54 mm SMD/female header, body ~8.5 mm (standard 2.54 header insulator)",
    ),
    (
        r"^HDR-TH",
        11.5,
        "2.54 mm through-hole pin header, overall ~11.5 mm (Samtec TSW class)",
    ),
    (
        r"^SOT-223",
        1.8,
        "SOT-223, package drawing body max 1.8 mm (JEDEC TO-261)",
    ),
    (
        r"^SOT-89",
        1.6,
        "SOT-89, package drawing body max 1.6 mm (JEDEC TO-243)",
    ),
    (
        r"^SOT-",
        1.3,
        "SOT-23 class, package drawing body max ~1.3 mm (JEDEC TO-236 family)",
    ),
    (
        r"^TSSOP",
        1.2,
        "TSSOP, package drawing body max 1.2 mm (JEDEC MO-153)",
    ),
    (
        r"^(QSOP|SSOP|SOP|SOIC|SO-)",
        1.75,
        "SOP/SOIC/QSOP class, package drawing body max ~1.75 mm (JEDEC MS-012/013)",
    ),
    (
        r"^(TQFN|QFN|HVSON|WSON|WSOF|DFN|USON)",
        1.0,
        "QFN/SON-class leadless, package drawing body max ~1.0 mm (JEDEC MO-220 family)",
    ),
    (
        r"^(TQFP|LQFP|QFP)",
        1.6,
        "QFP class, package drawing body max ~1.6 mm (JEDEC MS-026)",
    ),
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


__all__ = ["HEIGHT_TABLE", "height_mm"]
