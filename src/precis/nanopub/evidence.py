"""Read layer: everything the assembler and the mint gates need to know
about one claim hub, resolved once into a :class:`HubBundle`.

Two shapes of evidence edge exist in prod and BOTH are read (the
2026-08-15 dry-run-49 census: 10 hubs carry inbound ``corroborates``
paper→hub edges, 37 carry outbound hub→paper ``derived-from`` — a reader
of only the inbound shape silently sees "no evidence" on three quarters
of the graph):

* **inbound** paper→hub ``establishes``/``corroborates``/``contradicts``
  — the taproot evidence edges, read via
  :func:`precis.taproot.seniority.derive_evidence` (which also resolves
  per-edge grounding-chunk pointers);
* **outbound** hub→paper/patent ``derived-from`` — the chase lineage
  shape (``handlers/finding.py``'s begat chain). No per-passage pointer;
  contributes a paper-level evidence row only.

Grounding resolution stays **internal-coordinate** here (chunk ids,
ords): the publish boundary strips them — only DOI + ``pdf_sha256`` +
quote + snip enter a published graph (universal anchors rule).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from precis.taproot import seniority

if TYPE_CHECKING:
    from precis.store import Store

_PC_HANDLE = re.compile(r"^pc(\d+)$")

#: The acquisition-mode edge ``put(kind='finding', wants=...)`` writes:
#: ``finding --awaits-evidence--> DREAM:acquire stub`` (migration 0105,
#: no inverse). Deliberately NOT an evidence relation — the stub supports
#: nothing yet — so :func:`precis.taproot.seniority.derive_evidence` never
#: sees it and the hub's ``sources`` stay clean; it reaches the bundle as
#: :attr:`HubBundle.awaiting_sources` instead.
_AWAITS_EVIDENCE = "awaits-evidence"
_STATUS_NAMESPACE = "STATUS"
_STATUS_ACQUIRING = "acquiring"
_EVIDENCE_KINDS = ("paper", "patent")

#: ``refs.meta`` key declaring "this hub's primary source is not in the
#: corpus" for the shapes no edge can express: a primary known only by
#: DOI/title that never got a ``refs`` row, a hub whose only evidence edge
#: points at the *citing* paper (which we do hold), or a hub with no
#: evidence edge at all. Written by ``precis nanopub backfill-unheld``
#: (:func:`prose_marked_hubs` / :func:`declare_primary_source_unheld` — the
#: six legacy prose-marked hubs) — the live door for a new claim of this shape
#: is ``wants=``, which records the state as structure
#: (:data:`_AWAITS_EVIDENCE`) and needs no flag. ``refs.meta`` and not the
#: body: a reword rewrites ``finding_body``, and that is exactly the bug
#: this key exists to close.
PRIMARY_UNHELD_META_KEY = "primary_source_unheld"

#: Chunk `section_path` patterns that mark secondhand grounding — a
#: references list, related-work / prior-art survey, or background recap
#: cites the doers instead of being them. Layer-A hearsay gate
#: (resolved 2026-08-15: primary-source citations only).
HEARSAY_SECTION = re.compile(
    r"reference|bibliograph|related.work|prior.art|background|state.of.the.art",
    re.IGNORECASE,
)

#: Source-TITLE patterns that mark a review/perspective by genre — a paper
#: we may hold in full, but whose own prose attributes its findings to the
#: doers it surveys, so a quote from it is secondhand even from a results
#: section. This is the arm the four structural acquisition arms cannot
#: express: ``refs_without_body_chunks`` fires only when we do NOT hold the
#: text. (2026-08-27 audit:
#: ~39/1,490 hubs ground on review-titled papers; the title heuristic
#: eyeballed ~97% accurate on this corpus — a "Recent advances in X: A
#: review" is a review, and the rare primary paper with "review" in its
#: title over-blocks into a human decision, which is the safe direction.)
REVIEW_TITLE = re.compile(
    r"\b(?:mini-?|systematic )?reviews?\b"
    r"|\bperspectives?\b"
    r"|\broadmap\b"
    r"|state.of.the.art"
    r"|(?:recent\s+)?(?:advances|progress|developments)\s+(?:in|of|on)\s",
    re.IGNORECASE,
)

#: Claim-sentence patterns declaring a SYNTHESIS mode — for such a claim a
#: review is not hearsay but the primary source itself ("Review synthesis
#: identifies … as the degradation pathways"), so the review-title arm
#: stays quiet. Anything else grounded on a review-titled source blocks.
SYNTHESIS_MODE = re.compile(
    r"review synthesis|meta-analysis|systematic review|umbrella review"
    r"|survey of \d|synthesis of \d+\s+(?:studies|papers|reports)",
    re.IGNORECASE,
)


def is_review_title(title: str) -> bool:
    """True iff a source's title marks it a review/perspective by genre
    (:data:`REVIEW_TITLE`)."""
    return bool(REVIEW_TITLE.search(title or ""))


#: Harvester prose marking a claim whose primary source was never
#: ingested ("Paper not in corpus — needs acquisition."). Grounding such
#: a claim in a *citing* paper's text is hearsay whatever section the
#: chunk sits in — the intro-gap fix (fi19981/fi19987 precedent):
#: section-path matching alone misses intros, where papers cite prior
#: work most.
#:
#: **Prose is the weak arm of this check, not the primary one.** Nothing
#: in this codebase writes the marker: it is agent prose, authored in a
#: finding body during a lit-hunt/acquisition tick and carried into the
#: hub, so its wording is a convention rather than a contract and any
#: door that legitimately rewrites the body (``refine_claim_sentence``)
#: destroys it. The structural twins are what the gate leads with —
#: :func:`refs_without_body_chunks` ("we hold this source's metadata but
#: not its text"), :attr:`HubBundle.awaiting_sources` /
#: :attr:`HubBundle.acquiring` (the acquisition-mode mint's own edges and
#: status), and :data:`PRIMARY_UNHELD_META_KEY` (the declared flag for the
#: shapes no edge expresses). The prose stays a fallback only until the six
#: legacy hubs are stamped;
#: ``docs/backlog/acquisition-marker-lives-in-the-wrong-place.md`` tracks
#: retiring it entirely.
ACQUISITION_MARKER = re.compile(
    r"not (?:yet )?in (?:the )?corpus|needs? acquisition", re.IGNORECASE
)

#: In-quote citation markers — a quoted sentence that itself carries a
#: ``[12]`` / ``(Moore, 1965)`` marker is attributing its fact to
#: another work, so it cannot serve as primary grounding.
_NUMERIC_CITE = re.compile(r"\[\d{1,3}(?:\s*[,;–—-]\s*\d{1,3})*\]")
_AUTHOR_YEAR_CITE = re.compile(
    r"\((?:e\.g\.,?\s*)?[A-Z][\w'’-]+"
    r"(?:\s+(?:et al\.?|and\s+[A-Z][\w'’-]+|&\s*[A-Z][\w'’-]+))?"
    r",?\s+(?:1[789]|20)\d{2}[a-z]?\)"
)
_MILLER_INDEX = re.compile(r"^\[[0-2]{3,4}\]$")
#: Superscript citation-numeral residue that marker-ingest leaves as
#: literal HTML (pc550457's "…similar to the previous report.<sup>8</sup>",
#: found grounding pa4365, 2026-08-17). Requires a bare integer list
#: (comma/dash-separated, e.g. ``<sup>8,9</sup>`` or ``<sup>3–5</sup>``)
#: immediately inside the tag, closed by ``</sup>`` or truncated at the
#: end of the quote (an extraction trimmed right before the close) — so
#: genuine chemistry/math superscripts, which never open on a bare digit
#: run alone, are exempt: ``<sup>-1</sup>`` (negative exponent, starts
#: with ``-``) and ``<sup>3+</sup>`` (ionic charge, digit run doesn't
#: reach the close) both fail to match. Bare-integer lookalikes that DO
#: match the regex are context-filtered in :func:`citation_markers`:
#: ``10<sup>3</sup>`` (power of ten — digit before the tag),
#: ``<sup>13</sup>C`` (isotope — letter right after the close), and
#: ``m<sup>2</sup>``/``cm<sup>3</sup>`` (unit exponent — letter before
#: the tag with a lone 2/3/4 inside; a letter-preceded true citation
#: with those numbers is an acceptable recall loss, same trade as the
#: Miller-index carve-out).
_SUPERSCRIPT_CITE = re.compile(r"<sup>\s*\d+(?:\s*[,;–—-]\s*\d+)*\s*(?:</sup>|$)")
_UNIT_EXPONENTS = {"2", "3", "4"}


def _superscript_cites(text: str) -> list[str]:
    """`_SUPERSCRIPT_CITE` matches minus the scientific-notation
    lookalikes documented on the pattern."""
    out = []
    for m in _SUPERSCRIPT_CITE.finditer(text):
        prev = text[m.start() - 1] if m.start() else ""
        nxt = text[m.end()] if m.end() < len(text) else ""
        if prev.isdigit():  # 10<sup>3</sup>
            continue
        if nxt.isalpha():  # <sup>13</sup>C
            continue
        nums = re.findall(r"\d+", m.group(0))
        if prev.isalpha() and len(nums) == 1 and nums[0] in _UNIT_EXPONENTS:
            continue  # m<sup>2</sup>, cm<sup>3</sup>, Å<sup>3</sup>
        out.append(m.group(0))
    return out


def citation_markers(text: str) -> list[str]:
    """Citation markers found in ``text``: numeric brackets (``[12]``,
    ``[3,4]``, ``[1-5]``), author–year parens (``(Moore, 1965)``,
    ``(Xia et al. 2019)``), and ``<sup>N</sup>`` superscript-numeral
    residue left by marker-ingest (``<sup>8</sup>``, ``<sup>3,4</sup>``).
    Crystallographic Miller-index lookalikes (``[100]``, ``[111]``,
    ``[0001]`` — digits 0–2 only) are exempt: in a nano corpus they are
    directions, not references, and the rare citation number they shadow
    is an acceptable recall loss. Genuine superscript notation is exempt
    too (``<sup>-1</sup>`` exponents, ``<sup>3+</sup>`` charges,
    ``10<sup>3</sup>`` powers of ten, ``<sup>13</sup>C`` isotopes,
    ``m<sup>2</sup>``-style unit exponents — see
    :func:`_superscript_cites`)."""
    # Marker-extracted text escapes literal brackets (``[\[1,2\]](#page-…)``
    # markdown-link residue) — strip the escapes first or numeric markers
    # slip the net (caught live on fi19981's sim25 prefill, 2026-08-17).
    text = text.replace("\\[", "[").replace("\\]", "]")
    hits = [
        m.group(0)
        for m in _NUMERIC_CITE.finditer(text)
        if not _MILLER_INDEX.match(m.group(0))
    ]
    hits += [m.group(0) for m in _AUTHOR_YEAR_CITE.finditer(text)]
    hits += _superscript_cites(text)
    return hits


@dataclass(frozen=True, slots=True)
class ChunkInfo:
    """One body chunk's mint-relevant facts (internal coordinates)."""

    chunk_id: int
    ref_id: int
    ord: int
    text: str
    section_path: list[str]

    @property
    def is_hearsay_section(self) -> bool:
        return any(HEARSAY_SECTION.search(part) for part in self.section_path)


