"""Term coverage at approve and sign — the DB half (warning, never blocking).

:mod:`precis.taproot.coverage` is the pure text rule: which terms the claim
names that no grounding passage carries, with acronyms expanded from the
evidence papers' own text. This module feeds it and answers the follow-up a
reviewer actually needs: *where in the papers is it?* fi189535's claim named
TEM and STS measurements; its grounding was an abstract plus a definition
sentence, and the TEM caption (pc209502) and the STS passage (pc209509)
sat in the same paper, one click away from being evidence.

* **Passages** — what the reviewer attests, i.e. each grounding passage's
  ``quote`` (the chunk text only when a prefill left the quote blank).
  Not ``context_sentence``: that is a paper-level sentence, not a quoted
  passage.
* **Papers behind the hub** — the grounding passages' own papers first,
  then every other evidence source on the hub. Their live body chunks
  (``ord >= 0``, ``retired_at IS NULL``, references blocks excluded) feed
  both the acronym map and the suggestions.
* **Suggestions** — up to :data:`SUGGESTIONS_PER_TERM` chunks per
  uncovered term that literally carry it (the acronym or an expansion; the
  mode stem; the number). Ranked by :func:`chunk_tier` — figure/table
  captions, then methods/results sections, then other body text, then
  abstract and front matter — then grounding papers before other
  evidence, then how often the chunk says it, then reading order; the
  best chunk of each paper is taken before a second from any one paper
  (:func:`_pick`). Section information is whatever the
  reader stored in ``chunks.section_path`` (many papers have only the title
  there, so the abstract is also recognised by containment in
  ``refs.meta['abstract']`` and the first two chunks count as front
  matter).

It is a *warning*: :func:`coverage_warning` returns text for a
non-blocking preflight issue, a CLI line, and the approve-view note; no
caller refuses on it until the build-2 sweep
(:mod:`precis.nanopub.grounding_sweep`, ``precis nanopub sweep-grounding``)
measures its false-alarm rate (claims-and-evidence thread, D3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.nanopub import evidence
from precis.nanopub.gates import integral_chunk_id
from precis.taproot import coverage
from precis.taproot.coverage import KIND_ACRONYM, KIND_MODE, Term
from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.store import Store

#: Preflight check slug (and the gate-panel row name).
CHECK = "term-coverage"

SUGGESTIONS_PER_TERM = 3

#: How many uncovered terms the message spells out before "+N more".
_TERM_CAP = 6

_SNIPPET_CHARS = 110

#: Ranking tiers (lower sorts first).
TIER_CAPTION, TIER_METHODS, TIER_BODY, TIER_FRONT = 0, 1, 2, 3

_CAPTION_START_RE = re.compile(
    r"^\s*(?:supplementary\s+)?(?:fig(?:ure)?s?\.?|table|scheme)\s*S?\d", re.IGNORECASE
)
_METHODS_RE = re.compile(
    r"method|experiment|material|result|characteri[sz]ation|measurement|"
    r"procedure|synthesis|computational|simulation|discussion",
    re.IGNORECASE,
)
_FRONT_RE = re.compile(
    r"abstract|summary|highlights|keywords|affiliation|author", re.IGNORECASE
)
_CAPTION_KINDS = frozenset({"figure", "caption", "table"})
_WS_RE = re.compile(r"\s+")

#: A front-matter heading ("ABSTRACT") over more chunks of one paper than
#: this is an extraction fault (the reader filed the whole paper under it),
#: not an abstract; it no longer makes its chunks front matter.
FRONT_SECTION_MAX_CHUNKS = 6


@dataclass(frozen=True, slots=True)
class Suggestion:
    """One chunk that carries an uncovered term."""

    chunk_handle: str
    snippet: str
    tier: int
    in_grounding_paper: bool


@dataclass(frozen=True, slots=True)
class UncoveredTerm:
    term: Term
    suggestions: tuple[Suggestion, ...]


@dataclass(frozen=True, slots=True)
class PaperChunk:
    """One live body chunk of an evidence paper."""

    chunk_id: int
    ref_id: int
    handle: str
    ord: int
    kind: str
    section_path: list[str]
    text: str


def heading_sizes(chunks: list[PaperChunk]) -> dict[tuple[int, tuple[str, ...]], int]:
    """How many of ``chunks`` share each ``(ref_id, section_path)`` — the
    ``heading_chunks`` input of :func:`chunk_tier`. Pass a paper's whole
    chunk list; a partial one under-counts."""
    sizes: dict[tuple[int, tuple[str, ...]], int] = {}
    for c in chunks:
        key = (c.ref_id, tuple(c.section_path))
        sizes[key] = sizes.get(key, 0) + 1
    return sizes


def chunk_tier(
    *,
    kind: str,
    section_path: list[str],
    ord_: int,
    text: str,
    abstract: str | None,
    heading_chunks: int = 0,
) -> int:
    """Where a chunk ranks as a suggestion — see the module docstring.
    ``heading_chunks`` is how many chunks of the paper share this chunk's
    ``section_path`` (:func:`heading_sizes`); over
    :data:`FRONT_SECTION_MAX_CHUNKS` the heading is not read as front
    matter (0 = unknown, the heading counts)."""
    if kind in _CAPTION_KINDS or _CAPTION_START_RE.match(text):
        return TIER_CAPTION
    path = " > ".join(section_path)
    head = _WS_RE.sub(" ", text).strip().lower()[:60]
    front_heading = (
        _FRONT_RE.search(path) is not None
        and heading_chunks <= FRONT_SECTION_MAX_CHUNKS
    )
    if (
        front_heading
        or ord_ <= 1
        or (abstract and len(head) >= 30 and head in _WS_RE.sub(" ", abstract).lower())
    ):
        return TIER_FRONT
    if _METHODS_RE.search(path):
        return TIER_METHODS
    return TIER_BODY


def passage_texts(store: Store, grounding: dict[str, Any]) -> list[str]:
    """What a grounding envelope's passages say: each ``quote``, or the
    pinned chunk's text where the quote is blank."""
    passages = list((grounding or {}).get("passages") or [])
    blank_ids = [
        cid
        for p in passages
        if not str(p.get("quote") or "").strip()
        and (cid := integral_chunk_id(p.get("chunk_id"))) is not None
    ]
    by_id = {c.chunk_id: c.text for c in evidence.fetch_chunks(store, blank_ids)}
    out: list[str] = []
    for p in passages:
        text = str(p.get("quote") or "").strip()
        if not text:
            cid = integral_chunk_id(p.get("chunk_id"))
            text = by_id.get(cid, "") if cid is not None else ""
        if text:
            out.append(text)
    return out


