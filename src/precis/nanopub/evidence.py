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

#: Chunk `section_path` patterns that mark secondhand grounding — a
#: references list, related-work / prior-art survey, or background recap
#: cites the doers instead of being them. Layer-A hearsay gate
#: (resolved 2026-08-15: primary-source citations only).
HEARSAY_SECTION = re.compile(
    r"reference|bibliograph|related.work|prior.art|background|state.of.the.art",
    re.IGNORECASE,
)

#: Harvester prose marking a claim whose primary source was never
#: ingested ("Paper not in corpus — needs acquisition."). Grounding such
#: a claim in a *citing* paper's text is hearsay whatever section the
#: chunk sits in — the intro-gap fix (fi19981/fi19987 precedent):
#: section-path matching alone misses intros, where papers cite prior
#: work most.
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


def citation_markers(text: str) -> list[str]:
    """Citation markers found in ``text``: numeric brackets (``[12]``,
    ``[3,4]``, ``[1-5]``) and author–year parens (``(Moore, 1965)``,
    ``(Xia et al. 2019)``). Crystallographic Miller-index lookalikes
    (``[100]``, ``[111]``, ``[0001]`` — digits 0–2 only) are exempt:
    in a nano corpus they are directions, not references, and the rare
    citation number they shadow is an acceptable recall loss."""
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

    def _source(ref_id: int, role: str, via: str) -> EvidenceSource | None:
        ref = refs_by_id.get(ref_id)
        if ref is None or ref.kind not in ("paper", "patent"):
            return None
        return EvidenceSource(
            ref_id=ref.id,
            kind=ref.kind,
            title=ref.title,
            year=ref.year,
            doi=(ref.meta or {}).get("doi"),
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

    with store.pool.connection() as conn:
        body_row = conn.execute(
            "SELECT text FROM chunks "
            "WHERE ref_id = %s AND chunk_kind = 'finding_body' "
            "ORDER BY ord LIMIT 1",
            (hub_ref_id,),
        ).fetchone()

    return HubBundle(
        hub_ref_id=hub_ref_id,
        sentence=hub_ref.title,
        artifact_type="compound" if atoms else "claim",
        sources=sources,
        grounding_chunks=grounding_chunks,
        conjunct_atoms=atoms,
        contradicts=contradicts,
        body=str(body_row[0]) if body_row is not None else "",
    )


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