@dataclass(frozen=True, slots=True)
class EvidenceSource:
    """One evidence paper/patent, deduped across both edge shapes."""

    ref_id: int
    kind: str
    title: str
    year: int | None
    doi: str | None
    pdf_sha256: str | None
    #: 'establishes' | 'corroborates' | 'contradicts' | 'derived-from'
    role: str
    #: 'inbound' (paper→hub taproot edge) | 'outbound' (hub→paper lineage)
    via: str


@dataclass(frozen=True, slots=True)
class HubBundle:
    """One hub's assembled mint inputs. ``sentence`` is the LIVE
    ``finding.title`` — the frozen approved string lives on the publish
    row; the drift gate compares the two."""

    hub_ref_id: int
    sentence: str
    #: 'claim' (atomic) or 'compound' (has conjunct-of atoms).
    artifact_type: str
    sources: list[EvidenceSource]
    #: Resolved grounding chunks, one per inbound edge that pins one.
    grounding_chunks: list[ChunkInfo]
    #: Atom hub ids (non-empty iff compound), with their sentences.
    conjunct_atoms: list[tuple[int, str]] = field(default_factory=list)
    #: Evidence sources whose stored relation is a live `contradicts`.
    contradicts: list[EvidenceSource] = field(default_factory=list)
    #: The hub's ``finding_body`` chunk text ('' when absent) — the
    #: acquisition-marker gate reads it (title alone misses the
    #: harvester's "not in corpus" note).
    body: str = ""
    #: Evidence sources we do NOT hold: a ``refs`` row (title, DOI, maybe
    #: a sha) with zero live body chunks — a stub, or a paper whose fetch
    #: never landed. The acquisition gate's derived arm: this is the same
    #: state the harvester's "not in corpus" prose describes, read off the
    #: world instead of out of a rewritable text field.
    unheld_sources: list[EvidenceSource] = field(default_factory=list)
    #: ``DREAM:acquire`` stubs this hub still awaits, read off its outbound
    #: ``awaits-evidence`` edges and filtered to the ones with no live body
    #: chunk (a stub whose text landed is no longer a missing primary, so
    #: this list self-clears without a cleanup pass). The acquisition-mode
    #: mint has always written those edges; nothing read them here until
    #: 2026-08-20.
    awaiting_sources: list[EvidenceSource] = field(default_factory=list)
    #: Hub carries ``STATUS:acquiring`` — the acquisition-mode lifecycle
    #: state, live until ``chase`` grounds it (→ ``tracing``) or gives up
    #: (→ ``dead_chain``). Catches the hub whose awaited stub was since
    #: soft-deleted, where the edge resolves to nothing.
    acquiring: bool = False
    #: Hub declares :data:`PRIMARY_UNHELD_META_KEY` in ``refs.meta``.
    primary_source_unheld: bool = False


