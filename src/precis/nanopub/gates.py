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
from precis.taproot.notation import lint_notation
from precis.taproot.sentence_lint import lint_claim_sentence

if TYPE_CHECKING:
    from precis.store import Store


@dataclass(frozen=True, slots=True)
class GateViolation:
    """One failed gate: machine-routable ``gate`` slug + human line."""

    gate: str
    message: str


#: The Phase-1 enforcement-asymmetry split
#: (``docs/backlog/nanopub-corpus-remediation.md``): ``lint_notation`` /
#: ``lint_claim_sentence`` ADVISE everywhere (authoring, reword) and BLOCK
#: only here, at approve/sign. This frozenset is the single place that
#: decides block-vs-advise per lint code, so widening or narrowing the
#: split is a one-line, auditable edit rather than scattered call-site
#: logic — deliberately not derived from the lint modules themselves.
#:
#: BLOCKED — admissibility/grammar codes (a sentence failing these must
#: never become a published artifact) plus every notation code that is
#: *unambiguously wrong*: there is no excuse for 'kOhm' or '1e-6' surviving
#: to approve. Most are also mechanically fixable by ``normalize_notation``;
#: ``tex-residue`` is the exception — it blocks on sight, but only the
#: simple ``$_{60}$``/``$^{2}$`` forms auto-fix, and a fragment like
#: ``\mu_B$`` has no closed rewrite and waits for a human. Blocking does not
#: imply auto-fixable; it implies no correct sentence looks like this.
#:
#: NOT blocked (advisory only — judgment calls or informational, must
#: never gate a mint): ``two-denominator-solidus`` (deciding which
#: factors move under a negative exponent is judgment, not transcription),
#: ``formula-ascii-subscript`` (detector-only: ~23% of naive matches are
#: nomenclature, not stoichiometry — crown ethers ``DB18C6``, functionals
#: ``B3LYP``, ``S22``, vitamin ``B12``, point group ``C4``),
#: ``approx-spacing``, ``tilde-approximation``, every ``scope-*`` code, and
#: ``past-tense``/``present-perfect`` (2026-08-19 tense standard —
#: machine-undecidable which reading applies, per
#: ``sentence_lint``'s tense docstring; ``past-passive`` is the one tense
#: code blocked, since a bare "study happened" passive with no result
#: stated is never a claim).
_BLOCKING_LINT_CODES: frozenset[str] = frozenset(
    {
        # lint_claim_sentence — admissibility + grammar
        "not-falsifiable",
        "dangling-reference",
        "multi-assertion",
        "no-evidence-verb",
        "no-epistemic-mode",
        "over-long",
        "author-name",
        "no-terminal-period",
        "em-dash",
        "past-passive",
        # lint_notation — deterministically fixable
        "ascii-plusminus",
        "ascii-micro",
        "ascii-degrees",
        "ascii-ohm",
        "ascii-angstrom",
        "ascii-micrometre",
        "e-notation",
        "digit-grouping",
        "ascii-multiplication",
        "ascii-x-multiplier",
        "hyphen-numeric-range",
        "caret-exponent",
        "ascii-minus-exponent",
        "tex-residue",
    }
)

#: Blocking codes that do not apply to a given artifact type — a *scope*
#: table, not a loosening of ``_BLOCKING_LINT_CODES`` (a ``claim`` still
#: faces the full set, and any type absent here does too: the default is
#: strict, so a new artifact type fails closed).
#:
#: ``hypothesis`` is exempt from the epistemic pair because the pair asks
#: "how was this established?" and a hypothesis, by definition, was not.
#: No measurement exists yet: the mode lives in the artifact type plus
#: ``precis:testableBy``, which ``run_mint_gates`` already requires by
#: schema-lint — so the requirement moves to a field that can actually be
#: checked rather than being dropped. Enforcing it on the sentence is
#: worse than useless: the cheapest way to satisfy "name a technique that
#: showed this" for a conjecture is to write one that did not, which
#: manufactures exactly the false attribution the gate exists to prevent.
#: Witness: ``docs/reference/nanopub-example/qi-hypothesis-scaled-
#: switching.trig``, this repo's own reference hypothesis, fails the gate
#: today on ``no-epistemic-mode`` alone.
#:
#: ``compound`` is deliberately NOT listed. It has the same shape of
#: argument (a compound cites no paper — its trust derives worst-of-atoms,
#: and the ``compound-shape`` gate already requires the ``conjunct-of``
#: atoms that carry the modes), so exempting it from ``no-epistemic-mode``
#: is a one-line addition here. It is left strict pending a decision,
#: because unlike a hypothesis a compound *does* assert something
#: established, and the failure mode of exempting it — a conjunction that
#: names no mode at all — has not been measured against the corpus.
_ARTIFACT_LINT_EXEMPTIONS: dict[str, frozenset[str]] = {
    "hypothesis": frozenset({"no-epistemic-mode", "no-evidence-verb"}),
}


