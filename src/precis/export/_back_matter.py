"""The one back-matter order shared by the PDF (``latex.py``) and docx
(``docx.py``) exporters.

Both formats end a draft with the same optional blocks; keeping their order
in a single constant stops the two from drifting (gr493199: References must
precede Acronyms in both). Each exporter builds its blocks as
``{name: payload}`` and walks them through :func:`ordered_blocks`; a block
that is absent for the export (no unverified claims, patent mode has no
``references``, …) is passed as ``None``/left out and so stays out of the
output.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping

#: Block names, in output order. ``sources`` is the PDF-only cited-source
#: appendix (``pdfpages``); docx has no counterpart and never supplies it.
BACK_MATTER_ORDER: tuple[str, ...] = (
    "unverified",
    "data_package",
    "references",
    "acronyms",
    "published",
    "sources",
)


def ordered_blocks[T](blocks: Mapping[str, T | None]) -> Iterator[tuple[str, T]]:
    """Yield ``(name, payload)`` for each supplied, non-``None`` block in
    :data:`BACK_MATTER_ORDER`. A name outside the order is a programming
    error and raises, so a new block cannot silently vanish."""
    unknown = set(blocks) - set(BACK_MATTER_ORDER)
    if unknown:
        raise KeyError(f"unknown back-matter block(s): {sorted(unknown)}")
    for name in BACK_MATTER_ORDER:
        payload = blocks.get(name)
        if payload is not None:
            yield name, payload