def load_bundle(store: Store, hub_ref_id: int) -> HubBundle:
    """Resolve one hub into a :class:`HubBundle`. Raises
    :class:`precis.protocol.BadInput` (via seniority) when the ref is not
    a live ``TAPROOT:claim`` finding."""
    hub_ref = store.fetch_refs_by_ids([hub_ref_id]).get(hub_ref_id)
    if hub_ref is None:
        from precis.errors import BadInput

        raise BadInput(f"no live ref {hub_ref_id}")

    evidence = seniority.derive_evidence(store, hub_ref_id)
    conjuncts = seniority.derive_conjuncts(store, hub_ref_id)
    atoms = [(c.hub_ref_id, c.sentence) for c in conjuncts.refined_by]

    inbound_ids = {
        e.paper_ref_id
        for e in (
            evidence.originators + evidence.corroborators + evidence.contradictors
        )
    }
    outbound = [
        link
        for link in store.links_for(
            hub_ref_id, direction="out", relation="derived-from"
        )
        if link.src_ref_id == hub_ref_id
    ]
    outbound_ids = {link.dst_ref_id for link in outbound}

    refs_by_id = store.fetch_refs_by_ids(inbound_ids | outbound_ids)
    # DOIs live in ref_identifiers (id_kind='doi') — refs.meta['doi'] is a
    # legacy location a handful of rows still carry, kept as fallback only.
    ids_by_ref = store.identifiers_for_refs(list(inbound_ids | outbound_ids))

    def _source(ref_id: int, role: str, via: str) -> EvidenceSource | None:
        ref = refs_by_id.get(ref_id)
        if ref is None or ref.kind not in ("paper", "patent"):
            return None
        return EvidenceSource(
            ref_id=ref.id,
            kind=ref.kind,
            title=ref.title,
            year=ref.year,
            doi=ids_by_ref.get(ref.id, {}).get("doi") or (ref.meta or {}).get("doi"),
            pdf_sha256=ref.pdf_sha256,
            role=role,
            via=via,
        )

    sources: list[EvidenceSource] = []
    contradicts: list[EvidenceSource] = []
    seen: set[tuple[int, str]] = set()
    for edge_list, role in (
        (evidence.originators, "establishes"),
        (evidence.corroborators, "corroborates"),
        (evidence.contradictors, "contradicts"),
    ):
        for edge in edge_list:
            src = _source(edge.paper_ref_id, role, "inbound")
            if src is None or (src.ref_id, role) in seen:
                continue
            seen.add((src.ref_id, role))
            (contradicts if role == "contradicts" else sources).append(src)
    for link in outbound:
        src = _source(link.dst_ref_id, "derived-from", "outbound")
        if src is None or (src.ref_id, "derived-from") in seen:
            continue
        # A paper already carrying an inbound evidence role subsumes its
        # lineage edge — don't list it twice.
        if any(s.ref_id == src.ref_id for s in sources):
            continue
        seen.add((src.ref_id, "derived-from"))
        sources.append(src)

    grounding_chunks = _resolve_grounding(store, evidence.grounding)
    # Outbound lineage anchors ground too: a ``derived-from`` edge pinning
    # ``dst_chunk_id`` names the exact passage in the primary the claim was
    # read from (fi19981's shape — the Moore paper's §IV chunk). Inbound
    # edges carry theirs via ``seniority``'s GroundingRef handles; lineage
    # edges have no GroundingRef, so fold their pins in here.
    seen_chunk_ids = {c.chunk_id for c in grounding_chunks}
    lineage_pins = [
        link.dst_chunk_id
        for link in outbound
        if link.dst_chunk_id is not None and link.dst_chunk_id not in seen_chunk_ids
    ]
    grounding_chunks += fetch_chunks(store, lineage_pins)

    unheld = refs_without_body_chunks(store, [s.ref_id for s in sources])

    return HubBundle(
        hub_ref_id=hub_ref_id,
        sentence=hub_ref.title,
        artifact_type="compound" if atoms else "claim",
        sources=sources,
        grounding_chunks=grounding_chunks,
        conjunct_atoms=atoms,
        contradicts=contradicts,
        body=hub_body(store, hub_ref_id),
        unheld_sources=[s for s in sources if s.ref_id in unheld],
        awaiting_sources=_awaiting_sources(store, hub_ref_id),
        acquiring=store.has_tag(hub_ref_id, _STATUS_NAMESPACE, _STATUS_ACQUIRING),
        primary_source_unheld=bool((hub_ref.meta or {}).get(PRIMARY_UNHELD_META_KEY)),
    )