def resolve_artifact_type(bundle: ev.HubBundle, payload: dict[str, Any]) -> str:
    """Which of the three artifact types this mint is —
    ``'claim' | 'compound' | 'hypothesis'``.

    :attr:`~precis.nanopub.evidence.HubBundle.artifact_type` only ever says
    ``claim`` or ``compound``: those are derived from the hub's edges
    (``conjunct-of`` or not). ``hypothesis`` is a mint-time shape with no
    edge to derive it from, so it is known only from the payload.

    One function because three call sites need the same answer and now
    *disagreeing* would be a silent defect rather than a cosmetic one:
    :func:`run_mint_gates` scopes the blocking lint by this value, while
    :func:`precis.nanopub.mint.approve` stores it on the publish row and the
    ``mint-preflight`` view prints it. A preflight that says ``hypothesis``
    over gates run as ``claim`` is exactly the sort of lie a shared resolver
    makes impossible."""
    if payload.get("hypothesis"):
        return "hypothesis"
    return bundle.artifact_type


def run_mint_gates(
    store: Store,
    bundle: ev.HubBundle,
    payload: dict[str, Any],
    *,
    hub_meta: dict[str, Any] | None = None,
    at_sign: bool = False,
    provenance_body: str | None = None,
) -> list[GateViolation]:
    """All Layer-A gates over one hub's frozen mint ``payload`` (the
    publish row's ``grounding`` envelope: ``passages`` / ``fields`` /
    ``motivation`` / ``testable_by`` / ``hanging``). Returns every
    violation, not just the first — the review surface shows the full
    list; mint proceeds only on an empty return.

    ``at_sign`` adds the mint-order gate (#15): approval order is free
    (a compound's text can freeze before its atoms sign), only *mint*
    order is constrained — a compound signs after its atoms.

    ``provenance_body`` overrides ``bundle.body`` for the acquisition
    gate's prose arm ONLY (:func:`check_primary_source` — the one check
    that reads a *state of the world*, is this hub's primary source in
    the corpus, rather than the text being published).
    :func:`precis.nanopub.mint.approve` passes the hub's PRE-reword body
    there: a review-time reword replaces ``finding_body`` with the new
    sentence, so gating the post-reword body would let the reword launder
    the harvester's "not in corpus" note out of a claim on the very call
    that approves it. Every other gate stays on ``bundle``: the
    sentence-shaped lints must see exactly the text that gets frozen and
    signed."""
    violations: list[GateViolation] = []

    # 1 — contradicts-edge gate (first; SQL-cheap; worst-of applies).
    violations += check_contradicts(store, bundle)

    # Resolved before the sentence lint, not just before the schema arm:
    # the blocking set is scoped by artifact type
    # (`_ARTIFACT_LINT_EXEMPTIONS`).
    artifact_type = resolve_artifact_type(bundle, payload)

    # claim-sentence lint, blocking half (2026-08-19 corpus remediation,
    # Phase 1). Advisory codes are deliberately not surfaced here — see
    # check_claim_sentence's docstring.
    violations += check_claim_sentence(bundle.sentence, artifact_type=artifact_type)

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

    violations += check_primary_source(bundle, payload, provenance_body=provenance_body)

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


