"""Small enclosure screw selection, in mm; no supplier or material promise.

The explicit rows keep M2/M2.5 usable despite their absence from the shared
clearance table. Pilots preserve thread_forming.json's rigid-thermoplastic
house factor (0.8 times major diameter), floored at its stored minor value
(the metric d-minus-pitch drill approximation). Clearance preserves
fit_classes.json's house rule, nominal plus 0.2 mm, not an ISO fit class.
Head envelopes retain the enclosure's house socket-cap starting dimensions,
not a supplier specification or a particular thread-forming screw's geometry.
"""

from dataclasses import dataclass

from precis.errors import BadInput


@dataclass(frozen=True)
class EnclosureScrew:
    pilot: float
    clearance: float
    head_diameter: float
    head_height: float
    source: str


_SOURCE = (
    "pilot: thread_forming.json rigid-thermoplastic house rule, 0.8 × major Ø "
    "floored at stored minor Ø (metric d − P drill approximation); "
    "clearance: fit_classes.json house rule, nominal + 0.2 mm, NOT an ISO fit class; "
    "socket-cap head envelope: enclosure house starting point, not a supplier specification"
)
_SCREWS = {
    "M2": EnclosureScrew(1.6, 2.2, 3.8, 2.0, _SOURCE),
    "M2.5": EnclosureScrew(2.05, 2.7, 4.5, 2.5, _SOURCE),
    "M3": EnclosureScrew(2.5, 3.2, 5.5, 3.0, _SOURCE),
    "M4": EnclosureScrew(3.3, 4.2, 7.0, 4.0, _SOURCE),
}


def enclosure_screw(size: object) -> EnclosureScrew:
    """Resolve a supported screw size or report the available choices."""
    key = size.strip().upper() if isinstance(size, str) else ""
    if key not in _SCREWS:
        raise BadInput(
            f"unsupported enclosure screw_size {size!r}",
            next="set screw_size to one of: M2, M2.5, M3, M4",
            options=list(_SCREWS),
        )
    return _SCREWS[key]