def _awaiting_sources(store: Store, hub_ref_id: int) -> list[EvidenceSource]:
    """The acquisition-mode stubs a hub is still waiting on — outbound
    :data:`_AWAITS_EVIDENCE` targets, live, paper/patent, with no body
    chunk of their own.

    Pure plumbing: ``put(kind='finding', wants=...)`` has written this edge
    (and a ``DREAM:acquire`` stub per ``wants=`` descriptor) since
    migration 0105, and ``chase``'s acquiring arm polls it — the mint path
    simply never looked. A soft-deleted stub is dropped rather than
    counted: with nothing left to acquire the edge says nothing, and
    :attr:`HubBundle.acquiring` is the arm that still covers that hub.
    """
    links = [
        link
        for link in store.links_for(
            hub_ref_id, direction="out", relation=_AWAITS_EVIDENCE
        )
        if link.src_ref_id == hub_ref_id
    ]
    if not links:
        return []
    stubs = store.fetch_refs_by_ids(
        {link.dst_ref_id for link in links}, include_deleted=False
    )
    unheld = refs_without_body_chunks(store, list(stubs))
    ids_by_ref = store.identifiers_for_refs(list(stubs))
    return [
        EvidenceSource(
            ref_id=ref.id,
            kind=ref.kind,
            title=ref.title,
            year=ref.year,
            doi=ids_by_ref.get(ref.id, {}).get("doi") or (ref.meta or {}).get("doi"),
            pdf_sha256=ref.pdf_sha256,
            role=_AWAITS_EVIDENCE,
            via="outbound",
        )
        for ref_id, ref in sorted(stubs.items())
        if ref_id in unheld and ref.kind in _EVIDENCE_KINDS
    ]


