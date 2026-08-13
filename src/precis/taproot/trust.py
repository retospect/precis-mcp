"""The single shared trust derivation for a finding-backed citation.

The acquisition-mode mint gave a claim a life *before* its evidence is verified
(``STATUS:acquiring``, and the pre-existing ``tracing``) — this module is
the read-time mapping from that machine state to the human-facing trust
labels, so a citation never quietly launders an unverified claim into
finished prose. **Read/render-time only** — no new machine STATUS, no
writes here.

Two DIFFERENT ``unacquirable_override`` shapes feed this module, and they
must not be confused:

* **Paper-level** (``refs.meta.unacquirable_override = {note, by, at}``,
  written from a paper's Meta tab, :mod:`precis_web.routes.papers`) is a
  pure *acquirability fact* — "I tried hard and could not get this; the
  metadata is correct." It never softens a claim's label. A lifecycle
  finding blocked on such a paper only gets its note enriched (the
  *note-enrichment* rule below); a clean hub whose every print-visible
  grounding paper carries one is *hardened* down to ``unverified`` (the
  *harden* rule), never assumed to fabricate a claim-backing assertion
  nobody made.
* **Claim-level** (``meta.unacquirable_override = {mode, note, by, at}``
  on the *finding itself* — ``edit(kind='finding', unacquirable_note=…)``
  or, for a hub, ``POST /claim/<head>/unacquirable``) is the only
  softener: an explicit author assertion that Ⓐ (``mode='abstract'``) the
  abstract on file backs THIS claim, or ✍ (``mode='vouched'``, also the
  legacy no-``mode`` shape) the author vouches for it. It converts an
  otherwise-**unverified** label (including one just hardened by the
  harden rule) to the calmer ``abstract``/``vouched`` state — never
  ``unsupported`` (a negative terminal verification always outranks it:
  the paper WAS read; "trust me" doesn't unread it).

:func:`claim_trust` branches hub vs. lifecycle finding exactly as
:func:`precis.taproot.cite.finding_cite_keys` does, so a caller can never
hit the wrong arm. Both the draft exporters (:mod:`precis.export.docx` /
:mod:`precis.export.latex`) and (stage b) the smartdraft editor badge
import THIS function — the label mapping lives in one place so the two
surfaces cannot drift.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from functools import reduce
from typing import Any, Literal, cast

from precis.store.protocols import ClaimTrustStore
from precis.taproot.cite import finding_cite_keys, hub_cite_keys
from precis.taproot.seniority import (
    HubEvidence,
    conjunct_atoms_bulk,
    derive_conjuncts,
    derive_evidence_bulk,
    is_claim_hub,
    is_claim_hub_bulk,
)
from precis.workers._chase_llm import is_corroborating

TrustLabel = Literal["clean", "abstract", "vouched", "unverified", "unsupported"]

#: Confidence ladder, worst (loudest) → best, for a "worst-of" reduction
#: across a block's cite heads (``smartdraft.claim_trust_for_block``) and
#: for CSS precedence. ``clean`` (verified full text) is quietest; the two
#: author-declared/abstract-only middle states sit *below* ``unverified``
#: (they're more trustworthy than "not looked yet") but are still marked;
#: ``unsupported`` (read and contradicts) is loudest.
_TRUST_SEVERITY: dict[str, int] = {
    "clean": 0,
    "abstract": 1,
    "vouched": 2,
    "unverified": 3,
    "unsupported": 4,
}


def worse_trust(a: str, b: str) -> str:
    """The louder of two trust labels (higher :data:`_TRUST_SEVERITY`)."""
    return a if _TRUST_SEVERITY.get(a, 0) >= _TRUST_SEVERITY.get(b, 0) else b


_STATUS_NAMESPACE = "STATUS"
_STATUS_ESTABLISHED = "established"
_STATUS_DEAD_CHAIN = "dead_chain"
_STATUS_MULTI_CANDIDATE = "multi_candidate"

#: A hub with no print-visible supporter — mirrors ``FindingCite.inflight``.
_HUB_UNVERIFIED_NOTE = "claim hub has no print-visible supporter yet"

#: The harden-rule note (rule 3): a clean hub whose every print-visible
#: grounding paper is declared unacquirable overstates itself, so it's
#: downgraded here — this is a fact-driven harden, not an author assertion
#: (``TrustState.overridden`` stays ``False``); a claim-level override can
#: still soften it right back down, same as any other unverified label.
_HUB_GROUNDING_UNACQUIRABLE_NOTE = "grounded only on sources declared unacquirable"

#: ``dead_chain`` reasons with a specific human note (Motivation table);
#: any other reason renders as its own reason slug.
_DEAD_CHAIN_NOTES = {
    "unacquirable": "no OA copy obtainable; hand-download queued",
}

#: The generic "hasn't reached a terminal hop yet" note — covers
#: ``acquiring``/``tracing`` and any other in-progress lifecycle state
#: (``cycle``, an unrecognized status, a missing STATUS tag).
_PENDING_NOTE = "source pending"

#: ``TrustState.status`` for a compound hub — distinct from the plain hub's
#: ``'hub'`` (docs/backlog/taproot-atomic-claims.md) so a debugging read can
#: tell "this label came from rolling up atoms" from "this label came from
#: the hub's own evidence."
_COMPOUND_STATUS = "hub-compound"

#: Truncation width for the weakest-atom title named in a compound's note —
#: mirrors :mod:`precis.utils.refeye`'s ``title[:89]`` idiom for the same
#: "advisory one-liner, not the full sentence" purpose.
_CONJUNCT_TITLE_LIMIT = 89


@dataclass(frozen=True)
class TrustState:
    """One finding's derived trust — the shared read model both export
    and the (stage b) smartdraft badge consume."""

    label: TrustLabel
    note: str | None
    #: True iff a **claim-level** ``unacquirable_override`` — the author's
    #: assertion made on the finding/hub itself (never inherited from a
    #: source paper's acquirability fact) — converted an otherwise-
    #: **unverified** label to the softer ``abstract`` (Ⓐ — the abstract
    #: backs it) or ``vouched`` (✍ — author vouches, source unobtainable).
    #: Never true for "unsupported" (a negative terminal verification
    #: always renders, override or not) or a label that was already
    #: "clean". Also never true for a paper-level-only harden (a clean hub
    #: downgraded because every grounding paper is declared unacquirable —
    #: a fact, not an assertion, so ``overridden`` stays False even though
    #: the label moved off "clean"). The override no longer folds all the
    #: way to clean: no one read the full text, so the claim keeps a
    #: (calm) mark.
    overridden: bool
    #: The raw machine status this was derived from — ``'hub'`` for a
    #: Taproot claim hub, else the STATUS tag value (or 'tracing' when
    #: absent, matching the handler's own default). Debugging aid only;
    #: never surfaced as the human-facing label.
    status: str


def _status_of(store: ClaimTrustStore, ref_id: int) -> str | None:
    """The STATUS:* tag value on ``ref_id``, or ``None`` if untagged."""
    for tag in store.tags_for(ref_id):
        if getattr(tag, "namespace", None) == "closed" and (
            getattr(tag, "prefix", None) == _STATUS_NAMESPACE
        ):
            return str(tag.value)
    return None


def _hub_trust(
    store: ClaimTrustStore,
    ref_id: int,
    *,
    evidence: HubEvidence | None = None,
    cite_key_map: dict[int, list[str]] | None = None,
) -> tuple[TrustLabel, str | None, str]:
    """A ``TAPROOT:claim`` hub's trust: empty print set (no originators AND
    no corroborators, i.e. :attr:`FindingCite.inflight`) → unverified; any
    print-visible supporter → clean. Hub "unsupported" is deferred — a
    contradictor alongside support is normal science, already surfaced on
    the claim page (``render_claim_evidence``).

    ``evidence`` — when the caller (``precis_web.claim_render``) already
    :func:`~precis.taproot.seniority.derive_evidence`'d this hub a moment
    ago, thread it through rather than re-deriving it here (the "derives
    each hub TWICE" defect, OPEN-ITEMS.md batch C). ``cite_key_map``
    likewise skips the per-supporter ``ref_cite_keys`` round trip when a
    bulk caller already resolved it."""
    if evidence is not None:
        cite_keys, _notes = hub_cite_keys(store, evidence, cite_key_map=cite_key_map)
        inflight = not cite_keys
    else:
        fc = finding_cite_keys(store, ref_id)
        inflight = fc.inflight
    if inflight:
        return "unverified", _HUB_UNVERIFIED_NOTE, "hub"
    return "clean", None, "hub"


def _truncate_conjunct_title(title: str) -> str:
    """Trim a weakest-atom title to :data:`_CONJUNCT_TITLE_LIMIT` chars for
    the compound note — same trim-and-ellipsize shape
    :mod:`precis.utils.refeye` uses for its own advisory one-liners."""
    if len(title) <= _CONJUNCT_TITLE_LIMIT:
        return title
    return title[:_CONJUNCT_TITLE_LIMIT].rstrip() + "…"


def _compound_trust(
    atoms: list[tuple[int, str, TrustState]],
) -> tuple[TrustLabel, str, str]:
    """A compound hub's rolled-up trust: worst-of its atoms' OWN trust
    states (taproot-atomic-claims.md's decomposition plan, step 4).
    ``atoms`` is ``(atom_ref_id, title, TrustState)`` triples — each state
    already a full, depth-1 :func:`claim_trust` derivation (the caller
    passed ``_expand_conjuncts=False`` deriving it, so a miswired
    compound-of-compound can't recurse through here).

    The label is ``reduce(worse_trust, ...)`` across every atom's label —
    the loudest one wins, exactly the same "worst-of" composition
    ``smartdraft.claim_trust_for_block`` already applies across a block's
    cite heads. The note names the first atom (input order — both callers
    feed atoms ref_id-ascending) whose OWN label matches that worst label,
    so a tie is broken deterministically rather than arbitrarily.

    Never called with an empty ``atoms`` list — both callers only reach
    here once the atom map (:func:`~precis.taproot.seniority.
    derive_conjuncts`/:func:`~precis.taproot.seniority.conjunct_atoms_bulk`)
    came back non-empty."""
    labels: list[str] = [state.label for _, _, state in atoms]
    worst_label = cast(TrustLabel, reduce(worse_trust, labels))
    worst_title = next(title for _, title, state in atoms if state.label == worst_label)
    note = f"weakest conjunct: {_truncate_conjunct_title(worst_title)}"
    return worst_label, note, _COMPOUND_STATUS


def _apply_claim_level_override(
    label: TrustLabel, note: str | None, status: str, meta: dict[str, Any]
) -> TrustState:
    """Rule 3 — the only softener: the finding's/hub's OWN claim-level
    ``meta.unacquirable_override`` (never inherited from a paper) converts
    an otherwise-**unverified** label to the softer ``abstract``/``vouched``.
    Factored out of :func:`claim_trust`'s tail so a compound's rolled-up
    ``(label, note)`` (:func:`_compound_trust`) composes through the exact
    same rule rather than a second copy of it — the plan's "rule-3 vouch
    softener applies AFTER the rollup" (docs/backlog/
    taproot-atomic-claims.md)."""
    if label == "unverified":
        override = meta.get("unacquirable_override")
        if isinstance(override, dict) and override:
            # Mode picks the softer state: 'abstract' → Ⓐ (the abstract on
            # file backs it), anything else (incl. a legacy override with no
            # mode) → ✍ vouched (author asserts; source unobtainable). Never
            # all the way to clean — no one read the full text.
            soft: TrustLabel = (
                "abstract" if override.get("mode") == "abstract" else "vouched"
            )
            return TrustState(
                label=soft,
                note=override.get("note") or note,
                overridden=True,
                status=status,
            )
    return TrustState(label=label, note=note, overridden=False, status=status)


def _lifecycle_trust(
    store: ClaimTrustStore, ref_id: int, meta: dict[str, Any]
) -> tuple[TrustLabel, str | None, str]:
    """A plain (non-hub) finding's trust, derived from its STATUS tag."""
    status = _status_of(store, ref_id) or "tracing"
    if status == _STATUS_ESTABLISHED:
        chain = meta.get("chain") or []
        verification = None
        if chain and isinstance(chain[-1], dict):
            verification = chain[-1].get("verification")
        # A non-dict blob (legacy shape / corrupted meta) can't establish a
        # negative verdict — treat it the same as absent: clean (the chain
        # traced to ground, today's bar). Only a real verification dict can
        # push a claim into "unsupported".
        if isinstance(verification, dict) and not is_corroborating(verification):
            note = verification.get("support_reason")
            return "unsupported", note, status
        return "clean", None, status
    if status == _STATUS_DEAD_CHAIN:
        reason = meta.get("dead_reason")
        note = _DEAD_CHAIN_NOTES.get(str(reason), str(reason) if reason else None)
        return "unverified", note, status
    if status == _STATUS_MULTI_CANDIDATE:
        return "unverified", "ambiguous citation awaiting pick", status
    # acquiring, tracing, cycle, or any other/unrecognized in-progress state.
    return "unverified", _PENDING_NOTE, status


def claim_trust(
    store: ClaimTrustStore,
    finding_ref_id: int,
    *,
    evidence: HubEvidence | None = None,
    cite_key_map: dict[int, list[str]] | None = None,
    ref: Any = None,
    paper_refs: dict[int, Any] | None = None,
    conjunct_atom_ids: list[int] | None = None,
    _expand_conjuncts: bool = True,
) -> TrustState:
    """Derive a finding's trust label — the ONE mapping every trust
    surface (export marking, the smartdraft badge) reads.

    Branches hub vs. lifecycle finding exactly as
    :func:`precis.taproot.cite.finding_cite_keys` does — with one further
    split inside the hub arm: a **compound** hub (non-empty ``conjunct-of``
    atoms, :func:`~precis.taproot.seniority.derive_conjuncts`) never derives
    its own evidence (:mod:`precis.taproot.hub`'s ``attach_evidence`` guard
    forbids a direct evidence edge onto one) — its label is instead the
    worst-of its atoms' own trust (:func:`_compound_trust`). Four rules then
    apply, in order:

    1. **Hub harden.** A clean *plain* hub (not a compound — see above)
       whose every print-visible grounding paper carries a *paper-level*
       ``unacquirable_override`` (a pure acquirability fact, set from a
       paper's Meta tab) overstates itself — no one read any of those
       sources in full — so it's downgraded to ``unverified`` with an
       explanatory note. This is a fact-driven harden, not an author
       assertion: ``TrustState.overridden`` stays ``False``.
    2. **Lifecycle note enrichment.** An unverified lifecycle finding
       blocked on a paper that itself carries a paper-level override gets
       its note enriched (naming the blocking source's declared reason) —
       the label is untouched; a paper being unobtainable is not itself a
       claim-backing assertion.
    3. **Compound rollup.** A compound hub's ``(label, note)`` is
       ``reduce(worse_trust, ...)`` across its atoms — see
       :func:`_compound_trust`. Depth-1 by construction: each atom is
       derived with ``_expand_conjuncts=False``, so a miswired
       compound-of-compound can't recurse through here.
    4. **Claim-level softener.** The finding's/hub's OWN ``meta.
       unacquirable_override`` (set via ``edit(kind='finding',
       unacquirable_note=…)`` or, for a hub, ``POST /claim/<head>/
       unacquirable``) then converts an otherwise-**unverified** label
       (including one just hardened by rule 1, OR just rolled up by rule 3)
       to the softer ``abstract`` (Ⓐ) / ``vouched`` (✍) — a human can vouch
       for the whole bundle even when one conjunct is unsupported. Composes
       with rules 1 and 3. Never applied to an **unsupported** label (a
       negative terminal verification outranks any override: the paper was
       read; "trust me" doesn't unread it).

    ``evidence``/``cite_key_map`` thread a caller's already-derived hub
    evidence + bulk cite_key resolution straight into :func:`_hub_trust`
    (batch/de-dup fix — a caller passing ``evidence`` also implies the
    ref IS a hub, so the ``is_claim_hub`` re-check is skipped too; it does
    NOT imply non-compound — a compound's derived evidence is legitimately
    empty, so the conjunct check still runs). ``conjunct_atom_ids`` is that
    check's own threading knob: a caller that already batched
    :func:`~precis.taproot.seniority.conjunct_atoms_bulk` over its hub set
    passes each hub's list (``[]`` for a plain atomic hub) and this function
    issues no per-hub :func:`~precis.taproot.seniority.derive_conjuncts`
    queries; ``None`` (the default) keeps the lazy per-call derive.
    ``ref`` lets a caller that already fetched
    the finding's ``Ref`` (e.g. for its title) skip this function's own
    ``fetch_refs_by_ids`` call. ``paper_refs`` is the bulk twin for the
    grounding-paper meta the hub-harden check reads (mirrors
    ``cite_key_map``). ``_expand_conjuncts`` is private — the depth-1 guard
    rule 3 relies on; every real caller leaves it at the default ``True``.
    All threading params default to the old single-hub, no-cache behaviour.
    """
    if ref is None:
        ref = store.fetch_refs_by_ids([finding_ref_id]).get(finding_ref_id)
    meta = (ref.meta or {}) if ref is not None else {}

    is_hub = evidence is not None or is_claim_hub(store, finding_ref_id)
    atom_ids: list[int] = []
    if is_hub and _expand_conjuncts:
        if conjunct_atom_ids is not None:
            atom_ids = list(conjunct_atom_ids)
        else:
            atom_ids = [
                cr.hub_ref_id
                for cr in derive_conjuncts(store, finding_ref_id).refined_by
            ]

    hub_evidence: HubEvidence | None = None
    label: TrustLabel
    note: str | None
    status: str
    if atom_ids:
        titles = {
            rid: (r.title or f"<claim {rid}>")
            for rid, r in store.fetch_refs_by_ids(atom_ids).items()
        }
        atoms = [
            (
                a,
                titles.get(a, f"<claim {a}>"),
                claim_trust(store, a, _expand_conjuncts=False),
            )
            for a in atom_ids
        ]
        label, note, status = _compound_trust(atoms)
    elif is_hub:
        # Resolve the hub's evidence once (thread the caller's when given) so
        # both the label AND the harden check below read the same derivation
        # — no second derive.
        if evidence is None:
            evidence = finding_cite_keys(
                store, finding_ref_id, assume_hub=True, cite_key_map=cite_key_map
            ).evidence
        hub_evidence = evidence
        label, note, status = _hub_trust(
            store, finding_ref_id, evidence=evidence, cite_key_map=cite_key_map
        )
    else:
        label, note, status = _lifecycle_trust(store, finding_ref_id, meta)

    if hub_evidence is not None and label == "clean":
        # Rule 1 — harden: every print-visible grounding paper declared
        # unacquirable (a fact, not an author claim-backing assertion), so
        # 'clean' overstates it. Never reached for a compound (hub_evidence
        # stays None there — rule 3 is its rollup instead).
        if _hub_grounding_unacquirable(store, hub_evidence, cite_key_map, paper_refs):
            label = "unverified"
            note = _HUB_GROUNDING_UNACQUIRABLE_NOTE
    elif hub_evidence is None and not atom_ids and label == "unverified":
        # Rule 2 — a lifecycle finding's blocking source paper being
        # declared unacquirable is a fact about acquirability, never a
        # softener: enrich the note only, so a human knows to vouch at the
        # claim level (rule 4) or drop the cite. Guarded off for a compound
        # (`not atom_ids`) — its meta carries no lifecycle ``chain``, so this
        # would be a silent no-op there anyway, but skipping it keeps the
        # branch honest about which finding shape it's for.
        paper_override = _source_paper_override(store, meta)
        paper_note = (
            paper_override.get("note") if isinstance(paper_override, dict) else None
        )
        if paper_note:
            addition = f"blocking source declared unacquirable: {paper_note}"
            note = f"{note} — {addition}" if note else addition

    # Rule 4 — the only softener: the finding's/hub's OWN claim-level
    # declaration (never inherited from a paper). Applies to a compound's
    # rolled-up label exactly as it would to a plain hub's own (rule 3
    # composes with rule 4, same as rule 1 already did).
    return _apply_claim_level_override(label, note, status, meta)


def _source_paper_override(
    store: ClaimTrustStore, meta: dict[str, Any]
) -> dict[str, Any] | None:
    """The paper-level ``unacquirable_override`` (``{note, by, at}``, no
    ``mode``) on a lifecycle finding's *source paper* — the read-through
    that lets rule 2 in :func:`claim_trust` enrich an unverified finding's
    note with WHY its blocking source can't be obtained, without editing
    each finding. Never a softener — a paper's acquirability is a fact
    about the paper, not an assertion about any particular claim resting
    on it.

    A chased finding names its blocking paper as its chain frontier
    (``meta.chain[-1].ref_id`` — the stub whose PDF it's waiting on). A hub
    has no such chain (its trust comes from supporters), so this is a no-op
    there — the hub harden rule (:func:`_hub_grounding_unacquirable`) is
    its own, separate check."""
    chain = meta.get("chain") or []
    if not (chain and isinstance(chain[-1], dict)):
        return None
    frontier_id = chain[-1].get("ref_id")
    if not isinstance(frontier_id, int):
        return None
    ref = store.fetch_refs_by_ids([frontier_id]).get(frontier_id)
    pmeta = (getattr(ref, "meta", None) or {}) if ref is not None else {}
    override = pmeta.get("unacquirable_override")
    return override if isinstance(override, dict) else None


def _has_cite_key(
    store: ClaimTrustStore, paper_ref_id: int, cite_key_map: dict[int, list[str]] | None
) -> bool:
    """Whether a supporter paper is *print-visible* — has a resolvable
    cite_key. Mirrors :func:`~precis.taproot.cite._cite_keys_for_group`'s
    per-edge lookup (bulk map when threaded, else the per-paper query)."""
    aliases = (
        cite_key_map.get(paper_ref_id, [])
        if cite_key_map is not None
        else store.ref_cite_keys(paper_ref_id)
    )
    return bool(aliases)


def _hub_grounding_unacquirable(
    store: ClaimTrustStore,
    evidence: HubEvidence,
    cite_key_map: dict[int, list[str]] | None,
    paper_refs: dict[int, Any] | None = None,
) -> bool:
    """Whether EVERY print-visible grounding paper behind a clean hub
    carries a paper-level ``unacquirable_override`` — rule 1 (harden) in
    :func:`claim_trust`'s check. ``True`` means no one read any of the
    hub's grounding sources in full, so 'clean' overstates the claim.

    Returns ``False`` when at least one grounding paper is genuinely
    acquirable (a real read-grounding remains) or the hub has no
    print-visible supporter at all (not actually a clean hub — defensive;
    the caller only reaches this when ``label == 'clean'``).

    The grounding group mirrors :func:`~precis.taproot.cite.hub_cite_keys`:
    originators (those with a cite_key) when any exist, else corroborators —
    exactly the papers that reach the print citation."""
    for group in (evidence.originators, evidence.corroborators):
        grounding = [
            e.paper_ref_id
            for e in group
            if _has_cite_key(store, e.paper_ref_id, cite_key_map)
        ]
        if grounding:
            break
    else:
        return False  # inflight — no print-visible supporter (not a clean hub)
    # ``paper_refs`` — a bulk caller's pre-fetched grounding-paper refs (mirrors
    # ``cite_key_map``); ``None`` falls back to the per-call fetch.
    refs = paper_refs if paper_refs is not None else store.fetch_refs_by_ids(grounding)
    for pid in grounding:
        r = refs.get(pid)
        override = (getattr(r, "meta", None) or {}).get("unacquirable_override")
        if not isinstance(override, dict):
            return False  # this grounding paper IS acquirable → hub stays clean
    return True


def claim_trust_bulk(
    store: ClaimTrustStore, finding_ref_ids: Iterable[int]
) -> dict[int, TrustState]:
    """Bulk twin of :func:`claim_trust` — resolve many findings' trust in a
    handful of queries instead of one ``claim_trust`` call (~7 round trips
    once a hub's supporters are counted) per finding.

    Splits ``finding_ref_ids`` into hub vs. lifecycle findings with ONE
    :func:`~precis.taproot.seniority.is_claim_hub_bulk` query, then ONE
    :func:`~precis.taproot.seniority.conjunct_atoms_bulk` query over the hub
    ids to find which are compounds and what their atoms are (the "exactly
    one more" query this atomic-claims build adds — taproot-atomic-claims.md
    step 4). Atom ids that fall outside the caller's original set (a
    compound was requested but its atoms weren't) are unioned into the SAME
    :func:`~precis.taproot.seniority.derive_evidence_bulk` (3 more queries,
    regardless of hub+atom count) + bulk cite_key + refs batches every plain
    hub already used — so an atom's own trust costs zero further queries,
    same as a plain hub's. Every hub then gets its :class:`TrustState` from
    :func:`claim_trust` with ``_expand_conjuncts=False`` and everything
    pre-threaded (0 further queries per hub) — for a compound, that means
    building its atom :class:`TrustState`\\ s once (memoized across
    compounds sharing an atom) and reducing via :func:`_compound_trust`
    instead. A lifecycle (non-hub) finding still costs its own
    :func:`claim_trust` call — its STATUS-tag derivation isn't itself N+1
    today, so batching it is out of this fix's scope.

    The smartdraft reader's per-block claim-trust badge
    (``smartdraft.py::claim_trust_for_block``) is this function's
    motivating caller: many rendered blocks citing many distinct hubs used
    to cost one full ``claim_trust`` derivation per distinct head."""
    ids = list(dict.fromkeys(int(r) for r in finding_ref_ids))
    if not ids:
        return {}

    hub_flags = is_claim_hub_bulk(store, ids)
    hub_ids = [r for r in ids if hub_flags.get(r)]
    lifecycle_ids = [r for r in ids if not hub_flags.get(r)]

    out: dict[int, TrustState] = {}
    if hub_ids:
        atoms_by_hub = conjunct_atoms_bulk(store, hub_ids)
        atom_ids = sorted({a for atoms in atoms_by_hub.values() for a in atoms})
        # Union: every plain hub needs its own evidence; every atom (even
        # one the caller never asked about directly) needs its own evidence
        # too, to derive its own trust for the rollup below.
        all_hub_ids = sorted(set(hub_ids) | set(atom_ids))

        evidence_by_hub = derive_evidence_bulk(store, all_hub_ids)
        supporter_ids = {
            e.paper_ref_id
            for ev in evidence_by_hub.values()
            for e in (*ev.originators, *ev.corroborators)
        }
        cite_key_map = store.ref_cite_keys_bulk(supporter_ids) if supporter_ids else {}
        refs = store.fetch_refs_by_ids(all_hub_ids)
        # Supporter-paper refs for the hub-clean unacquirable-override check,
        # batched once (mirrors cite_key_map) rather than per-hub in claim_trust.
        paper_refs = (
            store.fetch_refs_by_ids(list(supporter_ids)) if supporter_ids else {}
        )

        # Every atom's OWN trust, computed once regardless of how many
        # compounds share it (two compounds pointing at the same atom would
        # otherwise re-derive it twice).
        atom_states: dict[int, TrustState] = {
            a: claim_trust(
                store,
                a,
                evidence=evidence_by_hub[a],
                cite_key_map=cite_key_map,
                ref=refs.get(a),
                paper_refs=paper_refs,
                _expand_conjuncts=False,
            )
            for a in atom_ids
        }

        for rid in hub_ids:
            atoms = atoms_by_hub.get(rid) or []
            if atoms:
                triples = [
                    (
                        a,
                        (
                            refs[a].title
                            if a in refs and refs[a].title
                            else f"<claim {a}>"
                        ),
                        atom_states[a],
                    )
                    for a in atoms
                ]
                label, note, status = _compound_trust(triples)
                compound_meta = (refs[rid].meta or {}) if rid in refs else {}
                out[rid] = _apply_claim_level_override(
                    label, note, status, compound_meta
                )
            else:
                out[rid] = claim_trust(
                    store,
                    rid,
                    evidence=evidence_by_hub[rid],
                    cite_key_map=cite_key_map,
                    ref=refs.get(rid),
                    paper_refs=paper_refs,
                    _expand_conjuncts=False,
                )
    for rid in lifecycle_ids:
        out[rid] = claim_trust(store, rid)
    return out


__all__ = [
    "TrustLabel",
    "TrustState",
    "claim_trust",
    "claim_trust_bulk",
    "worse_trust",
]
