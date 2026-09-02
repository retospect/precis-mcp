"""End-to-end lock: a heading's own ``\\label{...}`` is captured into the
heading chunk's ``meta.label`` at import (gripe 271293) — glued onto the
same line as the heading command, or its own standalone paragraph. Neither
variant used to have a durable home; both dangled through ``\\ref``
resolution only, with nothing in ``meta`` to show for it.

DB-gated (uses the ``store`` fixture) — runs in the integration gate.
"""

from __future__ import annotations

from pathlib import Path

from precis.draftimport.build import run_import
from precis.store import Store

_TEX = r"""
\documentclass{article}
\begin{document}
\section{Glued Heading}\label{sec:glued}
Prose right after the heading, no blank line before it.

\section{Standalone Heading}

\label{sec:standalone}

Prose describing the standalone-labelled section.

See \ref{sec:glued} and \ref{sec:standalone} for details.
\end{document}
"""


def _write_tex(tmp_path: Path) -> Path:
    main = tmp_path / "main.tex"
    main.write_text(_TEX, encoding="utf-8")
    return main


def test_glued_and_standalone_heading_labels_persist_to_meta(
    store: Store, tmp_path: Path
) -> None:
    main = _write_tex(tmp_path)
    run_import(store, main, slug="heading-label-note", title="Heading Label Note")

    ref = store.get_ref(kind="draft", id="heading-label-note")
    assert ref is not None
    chunks = store.drafts.reading_order(ref.id)

    headings = {c.text: c for c in chunks if c.chunk_kind == "heading"}
    assert "Glued Heading" in headings
    assert "Standalone Heading" in headings

    glued = headings["Glued Heading"]
    standalone = headings["Standalone Heading"]
    assert glued.meta.get("label") == "sec:glued"
    assert standalone.meta.get("label") == "sec:standalone"

    # No stray empty/placeholder paragraph left behind by the peeled-off
    # standalone \label — the very next child is the real prose.
    standalone_children = [
        c for c in chunks if c.parent_chunk_id == standalone.chunk_id
    ]
    assert standalone_children, "standalone heading has no children at all"
    assert "describing the standalone" in (standalone_children[0].text or "")

    # Both \ref{}s resolved to a live internal handle (dc<id>), not left as
    # dangling literal \ref{...} text anywhere in the draft.
    joined = "\n".join(c.text or "" for c in chunks)
    assert "\\ref{" not in joined