def refs_without_body_chunks(store: Store, ref_ids: list[int]) -> set[int]:
    """Of ``ref_ids``, the ones with **no live body chunk** (``ord >= 0``
    and ``retired_at IS NULL``) — i.e. sources we know of but do not hold
    the text of: ``DREAM:acquire`` stubs, refs whose PDF fetch never
    landed, papers whose chunks were retired.

    This is the acquisition state read structurally. "Is this claim's
    primary source in the corpus?" is a fact about the world, and asking
    the world is the only phrasing a reword cannot launder — the prose
    marker lives in ``finding_body``, which the retitle door legitimately
    replaces (see :data:`ACQUISITION_MARKER`).

    ``ord < 0`` card variants are deliberately excluded: a synthesized
    summary card is not the paper's text, and a stub that gained one is
    still unheld."""
    if not ref_ids:
        return set()
    ids = list(dict.fromkeys(int(r) for r in ref_ids))
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ref_id FROM chunks "
            "WHERE ref_id = ANY(%s) AND ord >= 0 AND retired_at IS NULL",
            (ids,),
        ).fetchall()
    return set(ids) - {int(r[0]) for r in rows}


def hub_body(store: Store, hub_ref_id: int) -> str:
    """One hub's ``finding_body`` chunk text ('' when absent) —
    :attr:`HubBundle.body` on its own, for a caller that needs to read it
    at a moment other than bundle-load time.

    That caller is :func:`precis.nanopub.mint.approve`, which snapshots
    the body BEFORE a review-time reword: the reword replaces
    ``finding_body`` with the new sentence, so the acquisition-marker gate
    would otherwise lose the harvester's "not in corpus" note on the very
    call that approves the claim."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT text FROM chunks "
            "WHERE ref_id = %s AND chunk_kind = 'finding_body' "
            "ORDER BY ord LIMIT 1",
            (hub_ref_id,),
        ).fetchone()
    return str(row[0]) if row is not None else ""


def prose_marked_hubs(store: Store) -> list[tuple[int, str, str]]:
    """Every live ``finding`` whose ``finding_body`` still carries the
    legacy :data:`ACQUISITION_MARKER` prose, as ``(ref_id, title,
    matched marker)`` — the backfill's dry run, and the standing answer
    to "can the prose arm go yet?" (empty = yes).

    **Deliberately not scoped to canonical claim hubs.** This asked
    :func:`~precis.taproot.canon.claim_hub_predicate_sql` until
    2026-08-21, which made the answer wrong in the dangerous direction:
    ``mint``/``approve`` apply no such predicate, so the gate runs on any
    ``finding`` handed to them, and all six of prod's prose-marked rows
    carry ``TAPROOT:claim`` *without* ``STATUS:canonical`` — chase-tree
    findings, invisible to the strict query. Unstamped, they would have
    reported "retirable" while the prose was still the only record of
    their acquisition state. The two errors are not symmetric: a false
    positive delays retiring a paragraph, a false negative silently
    deletes a live provenance gate, so this matches the gate's reach
    rather than the corpus's tidier definition of a hub.

    The one regex serves both dialects: PostgreSQL's ARE understands
    ``(?:…)`` non-capturing groups, so :data:`ACQUISITION_MARKER`'s
    pattern goes straight into ``~*`` and there is no second copy to
    drift. Refs already carrying :data:`PRIMARY_UNHELD_META_KEY` are
    excluded — that is what makes the backfill idempotent and this list
    a shrinking work queue rather than a census."""
    params: dict[str, object] = {
        "marker": ACQUISITION_MARKER.pattern,
        "flag": PRIMARY_UNHELD_META_KEY,
    }
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title,
                   substring(c.text from %(marker)s)
              FROM refs r
              JOIN chunks c ON c.ref_id = r.ref_id
             WHERE r.kind = 'finding'
               AND r.retired_at IS NULL
               AND c.chunk_kind = 'finding_body'
               AND c.ord >= 0
               AND c.retired_at IS NULL
               AND c.text ~* %(marker)s
               AND (r.meta ->> %(flag)s)::boolean IS NOT TRUE
             ORDER BY r.ref_id
            """,
            params,
        ).fetchall()
    return [(int(r[0]), str(r[1] or ""), str(r[2] or "")) for r in rows]


