"""TexHandler — read/write ``.tex`` files under a configured root.

**Status: stupid first cut.** Subclasses :class:`PlaintextHandler` and
overrides ``_KIND`` / ``_EXTENSIONS`` / ``_DEFAULT_EXT`` (plus the
``spec``). The block grammar stays paragraphs-separated-by-blank-lines
— LaTeX sectioning commands, environments, and macros are **not**
parsed. Storing raw LaTeX source means the agent can
``edit(mode='find-replace')`` against literal source, semantic search
works on the source text (``bge-m3`` reads LaTeX), and we don't have
to maintain a custom AST.

A future refinement can recognise ``\\section`` / ``\\begin{...}``
boundaries; until then this is the same handler as plaintext with a
different file extension.

Same address grammar as plaintext (``slug``, ``slug~SLUG``, ``slug~N``,
``slug/raw``, ``/`` for index) and same edit / put / delete / tag /
link semantics. See ``precis-files-help`` and ``precis-plaintext-help``
for the shared protocol.
"""

from __future__ import annotations

from typing import ClassVar

from precis.handlers.plaintext import PlaintextHandler
from precis.protocol import KindSpec


class TexHandler(PlaintextHandler):
    """Slug-addressed read/write handler for ``.tex`` files.

    Paragraph block grammar inherited from :class:`PlaintextHandler`.
    Intentionally minimal — the next refinement step would add LaTeX
    sectioning awareness without changing the public surface.
    """

    spec: ClassVar[KindSpec] = KindSpec(
        kind="tex",
        title="LaTeX",
        description=(
            "Read and edit local LaTeX files (.tex) under a configured "
            "root. First-cut paragraph block grammar (no sectioning yet); "
            "lazy re-ingest on stale mtime."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        supports_edit=True,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        views=("raw",),
        modes=("create",),
    )

    _KIND: ClassVar[str] = "tex"
    _EXTENSIONS: ClassVar[tuple[str, ...]] = (".tex",)
    _DEFAULT_EXT: ClassVar[str] = ".tex"
