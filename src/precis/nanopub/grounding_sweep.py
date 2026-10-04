"""Read-only sweep of frozen groundings (signed-grounding recurrence, D3).

G2 (:mod:`precis.nanopub.term_coverage`) and G3 (:mod:`.pin_lint`) warn
until something measures their false-alarm rate. This sweep is that
measurement, and it is also the list of hubs a human might re-ground:
every publish row in reviewed/signed/anchored/published that carries
``grounding.passages``, checked for

* **Shallow grounding** — the claim needs a body passage (the depth policy,
  :func:`precis.workers.hub_refine.claim_depth_policy`, says body-required,
  or it names a method/measurement, :func:`.term_coverage.names_method`) but
  *every* grounding passage is front matter (G2's tier 3: abstract,
  author block, first two chunks) or a definition sentence.
* **Method coverage gaps** — G2's uncovered terms, each with its top
  suggestions (:func:`.term_coverage.analyse`).
* **Better passages** — for a flagged hub, the best non-grounding chunks of
  its papers that carry at least one claim term, in D2's order
  (:func:`.term_coverage.rank_for_claim`): captions, methods/results, body,
  front matter last. These are the supersede candidates. Nothing is
  changed; the sweep never writes.

The summary (counts by state, shallow count, gap count by term kind, rates
per 100 hubs) is the number that decides whether G2/G3 become blocking.

Two narrow rules, both deliberately conservative (a miss costs a missed
warning, a false hit costs a reviewer's minute):

* **Definition sentence** (:func:`is_definition`) — a passage of at most
  :data:`DEFINITION_MAX_CHARS` characters that matches one of
  :data:`_DEFINITION_CUES`: "is/are defined|described|known|referred to|
  termed|called as", "refers to", "is a/an … that|which|where", "we
  call|term|define", "termed", "so-called", "the term … is used". A long
  passage that merely contains such a clause is a body passage.
* **Short letter** (:func:`is_short_letter`) — a paper with at most
  :data:`LETTER_MAX_CHUNKS` live body chunks and no heading that names
  methods, experiments or results. There the opening paragraph *is* the
  body (Kroto 1985, Iijima 1991 have no sections at all, only a title or
  author line), so the position rules of :func:`.term_coverage.chunk_tier`
  (the first two chunks; containment in the stored abstract) do not make a
  passage front matter; a section heading that says abstract/summary still
  does.

A paper whose reader filed every chunk under ``ABSTRACT`` (ref 3755) is
handled by :func:`.term_coverage.chunk_tier` itself
(:data:`.term_coverage.FRONT_SECTION_MAX_CHUNKS`), for D2 and G2 as well.

Bibliography items that were not tagged ``references`` ("- (19) Meng, T.
Z.; …") are dropped before term and passage matching (:func:`is_bib_item`):
they carry every method acronym of the papers they cite.

:func:`assess` is the DB-free core (tested on built chunks, and what the
prod measurement runs on a read-only snapshot); :func:`sweep` loads rows.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from precis.nanopub import evidence, term_coverage
from precis.nanopub.gates import integral_chunk_id
from precis.nanopub.term_coverage import (
    TIER_BODY,
    TIER_CAPTION,
    TIER_FRONT,
    TIER_METHODS,
    PaperChunk,
    UncoveredTerm,
)
from precis.taproot import coverage
from precis.taproot.coverage import KINDS
from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: Publish states whose grounding is frozen (:func:`precis.nanopub.state.
#: frozen_rung`); the same tuple :mod:`.pin_lint` uses.
FROZEN_STATES = ("reviewed", "signed", "anchored", "published")

#: A paper this short with no methods/results heading is a letter.
LETTER_MAX_CHUNKS = 45

#: A passage longer than this is body text, not a definition sentence.
DEFINITION_MAX_CHARS = 320

#: Non-grounding chunks listed per flagged hub.
BETTER_PER_HUB = 3

_TIER_NAMES = {
    TIER_CAPTION: "caption",
    TIER_METHODS: "methods",
    TIER_BODY: "body",
    TIER_FRONT: "front",
}

_WS_RE = re.compile(r"\s+")
_LETTER_BODY_HEADING_RE = re.compile(
    r"\b(?:methods?|experimental|experiments?|procedures?|results?|"
    r"computational|discussion)\b",
    re.IGNORECASE,
)
_BIB_ITEM_RE = re.compile(
    r"^\s*(?:-\s*)?(?:<span[^>]*>\s*</span>\s*)?(?:\[\d+\]|\(\d+\))\s+[A-Z]"
)
_DEFINITION_CUES = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(?:is|are) (?:defined|described|known|referred to|termed|called) as\b",
        r"\brefers? to\b",
        r"\b(?:is|are) an? \b[^.;]{0,80}\b(?:that|which|where)\b",
        r"\bwe (?:call|term|define|refer to)\b",
        r"\btermed\b|\bso-called\b",
        r"\bthe terms? [^.;]{0,60}\b(?:is|are) used\b",
    )
)


# ------------------------------------------------------------- the rules


def is_definition(text: str) -> bool:
    """True for a short passage that defines or names a thing — the
    abstract-grade support a measurement claim must not rest on alone."""
    flat = _WS_RE.sub(" ", text).strip()
    return len(flat) <= DEFINITION_MAX_CHARS and any(
        cue.search(flat) for cue in _DEFINITION_CUES
    )


def is_short_letter(chunks: list[PaperChunk]) -> bool:
    """True when ``chunks`` (one paper's live body chunks) look like a
    short letter: at most :data:`LETTER_MAX_CHUNKS` of them and no section
    heading that names methods, experiments or results."""
    if not chunks or len(chunks) > LETTER_MAX_CHUNKS:
        return False
    return not any(
        _LETTER_BODY_HEADING_RE.search(part) for c in chunks for part in c.section_path
    )


def is_bib_item(chunk: PaperChunk) -> bool:
    """A bibliography list item the reader did not tag ``references``."""
    return _BIB_ITEM_RE.match(chunk.text) is not None


def passage_tier(
    chunk: PaperChunk,
    abstract: str | None,
    *,
    letter: bool = False,
    heading_chunks: int = 0,
) -> int:
    """:func:`.term_coverage.chunk_tier` of a grounding passage's chunk. In a
    short ``letter`` the position rules are switched off (ord 0, the title
    block, stays front)."""
    return term_coverage.chunk_tier(
        kind=chunk.kind,
        section_path=chunk.section_path,
        ord_=(99 if chunk.ord >= 1 else 0) if letter else chunk.ord,
        text=chunk.text,
        abstract=None if letter else abstract,
        heading_chunks=heading_chunks,
    )


# --------------------------------------------------------------- records


@dataclass(frozen=True, slots=True)
class GroundingPassage:
    """One passage of the frozen grounding: what the reviewer attested
    (``text``) and the chunk it points at (``None`` when that is gone)."""

    text: str
    chunk: PaperChunk | None = None


@dataclass(frozen=True, slots=True)
class HubInput:
    """Everything :func:`assess` reads for one hub. ``chunks`` are the live
    body chunks of the grounding papers and the hub's other evidence papers."""

    hub_ref_id: int
    state: str
    sentence: str
    passages: list[GroundingPassage]
    chunks: list[PaperChunk]
    abstracts: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PassageVerdict:
    handle: str
    tier: str
    #: ``"front"`` | ``"definition"`` | ``None`` (a body passage).
    shallow: str | None
    letter: bool
    snippet: str


@dataclass(frozen=True, slots=True)
class SuggestedPassage:
    handle: str
    tier: str
    snippet: str


@dataclass(frozen=True, slots=True)
class GapReport:
    kind: str
    term: str
    expansion: str | None
    suggestions: tuple[SuggestedPassage, ...]


@dataclass(frozen=True, slots=True)
class HubReport:
    hub_ref_id: int
    state: str
    sentence: str
    needs_body: bool
    passages: tuple[PassageVerdict, ...]
    shallow: bool
    gaps: tuple[GapReport, ...]
    better: tuple[SuggestedPassage, ...]

    @property
    def flagged(self) -> bool:
        return self.shallow or bool(self.gaps)


def _handle(hub_ref_id: int) -> str:
    return handle_registry.try_format("finding", hub_ref_id) or f"fi{hub_ref_id}"


def _cap(text: str, n: int = 120) -> str:
    flat = _WS_RE.sub(" ", text).strip()
    return flat[:n] + ("…" if len(flat) > n else "")


# ------------------------------------------------------------- the core


def needs_body(sentence: str) -> bool:
    """The depth policy says body-required, or the claim names a method."""
    from precis.workers.hub_refine import DEPTH_BODY_REQUIRED, claim_depth_policy

    return claim_depth_policy(sentence) == DEPTH_BODY_REQUIRED or (
        term_coverage.names_method(sentence)
    )


def better_passages(
    sentence: str,
    gap_terms: list[str],
    chunks: list[PaperChunk],
    abstracts: dict[int, str],
    grounding_chunk_ids: set[int],
    grounding_refs: set[int],
    sizes: dict[tuple[int, tuple[str, ...]], int],
    tier_of: Callable[[PaperChunk], int],
    *,
    limit: int = BETTER_PER_HUB,
) -> list[SuggestedPassage]:
    """The best non-grounding chunks: those carrying at least one claim term
    (every chunk when the claim names none), in D2's order
    (:func:`.term_coverage.rank_for_claim`, grounding papers first among
    equals), then regrouped by ``tier_of`` (``chunk_tier`` with the
    short-letter correction); ``sizes`` are the papers' heading counts. ``gap_terms`` (the uncovered terms' text)
    are appended to the ranked sentence: a claim that spells out
    "transmission electron microscopy" is carried by the caption that says
    "TEM", which :func:`~precis.taproot.coverage.claim_terms` of the bare
    sentence does not know."""
    ranked_on = " ".join([sentence, *gap_terms])
    pool = [c for c in chunks if c.chunk_id not in grounding_chunk_ids]
    terms = coverage.claim_terms(ranked_on)
    if terms:
        amap = coverage.acronym_map(list(abstracts.values()) + [c.text for c in chunks])
        pool = [
            c
            for c in pool
            if any(
                coverage.find_term(t, coverage.prepare(c.text), amap) is not None
                for t in terms
            )
        ]
    order = term_coverage.rank_for_claim(
        ranked_on, pool, abstracts, grounding_refs=grounding_refs, sizes=sizes
    )
    ranked = [pool[i] for i in order]
    keyed = sorted(((tier_of(c), c) for c in ranked), key=lambda k: k[0])
    return [
        SuggestedPassage(c.handle, _TIER_NAMES[t], _cap(c.text))
        for t, c in keyed[:limit]
    ]


def assess(inp: HubInput) -> HubReport:
    """Judge one hub's frozen grounding — pure, no DB."""
    by_ref: dict[int, list[PaperChunk]] = {}
    for c in inp.chunks:
        by_ref.setdefault(c.ref_id, []).append(c)
    letters = {r: is_short_letter(cs) for r, cs in by_ref.items()}
    chunks = [c for c in inp.chunks if not is_bib_item(c)]
    sizes = term_coverage.heading_sizes(chunks)

    def tier_of(c: PaperChunk) -> int:
        return passage_tier(
            c,
            inp.abstracts.get(c.ref_id),
            letter=letters.get(c.ref_id, False),
            heading_chunks=sizes.get((c.ref_id, tuple(c.section_path)), 0),
        )

    verdicts: list[PassageVerdict] = []
    for p in inp.passages:
        letter = False
        tier = TIER_BODY
        if p.chunk is not None:
            letter = letters.get(p.chunk.ref_id, False)
            tier = tier_of(p.chunk)
        shallow = (
            "front"
            if tier == TIER_FRONT
            else "definition"
            if is_definition(p.text)
            else None
        )
        verdicts.append(
            PassageVerdict(
                handle=p.chunk.handle if p.chunk is not None else "(chunk gone)",
                tier=_TIER_NAMES[tier],
                shallow=shallow,
                letter=letter,
                snippet=_cap(p.text),
            )
        )

    body = needs_body(inp.sentence)
    shallow_hub = bool(
        body and verdicts and all(v.shallow is not None for v in verdicts)
    )

    grounding_ids = {p.chunk.chunk_id for p in inp.passages if p.chunk is not None}
    grounding_refs = {p.chunk.ref_id for p in inp.passages if p.chunk is not None}
    uncovered: list[UncoveredTerm] = term_coverage.analyse(
        inp.sentence,
        [p.text for p in inp.passages if p.text],
        chunks,
        inp.abstracts,
        grounding_refs,
    )
    gaps = tuple(
        GapReport(
            kind=u.term.kind,
            term=u.term.text,
            expansion=u.term.expansion,
            suggestions=tuple(
                SuggestedPassage(s.chunk_handle, _TIER_NAMES[s.tier], s.snippet)
                for s in u.suggestions
            ),
        )
        for u in uncovered
    )
    better: list[SuggestedPassage] = []
    if shallow_hub or gaps:
        better = better_passages(
            inp.sentence,
            [g.term for g in gaps],
            chunks,
            inp.abstracts,
            grounding_ids,
            grounding_refs,
            sizes,
            tier_of,
        )
    return HubReport(
        hub_ref_id=inp.hub_ref_id,
        state=inp.state,
        sentence=inp.sentence,
        needs_body=body,
        passages=tuple(verdicts),
        shallow=shallow_hub,
        gaps=gaps,
        better=tuple(better),
    )


# ---------------------------------------------------------------- summary


def summarise(reports: list[HubReport]) -> dict[str, Any]:
    """Counts by state, shallow count, gap hubs and gap terms by kind, and
    each as a rate per 100 hubs."""
    n = len(reports)
    by_state: dict[str, int] = {}
    for r in reports:
        by_state[r.state] = by_state.get(r.state, 0) + 1
    shallow = sum(1 for r in reports if r.shallow)
    gap_hubs = sum(1 for r in reports if r.gaps)
    gaps_by_kind = {
        k: sum(1 for r in reports for g in r.gaps if g.kind == k) for k in KINDS
    }
    hubs_by_kind = {
        k: sum(1 for r in reports if any(g.kind == k for g in r.gaps)) for k in KINDS
    }

    def per100(x: int) -> float:
        return round(100.0 * x / n, 1) if n else 0.0

    return {
        "hubs": n,
        "by_state": dict(sorted(by_state.items())),
        "needs_body": sum(1 for r in reports if r.needs_body),
        "shallow": shallow,
        "gap_hubs": gap_hubs,
        "gap_terms_by_kind": gaps_by_kind,
        "gap_hubs_by_kind": hubs_by_kind,
        "flagged": sum(1 for r in reports if r.flagged),
        "per_100_hubs": {
            "shallow": per100(shallow),
            "gap_hubs": per100(gap_hubs),
            "gap_terms": per100(sum(gaps_by_kind.values())),
            "gap_hubs_by_kind": {k: per100(v) for k, v in hubs_by_kind.items()},
            "flagged": per100(sum(1 for r in reports if r.flagged)),
        },
    }


def to_dict(reports: list[HubReport]) -> dict[str, Any]:
    """The JSON shape: ``{"summary": {...}, "hubs": [...]}``, one entry per
    hub (flagged or not), in hub order."""
    hubs = []
    for r in sorted(reports, key=lambda r: r.hub_ref_id):
        hubs.append(
            {
                "hub": _handle(r.hub_ref_id),
                "state": r.state,
                "sentence": r.sentence,
                "needs_body": r.needs_body,
                "flagged": r.flagged,
                "shallow": r.shallow,
                "passages": [
                    {
                        "chunk": v.handle,
                        "tier": v.tier,
                        "shallow": v.shallow,
                        "short_letter": v.letter,
                        "text": v.snippet,
                    }
                    for v in r.passages
                ],
                "gaps": [
                    {
                        "kind": g.kind,
                        "term": g.term,
                        "expansion": g.expansion,
                        "suggestions": [
                            {"chunk": s.handle, "tier": s.tier, "text": s.snippet}
                            for s in g.suggestions
                        ],
                    }
                    for g in r.gaps
                ],
                "better": [
                    {"chunk": b.handle, "tier": b.tier, "text": b.snippet}
                    for b in r.better
                ],
            }
        )
    return {"summary": summarise(reports), "hubs": hubs}


def render_md(reports: list[HubReport]) -> str:
    """The human report: the summary block, then every flagged hub."""
    s = summarise(reports)
    p = s["per_100_hubs"]
    lines = [
        "# Frozen-grounding sweep",
        "",
        f"{s['hubs']} hubs ("
        + ", ".join(f"{k} {v}" for k, v in s["by_state"].items())
        + f"); {s['needs_body']} need a body passage.",
        f"- shallow grounding: {s['shallow']} ({p['shallow']} per 100 hubs)",
        f"- hubs with a method coverage gap: {s['gap_hubs']} "
        f"({p['gap_hubs']} per 100); terms by kind: "
        + ", ".join(f"{k} {v}" for k, v in s["gap_terms_by_kind"].items()),
        f"- flagged (shallow or gap): {s['flagged']} ({p['flagged']} per 100)",
        "",
    ]
    for r in sorted(reports, key=lambda r: r.hub_ref_id):
        if not r.flagged:
            continue
        marks = (["shallow"] if r.shallow else []) + (["gap"] if r.gaps else [])
        lines.append(f"## {_handle(r.hub_ref_id)} [{r.state}] {', '.join(marks)}")
        lines.append(f"> {_cap(r.sentence, 240)}")
        for v in r.passages:
            tag = v.shallow or "body"
            lines.append(
                f"- grounding {v.handle} ({v.tier}"
                f"{', short letter' if v.letter else ''}; {tag}): {v.snippet}"
            )
        for g in r.gaps:
            exp = f" ({g.expansion})" if g.expansion else ""
            where = (
                "; ".join(f"{s.handle} [{s.tier}] {s.snippet}" for s in g.suggestions)
                or "not found in the evidence papers"
            )
            lines.append(f"- gap {g.kind} {g.term}{exp} -> {where}")
        for b in r.better:
            lines.append(f"- better {b.handle} [{b.tier}]: {b.snippet}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- DB load


def _passage_chunks(store: Store, chunk_ids: list[int]) -> dict[int, PaperChunk]:
    """The chunks the passages point at, retired or not (a frozen passage
    keeps its meaning even if the paper was since re-chunked)."""
    if not chunk_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT c.chunk_id, c.ref_id, r.kind, c.ord, c.chunk_kind,
                   c.section_path, c.text
              FROM chunks c JOIN refs r ON r.ref_id = c.ref_id
             WHERE c.chunk_id = ANY(%s)
            """,
            (chunk_ids,),
        ).fetchall()
    out: dict[int, PaperChunk] = {}
    for r in rows:
        out[int(r[0])] = PaperChunk(
            chunk_id=int(r[0]),
            ref_id=int(r[1]),
            handle=handle_registry.try_format(str(r[2]), int(r[0]), chunk=True)
            or f"chunk:{r[0]}",
            ord=int(r[3]),
            kind=str(r[4] or ""),
            section_path=list(r[5] or []),
            text=str(r[6] or ""),
        )
    return out


def hub_input(store: Store, row: Any, sentence: str) -> HubInput:
    """Load one frozen publish ``row`` into a :class:`HubInput`."""
    raw = list((row.grounding or {}).get("passages") or [])
    ids = [
        cid for p in raw if (cid := integral_chunk_id(p.get("chunk_id"))) is not None
    ]
    pinned = _passage_chunks(store, ids)
    passages: list[GroundingPassage] = []
    for p in raw:
        cid = integral_chunk_id(p.get("chunk_id"))
        chunk = pinned.get(cid) if cid is not None else None
        text = str(p.get("quote") or "").strip() or (chunk.text if chunk else "")
        passages.append(GroundingPassage(text=text, chunk=chunk))
    grounding_refs = sorted({c.ref_id for c in pinned.values()})
    try:
        bundle = evidence.load_bundle(store, row.claim_ref_id)
        other = [s.ref_id for s in bundle.sources if s.ref_id not in grounding_refs]
    except Exception:  # a hub that no longer resolves is still swept
        log.warning("sweep: no bundle for hub %s", row.claim_ref_id, exc_info=True)
        other = []
    chunks, abstracts = term_coverage.paper_chunks(store, grounding_refs + other)
    return HubInput(
        hub_ref_id=row.claim_ref_id,
        state=row.state,
        sentence=sentence,
        passages=passages,
        chunks=chunks,
        abstracts=abstracts,
    )


def sweep(store: Store, states: tuple[str, ...] = FROZEN_STATES) -> list[HubReport]:
    """Assess every frozen grounding in ``states``. Pure read."""
    rows = [
        r
        for state in states
        for r in store.nanopub_rows_in_state(state, limit=100_000)
        if (r.grounding or {}).get("passages")
    ]
    refs = store.fetch_refs_by_ids([r.claim_ref_id for r in rows])
    reports: list[HubReport] = []
    for r in rows:
        ref = refs.get(r.claim_ref_id)
        sentence = r.approved_title or (ref.title if ref is not None else "") or ""
        reports.append(assess(hub_input(store, r, sentence)))
    return reports