def check_primary_source(
    bundle: ev.HubBundle,
    payload: dict[str, Any],
    *,
    provenance_body: str | None = None,
) -> list[GateViolation]:
    """Acquisition gate (2026-08-16 intro-gap fix, derived 2026-08-20): a
    hub whose primary source is not in the corpus cannot be grounded in a
    *citing* paper's text — that is hearsay whatever section the chunk
    sits in, and section-path matching alone misses intros. Hanging mints
    (no passages) stay allowed: that IS the designed path while the paper
    hunt runs.

    Four arms, any one blocks — three structural, one prose:

    * **derived** (:attr:`~precis.nanopub.evidence.HubBundle.unheld_sources`)
      — an evidence paper/patent with no live body chunk is one we know of
      but do not hold, so a passage quoted from some *other* paper is
      secondhand by construction. Structural, so a reword cannot launder
      it and no writer has to remember to carry it forward.
    * **awaiting** (:attr:`~precis.nanopub.evidence.HubBundle.awaiting_sources`
      / :attr:`~precis.nanopub.evidence.HubBundle.acquiring`) — the
      acquisition-mode mint's own record: a ``DREAM:acquire`` stub bound by
      ``awaits-evidence``, or the ``STATUS:acquiring`` tag when the stub is
      gone. Written by ``put(kind='finding', wants=...)`` since migration
      0105; this gate is just the first reader. The derived arm misses it
      because ``awaits-evidence`` is deliberately not an evidence relation
      — the stub supports nothing yet — so the stub never enters
      ``bundle.sources``.
    * **declared** (:data:`~precis.nanopub.evidence.PRIMARY_UNHELD_META_KEY`
      on ``refs.meta``) — for the shapes no edge expresses: a primary known
      only by DOI/title that never got a ``refs`` row, a hub whose only
      evidence edge points at the *citing* paper (which we do hold), or a
      hub with no evidence edge at all. ``refs.meta`` survives a reword;
      ``finding_body`` does not.
    * **prose** — the harvester's "not in corpus" marker in the hub body or
      sentence, the pre-structural way of saying the same thing.
      ``provenance_body`` overrides
      :attr:`~precis.nanopub.evidence.HubBundle.body` for this arm alone,
      so :func:`precis.nanopub.mint.approve` can read the PRE-reword body
      (the retitle door replaces ``finding_body`` with the new sentence).

    **Retiring the prose arm** needs one thing to be true: every live
    ``finding`` whose body matches
    :data:`~precis.nanopub.evidence.ACQUISITION_MARKER` carries the declared
    flag instead. ``precis nanopub backfill-unheld`` is that migration, and
    its dry run is the test — an empty listing means the regex has no
    remaining source and can go with the drop of a paragraph, since no code
    path writes the prose. **Verified empty on prod 2026-08-21**: all six
    rows are stamped, and none is ``STATUS:canonical``, so the dry run only
    became a sound test once
    :func:`~precis.nanopub.evidence.prose_marked_hubs` stopped scoping
    itself to canonical hubs (see its docstring). The arm stays for this
    release so nothing regresses mid-deploy.

    A pure-prose hub is exactly the case ``approve`` must refuse BEFORE
    rewording — see its short-circuit; once the body is rewritten the
    marker is gone for good (``replace_body_chunk`` hard-deletes). The
    structural arms need no such care."""
    if not (payload.get("passages") or []):
        return []
    out: list[GateViolation] = []
    for src in bundle.unheld_sources:
        out.append(
            GateViolation(
                "primary-source",
                f"evidence {src.kind} ref {src.ref_id} "
                f"({src.title[:60]}…, role {src.role}) has no stored text — "
                "we hold its metadata, not the paper, so this claim's "
                "primary source is not in the corpus and any passage quoted "
                "from another work is secondhand; acquire it and re-ground, "
                "or mint explicitly hanging",
            )
        )
    for src in bundle.awaiting_sources:
        out.append(
            GateViolation(
                "primary-source",
                f"hub awaits evidence from {src.kind} ref {src.ref_id} "
                f"({src.title[:60]}…) — an acquisition-mode stub with no "
                "stored text, so the paper this claim was minted against is "
                "still not in the corpus; wait for the fetch (chase grounds "
                "the claim itself once it lands), or mint explicitly hanging",
            )
        )
    if bundle.acquiring and not bundle.awaiting_sources:
        # The stub(s) are gone (soft-deleted) but the lifecycle state says
        # the claim was never grounded — chase flips STATUS:acquiring the
        # moment it is. Named separately only when no stub survives to
        # name; otherwise the loop above already says which paper.
        out.append(
            GateViolation(
                "primary-source",
                "hub is STATUS:acquiring — the acquisition-mode lifecycle "
                "says its supporting paper never landed (chase flips this to "
                "tracing on grounding), so any passage quoted from another "
                "work is secondhand; acquire the primary, or mint explicitly "
                "hanging",
            )
        )
    if bundle.primary_source_unheld:
        out.append(
            GateViolation(
                "primary-source",
                f"hub declares meta.{ev.PRIMARY_UNHELD_META_KEY} — its "
                "primary source is recorded as not in the corpus, so any "
                "grounding passage is secondhand; acquire the primary and "
                "re-ground (clear the flag with it), or mint explicitly "
                "hanging",
            )
        )
    body = bundle.body if provenance_body is None else provenance_body
    marked = ev.ACQUISITION_MARKER.search(body) or ev.ACQUISITION_MARKER.search(
        bundle.sentence
    )
    if marked:
        out.append(
            GateViolation(
                "primary-source",
                f"hub carries a needs-acquisition marker "
                f"({marked.group(0)!r}) — its primary source is "
                "explicitly not in the corpus, so any grounding passage "
                "is secondhand; acquire the primary and re-ground, or "
                "mint explicitly hanging",
            )
        )
    return out


