"""Layer-1 + Layer-2 document context for the draw-with-me loop (the
diagram-propose design, extending ADR 0057).

A figure is usually *part of a document*. When it is, the drawer should open
with the owning draft in front of it — not guess from the figure title. This
module assembles that context in two layers:

- **Layer 1 — the owning draft, collapsed.** Every block as a heading + gloss
  row (the ``get(kind='draft')`` outline). Cheap, always included; the drawer
  expands what it needs via Layer 3's tools.
- **Layer 2 — the paragraphs the instruction points at, expanded in place.**
  Parse the salient terms from the draw instruction (*"draw the wibbler inside
  the flurb"* → ``wibbler``, ``flurb``), find where the owning draft defines
  them, and **fisheye those blocks** (verbatim + neighbourhood) right in the
  outline. This is not a separate result list — it is expansion of the Layer-1
  tree, because the drawer is *inside* the draft.

The retrieval here is deterministic keyword/verbatim matching over the draft's
own chunks (no embedder dependency, so it degrades nowhere); the semantic leg
and corpus-wide / external search are Layer 3 — the drawer's own tools. The
expansion reuses the canonical fisheye renderer (``utils.eye_render.render_eye``)
via an injected ``expand`` hook, so the core assembler is pure and unit-testable
with fakes.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any, Protocol

log = logging.getLogger(__name__)

#: How many instruction-matched paragraphs to fisheye-expand. Keeps the prompt
#: bounded — the collapsed outline already carries the whole document.
_MAX_EXPAND = 3

#: Chunk kinds that make poor expansion targets: headings carry no body, and
#: ``figure`` chunks are the captions/anchors themselves (self-reference).
_SKIP_EXPAND_KINDS = frozenset({"heading", "figure"})

#: Instruction words that carry no subject — drawing verbs, view vocabulary,
#: patent boilerplate, and ordinary stopwords. Stripped before term matching so
#: "showing the planar body" seeds on ``planar``/``body``, not ``showing``.
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "with",
        "from",
        "into",
        "onto",
        "over",
        "under",
        "between",
        "above",
        "below",
        "its",
        "it",
        "this",
        "that",
        "these",
        "those",
        "is",
        "are",
        "be",
        "as",
        "by",
        "one",
        "two",
        "three",
        "first",
        "second",
        "third",
        "draw",
        "drawing",
        "show",
        "showing",
        "shown",
        "depict",
        "depicting",
        "view",
        "perspective",
        "figure",
        "fig",
        "diagram",
        "image",
        "picture",
        "according",
        "embodiment",
        "example",
        "exemplary",
        "illustrat",
        "illustrating",
        "illustrated",
        "make",
        "add",
        "please",
        "left",
        "right",
        "top",
        "bottom",
        "side",
        "front",
        "back",
        "here",
        "there",
    }
)


class _ChunkLike(Protocol):
    dc: str
    depth: int
    chunk_kind: str
    text: str
    handle: str
    chunk_id: int


class _StoreLike(Protocol):
    def reading_order(self, ref_id: int, *, kind: str = ...) -> list[Any]: ...


#: An expansion hook: ``(store, draft_chunk_handle) -> rendered fisheye text``.
ExpandFn = Callable[[Any, str], str]


def owning_document(store: Any, figure_ref_id: int) -> tuple[int, int] | None:
    """Resolve ``(draft_ref_id, anchor_chunk_id)`` for a figure, or ``None``.

    Defensive: a store without :meth:`figure_owning_draft` (a fake, or a build
    predating ADR 0058) or a free-standing figure both read as ``None`` — the
    caller then supplies no document context and the loop behaves as before.
    """
    fn = getattr(store, "figure_owning_draft", None)
    if fn is None:
        return None
    try:
        return fn(figure_ref_id)
    except Exception:  # pragma: no cover — defensive against store variance
        log.debug("owning_document: figure_owning_draft failed", exc_info=True)
        return None


def entities_from_instruction(instruction: str) -> list[str]:
    """The salient subject terms of a draw instruction, in first-seen order.

    Quoted phrases are kept whole (``"anchor formation"``); the rest is content
    words minus drawing/boilerplate stopwords. Deduped, lower-cased, capped —
    this is a cheap seed for Layer-2 matching, not a parser."""
    terms: list[str] = []
    seen: set[str] = set()

    def _add(term: str) -> None:
        t = term.strip().lower()
        if len(t) >= 3 and t not in seen:
            seen.add(t)
            terms.append(t)

    for m in re.finditer(r"""["'“”‘’]([^"'“”‘’]{3,60})["'“”‘’]""", instruction):
        _add(m.group(1))
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", instruction):
        low = tok.lower()
        if low in _STOPWORDS or any(low.startswith(s) for s in ("illustrat",)):
            continue
        _add(low)
    return terms[:12]


def _chunk_haystack(chunk: Any) -> str:
    """Lower-cased searchable text for a chunk: its keywords + body."""
    kws = getattr(chunk, "keywords", None) or []
    return (
        " ".join(str(k) for k in kws) + " " + (getattr(chunk, "text", "") or "")
    ).lower()


def pick_paragraphs(
    chunks: list[Any],
    terms: list[str],
    *,
    anchor_chunk_id: int,
    limit: int = _MAX_EXPAND,
) -> list[Any]:
    """The blocks an instruction points at: those matching the most terms, in
    descending match then reading order. Skips the anchor figure chunk itself
    and non-expandable kinds. Empty when nothing matches (Layer 2 degrades to
    the collapsed outline; the drawer searches via Layer 3)."""
    if not terms:
        return []
    scored: list[tuple[int, int, Any]] = []
    for order, c in enumerate(chunks):
        if getattr(c, "chunk_id", None) == anchor_chunk_id:
            continue
        if getattr(c, "chunk_kind", "") in _SKIP_EXPAND_KINDS:
            continue
        hay = _chunk_haystack(c)
        score = sum(1 for t in terms if t in hay)
        if score:
            scored.append((score, order, c))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [c for _s, _o, c in scored[:limit]]


def _default_expand(store: Any, handle: str) -> str:
    """Fisheye a draft chunk via the canonical renderer (verbatim + neighbours,
    ADR 0051 §6). Imported lazily so this module stays import-cheap and testable
    without the eye-render stack."""
    from precis.utils.eye_render import render_eye

    return render_eye(store, handle, "fisheye")


def _doc_title(store: Any, draft_ref_id: int, chunks: list[Any]) -> str:
    ref = None
    getter = getattr(store, "get_ref", None)
    if getter is not None:
        try:
            ref = getter(kind="draft", id=draft_ref_id)
        except Exception:  # pragma: no cover — defensive
            ref = None
    title = " ".join((getattr(ref, "title", "") or "").split())
    if title:
        return title
    for c in chunks:  # first heading's text, else nothing
        if (
            getattr(c, "chunk_kind", "") == "heading"
            and (getattr(c, "text", "") or "").strip()
        ):
            return " ".join(c.text.split())
    return ""


def _gloss(chunk: Any, views: dict[str, Any]) -> str:
    v = views.get(getattr(chunk, "handle", ""), {}) if views else {}
    gloss = v.get("summary") or v.get("keywords") or ""
    if not gloss:
        txt = getattr(chunk, "text", "") or ""
        gloss = txt.splitlines()[0] if txt else ""
    return " ".join(str(gloss).split())


_ADMONITION = (
    "You are drawing a figure that lives **inside this document**. The "
    "document's own words are the ground truth for what to draw — draw from "
    "them, not from the figure title. The outline is collapsed; the paragraphs "
    "your instruction points at are expanded below. You may also search the "
    "corpus (to fill a genuine gap) and go external only for drawing craft the "
    "document can't supply. **Bind every element you commit to the chunk it "
    "depicts** (the `links` field of your reply)."
)


def _format(title: str, outline_rows: list[str], expansions: list[str]) -> str:
    head = (
        f"## The document this figure illustrates — {title}"
        if title
        else ("## The document this figure illustrates")
    )
    parts = [head, _ADMONITION, "### Outline (collapsed)", "\n".join(outline_rows)]
    if expansions:
        parts.append(
            "### The paragraphs your instruction points at (fisheye)\n"
            + "\n\n".join(e.strip() for e in expansions if e and e.strip())
        )
    return "\n\n".join(p for p in parts if p.strip())


def build_document_context(
    store: Any,
    *,
    draft_ref_id: int,
    anchor_chunk_id: int,
    instruction: str,
    expand: ExpandFn | None = None,
    max_expand: int = _MAX_EXPAND,
) -> str:
    """Assemble Layer-1 (collapsed outline) + Layer-2 (instruction-matched
    paragraphs, fisheyed) for the owning draft. ``""`` when the draft has no
    chunks. ``expand`` defaults to the canonical fisheye renderer; tests inject
    a fake. A failing expansion degrades to the block's own verbatim text — a
    thin context is still better than none."""
    chunks = list(store.reading_order(draft_ref_id, kind="draft"))
    if not chunks:
        return ""
    expand = expand or _default_expand
    title = _doc_title(store, draft_ref_id, chunks)
    views: dict[str, Any] = {}
    bv = getattr(store, "block_views", None)
    if bv is not None:
        try:
            views = bv(draft_ref_id) or {}
        except Exception:  # pragma: no cover — defensive
            views = {}
    outline = [
        f"{'  ' * getattr(c, 'depth', 0)}{c.dc}  [{c.chunk_kind}] {_gloss(c, views)}".rstrip()
        for c in chunks
    ]
    terms = entities_from_instruction(instruction)
    picks = pick_paragraphs(
        chunks, terms, anchor_chunk_id=anchor_chunk_id, limit=max_expand
    )
    expansions: list[str] = []
    for c in picks:
        try:
            expansions.append(expand(store, c.dc))
        except Exception:
            log.debug(
                "build_document_context: expand failed for %s", c.dc, exc_info=True
            )
            expansions.append(
                f"{c.dc}  [{c.chunk_kind}]\n{(getattr(c, 'text', '') or '').strip()}"
            )
    return _format(title, outline, expansions)


def document_context_for(store: Any, figure_ref_id: int, instruction: str) -> str:
    """Resolve the owning draft and build its context, or ``""`` for a
    free-standing figure. The single entry point the turn loop calls."""
    owned = owning_document(store, figure_ref_id)
    if owned is None:
        return ""
    draft_ref_id, anchor_chunk_id = owned
    return build_document_context(
        store,
        draft_ref_id=draft_ref_id,
        anchor_chunk_id=anchor_chunk_id,
        instruction=instruction,
    )
