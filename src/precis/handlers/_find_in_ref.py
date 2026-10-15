"""Find-in-ref — ``get(kind=K, id=N, q='…')`` locates text inside one ref.

gr486840: ``get(kind='memory', id=483106, q='plaza packing')`` used to
accept ``q`` and silently ignore it, so agents fell back to grepping
``chunks.text`` through ``prod-psql``. This module is the single shared
implementation the text-bearing kinds route to: a ``q=`` on a
concrete-id ``get`` scans the ref's chunks (or file body for the
file-backed kinds) and returns only the matching passages — a few lines
of context each, labelled with the chunk/selector handle and line number
— plus a match count, or ``no match`` when nothing hits.

Dispatch contract: :attr:`~precis.protocol.Handler.supports_find_in_ref`
opts a kind in; the dispatcher then routes ``get`` + non-None ``q`` to
``handler.find_in_ref`` *before* ``get`` runs, so a subclass's ``view=``
interception can never drop ``q`` silently (``view``+``q`` is a
``BadInput`` here). Kinds where ``q`` is the compute/fetch query
(calc, the cache-backed fetchers) do not opt in and are untouched;
kinds that never declared ``q`` at all still refuse it at the strictness
gate.

Matching: case-insensitive literal substring by default; ``q='re:<pat>'``
opts into a bounded regex — length-capped, no backrefs, no nested
quantifiers (the classic ReDoS shape), and each scanned line is itself
truncated before matching, so a bad pattern costs at most a bounded
scan of one short line. ``args={'context': N}`` widens the ±3-line
window (capped).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

from precis.errors import BadInput
from precis.response import Response
from precis.utils import handle_registry

#: Spelling that opts a ``q=`` into regex mode: ``q='re:<pattern>'``.
REGEX_PREFIX = "re:"

#: Bound on the caller-supplied pattern — long enough for any realistic
#: find, short enough that a pasted blob can't become the pattern.
_MAX_PATTERN_CHARS = 256

#: Each line is truncated to this before a regex scan — the per-match
#: backtracking budget scales with line length, so a pathological
#: pattern that slips the shape checks below still dies on a bounded
#: input. Literal matching is O(n) and scans the untruncated line.
_MAX_REGEX_LINE_CHARS = 4096

#: Shape checks that refuse the two classic catastrophic-backtracking
#: idioms before ``re.compile`` ever sees them: a quantifier applied to
#: a group whose own tail is quantified (``(a+)+``, ``(x*)*`` …), and
#: numbered/named backrefs or conditionals (``\1``, ``\g<n>``, ``(?()``)
#: — the first needs exponential tries to fail, the second isn't text
#: finding anyway.
_NESTED_QUANT_RE = re.compile(r"[*+?}]\s*\)\s*[*+?{]")
_BACKREF_RE = re.compile(r"\\[1-9]|\\g<(?:!P?)?[a-zA-Z0-9_]*>|\(\?\(")

#: Context lines rendered either side of a match (grep -C). The
#: ``args={'context': N}`` override is clamped to this ceiling — a wider
#: window is a full-ref read wearing a find's clothes.
DEFAULT_CONTEXT_LINES = 3
_MAX_CONTEXT_LINES = 25

#: Render cap on matched *lines* (not passages). Beyond it the tail is
#: summarised, not rendered — the dispatcher's own 24 KiB split would
#: page it anyway, but a ``q='the'``-shaped query doesn't deserve the
#: render cost first.
_MAX_MATCH_LINES = 400

#: chunk_kind values that carry the ref's primary prose — rendered
#: unlabelled. ``<kind>_body`` also counts (memory_body, gripe_body, …).
#: Anything else (``<kind>_comment``, quest_log, job_event, review
#: notes) gets a ``(<chunk_kind>)`` tag in the passage header so the
#: caller can tell body hits from commentary hits.
_PRIMARY_CONTENT_KINDS = frozenset(
    {
        "body",
        "paragraph",
        "block",
        "text",
        "heading",
        "term",
        "table",
        "figure",
        "equation",
        "references",
        "claim",
        "step",
        "field",
        "pres_slide",
        "patent_claim",
        "edgar_section",
        "draft_section",
    }
)


def _is_primary_chunk(kind_tag: Any) -> bool:
    return isinstance(kind_tag, str) and (
        kind_tag in _PRIMARY_CONTENT_KINDS or kind_tag.endswith("_body")
    )


def find_preflight(
    *,
    kind: str,
    id: Any = None,
    view: Any = None,
    q: Any = None,
) -> str:
    """Validate a find-in-ref call and return the stripped query.

    Shared by every ``find_in_ref`` implementation so the refusal shapes
    are identical across kinds: empty ``q``, ``q`` combined with a
    ``view`` (the eye ladder never reaches here — the dispatcher routes
    it to ``Handler.eye`` first, and a ladder label's own ``q`` keeps
    its ring-expansion meaning), a batch or ``/path`` list id, and a
    missing id all raise ``BadInput`` with a recovery hint rather than
    falling through to a whole-ref render that drops ``q``.
    """
    if q is None or not str(q).strip():
        raise BadInput(
            f"get(kind={kind!r}): q= is empty — find-in-ref matches literal "
            "text case-insensitively, or q='re:<regex>' for a bounded pattern",
            next=f"get(kind={kind!r}, id=<ref>, q='text to find')",
        )
    if view is not None:
        raise BadInput(
            f"get(kind={kind!r}): q= (find-in-ref) doesn't combine with "
            f"view={view!r} — drop view=, or use an eye-ladder extent "
            "where q= keeps its ring-expansion meaning",
            next=f"get(kind={kind!r}, id=<ref>, q={str(q).strip()!r})",
        )
    if id is None:
        raise BadInput(
            f"get(kind={kind!r}, q=...) is find-in-ref — it needs the ref "
            "to search inside",
            next=(
                f"get(kind={kind!r}, id=<ref>, q={str(q).strip()!r}) — or "
                f"search(kind={kind!r}, q=...) to rank refs by content"
            ),
        )
    if isinstance(id, (list, tuple)):
        raise BadInput(
            f"get(kind={kind!r}): find-in-ref is one ref at a time",
            next=f"get(kind={kind!r}, id=<ref>, q={str(q).strip()!r})",
        )
    if isinstance(id, str) and id.strip().startswith("/"):
        raise BadInput(
            f"get(kind={kind!r}): q= finds inside one ref, not inside a "
            f"list view {id!r}",
            next=(
                f"get(kind={kind!r}, id=<ref>, q={str(q).strip()!r}) — or "
                f"search(kind={kind!r}, q=...) to rank the listed refs"
            ),
        )
    return str(q).strip()


def _clamp_context(context: Any) -> int:
    if context is None:
        return DEFAULT_CONTEXT_LINES
    try:
        n = int(context)
    except (TypeError, ValueError):
        raise BadInput(
            f"args={{'context': …}} takes an int, got {context!r}",
            next="drop it for the default ±3 lines",
        ) from None
    return max(0, min(n, _MAX_CONTEXT_LINES))


def _compile_matcher(q: str) -> Callable[[str], bool]:
    """Return a per-line predicate for ``q``.

    Literal ``q`` matches as a case-insensitive substring. ``re:<pat>``
    compiles to a bounded regex — pattern length capped, backrefs /
    conditionals / nested quantifiers refused up front, and each scanned
    line truncated to :data:`_MAX_REGEX_LINE_CHARS` so the worst case is
    a bounded scan of a short string.
    """
    if not q.startswith(REGEX_PREFIX):
        needle = q.casefold()
        return lambda line: needle in line.casefold()

    pat = q[len(REGEX_PREFIX) :]
    if not pat.strip():
        raise BadInput(
            "q='re:' has no pattern",
            next="q='re:<regex>' — or drop the prefix for literal text",
        )
    if len(pat) > _MAX_PATTERN_CHARS:
        raise BadInput(
            f"find-in-ref regex is capped at {_MAX_PATTERN_CHARS} chars "
            f"({len(pat)} given)",
            next="narrow the pattern, or use a literal q=",
        )
    if _NESTED_QUANT_RE.search(pat) or _BACKREF_RE.search(pat):
        raise BadInput(
            "find-in-ref refuses backrefs and nested quantifiers "
            "(catastrophic-backtracking shapes) in q='re:…'",
            next="simplify the pattern, or use a literal q=",
        )
    try:
        rx = re.compile(pat, re.IGNORECASE)
    except re.error as exc:
        raise BadInput(
            f"bad find-in-ref regex {pat!r}: {exc}",
            next="drop the 're:' prefix for case-insensitive literal text",
        ) from exc
    return lambda line: rx.search(line[:_MAX_REGEX_LINE_CHARS]) is not None


def _passages(
    hit_lines: list[int], *, context: int
) -> list[tuple[int, int, list[int]]]:
    """Merge hit lines into ``(start, end, hits)`` windows (0-based, incl.).

    Two hits share one passage when their context windows touch — the
    same shape ``grep -C`` produces, so context is never printed twice.
    """
    out: list[tuple[int, int, list[int]]] = []
    for h in hit_lines:
        lo, hi = h - context, h + context
        if out and lo <= out[-1][1] + 1:
            prev_lo, prev_hi, prev_hits = out[-1]
            out[-1] = (prev_lo, max(prev_hi, hi), [*prev_hits, h])
        else:
            out.append((lo, hi, [h]))
    return out


def _chunk_id(chunk: Any) -> Any:
    """The row's primary key across chunk shapes (``ChunkRow.id``,
    ``DraftChunk.chunk_id``)."""
    cid = getattr(chunk, "id", None)
    return cid if cid is not None else getattr(chunk, "chunk_id", None)


def _chunk_pos(chunk: Any) -> Any:
    """The row's in-ref position (``ChunkRow.ord``, ``DraftChunk.pos``)."""
    pos = getattr(chunk, "ord", None)
    return pos if pos is not None else getattr(chunk, "pos", "?")


