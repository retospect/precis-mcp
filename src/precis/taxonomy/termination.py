"""The termination subject axis: facet notation and crystal-system parents.

`taxonomy-bootstrap.md` §"Generated subject axes". A facet is written three
ways in the corpus: packed single digits (``(111)``, ``{100}``), packed with
an explicit sign or a combining-macron over-bar for a negative index
(``(1-10)``, ``(1̄10)`` — U+0304 COMBINING MACRON), or space-separated when a
component needs more than one digit (``(10 1 0)``, and the hexagonal
4-index form ``(0001)``). :func:`parse_facet` handles all four; it never
raises, only returns ``None``.

The crystal-system parent (:func:`termination_edges`) always names the
*literal* facet the label wrote, e.g. ``Pd(111)`` ⇒ ``fcc-111`` — it never
expands to the facet's symmetry-equivalent family. :func:`cubic_equivalents`
is the family expansion, kept separate and cubic-only: for any other crystal
system, working out which facets are equivalent needs the actual space
group (``spglib``), which this module does not have and is not going to
fake by pretending cubic rules apply everywhere.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from typing import Final

from precis.taxonomy.types import AxisEdge

_AXIS = "termination"

_DELIMS: Final[dict[str, str]] = {"(": ")", "{": "}"}

#: Finds a parenthesised or braced facet substring inside a larger label
#: (``"Pd(111)"``) so `termination_edges` can pull it out of a compound
#: subject label rather than requiring the caller to pre-slice it.
_FACET_SPAN: Final[re.Pattern[str]] = re.compile(r"[({][^)}]*[)}]")

#: A negative packed digit is either an explicit ASCII ``-`` before it, or a
#: combining macron (crystallography's over-bar) immediately after it.
_MACRON = "̄"


@dataclass(frozen=True, slots=True)
class Facet:
    """One parsed facet notation.

    ``family`` is ``True`` for ``{hkl}`` (the whole symmetry-equivalent
    family) and ``False`` for a specific ``(hkl)`` surface — the source
    text's own distinction, not anything this module infers.
    """

    indices: tuple[int, ...]
    family: bool
    raw: str


def parse_facet(label: str) -> Facet | None:
    """Parse a label that *is* a facet notation; ``None`` if it is not.

    Delimiters must match (``(...)`` or ``{...}``); the content is either
    packed single-signed-digits or whitespace-separated multi-digit tokens.
    Any other shape, or an index count outside 3-4, returns ``None`` rather
    than raising.
    """
    text = label.strip()
    if len(text) < 3 or text[0] not in _DELIMS or text[-1] != _DELIMS[text[0]]:
        return None
    indices = _parse_indices(text[1:-1])
    if indices is None or len(indices) not in (3, 4):
        return None
    return Facet(indices=tuple(indices), family=(text[0] == "{"), raw=text)


def _parse_indices(inner: str) -> list[int] | None:
    if not inner:
        return None
    if any(ch.isspace() for ch in inner):
        tokens = inner.split()
        out: list[int] = []
        for token in tokens:
            if not re.fullmatch(r"-?\d+", token):
                return None
            out.append(int(token))
        return out
    return _parse_packed(inner)


def _parse_packed(inner: str) -> list[int] | None:
    out: list[int] = []
    i, n = 0, len(inner)
    while i < n:
        ch = inner[i]
        if ch == "-":
            i += 1
            if i >= n or not inner[i].isdigit():
                return None
            out.append(-int(inner[i]))
            i += 1
        elif ch.isdigit():
            value = int(ch)
            i += 1
            if i < n and inner[i] == _MACRON:
                value = -value
                i += 1
            out.append(value)
        else:
            return None
    return out


def cubic_equivalents(indices: tuple[int, ...]) -> frozenset[tuple[int, ...]]:
    """All cubic-symmetry-equivalent index tuples for ``indices``.

    Symmetry assumption: the full cubic point group ``m-3m`` (order 48 —
    all 3! coordinate permutations times all 2**3 independent sign flips,
    i.e. rotations plus inversion). A component that is ``0`` makes half of
    its sign flips no-ops, and a repeated magnitude makes some permutations
    coincide, so the raw 48 collapses per direction to the textbook cubic
    multiplicities: 6 for a ``<100>``-type direction, 8 for ``<111>``, 12 for
    ``<110>`` — not 24; permutations of an already-two-fold-degenerate
    ``(1,-1,0)`` only add 2x, not 4x, over the ``<100>`` case, and dividing
    a plain 6x3!x2**3 count by the coincidences it does not remove would
    overcount.
    """
    equivalents = {
        tuple(p * s for p, s in zip(perm, signs, strict=True))
        for perm in itertools.permutations(indices)
        for signs in itertools.product((1, -1), repeat=len(indices))
    }
    return frozenset(equivalents)


def facet_span(label: str) -> str | None:
    """The facet substring inside ``label``, if it contains one that parses.

    Public so `subjects.py` can split a compound label (``"Pd(111)"``) into
    its composition and termination halves without reaching into this
    module's regex directly.
    """
    match = _FACET_SPAN.search(label)
    if not match or parse_facet(match.group(0)) is None:
        return None
    return match.group(0)


def _find_facet(label: str) -> Facet | None:
    span = facet_span(label)
    return parse_facet(span) if span is not None else None


def termination_edges(
    label: str,
    *,
    crystal_symmetry: str | None,
    site_classes: tuple[str, ...] = (),
) -> tuple[AxisEdge, ...]:
    """Termination-axis parent proposals for a label containing a facet.

    Emits the crystal-system parent (``f"{crystal_symmetry}-{hkl}"``, the
    *literal* facet — see the module docstring) when a facet substring and a
    known symmetry are both present, plus one parent per ``site_classes``
    term (`taxonomy-bootstrap.md`'s edge/step/vacancy/dual-atom vocabulary,
    read from the campaign config by the caller so this module never imports
    it) found as a whole word in the label. Empty when neither applies.
    """
    child = _subject_key(label)
    edges: list[AxisEdge] = []
    facet = _find_facet(label)
    if facet is not None and crystal_symmetry:
        hkl = "".join(str(i) for i in facet.indices)
        edges.append(
            AxisEdge(child=child, parent=f"{crystal_symmetry}-{hkl}", axis=_AXIS)
        )
    lowered = label.lower()
    for site_class in site_classes:
        pattern = re.escape(site_class.lower())
        if re.search(rf"\b{pattern}\b", lowered):
            edges.append(AxisEdge(child=child, parent=site_class.lower(), axis=_AXIS))
    seen: dict[tuple[str, str, str], AxisEdge] = {}
    for edge in edges:
        seen.setdefault((edge.child, edge.parent, edge.axis), edge)
    return tuple(seen.values())


def _subject_key(label: str) -> str:
    """Same rule as `subjects.subject_key`; duplicated, see `composition._subject_key`."""
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