def declare_primary_source_unheld(store: Store, ref_ids: list[int]) -> int:
    """Stamp :data:`PRIMARY_UNHELD_META_KEY` onto each ref; returns how
    many were written.

    The prose is deliberately left in place: ``chunks`` is append-only for
    ``ord >= 0``, so rewriting a body means DELETE + INSERT through a
    registered synthesis pass and would re-run the embedding/summary
    cascade to remove a sentence that harms nothing. Moving the *state*
    is the point, not tidying the text."""
    written = 0
    for ref_id in ref_ids:
        store.update_ref(ref_id, meta_patch={PRIMARY_UNHELD_META_KEY: True})
        written += 1
    return written


def _resolve_grounding(
    store: Store, grounding: list[seniority.GroundingRef]
) -> list[ChunkInfo]:
    """Resolve ``pc<chunk_id>`` grounding handles to chunk facts. A
    non-``pc`` handle (legacy ``slug~ord``) or a vanished chunk is
    skipped — the gates treat missing grounding as its own failure, not
    a crash."""
    chunk_ids: list[int] = []
    for ref in grounding:
        m = _PC_HANDLE.match(ref.source_handle or "")
        if m:
            chunk_ids.append(int(m.group(1)))
    if not chunk_ids:
        return []
    return fetch_chunks(store, chunk_ids)