def paper_chunks(
    store: Store, ref_ids: list[int]
) -> tuple[list[PaperChunk], dict[int, str]]:
    """Live body chunks of ``ref_ids`` plus each ref's stored abstract."""
    if not ref_ids:
        return [], {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT c.chunk_id, c.ref_id, r.kind, c.ord, c.chunk_kind,
                   c.section_path, c.text, r.meta->>'abstract'
              FROM chunks c JOIN refs r ON r.ref_id = c.ref_id
             WHERE c.ref_id = ANY(%s) AND c.ord >= 0 AND c.retired_at IS NULL
               AND c.chunk_kind <> 'references'
             ORDER BY c.ref_id, c.ord
            """,
            (ref_ids,),
        ).fetchall()
    chunks: list[PaperChunk] = []
    abstracts: dict[int, str] = {}
    for r in rows:
        handle = handle_registry.try_format(str(r[2]), int(r[0]), chunk=True)
        if r[7]:
            abstracts[int(r[1])] = str(r[7])
        chunks.append(
            PaperChunk(
                chunk_id=int(r[0]),
                ref_id=int(r[1]),
                handle=handle or f"chunk:{r[0]}",
                ord=int(r[3]),
                kind=str(r[4] or ""),
                section_path=list(r[5] or []),
                text=str(r[6] or ""),
            )
        )
    return chunks, abstracts


def _snippet(text: str, pos: int | None) -> str:
    """A one-line window of the (markup-stripped) ``text`` around ``pos``."""
    flat = _WS_RE.sub(" ", text)
    start = 0
    if pos:
        start = max(0, len(_WS_RE.sub(" ", text[:pos])) - 30)
    end = start + _SNIPPET_CHARS
    return (
        ("…" if start else "")
        + flat[start:end].strip()
        + ("…" if end < len(flat) else "")
    )


def term_coverage(
    store: Store,
    hub_ref_id: int,
    sentence: str,
    grounding: dict[str, Any],
    *,
    bundle: evidence.HubBundle | None = None,
) -> list[UncoveredTerm]:
    """Terms ``sentence`` names that no passage of ``grounding`` carries,
    each with up to :data:`SUGGESTIONS_PER_TERM` chunks from the hub's
    papers that do. ``[]`` when the grounding has no passage text. Pure
    read."""
    passages = passage_texts(store, grounding)
    if not passages:
        return []

    grounding_chunk_ids = [
        cid
        for p in (grounding or {}).get("passages") or []
        if (cid := integral_chunk_id(p.get("chunk_id"))) is not None
    ]
    grounding_refs = {
        c.ref_id for c in evidence.fetch_chunks(store, grounding_chunk_ids)
    }
    if bundle is None:
        bundle = evidence.load_bundle(store, hub_ref_id)
    other_refs = [s.ref_id for s in bundle.sources if s.ref_id not in grounding_refs]
    chunks, abstracts = paper_chunks(store, sorted(grounding_refs) + other_refs)

    return analyse(sentence, passages, chunks, abstracts, grounding_refs)


def analyse(
    sentence: str,
    passages: list[str],
    chunks: list[PaperChunk],
    abstracts: dict[int, str],
    grounding_refs: set[int],
) -> list[UncoveredTerm]:
    """The DB-free core of :func:`term_coverage`: ``passages`` (the signed
    texts), ``chunks`` (the hub's papers' live body chunks) and each
    paper's ``abstracts``; ``grounding_refs`` are the refs the grounding
    quotes from (their chunks rank first among equals)."""
    amap = coverage.acronym_map(
        passages + list(abstracts.values()) + [c.text for c in chunks]
    )
    missing = coverage.uncovered_terms(sentence, passages, amap)
    if not missing:
        return []

    prepared = [(c, coverage.prepare(c.text)) for c in chunks]
    sizes = heading_sizes(chunks)
    out: list[UncoveredTerm] = []
    for term in missing:
        hits: list[tuple[tuple[int, int, int, int, int], int, Suggestion]] = []
        for chunk, prep in prepared:
            pos = coverage.find_term(term, prep, amap)
            if pos is None:
                continue
            tier = chunk_tier(
                kind=chunk.kind,
                section_path=chunk.section_path,
                ord_=chunk.ord,
                text=chunk.text,
                abstract=abstracts.get(chunk.ref_id),
                heading_chunks=sizes.get((chunk.ref_id, tuple(chunk.section_path)), 0),
            )
            in_grounding = chunk.ref_id in grounding_refs
            density = coverage.count_term(term, prep, amap)
            hits.append(
                (
                    (tier, 0 if in_grounding else 1, -density, chunk.ref_id, chunk.ord),
                    chunk.ref_id,
                    Suggestion(
                        chunk_handle=chunk.handle,
                        snippet=_snippet(prep.text, pos),
                        tier=tier,
                        in_grounding_paper=in_grounding,
                    ),
                )
            )
        hits.sort(key=lambda h: h[0])
        out.append(UncoveredTerm(term=term, suggestions=_pick(hits)))
    return out


def _pick(
    hits: list[tuple[tuple[int, int, int, int, int], int, Suggestion]],
) -> tuple[Suggestion, ...]:
    """Top :data:`SUGGESTIONS_PER_TERM` of the sorted ``hits``: first the
    best chunk of each paper (body tiers only), so one paper's run of
    captions cannot crowd out the others; then the rest in rank order. The
    chosen set is shown in rank order."""
    chosen: list[int] = []
    seen_refs: set[int] = set()
    for i, (key, ref_id, _s) in enumerate(hits):
        if key[0] < TIER_FRONT and ref_id not in seen_refs:
            seen_refs.add(ref_id)
            chosen.append(i)
    chosen = chosen[:SUGGESTIONS_PER_TERM]
    for i in range(len(hits)):
        if len(chosen) >= SUGGESTIONS_PER_TERM:
            break
        if i not in chosen:
            chosen.append(i)
    return tuple(hits[i][2] for i in sorted(chosen))


def describe(item: UncoveredTerm) -> str:
    t = item.term
    if t.kind == KIND_ACRONYM:
        label = t.text + (f" ({t.expansion})" if t.expansion else "")
    elif t.kind == KIND_MODE:
        label = f"'{t.text}'"
    else:
        label = f"number {t.text}"
    if not item.suggestions:
        return f"{label} (not found in the evidence papers either)"
    where = ", ".join(f"{s.chunk_handle} “{s.snippet}”" for s in item.suggestions)
    return f"{label} → {where}"


def format_message(items: list[UncoveredTerm], *, cap: int = _TERM_CAP) -> str:
    shown = "; ".join(describe(i) for i in items[:cap])
    more = len(items) - cap
    return (
        f"{len(items)} term(s) the claim names appear in no grounding "
        f"passage: {shown}{f'; +{more} more' if more > 0 else ''}. Warning "
        "only — attach a passage that carries them, or reword the claim."
    )


def coverage_warning(
    store: Store,
    hub_ref_id: int,
    sentence: str,
    grounding: dict[str, Any],
    *,
    bundle: evidence.HubBundle | None = None,
) -> str | None:
    """The non-blocking message for :func:`term_coverage`'s findings, or
    ``None`` when every term is covered (or there is nothing to compare)."""
    items = term_coverage(store, hub_ref_id, sentence, grounding, bundle=bundle)
    return format_message(items) if items else None


# ------------------------------------------------------------ prefill order


def names_method(sentence: str) -> bool:
    """True when the claim names a method/measurement term — an acronym or
    a mode word (:func:`~precis.taproot.coverage.claim_terms`). With the
    depth policy (:func:`~precis.workers.hub_refine.claim_depth_policy`)
    this decides whether the approve prefill ranks body passages ahead of
    the abstract."""
    return any(
        t.kind in (KIND_ACRONYM, KIND_MODE) for t in coverage.claim_terms(sentence)
    )


def rank_for_claim(
    sentence: str,
    chunks: list[PaperChunk],
    abstracts: dict[int, str],
    *,
    grounding_refs: set[int] | None = None,
    sizes: dict[tuple[int, tuple[str, ...]], int] | None = None,
) -> list[int]:
    """The DB-free core of :func:`order_for_prefill`: indices into ``chunks``
    in prefill order — :func:`chunk_tier` first (captions, methods/results,
    other body, abstract/front matter last), then more of the claim's
    :func:`~precis.taproot.coverage.claim_terms` carried by the chunk, then
    chunks of ``grounding_refs`` (the grounding's own papers) before the
    rest, then the incoming order. Every index appears exactly once.
    ``sizes`` (:func:`heading_sizes`) defaults to counting ``chunks``;
    pass the papers' full counts when ``chunks`` is a subset."""
    terms = coverage.claim_terms(sentence)
    amap = coverage.acronym_map(list(abstracts.values()) + [c.text for c in chunks])
    sizes = heading_sizes(chunks) if sizes is None else sizes
    grounding = grounding_refs or set()
    keyed: list[tuple[int, int, int, int]] = []
    for i, c in enumerate(chunks):
        prep = coverage.prepare(c.text)
        carried = sum(1 for t in terms if coverage.find_term(t, prep, amap) is not None)
        tier = chunk_tier(
            kind=c.kind,
            section_path=c.section_path,
            ord_=c.ord,
            text=c.text,
            abstract=abstracts.get(c.ref_id),
            heading_chunks=sizes.get((c.ref_id, tuple(c.section_path)), 0),
        )
        keyed.append((tier, -carried, 0 if c.ref_id in grounding else 1, i))
    return [i for _t, _n, _g, i in sorted(keyed)]


def order_for_prefill(
    store: Store, chunks: list[evidence.ChunkInfo], sentence: str
) -> list[evidence.ChunkInfo]:
    """``chunks`` (a hub's grounding-candidate chunks) reordered for the
    approve prefill by :func:`rank_for_claim`. Only reorders — nothing is
    dropped, so the reviewer still chooses."""
    if len(chunks) < 2:
        return list(chunks)
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT c.chunk_id, c.chunk_kind, r.meta->>'abstract'
              FROM chunks c JOIN refs r ON r.ref_id = c.ref_id
             WHERE c.chunk_id = ANY(%s)
            """,
            ([c.chunk_id for c in chunks],),
        ).fetchall()
        size_rows = conn.execute(
            """
            SELECT ref_id, section_path, count(*) FROM chunks
             WHERE ref_id = ANY(%s) AND ord >= 0 AND retired_at IS NULL
               AND chunk_kind <> 'references'
             GROUP BY ref_id, section_path
            """,
            (sorted({c.ref_id for c in chunks}),),
        ).fetchall()
    sizes = {(int(r[0]), tuple(r[1] or ())): int(r[2]) for r in size_rows}
    kind_of = {int(r[0]): str(r[1] or "") for r in rows}
    abstract_of = {int(r[0]): str(r[2]) for r in rows if r[2]}
    abstracts = {
        c.ref_id: abstract_of[c.chunk_id] for c in chunks if c.chunk_id in abstract_of
    }
    paper = [
        PaperChunk(
            chunk_id=c.chunk_id,
            ref_id=c.ref_id,
            handle=f"chunk:{c.chunk_id}",
            ord=c.ord,
            kind=kind_of.get(c.chunk_id, ""),
            section_path=c.section_path,
            text=c.text,
        )
        for c in chunks
    ]
    return [chunks[i] for i in rank_for_claim(sentence, paper, abstracts, sizes=sizes)]