def check_claim_sentence(
    sentence: str, *, artifact_type: str = "claim"
) -> list[GateViolation]:
    """Claim-sentence lint's blocking half (``docs/backlog/nanopub-corpus-
    remediation.md`` Phase 1) — the notation and admissibility/grammar
    lints run everywhere as advice (``lint_notation``,
    ``lint_claim_sentence`` at authoring/reword); this is the one place
    a subset of that advice becomes a hard mint gate, per
    ``_BLOCKING_LINT_CODES``.

    Deliberately severe by design, not a bug: measured over the live
    corpus (2026-08-19, 1,524 hubs), only 21 (1.4%) lint clean, and
    ``no-epistemic-mode`` alone hits 1,419 — so this gate will block
    almost every legacy hub at approve. That is exactly the point: approve
    is where a reviewer authors the publishable sentence, and the
    remediation doc's Phase-3 cost argument is that a hub gets its grammar
    fixed *on demand* when someone actually wants to publish it, instead
    of rewriting ~1,400 sentences speculatively. Do not loosen
    ``_BLOCKING_LINT_CODES`` to make the corpus "pass" this gate — that
    defeats the remediation's whole premise.

    Advisory-only codes (``two-denominator-solidus``, ``approx-spacing``,
    ``tilde-approximation``, every ``scope-*`` code) are intentionally
    never turned into a violation here — ``run_mint_gates`` has no
    advisory channel today (its return type is "blocking or nothing"),
    and inventing one is out of scope for this gate; they stay visible
    only via the advisory lint surfaces (authoring/reword, ``precis
    taproot lint``).

    ``artifact_type`` scopes the blocking set per
    ``_ARTIFACT_LINT_EXEMPTIONS`` — a ``hypothesis`` does not face the
    epistemic pair, because it was established by nothing yet and its mode
    lives in ``precis:testableBy`` instead. The default is ``'claim'``, the
    strict set, so a caller that forgets to pass a type fails closed.

    Never raises: an empty/missing sentence returns no violations rather
    than failing the gate (a hub with a blank title has other problems a
    schema-lint-style gate should catch, not this one)."""
    out: list[GateViolation] = []
    if not sentence:
        return out
    blocking = _BLOCKING_LINT_CODES - _ARTIFACT_LINT_EXEMPTIONS.get(
        artifact_type, frozenset()
    )
    warnings = lint_notation(sentence) + lint_claim_sentence(sentence)
    seen: set[str] = set()
    for w in warnings:
        code = w.split(":", 1)[0].strip()
        if code not in blocking or code in seen:
            continue
        seen.add(code)
        out.append(
            GateViolation(
                "claim-sentence",
                f"{w} — fix the sentence before approve; this is the "
                "publishable-standard bar (docs/backlog/"
                "nanopub-corpus-remediation.md Phase 1), not a discretionary "
                "style note",
            )
        )
    return out


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
