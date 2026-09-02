"""``get(kind='finding', view='nanopub')`` rendering (nanopub slice 1).

Split-module pattern of :mod:`._finding_evidence`: free functions over
the store, called from ``FindingHandler.get``.

Three renderings, by publish state:

* **signed or later** — the artifact's exact frozen TriG bytes,
  prefixed by a ``#``-comment status header (comments are lexical
  syntax outside the integrity envelope, so the body still parses and
  still hashes correctly once the header is dropped; the header says
  where the byte-exact copy lives for verification).
* **reviewed** — assembled from the frozen publish-row payload
  (approved string + validated grounding), placeholder URI.
* **no row / candidate** — a best-effort draft from live hub state:
  the claim sentence, both evidence-edge shapes, conjunct atoms.
  Grounding quotes that mint will require may be absent — the header
  says so instead of inventing them.

Pure read; doubles as the draft-export format.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis.nanopub import assemble, evidence
from precis.nanopub.aida import aida_uri, canonical_sentence
from precis.response import Response

if TYPE_CHECKING:
    from precis.store import Store
    from precis.store.types import Ref


def render_nanopub_view(store: Store, ref: Ref) -> Response:
    row = store.nanopub_publish_row(ref.id)

    if (
        row is not None
        and row.artifact_id is not None
        and row.state
        in (
            "signed",
            "anchored",
            "published",
        )
    ):
        artifact = store.nanopub_artifact(row.artifact_id)
        if artifact is not None:
            header = (
                f"# SIGNED nanopub for fi{ref.id} — state: {row.state}\n"
                f"# trusty URI: {artifact.trusty_uri}\n"
                f"# exact bytes: nanopub_artifacts id {artifact.id} "
                f"(sha256 {artifact.byte_sha256[:16]}…); this header is an\n"
                f"# unsigned comment — verify against the stored bytes, "
                f"not this rendering.\n"
            )
            return Response(body=header + artifact.trig_bytes.decode("utf-8"))

    bundle = evidence.load_bundle(store, ref.id)
    notes: list[str] = []

    if row is not None and row.state == "reviewed":
        payload = row.grounding
        assert row.approved_title is not None
        inp = _input_from_payload(row, payload, bundle)
        notes.append(f"publish state: reviewed (frozen; publish row {row.id})")
    else:
        inp = _input_from_live(store, bundle)
        state = row.state if row is not None else "no publish row"
        notes.append(f"publish state: {state} — draft from live hub state")
        missing = [g.doi for g in inp.grounding if not g.quote]
        if inp.artifact_type == "claim" and (missing or not inp.grounding):
            notes.append(
                "mint will require a re-grounded verbatim quote + unique "
                "snip per passage (no source, no atom)"
            )
    # D1 gate parity (disputes-edge split, migration 0151): blocking =
    # any live `contradicts` edge touching the hub (adjudication-derived,
    # any counterpart kind, either direction — same read as
    # gates.check_contradicts); a `disputes` edge is a non-blocking open
    # question and must never render as a block.
    contradicted = evidence.live_contradicts(store, ref.id)
    if contradicted:
        ids = ", ".join(f"{e.kind} {e.ref_id} ({e.direction})" for e in contradicted)
        notes.append(
            f"UNMINTABLE while contradicted: live contradicts edge(s) "
            f"touching {ids} — resolved only through adjudication"
        )
    disputes = evidence.open_disputes(store, ref.id)
    if disputes:
        ids = ", ".join(f"{e.kind} {e.ref_id}" for e in disputes)
        notes.append(f"open question(s), non-blocking: disputes edge(s) with {ids}")

    body = assemble.draft_trig(inp)
    note_block = "".join(f"# {line}\n" for line in notes)
    return Response(body=note_block + body)


def _input_from_payload(
    row, payload: dict, bundle: evidence.HubBundle
) -> assemble.MintInput:
    grounding = [
        assemble.GroundingInput(
            doi=str(p.get("doi") or ""),
            pdf_sha256=str(p.get("pdf_sha256") or ""),
            quote=str(p.get("quote") or ""),
            snip=str(p.get("snip") or ""),
            role=str(p.get("role") or "corroborates"),
            source_title=p.get("source_title"),
        )
        for p in payload.get("passages") or []
    ]
    conjuncts = [
        assemble.ConjunctInput(
            aida_uri=aida_uri(sentence),
            trusty_uri=None,
        )
        for _atom_id, sentence in bundle.conjunct_atoms
    ]
    return assemble.MintInput(
        artifact_type=row.artifact_type,
        sentence=canonical_sentence(row.approved_title),
        aida_uri=row.aida_uri or aida_uri(row.approved_title),
        hub_ref_id=row.claim_ref_id,
        grounding=grounding,
        fields=dict(payload.get("fields") or {}),
        conjuncts=conjuncts,
        motivation=payload.get("motivation"),
        testable_by=payload.get("testable_by"),
    )


def _input_from_live(store: Store, bundle: evidence.HubBundle) -> assemble.MintInput:
    """Draft inputs from the live hub: one grounding entry per non-
    contradicting evidence source (quote/snip left empty — the mint
    contract fills and validates them), conjuncts from stored
    ``conjunct-of`` atoms."""
    chunks_by_ref: dict[int, list[evidence.ChunkInfo]] = {}
    for chunk in bundle.grounding_chunks:
        chunks_by_ref.setdefault(chunk.ref_id, []).append(chunk)

    grounding = []
    for src in bundle.sources:
        if not src.doi:
            continue
        shas = evidence.pdf_sha_rows(store, src.ref_id)
        grounding.append(
            assemble.GroundingInput(
                doi=src.doi,
                pdf_sha256=shas[0] if len(shas) == 1 else "",
                quote="",
                snip="",
                role="establishes" if src.role == "establishes" else "corroborates",
                source_title=src.title,
            )
        )
    conjuncts = [
        assemble.ConjunctInput(aida_uri=aida_uri(sentence), trusty_uri=None)
        for _atom_id, sentence in bundle.conjunct_atoms
    ]
    return assemble.MintInput(
        artifact_type=bundle.artifact_type,
        sentence=canonical_sentence(bundle.sentence),
        aida_uri=aida_uri(bundle.sentence),
        hub_ref_id=bundle.hub_ref_id,
        grounding=grounding if bundle.artifact_type == "claim" else [],
        conjuncts=conjuncts,
    )
