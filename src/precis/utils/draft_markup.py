"""Inline reference markers in draft prose (ADR 0033 §8).

Draft chunk text is markdown-ish: prose, ``$…$`` math, and inline
references. Two surfaces consume the *same* grammar (DRY, mirroring how
``mentions``/``linkify`` already split for notes):

* **parse / strip** (here) — pull references out of a chunk's source and
  reduce them to their surface words for embedding.
* **resolve** (here) — turn the references into live ``link`` targets so
  a draft becomes a node in the graph (the write-time autolinker).
* **highlight** (``precis_web.linkify``) — render the same markers as
  hover-preview anchors at read time.

The regex *atoms* live in :mod:`precis.utils.mentions` (the grammar
SSOT); this module is the draft-specific parser + resolver over them.

Reference forms:

* ``[[<handle>]]``           — **the** reference form: a handle is a ref to
                               *something* (``dc41`` a draft chunk, ``me5`` a
                               memory, ``pc10`` a paper chunk, …), resolved by
                               the one ADR 0036 decoder ``store.resolve_handle``.
* ``[text](<handle>)``       — same, with display text.
* ``[§<paper>~<n>]``         — paper **citation** (cite_key-keyed; exports to
                               a cite + bibliography — the one non-handle
                               exception, since the bibliography needs the key).
* ``[text](https://…)``      — a web link.

Legacy, still resolved during the transition: ``[¶<handle>]`` chunk
cross-refs and bare ``kind:ref`` mentions (``paper:miller89~4``).

The classes here are *syntactic* — what a handle points at (a section, a
glossary term, a memory) is resolved later against the target.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from precis.utils import handle_registry
from precis.utils.mentions import (
    DRAFT_CITE_PATTERN,
    DRAFT_MARKUP_PATTERN,
    LinkTarget,
    chunk_to_pos,
    resolve_handle_ref,
    resolve_link_targets,
)

log = logging.getLogger(__name__)

# class values
WEB = "web"  # external URL
XREF = "xref"  # ¶handle — this draft (section or glossary term)
CITE = "cite"  # §paper~n — external corpus citation
AUTHORING = "authoring"  # [[address]] — provenance only, renders to nothing

# Grammar atoms are single-sourced in ``mentions`` so the linkifier and
# this parser can never drift apart.
_PATTERN = DRAFT_MARKUP_PATTERN


@dataclass(frozen=True, slots=True)
class Reference:
    """A parsed inline reference."""

    cls: str  # WEB | XREF | CITE | AUTHORING
    target: str  # url, ``¶<handle>``, ``§<paper>~<n>``, or an address
    surface: str | None  # display text, when the form carried one
    raw: str  # the exact matched marker


def _classify_link(disp: str, tgt: str) -> Reference:
    raw = f"[{disp}]({tgt})"
    if tgt.startswith("¶"):
        return Reference(XREF, tgt, disp, raw)
    if tgt.startswith("§"):
        return Reference(CITE, tgt, disp, raw)
    if tgt.startswith(("http://", "https://")):
        return Reference(WEB, tgt, disp, raw)
    # any other address (memory:7x2, think:…) is an authoring link
    return Reference(AUTHORING, tgt, disp, raw)


def parse_references(text: str) -> list[Reference]:
    """Every inline bracket reference in ``text``, in order of appearance."""
    refs: list[Reference] = []
    for m in _PATTERN.finditer(text):
        if m.group("auth") is not None:
            refs.append(Reference(AUTHORING, m.group("auth"), None, m.group(0)))
        elif m.group("tgt") is not None:
            refs.append(_classify_link(m.group("disp"), m.group("tgt")))
        else:  # bare [¶…] / [§…]
            bare = m.group("bare")
            cls = XREF if bare.startswith("¶") else CITE
            refs.append(Reference(cls, bare, None, m.group(0)))
    return refs


def strip_markers(text: str) -> str:
    """Markers reduced to the surface words they carry, for embedding /
    search (ADR 0033 §8): display links keep their text, bare refs and
    authoring links are dropped. A pure function of *this* chunk's text —
    so ``content_sha`` over the source fully determines the embed input.
    """

    def repl(m: re.Match[str]) -> str:
        if m.group("auth") is not None:
            return ""  # authoring → nothing
        if m.group("tgt") is not None:
            return m.group("disp") or ""  # keep display words
        return ""  # bare [¶…]/[§…] → nothing

    return re.sub(r"\s+", " ", _PATTERN.sub(repl, text)).strip()


# ---------------------------------------------------------------------------
# Resolution — the write-time autolinker (reuses the ``mentions`` resolvers
# so there is one lookup path, not two).
# ---------------------------------------------------------------------------


def resolve_draft_handle(
    store: Any, handle: str, *, include_retired: bool = False
) -> LinkTarget | None:
    """A ``¶<handle>`` draft-chunk anchor → a chunk-level ``LinkTarget``.

    Maps the opaque handle to its chunk's ``(ref_id, ord)`` — ``ord`` is
    the chunk position the link layer addresses by (``chunks.ord``).
    Skips retired chunks unless asked, and rows with a NULL ``ord``.
    Best-effort: any lookup failure logs and yields ``None``.
    """
    h = handle[1:] if handle.startswith("¶") else handle
    sql = "SELECT ref_id, ord FROM chunks WHERE handle = %s" + (
        "" if include_retired else " AND retired_at IS NULL"
    )
    try:
        with store.pool.connection() as conn:
            row = conn.execute(sql, (h,)).fetchone()
    except Exception:
        log.debug("draft_markup: handle lookup failed for %r", handle, exc_info=True)
        return None
    if row is None or row[1] is None:
        return None
    return LinkTarget(int(row[0]), int(row[1]))


def resolve_universal_handle(store: Any, token: str) -> LinkTarget | None:
    """A bare ADR 0036 universal handle (``dc41`` a draft chunk, ``me5`` a
    memory, ``pc10`` a paper chunk, …) → a live ``LinkTarget`` via the one
    decoder ``store.resolve_handle``. This is the simple, uniform rule the
    LLM relies on: *a handle is a ref to something*. ``None`` if the token
    is not a well-formed / resolvable handle (so the caller falls through
    to the legacy ``kind:id`` / ``¶`` / ``§`` paths)."""
    if not handle_registry.is_well_formed(handle_registry.normalize(token)):
        return None
    try:
        r = store.resolve_handle(token)
    except Exception:
        log.debug("draft_markup: resolve_handle failed for %r", token, exc_info=True)
        return None
    if r is None:
        return None
    pos = r.chunk_ord if getattr(r, "chunk_id", None) is not None else None
    return LinkTarget(int(r.ref_id), pos)


def _resolve_reference(store: Any, ref: Reference) -> LinkTarget | None:
    """One parsed bracket reference → a live ``LinkTarget`` (or ``None``).

    A handle-shaped target (``[[dc41]]`` / ``[label](me5)``) resolves
    through the universal ADR 0036 decoder first — one rule, any kind.
    ``WEB`` is external (no graph edge); the legacy ``¶`` (XREF), ``§``
    (CITE), and bare ``kind:id`` (AUTHORING, via
    :func:`mentions.resolve_link_targets`) forms still resolve during the
    transition.
    """
    if ref.cls in (AUTHORING, XREF):
        universal = resolve_universal_handle(store, ref.target.lstrip("¶"))
        if universal is not None:
            return universal
    if ref.cls == XREF:
        return resolve_draft_handle(store, ref.target)
    if ref.cls == CITE:
        m = DRAFT_CITE_PATTERN.fullmatch(ref.target)
        if m is None:
            return None
        r = resolve_handle_ref(store, m.group("slug"))
        if r is None or getattr(r, "deleted_at", None) is not None:
            return None
        return LinkTarget(r.id, chunk_to_pos(m.group("chunk")))
    return None


def resolve_draft_link_targets(
    store: Any, text: str, *, exclude_ref_id: int | None = None
) -> list[LinkTarget]:
    """Resolve the *superset* of references in a draft's prose to live
    ``LinkTarget``s: bare ``kind:ref`` mentions (incl. those inside
    ``[txt](kind:id)`` / ``[[kind:id]]``, via the shared resolver),
    ``¶handle`` cross-refs, and ``§slug~n`` citation sugar.

    Deduped by ``(ref_id, pos)``; skips unresolved / soft-deleted targets
    and (like the note autolinker) anything pointing back at
    ``exclude_ref_id`` — so an intra-draft ``¶`` ref is a within-document
    concern (surfaced by the renderer / TOC), not a graph edge.
    """
    targets: dict[tuple[int, int | None], LinkTarget] = {}
    for t in resolve_link_targets(store, text, exclude_ref_id=exclude_ref_id):
        targets.setdefault((t.dst_ref_id, t.dst_pos), t)
    for ref in parse_references(text):
        tgt = _resolve_reference(store, ref)
        if tgt is None:
            continue
        if exclude_ref_id is not None and tgt.dst_ref_id == exclude_ref_id:
            continue
        targets.setdefault((tgt.dst_ref_id, tgt.dst_pos), tgt)
    return list(targets.values())
