"""The ``DiagramLang`` port + the value types shared across diagram languages.

``DiagramLang`` is the narrow surface the generic turn loop / context assembler
call — everything that differs between an SVG figure and a mermaid diagram:
the compile / sanitize / lint / element-extraction mechanics, the birth
source, the bounds model, and the language-specific prompt fragments (the
floor guidance, the canvas line, the JSON reply contract). A concrete instance
(``precis.figure.svg.SVG_LANG``) is passed into the core; nothing in the core
imports a concrete language, so a new language is additive.

``bounds`` is deliberately ``Any`` — for SVG it is the viewBox tuple
``(x, y, w, h)``; a language with automatic layout (mermaid) can use ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class LintFinding:
    """One mechanically-detected problem with a diagram's source.

    For SVG, ``kind`` is ``'compile'`` (unparseable), ``'bounds'`` (a shape
    spills past the viewBox), or ``'binding'`` (an element→chunk binding names
    an ``id`` no element carries — ADR 0057 drift). ``node`` is the offending
    element's ``id`` (or its tag when it has none); empty for a whole-document
    compile failure. Other languages reuse ``'compile'`` / ``'binding'`` and
    add their own geometry/topology kinds.
    """

    kind: str
    node: str
    message: str


@dataclass(frozen=True, slots=True)
class Element:
    """A named, bindable element of a diagram — the anchor a chunk binding
    attaches to (ADR 0057).

    ``id`` is the stable source identifier (the join key for a ``depicts``
    binding); ``tag`` a language-native type name (an SVG tag, a mermaid node
    shape); ``coords`` a compact human/model-readable geometry-or-topology
    string (``cx120 cy70 r8`` for an SVG circle, an edge list for a mermaid
    node, or ``""`` when neither applies).
    """

    id: str
    tag: str
    coords: str


@runtime_checkable
class DiagramLang(Protocol):
    """The per-language surface the generic diagram core calls."""

    #: The ref kind — ``"figure"``.
    kind: str
    #: Chunk-kind names for the four model-owned documents.
    source_kind: str  # e.g. "figure_node"
    vocab_kind: str  # e.g. "figure_vocab"
    notes_kind: str  # e.g. "figure_notes"
    turn_kind: str  # e.g. "figure_turn"
    #: The pinned prompt skill (prepended to every turn).
    skill_name: str  # e.g. "precis-figure-svg"
    #: The JSON reply key carrying the rewritten source.
    source_key: str  # e.g. "svg"
    #: The ``ref.meta`` key the bounds are mirrored onto.
    bounds_meta_key: str  # e.g. "viewbox"

    # -- source mechanics --------------------------------------------------
    def parse_error(self, source: str) -> str | None:
        """``None`` if the source compiles, else a one-line reason."""
        ...

    def sanitize(self, source: str) -> str:
        """Return a storage/inline-safe form; raise on unparseable input."""
        ...

    def lint(self, source: str, bounds: Any) -> list[LintFinding]:
        """Compile + geometry/topology findings (not binding drift)."""
        ...

    def elements(self, source: str) -> list[Element]:
        """Every bindable element (carrying a stable id)."""
        ...

    def lint_bindings(self, source: str, bound_ids: set[str]) -> list[LintFinding]:
        """A ``'binding'`` finding for each bound id absent from the source."""
        ...

    def default_source(self, bounds: Any) -> str:
        """The birth source of a new diagram at ``bounds``."""
        ...

    # -- bounds ------------------------------------------------------------
    def read_bounds(self, source: str) -> Any | None:
        """Extract the bounds the source itself declares, or ``None``."""
        ...

    def default_bounds(self) -> Any:
        """The fallback bounds when neither source nor ref.meta has any."""
        ...

    def bounds_from_meta(self, raw: Any) -> Any | None:
        """Coerce a ``ref.meta[bounds_meta_key]`` value to bounds, or None."""
        ...

    def bounds_to_meta(self, bounds: Any) -> Any:
        """Serialize bounds for ``ref.meta[bounds_meta_key]``."""
        ...

    # -- prompt fragments --------------------------------------------------
    def floor_guidance(self) -> str:
        """The inline guidance floor (present even if the skill fails to load)."""
        ...

    def canvas_section(self, bounds: Any) -> str:
        """The ``## Canvas`` prompt block describing the coordinate frame."""
        ...

    def json_contract(self) -> str:
        """The one-object JSON reply contract (uses ``source_key``)."""
        ...