def _chunk_label(kind: str, ref_display: str, chunk: Any) -> str:
    """The passage's chunk/selector handle.

    Kinds with a registered chunk code get the flat handle (``pc12``,
    ``dc12``, ``gc12``, ``mc12`` …); kinds without one get the
    ``<ref>~<ord>`` selector spelling — the same ``kind:identifier[~pos]``
    grammar link targets already use.
    """
    handle = handle_registry.try_format(kind, _chunk_id(chunk), chunk=True)
    if handle is not None:
        return handle
    return f"{ref_display}~{_chunk_pos(chunk)}"


def find_response(
    *,
    kind: str,
    display: str,
    chunks: Sequence[Any],
    q: str,
    context: Any = None,
    chunk_label: Callable[[Any], str] | None = None,
    ref_id: int | None = None,
) -> Response:
    """Scan ``chunks`` for ``q`` and render the matching passages.

    ``chunks`` is any ordered sequence of chunk-shaped rows (``.id``,
    ``.ord``, ``.slug``, ``.text``, ``.meta``, ``.chunk_kind`` —
    ``ChunkRow`` satisfies it; file-backed kinds pass an equivalent
    single-block shim). ``display`` is the caller-facing ref address
    used in the header and in ``~<ord>`` fallback labels. ``chunk_label``
    overrides the per-chunk header label for kinds whose chunks aren't
    handle-addressable (e.g. the md file index).

    Output pages through the ordinary dispatcher split — a long result
    returns the head plus a ``more()`` cursor like any oversized get.
    """
    ctx = _clamp_context(context)
    match = _compile_matcher(q)
    label_of = chunk_label or (lambda c: _chunk_label(kind, display, c))

    n_matches = 0
    matched_chunks = 0
    rendered = 0
    elided = 0
    sections: list[str] = []

    for chunk in chunks:
        text = getattr(chunk, "text", "") or ""
        lines = text.splitlines()
        hits = [i for i, ln in enumerate(lines) if match(ln)]
        if not hits:
            continue
        matched_chunks += 1
        n_matches += len(hits)
        if rendered >= _MAX_MATCH_LINES:
            elided += len(hits)
            continue
        if rendered + len(hits) > _MAX_MATCH_LINES:
            # Render up to the cap, then summarise the rest — counts
            # stay honest; only the render is bounded.
            elided += len(hits) - (_MAX_MATCH_LINES - rendered)
            hits = hits[: _MAX_MATCH_LINES - rendered]
        rendered += len(hits)
        label = label_of(chunk)
        kind_tag = getattr(chunk, "chunk_kind", None)
        tag = f" ({kind_tag})" if kind_tag and not _is_primary_chunk(kind_tag) else ""
        line_base = 1
        meta = getattr(chunk, "meta", None) or {}
        if isinstance(meta.get("line_start"), int):
            # File-backed kinds number lines against the file, not the
            # block — the number a caller would grep for on disk.
            line_base = int(meta["line_start"])
        elif isinstance(getattr(chunk, "line_start", None), int):
            # md-index blocks carry line_start as an attribute.
            line_base = int(chunk.line_start)
        hit_desc = ", ".join(str(line_base + h) for h in hits[:6])
        if len(hits) > 6:
            hit_desc += f", +{len(hits) - 6} more"
        for lo, hi, group in _passages(hits, context=ctx):
            lo_c, hi_c = max(lo, 0), min(hi, len(lines) - 1)
            marks = set(group)
            sections.append(f"## {label}{tag} · lines {hit_desc}")
            for i in range(lo_c, hi_c + 1):
                prefix = "> " if i in marks else "  "
                sections.append(f"{prefix}{line_base + i:>4}: {lines[i]}")
            sections.append("")

    head = [f"# find {q!r} in {display}"]
    if n_matches == 0:
        head.append(f"no match — scanned {len(chunks)} chunks")
    else:
        plural = "match" if n_matches == 1 else "matches"
        chunk_plural = "chunk" if matched_chunks == 1 else "chunks"
        head.append(f"{n_matches} {plural} in {matched_chunks} {chunk_plural}")
        if elided:
            head[-1] += f" — +{elided} matches elided (narrow q=)"
    head.append("")
    body = "\n".join(head + sections).rstrip() + "\n"
    return Response(body=body, ref_id=ref_id)
