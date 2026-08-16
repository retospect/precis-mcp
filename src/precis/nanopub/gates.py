"""Layer-A mechanical mint validators — pure code, no LLM.

The spec's gate checklist, mechanical subset (each docstring cites its
entry). Every check lives in the cheapest layer that can catch it;
failures auto-route (a hygiene defect points at its gripe/backlog item,
a grounding defect back to decomposition) rather than asking a human —
Layer C sees only survivors. Layer-B LLM verification (`qualify_claim`
one-way fit, the cross-binding prompt) is deliberately NOT here: those
are derived-queue worker jobs, and stacking more LLM review inside the
mint path was explicitly rejected.

Ordering matters only for #1: the ``contradicts`` gate is SQL-cheap and
runs first — a disputed hub is visible internally, unpublishable
externally, and spending anything further on it would be waste.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.nanopub import evidence as ev
from precis.nanopub import snip as sniplib
from precis.nanopub.vocab import QUANTITY_BOUNDS

if TYPE_CHECKING:
    from precis.store import Store


@dataclass(frozen=True, slots=True)
class GateViolation:
    """One failed gate: machine-routable ``gate`` slug + human line."""

    gate: str
    message: str


def run_mint_gates(
    store: Store,
    bundle: ev.HubBundle,
    payload: dict[str, Any],
    *,
    hub_meta: dict[str, Any] | None = None,
    at_sign: bool = False,
) -> list[GateViolation]:
    """All Layer-A gates over one hub's frozen mint ``payload`` (the
    publish row's ``grounding`` envelope: ``passages`` / ``fields`` /
    ``motivation`` / ``testable_by`` / ``hanging``). Returns every
    violation, not just the first — the review surface shows the full
    list; mint proceeds only on an empty return.

    ``at_sign`` adds the mint-order gate (#15): approval order is free
    (a compound's text can freeze before its atoms sign), only *mint*
    order is constrained — a compound signs after its atoms."""
    violations: list[GateViolation] = []

    # 1 — contradicts-edge gate (first; SQL-cheap; worst-of applies).
    violations += check_contradicts(store, bundle)

    # 2 + 16 — eligibility / rejection memo.
    if (hub_meta or {}).get("taproot_rejected"):
        violations.append(
            GateViolation(
                "rejected-memo",
                "hub carries meta.taproot_rejected — a human rejected this "
                "claim; the canonicalizer must not resurface it and mint "
                "must not publish it",
            )
        )

    artifact_type = bundle.artifact_type
    if payload.get("hypothesis"):
        artifact_type = "hypothesis"
    passages = list(payload.get("passages") or [])
    fields = dict(payload.get("fields") or {})
    hanging = bool(payload.get("hanging"))

    # 6 — schema lint: quote presence is gated by artifact type.
    if artifact_type == "hypothesis":
        if passages:
            violations.append(
                GateViolation(
                    "schema-lint",
                    "hypothesis with grounding passages — a hypothesis has "
                    "no supporting passage by definition (motivation, not "
                    "evidence); re-mint as a claim or drop the quotes",
                )
            )
        if not payload.get("testable_by"):
            violations.append(
                GateViolation(
                    "schema-lint",
                    "hypothesis without precis:testableBy — the "
                    "discriminating experiment is what separates a "
                    "conjecture from vibes",
                )
            )
        if not payload.get("motivation"):
            violations.append(
                GateViolation(
                    "schema-lint",
                    "hypothesis without precis:motivation prose naming the "
                    "inferential leap",
                )
            )
    elif artifact_type == "claim":
        if not passages and not hanging:
            violations.append(
                GateViolation(
                    "schema-lint",
                    "claim without a grounding quote — no source, no atom. "
                    "Either ground it (quote + snip validated against the "
                    "paper) or mint explicitly hanging "
                    "(provenance-unresolved; publish preflight will block "
                    "it until the original-paper hunt lands)",
                )
            )
    elif artifact_type == "compound":
        if passages:
            violations.append(
                GateViolation(
                    "schema-lint",
                    "compound with direct grounding passages — a compound "
                    "cites no paper; its trust derives worst-of-atoms",
                )
            )
        if not bundle.conjunct_atoms:
            violations.append(
                GateViolation(
                    "compound-shape",
                    "compound with no conjunct-of atoms — a compound claim "
                    "is expressed as its derivation "
                    "(atom ∧ atom → claim), never a flat sentence",
                )
            )

    # 7 (quantity bound) — review feedback 2026-08-15.
    if fields.get("quantity") and not fields.get("quantity_bound"):
        violations.append(
            GateViolation(
                "quantity-bound",
                "quantity value without bound semantics — state whether "
                f"{fields['quantity']!r} is exact, an upper bound, a lower "
                "bound, or an approx-range",
            )
        )
    if fields.get("quantity_bound") and fields["quantity_bound"] not in QUANTITY_BOUNDS:
        violations.append(
            GateViolation(
                "quantity-bound",
                f"unknown quantity_bound {fields['quantity_bound']!r} "
                f"(allowed: {', '.join(QUANTITY_BOUNDS)})",
            )
        )

    # Acquisition-marker hearsay gate (2026-08-16 intro-gap fix): a hub
    # whose own prose says the primary source was never ingested cannot
    # be grounded in a *citing* paper's text — section-path matching
    # alone misses intros. Hanging mints (no passages) stay allowed:
    # that IS the designed path while the paper hunt runs.
    if passages:
        marked = ev.ACQUISITION_MARKER.search(
            bundle.body
        ) or ev.ACQUISITION_MARKER.search(bundle.sentence)
        if marked:
            violations.append(
                GateViolation(
                    "primary-source",
                    f"hub carries a needs-acquisition marker "
                    f"({marked.group(0)!r}) — its primary source is "
                    "explicitly not in the corpus, so any grounding passage "
                    "is secondhand; acquire the primary and re-ground, or "
                    "mint explicitly hanging",
                )
            )

    # Per-passage gates (3, 4, 8, and the 2026-08-15 hearsay gate).
    for i, passage in enumerate(passages, start=1):
        violations += _check_passage(store, i, passage)

    # 5 — structured-field quote containment (fi176435 "2–30 GPa" class).
    quotes = [str(p.get("quote") or "") for p in passages]
    for key in ("material", "method", "quantity"):
        value = fields.get(key)
        if value and not any(
            sniplib.normalize_text(str(value)) in sniplib.normalize_text(q)
            for q in quotes
        ):
            violations.append(
                GateViolation(
                    "field-containment",
                    f"structured field {key}={value!r} is not contained in "
                    "any sourceQuote of this artifact — a field the quotes "
                    "don't state is an overclaim",
                )
            )

    # 15 — mint-order: a compound SIGNS only after its atoms are signed
    # (approval order stays free).
    if at_sign and artifact_type == "compound":
        violations += check_mint_order(store, bundle)

    return violations


def check_contradicts(store: Store, bundle: ev.HubBundle) -> list[GateViolation]:
    """Gate #1: a hub carrying a live unresolved ``contradicts`` edge is
    unmintable until adjudicated (source retracted, or a primary acquired
    and the claim corrected). Worst-of: one disputed member atom blocks a
    compound (fi189542 is the precedent case)."""
    out: list[GateViolation] = []
    for src in bundle.contradicts:
        out.append(
            GateViolation(
                "contradicts",
                f"live contradicts edge from {src.kind} {src.ref_id} "
                f"({src.title[:60]}…) — disputed claims are visible "
                "internally, unpublishable externally; adjudicate by "
                "artifacts (retract the source, or acquire the primary and "
                "re-mint), never by edit",
            )
        )
    for atom_id, _sentence in bundle.conjunct_atoms:
        atom_bundle = ev.load_bundle(store, atom_id)
        for src in atom_bundle.contradicts:
            out.append(
                GateViolation(
                    "contradicts",
                    f"conjunct atom fi{atom_id} carries a live contradicts "
                    f"edge from {src.kind} {src.ref_id} — worst-of blocks "
                    "the compound",
                )
            )
    return out


def _check_passage(
    store: Store, index: int, passage: dict[str, Any]
) -> list[GateViolation]:
    out: list[GateViolation] = []
    label = f"passage {index}"
    quote = str(passage.get("quote") or "")
    snip = str(passage.get("snip") or "")
    chunk_id = passage.get("chunk_id")
    doi = passage.get("doi")
    sha = passage.get("pdf_sha256")

    if not doi:
        out.append(
            GateViolation(
                "grounding",
                f"{label}: no DOI — provenance content "
                "is DOI + quote + snip (patent grounding is an "
                "open item)",
            )
        )

    chunk: ev.ChunkInfo | None = None
    if chunk_id is not None:
        chunks = ev.fetch_chunks(store, [int(chunk_id)])
        chunk = chunks[0] if chunks else None
    if chunk is None:
        out.append(
            GateViolation(
                "grounding",
                f"{label}: grounding chunk {chunk_id!r} not found — cannot "
                "validate the quote against stored text",
            )
        )
        return out

    # Hearsay gate (resolved 2026-08-15): cite the doers, not a
    # references list / related-work / background recap.
    if chunk.is_hearsay_section:
        out.append(
            GateViolation(
                "primary-source",
                f"{label}: grounded in a "
                f"{' / '.join(chunk.section_path) or 'references'} chunk — "
                "secondhand grounding is invalid even when the quote checks "
                "out; hunt the paper that DID the work (the claim may stay "
                "hanging meanwhile)",
            )
        )

    # Citation-marker hearsay gate (2026-08-16 intro-gap fix): a quote
    # that itself carries a citation marker attributes its fact to
    # another work — hearsay whatever section it sits in. If the fact is
    # this paper's own result, trim the quote to the bare assertion.
    markers = ev.citation_markers(quote)
    if markers:
        out.append(
            GateViolation(
                "primary-source",
                f"{label}: quote carries citation marker(s) "
                f"{', '.join(repr(m) for m in markers[:3])} — the quoted "
                "sentence attributes its fact to another work; if it is "
                "this paper's own result, trim the quote to the assertion "
                "itself, otherwise hunt the cited primary",
            )
        )

    # 3 — quote verbatim-containment.
    if not quote:
        out.append(GateViolation("grounding", f"{label}: empty quote"))
    elif not sniplib.contains_verbatim(quote, chunk.text):
        out.append(
            GateViolation(
                "quote-verbatim",
                f"{label}: quote is not verbatim in chunk pc{chunk.chunk_id} "
                "(compared on extraction-normalized text) — a paraphrase "
                "cannot be signed as a quotation",
            )
        )

    # 4 — snip validity + unique-within-paper.
    if not sniplib.is_valid_snip(snip):
        out.append(
            GateViolation(
                "snip",
                f"{label}: snip {snip!r} violates the contract (lowercase "
                "ASCII letters/digits/hyphens tokens, single-spaced)",
            )
        )
    else:
        haystacks = [c.text for c in ev.paper_body_chunks(store, chunk.ref_id)]
        matches = sniplib.count_matches(snip, haystacks)
        if matches != 1:
            out.append(
                GateViolation(
                    "snip",
                    f"{label}: snip matches {matches}× in the paper's stored "
                    "chunk text — must locate the passage uniquely "
                    "(no unique match → mint fails)",
                )
            )

    # 8 — ingested-chunk / pdf_sha256 gate.
    shas = ev.pdf_sha_rows(store, chunk.ref_id)
    if len(shas) != 1:
        out.append(
            GateViolation(
                "pdf-sha",
                f"{label}: ref {chunk.ref_id} has {len(shas)} pdf_sha256 "
                "rows — mint needs exactly one to pin the quoted copy "
                "(docs/backlog/pdf-sha256-identifier-hygiene.md)",
            )
        )
    elif sha and sha != shas[0]:
        out.append(
            GateViolation(
                "pdf-sha",
                f"{label}: passage pins sha {str(sha)[:12]}… but the ref's "
                f"identifier row says {shas[0][:12]}…",
            )
        )
    return out


def check_mint_order(store: Store, bundle: ev.HubBundle) -> list[GateViolation]:
    """Gate #15: a compound mints only after every conjunct atom carries
    a signed artifact (its trusty URI is what the compound's provenance
    hash-chains to)."""
    out: list[GateViolation] = []
    for atom_id, sentence in bundle.conjunct_atoms:
        row = store.nanopub_publish_row(atom_id)
        if (
            row is None
            or row.trusty_uri is None
            or row.state
            not in (
                "signed",
                "anchored",
                "published",
            )
        ):
            out.append(
                GateViolation(
                    "mint-order",
                    f"conjunct atom fi{atom_id} ({sentence[:50]}…) has no "
                    "signed artifact yet — atoms mint before compounds "
                    "(topo order)",
                )
            )
    return out


def check_drift(live_title: str, frozen_sha: str | None) -> GateViolation | None:
    """Gate #14: recompute ``claim_sha`` from the live ``finding.title``
    and compare to the publish row's frozen sha. A mismatch means the hub
    drifted from what was approved — pre-publication that is a re-review
    (reopen), post-publication the supersede trigger."""
    from precis.taproot.canon import claim_sha

    if frozen_sha is None:
        return None
    if claim_sha(live_title.strip()) == frozen_sha:
        return None
    return GateViolation(
        "drift",
        "hub title drifted from the approved string — the approved text is "
        "frozen at review; re-approve (pre-publication) or supersede "
        "(post-publication), never silently re-sign",
    )