def fetch_chunks(store: Store, chunk_ids: list[int]) -> list[ChunkInfo]:
    """Chunk facts by id, deleted chunks silently absent."""
    if not chunk_ids:
        return []
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id, ref_id, ord, text, section_path FROM chunks "
            "WHERE chunk_id = ANY(%s)",
            (chunk_ids,),
        ).fetchall()
    return [
        ChunkInfo(
            chunk_id=int(r[0]),
            ref_id=int(r[1]),
            ord=int(r[2]),
            text=str(r[3] or ""),
            section_path=list(r[4] or []),
        )
        for r in rows
    ]


def paper_body_chunks(store: Store, ref_id: int) -> list[ChunkInfo]:
    """All live body chunks (``ord >= 0``) of one paper, reading order —
    the haystack for snip unique-within-paper validation."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id, ref_id, ord, text, section_path FROM chunks "
            "WHERE ref_id = %s AND ord >= 0 ORDER BY ord",
            (ref_id,),
        ).fetchall()
    return [
        ChunkInfo(
            chunk_id=int(r[0]),
            ref_id=int(r[1]),
            ord=int(r[2]),
            text=str(r[3] or ""),
            section_path=list(r[4] or []),
        )
        for r in rows
    ]


def pdf_sha_rows(store: Store, ref_id: int) -> list[str]:
    """The sha256 candidates that could pin the quoted copy — the mint
    gate requires exactly one. ``refs.pdf_sha256`` (the held-file
    pointer) is authoritative when set: the metadata write-back at
    ingest (``_maybe_patch_pdf``) deliberately leaves TWO
    ``ref_identifiers`` rows per patched PDF — post-patch canonical +
    as-downloaded alias — so the dedup probe hits either byte sequence;
    those alias rows index re-ingests, they don't make the held copy
    ambiguous. Identifier rows are the fallback for shaless refs (zero
    of either = nothing to pin, e.g. ref 42109 —
    ``docs/backlog/pdf-sha256-identifier-hygiene.md``)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT pdf_sha256 FROM refs WHERE ref_id = %s AND pdf_sha256 IS NOT NULL",
            (ref_id,),
        ).fetchall()
        if not rows:
            rows = conn.execute(
                "SELECT DISTINCT id_value FROM ref_identifiers "
                "WHERE ref_id = %s AND id_kind = 'pdf_sha256'",
                (ref_id,),
            ).fetchall()
    return [str(r[0]) for r in rows]
