"""Whole-draft taproot backfill — a draft chunk's legacy paper citations ->
claim-hub cites, through the **same** canonicalizer cascade the forward
chase bridge uses.

Two citation forms, from the two grammars in draft prose:

* **``[pc<id>]``** (paper-chunk, the legacy grounded cite) — runs the full
  cascade and rewrites ``[pc]`` -> ``[fi<hub>]``.
* **``[pa<id>]``** (whole-paper) — classified by the cited paper's
  body-block count. **stub** (0 blocks) is **skipped** ("fetch first" —
  citing an unread paper as evidence is never minted); routes via fetch,
  then re-ground. **fetched** (>0 blocks) defaults to a ``[pa]``->``[pc]``
  **re-ground**: a ``LocateFn`` (lexical pick + Tier.MEDIUM confirm) picks
  the supporting passage, then the existing ``[pc]`` path promotes it
  (two-step). ``ref_level=True`` instead promotes **directly** in one step
  — but the dig-down ruling (below) still applies: the arm locates the
  supporting passage first and grounds the evidence edge there; it never
  mints a whole-paper (``ungrounded``) edge.

**The dig-down ruling** (gr720460, ruled 2026-11-20): a hub minted here
must carry its own evidence edge to a source chunk, and its statement must
be **drawn from that source** — never taken from the draft prose. Every
arm enforces this mechanically before any mint:

1. **Dig down to the source passage.** The ``[pc]`` arm's passage is the
   named chunk. The ref-level ``[pa]`` arm locates each fetched
   supporter's grounding passage (:data:`LocateFn` over the paper's
   groundable chunks). When no passage resolves at all, **nothing is
   minted and the draft sentence is reported unbacked** (action
   ``"unbacked"``, counted in the job summary, printed by the CLI) — an
   unbacked sentence never becomes a finding.
2. **Verify every atom against the supporter passages**
   (:func:`_verify_atoms_against_supporters`) — lone atoms included: a
   conjunct no cited passage asserts is refused (``"unverified"``), never
   minted with a wrong evidence edge.
3. **Qualify each verified atom against the passage that verified it**
   (injectable ``qualify_fn``, default
   :func:`precis.taproot.directed.qualify_claim`): the claim bends
   one-way to what the source actually licenses, anchored on a
   mechanically-checked verbatim quote. The **qualified** sentence — not
   the draft span's wording — is what the cascade judges and the hub
   states. A passage that asserts the atom's content but will not
   license it as a claim is refused like an unverified atom.

Finding creation here is a proposal class under the shared judge
(me731915): the mechanical precondition (steps 1-3) is enforced in code;
the judge (``dedup_judge`` -> :func:`~precis.taproot.canon.place`) then
rules whether the proposal converges (``attach``), mints (``new`` /
``new_contradicts``), or is held for review (``needs_review`` -> one
review todo, never an auto-applied risky merge).

**Grounding prose is a precondition, both arms** (gripe 245842): a title/
author front-matter block says "this paper exists", not "this passage
supports the claim" — and it *wins* naive lexical overlap on citing-span
topic words. :func:`_read_paper_chunks` filters it from the ``[pa]``
candidate pool; :func:`_ungroundable_handles` re-checks the ``[pc]`` arm.
Both degrade to a **skip** (``reground-nomatch``/``ungroundable``), never
a wrong grounding — the test is prose-presence, not ``ord`` (an abstract
grounds fine; a numeric table grounds a numeric claim).

Motivation: most corpus claims were first written as raw ``[pc]``/``[pa]``
cites before taproot hubs existed. This module walks a draft chunk's
existing cites and runs ``extract_claim -> block -> dedup_judge -> place
-> apply_extraction`` per cite group (mirrors
:func:`precis.workers.chase._taproot_bridge`) so a claim **converges onto
an existing hub** rather than minting a near-duplicate, then rewrites the
prose to ``[fi<hub>]``.

Two deliberate design choices:

* **Cite-group anchored, not per-sentence** — ``[pc<id>]`` markers already
  partition prose into grounded spans (a claim is whatever a citation
  grounds; one sentence can bundle two cited claims, or a claim can span
  two sentences). Contiguous bare pc-cites grounding one atomic span
  collapse to **one** hub with two evidence edges (free dedup).
* **On-demand, dry-run-first, one chunk at a time** — NOT a corpus sweep
  (a corpus-wide pass belongs to a worker, gated by a quality bar); the
  per-chunk human-in-the-loop is the safety gate. :func:`plan_chunk`
  writes nothing.

``extract``/``block``/``judge`` functions are injected (default: the real
:mod:`precis.taproot.canon` ones) — unit-testable with deterministic
fakes, mirroring :func:`precis.taproot.canon.place`'s ``merge_confirm_fn``.

**Decomposition** (docs/backlog/taproot-atomic-claims.md): ``extract_fn``
returns a :class:`~precis.taproot.canon.ClaimExtraction`. Per group, the
cascade tail runs once per atom (:func:`_run_cascade`); the pairs go to
:func:`precis.taproot.hub.apply_extraction`, which mints/converges each
atom with its evidence edge. Supporter papers beyond the first attach
``corroborates`` to **every** atom (the cited passage asserts all
conjuncts; LLM-free, so cheap).

**No composite hub** (gr720460, ruled 2026-11-20): a hub minted here must
carry its own evidence edge. The extraction's ``composite`` is the draft's
bundling sentence and holds no evidence by construction, so the backfill
neither places nor mints it: a split span's cite run rewrites to one
``[fi<hub>]`` per atom hub. Its ``not_claims`` no longer have a composite
hub to hold their memo; they ride the plan and are printed by the CLI
(json and text) and counted in the job summary. Other callers of
``apply_extraction`` (chase, migrate) keep their composites.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.store.types import ActorSlug
from precis.taproot.canon import (
    CanonicalClaim,
    ClaimExtraction,
    ExtractionUnavailable,
    MergeCandidate,
    MintCap,
    NotClaim,
    Placement,
    Verdict,
    block,
    dedup_judge,
    extract_claim_strict,
    merge_confirm,
    place,
)
from precis.taproot.grounding import has_grounding_prose
from precis.taproot.reground import (
    PaperChunk,
    RegroundingUnavailable,
    VerifyBatchFn,
    _fold_quote_text,
    verify_atoms_batch,
)
from precis.utils.draft_markup import strip_markers
from precis.utils.mentions import DRAFT_MARKUP_PATTERN
from precis.utils.sentences import split_sentences

if TYPE_CHECKING:
    from precis.store.store import Store
    from precis.taproot.directed import QualifyResult

__all__ = [
    "ChunkBackfill",
    "CiteGroup",
    "GroupPlan",
    "PcCite",
    "apply_chunk",
    "plan_chunk",
    "segment_cite_groups",
]

#: A bare pc-cite is exactly ``pc<digits>`` with no pin — the legacy
#: paper-chunk citation form. ``[fi…]`` (already a hub cite) and a pinned
#: ``[fi…>pc…]`` / ``[pc…+pa…]`` are already-authored and skipped.
_PC_HANDLE_RE = re.compile(r"^pc\d+$")

#: A bare pa-cite is exactly ``pa<digits>`` — a whole-**paper** cite (the
#: ``[pa]`` arm). Anchored alongside ``pc`` but kept in its own group (a pa
#: and a pc cite never fold together), so the per-group action (stub-skip /
#: re-ground / ref-level promote) is unambiguous.
_PA_HANDLE_RE = re.compile(r"^pa\d+$")

#: Leading whitespace + a prior sentence's trailing terminator, trimmed off a
#: grounded span so extract_claim reads the claim, not ". "/", " residue.
_LEADING_PUNCT_RE = re.compile(r"^[\s.,;:!?)—–-]+")

#: Below this word count a trailing sentence reads as a dangling fragment
#: ("Doping helps." / a transition clause) rather than the claim itself —
#: the sentence before it is pulled in too, so :func:`_bound_to_trailing_sentences`
#: never hands ``extract_claim`` a subject-less clause.
_FRAGMENT_WORD_MIN = 6

_WORD_RE = re.compile(r"\w+")

#: A figure caption's trailing image-reuse pointer — "Reproduced from",
#: "Adapted from", "Data from" (third-party: n/a — caption-pointer idiom,
#: not vendored code) — right before the cite marker it grounds
#: (gripe 450329). Provenance, not a claim about the world.
_FIGURE_POINTER_RE = re.compile(
    r"\b(?:reproduced|adapted|data)\s+from\s*$", re.IGNORECASE
)


@dataclass(frozen=True)
class PcCite:
    """One bare paper citation (``[pc<id>]`` or ``[pa<id>]``) and its exact
    position in the text."""

    handle: str  # "pc293" / "pa42"
    start: int  # offset of the "[" in the source text
    end: int  # offset just past the "]"
    raw: str  # the exact matched marker, e.g. "[pc293]"
    kind: str = "pc"  # "pc" (paper-chunk) | "pa" (whole-paper)


@dataclass(frozen=True)
class CiteGroup:
    """A grounded span: the prose since the previous cite, plus the run of
    one-or-more contiguous bare pc-cites that terminate it.

    ``span_text`` has inline markers stripped (so ``extract_claim`` reads the
    prose, not the bracket syntax). ``pc_cites`` keeps the raw offsets so the
    prose rewrite can replace them exactly (offset-based, duplicate-safe).
    """

    span_text: str
    pc_cites: list[PcCite]

    @property
    def handles(self) -> list[str]:
        return [c.handle for c in self.pc_cites]

    @property
    def kind(self) -> str:
        """``"pc"`` or ``"pa"`` — the group's citation form. Homogeneous by
        construction: :func:`segment_cite_groups` only folds contiguous cites
        of the *same* kind, so a group is all-pc or all-pa, never mixed."""
        return self.pc_cites[0].kind


@dataclass
class GroupPlan:
    """The planned outcome for one :class:`CiteGroup` — read-only until
    :func:`apply_chunk` acts on it."""

    group: CiteGroup
    #: ``"no-claim"`` — the span asserts nothing groundable (extract →
    #: ``ClaimExtraction.is_empty``);
    #: ``"unresolved"`` — a handle didn't resolve to a paper (skipped);
    #: ``"stub-fetch-first"`` — a ``[pa]`` cite to an un-fetched stub (0 blocks),
    #: skipped pending fetch (no write, prose left as ``[pa]``);
    #: ``"reground"`` — a fetched ``[pa]`` (default mode) whose passage was
    #: located: on apply, rewrites ``[pa]``→``[pc<chunk>]`` (see
    #: :attr:`reground_targets`), no hub minted — a cite refinement the existing
    #: ``[pc]`` path then promotes;
    #: ``"reground-nomatch"`` — a fetched ``[pa]`` for which the locate found no
    #: supporting passage (no write, prose left ``[pa]``; re-ground by hand or
    #: ``--ref-level`` to promote whole-paper);
    #: ``"ungroundable"`` — every ``[pc]`` supporter names a chunk with no
    #: groundable prose (a title/author front-matter block), so there is no
    #: passage to attach evidence to (no write, prose left ``[pc…]``);
    #: ``"bad-claim"`` — every extracted claim failed the shape check
    #: (verbatim copy of its chunk sentence, lowercase start, or leading
    #: "and" — an extraction fragment, not a claim);
    #: ``"unverified"`` — an extracted atom no cited supporter's own
    #: grounding passage asserts (or the passage asserts the content but
    #: declines to license it as a claim statement); the whole group is
    #: refused (no write, prose left) rather than silently dropping a
    #: conjunct of the sentence the cite run grounds;
    #: ``"unbacked"`` — the dig-down found no source passage at all (a
    #: ref-level ``[pa]`` promote whose cited papers locate no supporting
    #: passage, or a ``[pc]`` group every one of whose chunks retired
    #: between the ungroundable gate and the passage read): mint nothing,
    #: prose left, and :attr:`unbacked` carries
    #: the draft sentence for the unbacked report — an unbacked sentence
    #: never becomes a finding (gr720460, ruled 2026-11-20);
    #: ``"verify-unavailable"`` — the atom-verification dispatch itself
    #: failed (infra); retryable, prose left as-is;
    #: ``"extract-unavailable"`` — the extraction LLM dispatch failed (infra,
    #: :class:`~precis.taproot.canon.ExtractionUnavailable`), so nothing was
    #: decided: no write, prose left as-is, retryable — unlike ``no-claim``,
    #: which is a final semantic verdict;
    #: ``"mint-capped"`` — the primary grounding chunk already seeded the
    #: per-run limit of new hubs (:class:`~precis.taproot.canon.MintCap`,
    #: gr204874); no write, prose left as-is;
    #: ``"error"`` — a write failed for this group (isolated; batch continues);
    #: ``"partial"`` — the hub landed and the prose was rewritten to it, but a
    #: later supporter's evidence edge failed (a re-run attaches it);
    #: else the cascade action: ``"attach"`` / ``"new"`` /
    #: ``"new_contradicts"`` / ``"needs_review"``.
    action: str
    #: The claim/placement behind this group's ``action``/``note`` above —
    #: the atom whose placement ranks first in :data:`_ACTION_RANK` (a lone
    #: atom's own; for a split span the one needing most attention, so one
    #: ``needs_review`` atom marks the whole group).
    #: ``None`` for no-claim / unresolved / ungroundable / stub-fetch-first /
    #: reground(-nomatch).
    claim: CanonicalClaim | None = None
    placement: Placement | None = None
    #: Every atom's ``(claim, Placement)`` pair from :func:`_run_cascade`'s
    #: per-atom cascade tail, in extraction order — handed to
    #: :func:`precis.taproot.hub.apply_extraction` as its ``atoms=`` arg.
    #: For a group that passed the dig-down gate the claim is the
    #: **qualified** sentence (drawn from the verifying passage), never
    #: the draft span's wording. Empty for no-claim / unresolved /
    #: ungroundable / stub-fetch-first / reground(-nomatch) / unbacked.
    atom_plans: list[tuple[CanonicalClaim, Placement]] = field(default_factory=list)
    #: The verbatim grounding quote :func:`precis.taproot.directed.qualify_claim`
    #: returned for each atom in :attr:`atom_plans` (aligned by index) — the
    #: mechanical proof the statement is drawn from the source passage.
    #: ``None`` per atom when qualification did not run (same plans as above
    #: leave :attr:`atom_plans` empty).
    atom_quotes: list[str | None] = field(default_factory=list)
    #: Rejected conjuncts (:func:`extract_claim`'s ``not_claims``) — a
    #: report only: the backfill mints no composite hub to hold their audit
    #: memo (gr720460), so the CLI prints them per group and the job
    #: summary counts them (:attr:`ChunkBackfill.n_not_claims`). Empty when
    #: the extraction rejected nothing (including the no-decomposition case).
    not_claims: tuple[NotClaim, ...] = ()
    #: The extraction's ``explains`` pairs — indices into :attr:`atom_plans`
    #: (explaining atom, explained atom) — handed to
    #: :func:`precis.taproot.hub.apply_extraction`, which writes the
    #: ``explains`` edge between the two placed atom hubs and skips any pair
    #: it cannot (this path has no ``classify_extraction`` gate, so a bad
    #: pair is dropped at the door, never raised).
    explains: tuple[tuple[int, int], ...] = ()
    #: How many of :attr:`explains` :func:`apply_chunk` could not write
    #: (bad sentinel, out of range, unplaced endpoint, same hub, cycle) —
    #: each also logged at WARNING by ``apply_extraction``. Set post-write.
    explains_skipped: int = 0
    #: (handle, paper_ref_id) for each resolved supporter — for a fetched-``[pa]``
    #: ref-level promote, only the fetched supporters (stubs never mint evidence).
    supporters: list[tuple[str, int]] = field(default_factory=list)
    #: The first hub this group's prose cite targets once acted on (the
    #: lone atom's hub; the first of :attr:`cite_hub_ids` for a split span).
    #: Populated at plan time for ``attach`` (the matched candidate); set
    #: post-write by :func:`apply_chunk` for ``new``/``new_contradicts``.
    #: ``None`` for no-claim / needs_review / unresolved / stub-fetch-first
    #: / reground-needed (prose left untouched).
    hub_ref_id: int | None = None
    #: Every hub the rewritten cite run names, in atom order, deduplicated
    #: (two atoms can converge on one hub) — one ``[fi<hub>]`` each. Known
    #: at plan time only when every atom is an ``attach`` (a dry run then
    #: shows the whole run); otherwise set post-write by :func:`apply_chunk`
    #: once every atom has landed. Empty when the prose is left untouched.
    cite_hub_ids: list[int] = field(default_factory=list)
    #: For a ``"reground"`` action: the located chunk_id per supporter (run
    #: order), so :func:`apply_chunk` rewrites the ``[pa…]`` run to the matching
    #: ``[pc<chunk>]`` sequence. Empty for every other action.
    reground_targets: list[int] = field(default_factory=list)
    #: For a ``"reground"`` action: ``(paper_ref_id, chunk_id, chunk_text)`` per
    #: supporter, same run order as :attr:`reground_targets`. The locate already
    #: proved a claim-passage binding (:attr:`group`'s ``span_text`` *is* the
    #: claim - "a claim is whatever a citation grounds"); :func:`apply_chunk`
    #: persists it as a ``citation`` audit record so the intermediate ``[pc]``
    #: carries *what* is claimed, not just a bare pointer. Empty otherwise.
    reground_grounds: list[tuple[int, int, str]] = field(default_factory=list)
    #: Per-atom verified-supporter indices: ``atom_supporters[i]`` is the
    #: ordered list of indices into :attr:`supporters` whose own grounding
    #: passage verified atom ``i`` (aligned with :attr:`atom_plans`).
    #: ``None`` whenever verification did not run to completion: a no-write
    #: plan (no-claim, unresolved, ungroundable, unbacked, …) or the
    #: ``"verify-unavailable"`` infra refusal. A group with no passage at
    #: all is ``"unbacked"`` and never reaches the write path.
    #: Drives :func:`apply_chunk`'s per-atom evidence scoping.
    atom_supporters: list[list[int]] | None = None
    #: For a ref-level ``[pa]`` dig-down: the located passage's ``pc<chunk_id>``
    #: handle per supporter (aligned with :attr:`supporters`), ``None`` for a
    #: supporter whose paper located no passage. The apply path names this
    #: handle on the evidence edge (``meta.source_handle``) so the edge
    #: grounds at the located chunk; a ``None`` entry keeps the supporter's
    #: own ``pa<id>`` (never reached on the write path — an atom verifies
    #: against a passage or the group is refused). ``None`` for non-pa arms.
    dug_sources: list[str | None] | None = None
    #: For ``"unbacked"``: the draft sentence the dig-down could not ground —
    #: the unbacked report's payload (CLI print + job count). ``None`` for
    #: every other action.
    unbacked: str | None = None
    note: str = ""
    #: For ``"extract-unavailable"``: the router's failure class and the
    #: horizon it named (``None`` when unclassified) — the job that
    #: re-runs these chunks reports *why* and *when*, so a quota window
    #: is not confused with a dead endpoint.
    unavailable_class: str | None = None
    retry_at: datetime | None = None


@dataclass
class ChunkBackfill:
    """The plan (or applied result) for a whole draft chunk."""

    chunk_id: int
    draft_ref_id: int
    plans: list[GroupPlan]
    #: The rewritten chunk text (``[pc…]`` → ``[fi<hub>]``), populated by
    #: :func:`apply_chunk`; ``None`` on a pure plan / when nothing rewrote.
    rewritten_text: str | None = None

    @property
    def n_claims(self) -> int:
        return sum(1 for p in self.plans if p.claim is not None)

    @property
    def n_not_claims(self) -> int:
        """Conjuncts the extraction rejected as not-a-claim across this
        chunk's groups — reported, written to no hub."""
        return sum(len(p.not_claims) for p in self.plans)

    @property
    def n_explains_skipped(self) -> int:
        """``explains`` pairs the extraction proposed that this chunk's
        apply could not write (dropped with a WARNING naming ``dc<chunk>``)."""
        return sum(p.explains_skipped for p in self.plans)

    @property
    def n_unbacked(self) -> int:
        """Draft sentences this chunk's dig-down could not ground in any
        source passage — reported (``"unbacked"`` plans), never minted."""
        return sum(1 for p in self.plans if p.unbacked is not None)


# Injected cascade functions (default to the real canon ones) — keeps the
# segmenter + orchestration testable without an LLM/embedder. ``merge_confirm``
# is threaded to :func:`place` (a low-confidence ``same`` escalates to a BIG
# confirm — the same behaviour the chase bridge accepts).
ExtractFn = Callable[[str], ClaimExtraction]
BlockFn = Callable[[CanonicalClaim, Any, Any], list[MergeCandidate]]
JudgeFn = Callable[[str, str], Verdict]
MergeConfirmFn = Callable[[str, str], Verdict]

#: Bend a verified atom's wording one-way to what the verifying passage
#: licenses (:func:`precis.taproot.directed.qualify_claim`'s shape) — the
#: "statement drawn from the source" step of the dig-down ruling. Injected so
#: tests run deterministically without a network dispatch.
QualifyFn = Callable[[str, str], "QualifyResult"]


def _default_qualify(proposed: str, passage: str) -> QualifyResult:
    """The real qualify dispatch, bound lazily: :mod:`precis.taproot.directed`
    pulls in :mod:`precis.taproot.hub`, which the plan path otherwise imports
    only at write time — same lazy posture as ``apply_chunk``'s hub imports."""
    from precis.taproot.directed import qualify_claim

    return qualify_claim(proposed, passage)


#: Suggest the grounding passage for a ``[pa]``→``[pc]`` re-ground: given the
#: claim span and the cited paper's body chunks ``(chunk_id, ord, text)``,
#: return the chosen chunk (same tuple) or ``None`` when no passage supports the
#: span. Injected so tests run with a deterministic fake and no LLM/embedder
#: (the default reuses chase's lexical pick + Tier.MEDIUM ``_locate_chunk_in_target``
#: confirm — the arm's one LLM call, on the write/dry-run path only).
LocateFn = Callable[[str, list[tuple[int, int, str]]], tuple[int, int, str] | None]

_MERGE_CONFIRM_DEFAULT = merge_confirm

#: Placement actions, most attention first — a split span reports the
#: first-ranked action among its atoms (:func:`_run_cascade`).
_ACTION_RANK = ("needs_review", "new_contradicts", "new", "attach")

_TOKEN_RE = re.compile(r"\w+")
#: A numeric literal kept whole (``7.27``, ``0.77``, ``-3``) for table matching.
_NUMBER_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w])")


def _default_locate(
    span: str, chunks: list[tuple[int, int, str]]
) -> tuple[int, int, str] | None:
    """Default grounding-chunk locate: deterministic unigram-overlap pick over
    the paper's chunks, confirmed/corrected by the Tier.MEDIUM
    :func:`precis.workers._chase_llm._locate_chunk_in_target` — mirroring
    :func:`precis.workers.chase._select_target_chunk`'s selection. Returns the
    chosen ``(chunk_id, ord, text)``, or ``None`` when the span has no signal or
    the LLM finds no supporting passage (→ ``reground-nomatch``, no rewrite)."""
    if not chunks:
        return None
    tokens = {w.lower() for w in _TOKEN_RE.findall(span) if len(w) > 2}
    numbers = set(_NUMBER_RE.findall(span))
    if not tokens and not numbers:
        return None  # empty/too-short span: nothing to ground against, don't guess

    def _overlap(text: str) -> int:
        words = len(tokens & {w.lower() for w in _TOKEN_RE.findall(text) if len(w) > 2})
        # A benchmark-table cell ("7.27") carries none of the claim's topic words
        # and ``\w+`` shreds it into short tokens the length filter drops, so
        # match numeric literals whole and weight them above a topic word
        # (gripe 453835) — otherwise the table that holds the claim's numbers
        # loses to prose that merely shares its vocabulary.
        nums = len(numbers & set(_NUMBER_RE.findall(text)))
        return words + 5 * nums

    from precis.workers._chase_llm import _locate_chunk_in_target

    ranked = sorted(chunks, key=lambda c: -_overlap(c[2]))
    best = ranked[0]
    return _locate_chunk_in_target(
        claim=span,
        proposed=best,
        alternates=ranked[1:4],
    )


def _read_paper_chunks(store: Store, ref_id: int) -> list[tuple[int, int, str]]:
    """The **groundable** live body chunks ``(chunk_id, ord, text)`` of a paper
    ref, ord order. Read-only; the candidate pool a re-ground's
    :data:`LocateFn` picks from.

    Prose-less chunks (a title/author front-matter block) are filtered out
    here rather than left for the locate to reject: a title block is short and
    dense with exactly the claim's topic words, so it *wins* the unigram
    overlap in :func:`_default_locate` and the Tier.MEDIUM confirm sees no
    alternative to compare it against (gripe 245842). An empty pool degrades
    the caller to ``reground-nomatch`` — the prose is left as ``[pa]``, which
    is the honest outcome when the paper offers no groundable passage.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id, ord, text FROM chunks "
            "WHERE ref_id = %s AND ord >= 0 AND retired_at IS NULL ORDER BY ord",
            (ref_id,),
        ).fetchall()
    return [
        (int(cid), int(ordv), str(txt))
        for cid, ordv, txt in rows
        if has_grounding_prose(str(txt))
    ]


def _ungroundable_handles(store: Store, handles: list[str]) -> set[str]:
    """The subset of ``pc<chunk_id>`` handles whose chunk carries no
    groundable prose (gripe 245842).

    The ``[pa]`` arm is guarded at the candidate pool, but a ``[pc]`` cite
    names its chunk outright — including one an *earlier* backfill run wrote
    by re-grounding onto a title block — so the ``[pc]`` arm has to re-check
    at plan time rather than trust the handle. A **retired** chunk counts as
    ungroundable too: the handle still resolves, but the text is dead.
    """
    chunk_ids = {int(h[2:]): h for h in handles if _PC_HANDLE_RE.match(h)}
    if not chunk_ids:
        return set()
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id, text FROM chunks "
            "WHERE chunk_id = ANY(%s) AND retired_at IS NULL",
            (list(chunk_ids),),
        ).fetchall()
    # A retired chunk resolves upstream (``resolve_paper_ref_id`` filters
    # ``refs.retired_at``, not ``chunks.retired_at``) but is dead text: a
    # re-chunk retires the row and inserts a replacement, so grounding an edge
    # on the old id cites content no reader can reach. Absent from the live
    # rows ⇒ ungroundable, same as prose-less.
    live = {int(cid): str(txt) for cid, txt in rows}
    return {
        handle
        for cid, handle in chunk_ids.items()
        if cid not in live or not has_grounding_prose(live[cid])
    }


# ── segmentation ─────────────────────────────────────────────────────────


def _iter_bare_cites(text: str) -> list[PcCite]:
    """Every bare ``[pc<id>]`` / ``[pa<id>]`` cite (no pin) in ``text``, in
    order, each tagged with its ``kind`` (``"pc"``/``"pa"``).

    Uses the single-sourced :data:`DRAFT_MARKUP_PATTERN` so this never drifts
    from the draft grammar: a match is a candidate iff its ``bare`` group is
    ``pc<digits>`` or ``pa<digits>`` **and** it carries no ``pin`` group (a
    ``[pc…+pa…]`` or a hub-pinned ``[fi…>pc…]`` is already-authored, skipped).
    """
    out: list[PcCite] = []
    for m in DRAFT_MARKUP_PATTERN.finditer(text):
        bare = m.groupdict().get("bare")
        if not bare:
            continue
        if _PC_HANDLE_RE.match(bare):
            kind = "pc"
        elif _PA_HANDLE_RE.match(bare):
            kind = "pa"
        else:
            continue
        if m.groupdict().get("pin"):
            continue  # pinned — already authored
        out.append(
            PcCite(handle=bare, start=m.start(), end=m.end(), raw=m.group(0), kind=kind)
        )
    return out


def _bound_to_trailing_sentences(span: str) -> str:
    """Bound ``span`` to the sentence(s) immediately preceding the cite
    marker it grounds (gripe 450339) — never the whole marker-to-marker
    prose run. A citation landing several sentences into a paragraph must
    not pull in an unrelated earlier sentence just because it shares the
    span; a multi-sentence run collapses to its **last** sentence, plus the
    one before it when that last sentence is a short fragment (under
    :data:`_FRAGMENT_WORD_MIN` words) that would otherwise reach
    ``extract_claim`` with no subject.

    A span with 0-1 sentences is returned unchanged (nothing to bound —
    covers the empty-span no-claim case and the already-single-sentence
    common case).
    """
    sentences = split_sentences(span)
    if len(sentences) <= 1:
        return span
    start = sentences[-1].char_offset
    if len(_WORD_RE.findall(sentences[-1].text)) < _FRAGMENT_WORD_MIN:
        start = sentences[-2].char_offset
    return span[start:].strip()


def _is_figure_pointer_span(span_text: str) -> bool:
    """True iff ``span_text`` ends in a figure's provenance-pointer idiom
    ("Reproduced from"/"Adapted from"/"Data from" — third-party: n/a —
    caption-pointer idiom, not vendored code) right before the cite
    marker — an image-reuse trail, not a claim (gripe 450329)."""
    return bool(_FIGURE_POINTER_RE.search(span_text.rstrip()))


def segment_cite_groups(text: str) -> list[CiteGroup]:
    """Partition ``text`` into grounded cite-groups.

    Each bare paper cite (``[pc<id>]``/``[pa<id>]``) grounds the prose
    since the previous cite (any kind) or the chunk start — assumes the
    citation-follows-claim style (a prefix cite grounds the empty span
    before it -> no-claim -> left as-is). Contiguous bare cites of the
    **same kind**, whitespace-only between them, share one span (multiple
    papers, one claim) and collapse to one group.

    A cite that's neither a bare pc nor pa (``[fi…]``, ``[¶…]``, ``[§…]``,
    a pinned cite) is a hard boundary: not an anchor, but it **breaks
    contiguity**, so an anchor right after one starts its own group. A
    **kind switch** breaks contiguity the same way — a whole-paper cite
    and a passage cite are always routed to separate groups.

    Each group's ``span_text`` is further bounded to its trailing
    sentence(s) via :func:`_bound_to_trailing_sentences` — a multi-sentence
    prose run since the previous marker never hands ``extract_claim`` an
    earlier, unrelated sentence just because a cite happens to land after it.
    """
    # All markers (any kind) give the span boundaries; only bare pc/pa cites
    # are anchors. Walk markers in order; an anchor folds into the current
    # group ONLY when the immediately-preceding marker was an anchor of the
    # SAME kind (an unbroken same-kind run) — an intervening non-anchor marker
    # or a kind switch resets contiguity.
    cite_by_start: dict[int, PcCite] = {c.start: c for c in _iter_bare_cites(text)}
    markers: list[tuple[int, int, bool]] = [
        (m.start(), m.end(), m.start() in cite_by_start)
        for m in DRAFT_MARKUP_PATTERN.finditer(text)
    ]

    groups: list[CiteGroup] = []
    prev_end = 0
    prev_kind: str | None = None  # kind of the immediately-preceding anchor
    for start, end, is_anchor in markers:
        if not is_anchor:
            prev_end = end
            prev_kind = None
            continue
        cite = cite_by_start[start]
        # The prose since the previous marker, minus the previous sentence's
        # trailing terminator (a leading ". " / ", " belongs to that sentence,
        # not this claim), then bounded to the trailing sentence(s) this cite
        # actually grounds — cleaner input for extract_claim.
        span = _LEADING_PUNCT_RE.sub("", strip_markers(text[prev_end:start]).strip())
        span = _bound_to_trailing_sentences(span)
        if not span and prev_kind == cite.kind and groups:
            # Unbroken same-kind run (only whitespace since the last same-kind
            # cite): same grounded span, another supporting paper.
            groups[-1].pc_cites.append(cite)
        else:
            groups.append(CiteGroup(span_text=span, pc_cites=[cite]))
        prev_end = end
        prev_kind = cite.kind
    return groups


# ── chunk read ───────────────────────────────────────────────────────────


def _read_draft_chunk(store: Store, chunk_id: int) -> tuple[str, int, str]:
    """``(text, draft_ref_id, chunk_kind)`` for a live draft body chunk.
    Read-only. ``chunk_kind`` (e.g. ``"paragraph"``/``"figure"``) lets the
    planner special-case a figure caption's provenance pointer (gripe
    450329).

    Raises:
        BadInput: no such body chunk, or its owning ref isn't a live draft.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT c.text, c.ref_id, c.chunk_kind, r.kind, r.retired_at "
            "FROM chunks c JOIN refs r ON r.ref_id = c.ref_id "
            "WHERE c.chunk_id = %s AND c.ord >= 0 AND c.retired_at IS NULL",
            (chunk_id,),
        ).fetchone()
    if row is None:
        raise BadInput(
            f"no live draft body chunk with chunk_id={chunk_id}",
            next="pass a dc<id> handle for a live draft chunk",
        )
    text, ref_id, chunk_kind, kind, retired_at = row
    if kind != "draft" or retired_at is not None:
        raise BadInput(
            f"chunk_id={chunk_id} belongs to a {kind!r} ref (ref_id={ref_id}), "
            "not a live draft",
        )
    return str(text), int(ref_id), str(chunk_kind)


# ── planning (read-only, the dry-run core) ─────────────────────────────────


def _bad_claim_shape(sentence: str, *, span: str) -> bool:
    """Extraction-fragment check: a claim that repeats its own chunk
    sentence verbatim, opens lowercase, or opens with "and" is a sliver of
    the source text, not a claim — the harvest refuses it before any mint."""
    s = " ".join(sentence.split())
    if not s:
        return True
    if s == " ".join(span.split()):
        return True
    if s[0].islower():
        return True
    return bool(s[:4].lower() == "and ")


def _supporter_passages(
    store: Store, supporters: list[tuple[str, int]]
) -> list[PaperChunk | None]:
    """Each supporter's grounding passage, aligned with ``supporters`` —
    the ``pc<chunk_id>`` handle's live chunk as a :class:`PaperChunk`;
    ``None`` for a ``pa`` handle (whole-paper: no single passage to argue
    an atom against) or a handle that no longer resolves to a live chunk.
    Read-only."""
    pc_ids = [int(h[2:]) for h, _rid in supporters if _PC_HANDLE_RE.match(h)]
    by_chunk: dict[int, tuple[int, str | None, str]] = {}
    if pc_ids:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT chunk_id, ord, array_to_string(section_path, ' > '), "
                "text FROM chunks WHERE chunk_id = ANY(%s) "
                "AND retired_at IS NULL",
                (pc_ids,),
            ).fetchall()
        by_chunk = {
            int(cid): (int(ordv), str(sp) if sp else None, str(txt or ""))
            for cid, ordv, sp, txt in rows
        }
    passages: list[PaperChunk | None] = []
    for h, _rid in supporters:
        chunk_id = int(h[2:]) if _PC_HANDLE_RE.match(h) else None
        row = by_chunk.get(chunk_id) if chunk_id is not None else None
        passages.append(
            PaperChunk(
                chunk_id=chunk_id,
                chunk_ord=row[0],
                section_path=row[1],
                text=row[2],
            )
            if row is not None and chunk_id is not None
            else None
        )
    return passages


def _verify_atoms_against_supporters(
    atoms: list[CanonicalClaim],
    passages: list[PaperChunk | None],
    *,
    verify_batch_fn: VerifyBatchFn,
) -> list[list[int]]:
    """Per-atom verified supporters: ``out[i]`` is the ordered list of
    supporter indices whose own grounding passage asserts atom ``i`` —
    one :func:`verify_atoms_batch` call per supporter (all atoms × that
    supporter's passage). A returned ``supported`` verdict counts only
    when its ``quote`` also survives the verbatim-in-passage check (the
    anti-hallucination backstop — the substring leg of
    :func:`precis.taproot.reground._validate_quote`; the uniqueness leg
    is moot here: one cited passage, not a paper's chunk set)."""
    out: list[list[int]] = [[] for _ in atoms]
    for s_idx, passage in enumerate(passages):
        if passage is None:
            continue
        for r in verify_batch_fn(atoms, [passage]):
            if not (0 <= r.atom_index < len(atoms)) or not r.supported:
                continue
            if r.quote is None or (
                _fold_quote_text(r.quote) not in _fold_quote_text(passage.text)
            ):
                continue
            out[r.atom_index].append(s_idx)
    return out


def _campaign_candidates(
    store: Store,
    claim: CanonicalClaim,
    campaign: str,
    *,
    seen: set[int],
    k: int = 10,
) -> list[MergeCandidate]:
    """Lexical recall over the campaign's existing claim hubs — the
    ``campaign:<slug>``-tagged hubs' ``finding_body`` chunks — returned
    as extra :class:`MergeCandidate`\\ s for the judge. The ANN in
    :func:`~precis.taproot.canon.block` only recalls what the embedding
    puts near the claim; a harvest must not mint a hub the campaign
    already carries under different wording, so a tag-scoped lexical
    pass catches what the ANN misses. ``distance`` is not meaningful for
    a lexical hit — ``inf`` keeps it honest (``place`` picks the first
    confirmed candidate by input order, never by distance).
    ``seen``/``exclude_ref_ids`` keeps an already-ANN-returned hub from
    being judged twice."""
    rows = store.chunks.search_chunks_lexical(
        q=claim.sentence,
        kind="finding",
        tags=[f"campaign:{campaign}", "TAPROOT:claim", "STATUS:canonical"],
        chunk_kinds=["finding_body"],
        exclude_ref_ids=sorted(seen),
        limit=k,
    )
    return [
        MergeCandidate(
            hub_ref_id=ref.id,
            claim=str(chunk.text or ref.title or ""),
            distance=float("inf"),
        )
        for chunk, ref, _rank in rows
    ]


def _run_cascade(
    store: Store,
    embedder: Any,
    group: CiteGroup,
    supporters: list[tuple[str, int]],
    *,
    extract_fn: ExtractFn,
    block_fn: BlockFn,
    judge_fn: JudgeFn,
    merge_confirm_fn: MergeConfirmFn,
    passages: list[PaperChunk | None] | None = None,
    dug_sources: list[str | None] | None = None,
    campaign: str | None = None,
    verify_batch_fn: VerifyBatchFn = verify_atoms_batch,
    qualify_fn: QualifyFn | None = None,
) -> GroupPlan:
    """The shared extract → dig-down → verify → qualify → block → judge →
    place tail, once supporters are resolved. Runs the block/judge/place
    cascade once per atom; the extraction's composite is never placed (module
    docstring, "No composite hub") — canon (LLM/ANN) stays per-claim; only the
    write door (:func:`precis.taproot.hub.apply_extraction`, called from
    :func:`apply_chunk`) is decomposition-aware.

    The dig-down ruling (module docstring) is enforced here, before any
    cascade run: ``passages`` (the supporters' grounding passages — the
    ``[pc]`` arm's named chunks, or the ref-level ``[pa]`` arm's located
    passages) must resolve to at least one real passage, every atom must
    verify against one of them, and every verified atom is re-stated by
    ``qualify_fn`` against the passage that verified it — the qualified
    sentence, never the draft span's wording, is what reaches the judge.
    ``dug_sources`` (pa arm only) maps each supporter to its located
    ``pc<chunk_id>`` handle for the grounded evidence edge.

    An :class:`~precis.taproot.canon.ExtractionUnavailable` from ``extract_fn``
    (the strict extractor's infra-failure signal) becomes the retryable
    ``extract-unavailable`` plan, never ``no-claim``: an LLM outage is not a
    verdict that the span asserts nothing."""
    try:
        extraction = extract_fn(group.span_text)
    except ExtractionUnavailable as exc:
        cls = exc.reason_class or "unclassified"
        after = f" after {exc.retry_at.isoformat()}" if exc.retry_at else ""
        return GroupPlan(
            group=group,
            action="extract-unavailable",
            supporters=supporters,
            note=(
                f"claim extraction unavailable [{cls}] (dispatch failed or reply "
                f"cut off) — re-run{after}: {exc}"
            ),
            unavailable_class=exc.reason_class,
            retry_at=exc.retry_at,
        )
    if extraction.is_empty:
        return GroupPlan(
            group=group,
            action="no-claim",
            supporters=supporters,
            note="span asserts nothing groundable",
        )

    # Claim-shape refusal: an atom/composite that just repeats its own
    # chunk sentence verbatim, opens lowercase, or opens with "and" is an
    # extraction fragment, not a claim — refusing at the door keeps the
    # harvest from minting restatements and conjunction slivers.
    kept = [
        i
        for i, a in enumerate(extraction.atoms)
        if not _bad_claim_shape(a.sentence, span=group.span_text)
    ]
    atoms = [extraction.atoms[i] for i in kept]
    if not atoms:
        return GroupPlan(
            group=group,
            action="bad-claim",
            supporters=supporters,
            note=(
                "every extracted claim failed the shape check (verbatim copy "
                "of its chunk sentence, lowercase start, or leading 'and')"
            ),
        )
    # ``explains`` pairs index the ORIGINAL atom list — re-map them through
    # the shape filter so a dropped atom can't shift a surviving pair onto
    # the wrong (or a refused) endpoint. A pair touching a refused atom is
    # dropped outright; a pair already out of the original range is passed
    # through raw — apply_extraction's own sentinel check owns that case.
    explains = tuple(
        (kept.index(i), kept.index(j)) if i in kept and j in kept else (i, j)
        for i, j in extraction.explains
        if (
            (i in kept or not (0 <= i < len(extraction.atoms)))
            and (j in kept or not (0 <= j < len(extraction.atoms)))
        )
    )

    # ── the dig-down gate (gr720460 ruling): no source passage → no mint,
    # and the draft sentence is reported unbacked. The [pa] arm's no-passage
    # case is refused upstream in _plan_pa_group before extraction; reaching
    # here with no passage at all is the pc arm's defensive race (every cited
    # chunk retired between the ungroundable gate and this read) — the
    # verdict is the same either way: an unbacked sentence never becomes a
    # finding, and a hub is never minted on draft wording alone.
    if passages is None:
        passages = _supporter_passages(store, supporters)
    if not any(p is not None for p in passages):
        return GroupPlan(
            group=group,
            action="unbacked",
            supporters=supporters,
            dug_sources=dug_sources,
            not_claims=extraction.not_claims,
            unbacked=group.span_text.strip() or "(empty span)",
            note=(
                "dig-down found no live source passage for "
                f"{', '.join(h for h, _rid in supporters)} — nothing minted, "
                "sentence reported unbacked"
            ),
        )

    # Verify EVERY atom (lone atoms included) against the supporter
    # passages: a conjunct the cited passage never asserts is an
    # extraction artefact — minting it would attach the wrong evidence
    # edge. Every atom must verify against at least one supporter's own
    # passage or the whole group is refused (a partial mint would
    # silently drop a conjunct of the sentence the cite run grounds).
    try:
        atom_supporters = _verify_atoms_against_supporters(
            atoms, passages, verify_batch_fn=verify_batch_fn
        )
    except RegroundingUnavailable as exc:
        return GroupPlan(
            group=group,
            action="verify-unavailable",
            supporters=supporters,
            note=(f"atom verification dispatch failed (infra, retryable): {exc}"),
        )
    refused: dict[int, str] = {
        i: "not asserted by any cited passage"
        for i, v in enumerate(atom_supporters)
        if not v
    }
    # Qualify each verified atom against the passage that verified it:
    # the claim bends one-way to what the source licenses (the qualified
    # sentence replaces the draft-span wording before the judge sees it).
    # A passage that asserts the content but declines to license it as a
    # claim statement is refused exactly like an unverified atom.
    if qualify_fn is None:
        qualify_fn = _default_qualify
    from precis.taproot.directed import QualifyUnavailable

    reworded: list[CanonicalClaim] = []
    atom_quotes: list[str | None] = []
    for i, atom in enumerate(atoms):
        if i in refused:
            reworded.append(atom)
            atom_quotes.append(None)
            continue
        passage = passages[atom_supporters[i][0]]
        if passage is None:  # pragma: no cover — verified implies a passage
            reworded.append(atom)
            atom_quotes.append(None)
            continue
        try:
            res = qualify_fn(atom.sentence, passage.text)
        except QualifyUnavailable as exc:
            return GroupPlan(
                group=group,
                action="verify-unavailable",
                supporters=supporters,
                atom_supporters=atom_supporters,
                note=(f"claim qualification dispatch failed (infra, retryable): {exc}"),
            )
        if not res.supported or res.claim is None:
            refused[i] = (
                f"passage asserts the content but will not license it as "
                f"a claim statement: {res.reason}"
            )
            reworded.append(atom)
            atom_quotes.append(None)
            continue
        reworded.append(res.claim)
        atom_quotes.append(res.quote)
    qualified = reworded
    if refused:
        bad = "; ".join(
            f"atom {i}: {atoms[i].sentence[:80]!r} ({refused[i]})"
            for i in sorted(refused)
        )
        return GroupPlan(
            group=group,
            action="unverified",
            supporters=supporters,
            atom_supporters=atom_supporters,
            dug_sources=dug_sources,
            note=(
                "atom(s) not licensed by any cited passage — group "
                f"refused, prose left as-is: {bad}"
            ),
        )

    def _place_one(claim: CanonicalClaim) -> tuple[CanonicalClaim, Placement]:
        candidates = block_fn(claim, store, embedder)
        if campaign is not None:
            candidates = candidates + _campaign_candidates(
                store, claim, campaign, seen={c.hub_ref_id for c in candidates}
            )
        judged = [(cand, judge_fn(claim.sentence, cand.claim)) for cand in candidates]
        return claim, place(claim, judged, merge_confirm_fn=merge_confirm_fn)

    atom_plans = [_place_one(atom) for atom in qualified]

    # The composite is never placed: it is the draft's bundling sentence and
    # holds no evidence edge, so it cannot become a hub here (gr720460).
    # Reporting target: the atom whose placement needs most attention.
    target_claim, target_placement = min(
        atom_plans, key=lambda cp: _ACTION_RANK.index(cp[1].action)
    )
    # An all-attach group's cite run is already known: every hub exists.
    attach_ids = [
        pl.hub_ref_id
        for _c, pl in atom_plans
        if pl.action == "attach" and pl.hub_ref_id is not None
    ]
    cite_hub_ids = (
        list(dict.fromkeys(attach_ids)) if len(attach_ids) == len(atom_plans) else []
    )

    return GroupPlan(
        group=group,
        action=target_placement.action,
        claim=target_claim,
        placement=target_placement,
        atom_plans=atom_plans,
        atom_quotes=atom_quotes,
        not_claims=extraction.not_claims,
        explains=explains,
        supporters=supporters,
        atom_supporters=atom_supporters,
        dug_sources=dug_sources,
        hub_ref_id=target_placement.hub_ref_id,
        cite_hub_ids=cite_hub_ids,
        note=target_placement.reason,
    )


def _plan_reground(
    store: Store,
    group: CiteGroup,
    fetched: list[tuple[str, int]],
    *,
    locate_fn: LocateFn,
) -> GroupPlan:
    """The default fetched-``[pa]`` action: re-ground each cited paper to its
    best supporting passage, so the ``[pa<id>]`` run rewrites to a
    ``[pc<chunk>]`` run (which the existing ``[pc]`` path then promotes).

    **All-or-nothing per contiguous run.** Each supporter locates its own chunk;
    if :data:`locate_fn` returns ``None`` for *any* supporter, the whole run is
    left as ``reground-nomatch`` with **no write** — a partial rewrite would
    collapse the run's span and erase the un-located supporter's token (draft
    chunks are append-only), the same guard as the slice-1 mixed-run rule.
    """
    targets: list[int] = []
    grounds: list[tuple[int, int, str]] = []
    for _handle, paper_ref_id in fetched:
        chunks = _read_paper_chunks(store, paper_ref_id)
        chosen = locate_fn(group.span_text, chunks) if chunks else None
        if chosen is None:
            return GroupPlan(
                group=group,
                action="reground-nomatch",
                supporters=fetched,
                note=(
                    "no passage located to ground this [pa] — re-ground by hand "
                    "onto a real body passage, or drop the cite"
                ),
            )
        targets.append(chosen[0])  # chunk_id
        grounds.append((paper_ref_id, chosen[0], chosen[2]))
    return GroupPlan(
        group=group,
        action="reground",
        supporters=fetched,
        reground_targets=targets,
        reground_grounds=grounds,
        note="re-ground [pa]→[pc] at located passage(s): "
        + "".join(f"[pc{cid}]" for cid in targets),
    )


def _plan_pa_group(
    store: Store,
    embedder: Any,
    group: CiteGroup,
    supporters: list[tuple[str, int]],
    *,
    ref_level: bool,
    extract_fn: ExtractFn,
    block_fn: BlockFn,
    judge_fn: JudgeFn,
    merge_confirm_fn: MergeConfirmFn,
    locate_fn: LocateFn,
    campaign: str | None = None,
    verify_batch_fn: VerifyBatchFn = verify_atoms_batch,
    qualify_fn: QualifyFn | None = None,
) -> GroupPlan:
    """Route a whole-paper ``[pa]`` cite-group by its cited paper(s)' state.

    * **All stubs** (0 body blocks) — ``stub-fetch-first``: an evidence
      edge would be ungroundable; no write, left for a later fetch ->
      re-ground.
    * **Mixed** (a contiguous run with some stub, some fetched) — also
      ``stub-fetch-first``, no write: the prose collapse rewrites the
      *whole* contiguous run to one ``[fi<hub>]``, so promoting only the
      fetched supporters would silently erase the stub's token (not
      recoverable) — fetch the stub(s) first, then the whole run is
      cleanly promotable/re-groundable.
    * **All fetched, default mode** — ``reground``/``reground-nomatch``:
      :func:`_plan_reground` locates the passage and rewrites ``[pa]``->
      ``[pc]`` (no hub). The arm's default.
    * **All fetched, ``ref_level=True``** — the one-step promote override: dig
      down first — :data:`LocateFn` picks each supporter's grounding passage;
      when NO supporter's paper yields a passage the sentence is **unbacked**
      (no mint, reported, prose left). Otherwise the cascade runs over the
      located passages and the evidence edge grounds at the passage that
      verified/qualified the atom — a ref-level ``[pa]`` promote never mints
      a whole-paper (``ungrounded``) edge (gr720460 dig-down ruling).
    """
    fetched = [
        (h, rid) for (h, rid) in supporters if store.chunks.count_usable_chunks(rid) > 0
    ]
    if len(fetched) < len(supporters):
        # All stubs, or a mixed run — either way skip with no write (a mixed
        # run's prose collapse would erase the un-fetched token; see docstring).
        n_stub = len(supporters) - len(fetched)
        note = (
            "whole-paper cite to an un-fetched stub (0 blocks) — fetch first"
            if not fetched
            else (
                f"mixed [pa] run: {n_stub} of {len(supporters)} cited papers "
                "un-fetched — fetch all first, then promote/re-ground"
            )
        )
        return GroupPlan(
            group=group,
            action="stub-fetch-first",
            supporters=supporters,
            note=note,
        )
    if not ref_level:
        return _plan_reground(store, group, fetched, locate_fn=locate_fn)
    # Explicit one-step promote: dig down to each supporter's grounding
    # passage BEFORE extraction (an unbacked sentence never reaches the
    # extractor — no LLM spend on a sentence no source grounds). Every
    # supporter is fetched here (a mixed run was routed to stub-fetch-first
    # above), so collapsing the whole contiguous run to one [fi<hub>] never
    # erases an un-minted token.
    passages: list[PaperChunk | None] = []
    dug_sources: list[str | None] = []
    for _handle, paper_ref_id in fetched:
        chunks = _read_paper_chunks(store, paper_ref_id)
        chosen = locate_fn(group.span_text, chunks) if chunks else None
        if chosen is None:
            passages.append(None)
            dug_sources.append(None)
            continue
        cid, ordv, text = chosen
        passages.append(
            PaperChunk(chunk_id=cid, chunk_ord=ordv, section_path=None, text=text)
        )
        dug_sources.append(f"pc{cid}")
    if not any(p is not None for p in passages):
        sentence = group.span_text.strip()
        return GroupPlan(
            group=group,
            action="unbacked",
            supporters=fetched,
            dug_sources=dug_sources,
            unbacked=sentence or "(empty span)",
            note=(
                "dig-down found no source passage in "
                f"{', '.join(h for h, _rid in fetched)} — nothing minted, "
                "sentence reported unbacked"
            ),
        )
    return _run_cascade(
        store,
        embedder,
        group,
        fetched,
        extract_fn=extract_fn,
        block_fn=block_fn,
        judge_fn=judge_fn,
        merge_confirm_fn=merge_confirm_fn,
        passages=passages,
        dug_sources=dug_sources,
        campaign=campaign,
        verify_batch_fn=verify_batch_fn,
        qualify_fn=qualify_fn,
    )


def _plan_group(
    store: Store,
    embedder: Any,
    group: CiteGroup,
    *,
    extract_fn: ExtractFn,
    block_fn: BlockFn,
    judge_fn: JudgeFn,
    merge_confirm_fn: MergeConfirmFn,
    locate_fn: LocateFn = _default_locate,
    ref_level: bool = False,
    chunk_kind: str | None = None,
    campaign: str | None = None,
    verify_batch_fn: VerifyBatchFn = verify_atoms_batch,
    qualify_fn: QualifyFn | None = None,
) -> GroupPlan:
    """Resolve → route for ONE cite-group (read-only).

    Shared by :func:`plan_chunk` (dry-run) and :func:`apply_chunk`; the
    latter calls it per-group *after* minting the previous group's hub, so a
    later group's ``block`` ANN sees hubs earlier groups in the same chunk
    just minted (intra-chunk convergence). A ``pc`` group runs the cascade
    directly; a ``pa`` group routes through :func:`_plan_pa_group`
    (stub-skip / re-ground / ref-level promote). ``chunk_kind == "figure"``
    with a pointer-only span (gripe 450329) short-circuits to a no-claim
    plan before either arm runs — ``extract_fn``/``locate_fn`` are never
    called for it.
    """
    from precis.taproot.authoring import resolve_paper_ref_id

    # Resolve supporters first — an unresolvable handle means we can't
    # ground the claim, so skip the group rather than mint an evidence-less
    # hub (mirrors seed_claim_hub's paper-sourced invariant).
    supporters: list[tuple[str, int]] = []
    unresolved: list[str] = []
    for handle in group.handles:
        try:
            supporters.append((handle, resolve_paper_ref_id(store, handle)))
        except BadInput:
            unresolved.append(handle)
    if not supporters:
        return GroupPlan(
            group=group,
            action="unresolved",
            note=f"no handle resolved to a paper: {unresolved}",
        )

    if chunk_kind == "figure" and _is_figure_pointer_span(group.span_text):
        # A caption's image-reuse trail — third-party: n/a —
        # ("Reproduced from"/"Adapted from"/"Data from" + [pc/pa<id>])
        # is provenance, not a claim about the world — forced no-claim,
        # mechanically, before extract_fn (or a [pa] group's locate_fn)
        # ever sees it.
        return GroupPlan(
            group=group,
            action="no-claim",
            supporters=supporters,
            note="figure caption provenance pointer, not a claim — skipped",
        )

    if group.kind == "pa":
        # An unresolved handle inside a contiguous [pa] run poisons the WHOLE
        # run: a pa rewrite replaces the run's span as a unit (re-ground →
        # [pc…] per supporter, or ref-level → one [fi]), so a shrunk supporter
        # list would silently erase the unresolved token from the append-only
        # chunk — the slice-1 mixed-run erasure class, via an unresolved handle.
        # Skip the run (never a partial rewrite). (The [pc] path below keeps its
        # existing promote-collapse: a broken pc drops with no citeable loss.)
        if len(supporters) < len(group.handles):
            n_unres = len(group.handles) - len(supporters)
            return GroupPlan(
                group=group,
                action="unresolved",
                supporters=supporters,
                note=(
                    f"{n_unres} of {len(group.handles)} [pa] handle(s) in this run "
                    f"didn't resolve to a paper: {unresolved} — run skipped (no "
                    "partial rewrite). Fix the handle(s), then re-run."
                ),
            )
        return _plan_pa_group(
            store,
            embedder,
            group,
            supporters,
            ref_level=ref_level,
            extract_fn=extract_fn,
            block_fn=block_fn,
            judge_fn=judge_fn,
            merge_confirm_fn=merge_confirm_fn,
            locate_fn=locate_fn,
            campaign=campaign,
            verify_batch_fn=verify_batch_fn,
            qualify_fn=qualify_fn,
        )

    # [pc] arm: the handle names its grounding chunk outright, so a title/
    # author front-matter chunk can arrive here directly — hand-written, or
    # left in the prose by an earlier run's re-ground (before the candidate
    # pool was filtered). Drop those supporters, mirroring the promote-collapse
    # for a broken pc above (the group collapses to one [fi] either way, so no
    # citeable loss); when that empties the list there is nothing left to
    # ground, so skip rather than mint an evidence-less hub (gripe 245842).
    prose_less = _ungroundable_handles(store, [h for h, _ in supporters])
    if prose_less:
        supporters = [(h, rid) for h, rid in supporters if h not in prose_less]
        if not supporters:
            return GroupPlan(
                group=group,
                action="ungroundable",
                note=(
                    "every [pc] supporter names a chunk with no groundable "
                    f"prose (title/author front matter): {sorted(prose_less)} "
                    "— group skipped, prose left as [pc…]. Re-ground onto the "
                    "paper's body passage."
                ),
            )

    return _run_cascade(
        store,
        embedder,
        group,
        supporters,
        extract_fn=extract_fn,
        block_fn=block_fn,
        judge_fn=judge_fn,
        merge_confirm_fn=merge_confirm_fn,
        campaign=campaign,
        verify_batch_fn=verify_batch_fn,
        qualify_fn=qualify_fn,
    )


def plan_chunk(
    store: Store,
    embedder: Any,
    chunk_id: int,
    *,
    ref_level: bool = False,
    extract_fn: ExtractFn = extract_claim_strict,
    block_fn: BlockFn = block,
    judge_fn: JudgeFn = dedup_judge,
    merge_confirm_fn: MergeConfirmFn = _MERGE_CONFIRM_DEFAULT,
    locate_fn: LocateFn = _default_locate,
    campaign: str | None = None,
    verify_batch_fn: VerifyBatchFn = verify_atoms_batch,
    qualify_fn: QualifyFn | None = None,
) -> ChunkBackfill:
    """Plan the backfill of one draft chunk — writes **nothing**.

    For each paper cite-group: resolve its supporter papers, then route by
    kind. A ``[pc]`` group extracts the claim (empty extraction ->
    ``no-claim``, prose left as-is), digs down to the cited passage,
    verifies and qualifies each atom against it, then runs the
    canonicalizer cascade (``block`` -> ``dedup_judge`` -> ``place``) once
    per qualified atom to decide **attach** vs mint ``new``. A ``[pa]`` group
    classifies by block-count: stub -> ``stub-fetch-first``, fetched ->
    ``reground`` (locate the passage, rewrite ``[pa]``->``[pc]``;
    ``reground-nomatch`` if none found) unless ``ref_level`` promotes it
    directly — digging down first, reporting the sentence ``unbacked`` when
    no source passage exists. LLM- and embedder-bearing — inherent, since
    neither convergence nor the grounding passage can be known without the
    ANN + judge/locate/qualify dispatches.
    """
    text, draft_ref_id, chunk_kind = _read_draft_chunk(store, chunk_id)
    plans = [
        _plan_group(
            store,
            embedder,
            group,
            ref_level=ref_level,
            extract_fn=extract_fn,
            block_fn=block_fn,
            judge_fn=judge_fn,
            merge_confirm_fn=merge_confirm_fn,
            locate_fn=locate_fn,
            chunk_kind=chunk_kind,
            campaign=campaign,
            verify_batch_fn=verify_batch_fn,
            qualify_fn=qualify_fn,
        )
        for group in segment_cite_groups(text)
    ]
    return ChunkBackfill(chunk_id=chunk_id, draft_ref_id=draft_ref_id, plans=plans)


def _file_review_todo(
    store: Store,
    claim: CanonicalClaim,
    placement: Placement,
    *,
    chunk_id: int,
    set_by: ActorSlug,
) -> None:
    """A ``kind='todo'`` for a risky (``needs_review``) merge the backfill
    declined to auto-apply — mirrors
    :func:`precis.workers.chase._file_taproot_review_todo` but keyed on the
    draft chunk instead of a finding. Its own ``store.tx()`` (no hub/edge
    write on this path to stay atomic with).

    Filed once per (chunk, claim): a group left as ``[pc…]`` because an atom
    needs review is re-planned on every run, so an open todo for the same
    draft chunk and claim sentence is left alone rather than duplicated. A
    closed one does not block a new filing (the merge was ruled on, and it
    is risky again)."""
    from precis.store.types import Tag

    title = f"taproot: review backfill merge for draft chunk dc{chunk_id}"
    with store.tx() as c:
        open_already = c.execute(
            "SELECT 1 FROM refs r "
            "JOIN ref_tags rt ON rt.ref_id = r.ref_id "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE r.kind = 'todo' AND r.retired_at IS NULL "
            "AND t.namespace = 'STATUS' AND t.value = 'open' "
            "AND r.meta->>'source' = 'taproot:backfill' "
            "AND r.meta->>'draft_chunk' = %s "
            "AND r.meta->>'claim_sentence' = %s LIMIT 1",
            (f"dc{chunk_id}", claim.sentence),
        ).fetchone()
        if open_already is not None:
            return
        todo = store.insert_ref(
            kind="todo",
            slug=None,
            title=title[:200],
            meta={
                "source": "taproot:backfill",
                "draft_chunk": f"dc{chunk_id}",
                "claim_sentence": claim.sentence,
                "placement_reason": placement.reason,
                "candidate_hub_ref_id": placement.hub_ref_id,
            },
            conn=c,
        )
        store.add_tag(
            todo.id,
            Tag.closed("STATUS", "open"),
            set_by=set_by,
            replace_prefix=True,
            conn=c,
        )


# ── apply (writes: mint/converge hubs + rewrite prose) ─────────────────────


def _fi_run(hub_ids: list[int]) -> str:
    """The prose cite run for a group's hubs: one ``[fi<hub>]`` each."""
    return "".join(f"[fi{h}]" for h in hub_ids)


def _record_reground_citations(
    store: Store, plan: GroupPlan, *, set_by: ActorSlug
) -> int:
    """Persist the claim-passage binding a re-ground already proved.

    :func:`_default_locate`'s Tier.MEDIUM confirm proves the group's span
    is supported by the chosen chunk, but the plan keeps only
    ``chunk_id`` — the rewritten ``[pc]`` becomes a bare pointer. Mint one
    ``kind='citation'`` record per located supporter to hold that missing
    rung: claim = the cite-group span, source = the located passage.

    No ``verifier_confidence`` — locate returns a decision, not a score.
    ``source_quote`` is the whole located chunk, not a pinpointed excerpt
    — narrowing to verbatim words is :mod:`precis.taproot.reground`'s job.

    Tagged (open) ``origin:draft-backfill``, mirroring the ``meta.origin``
    fingerprint on evidence edges — not the closed ``ORIGIN:`` axis
    (provenance vocabulary fenced out of default search, which would hide
    these records from readers who need them).

    Best-effort and isolated: an audit record is never a precondition for
    the prose rewrite — a failure is noted on the plan, the batch
    continues. Returns the number of records minted.
    """
    from precis.quest.citation_mint import mint_citation

    claim = plan.group.span_text.strip()
    if not claim:
        return 0
    minted = 0
    for paper_ref_id, chunk_id, chunk_text in plan.reground_grounds:
        try:
            mint_citation(
                store,
                claim=claim,
                paper_ref_id=paper_ref_id,
                source_handle=f"pc{chunk_id}",
                source_quote=chunk_text,
                tags=["origin:draft-backfill"],
                set_by=set_by,
            )
        except Exception as exc:
            plan.note += f" (citation record failed for pc{chunk_id}: {exc})"
        else:
            minted += 1
    return minted


def apply_chunk(
    store: Store,
    embedder: Any,
    draft_handler: Any,
    chunk_id: int,
    *,
    set_by: ActorSlug = "agent",
    ref_level: bool = False,
    extract_fn: ExtractFn = extract_claim_strict,
    block_fn: BlockFn = block,
    judge_fn: JudgeFn = dedup_judge,
    merge_confirm_fn: MergeConfirmFn = _MERGE_CONFIRM_DEFAULT,
    locate_fn: LocateFn = _default_locate,
    mint_cap: MintCap | None = None,
    campaign: str | None = None,
    verify_batch_fn: VerifyBatchFn = verify_atoms_batch,
    qualify_fn: QualifyFn | None = None,
) -> ChunkBackfill:
    """Apply the backfill: mint/converge each claim hub through the cascade,
    attach its supporter papers as evidence, then rewrite the chunk prose
    ``[pc…]``/``[pa…]`` -> ``[fi<hub>]`` via the draft edit door.

    ``ref_level`` (threaded to :func:`_plan_group`) selects the ``[pa]``
    arm's one-step promote: without it a fetched ``[pa]`` group is
    **re-grounded** (``locate_fn`` picks the passage, rewrites to
    ``[pc<chunk>]``, no hub yet); with it the fetched ``[pa]`` promotes
    directly to ``[fi<hub>]`` — but only after the dig-down locates a source
    passage, and the evidence edge grounds at that passage (a whole-paper
    ``ungrounded`` edge is never minted; a sentence no passage grounds is
    reported ``unbacked``). A stub ``[pa]`` is always skipped.

    The prose rewrite goes through ``draft_handler.edit``
    (:meth:`precis.store._draft_ops.DraftOps.edit_text`) — an **in-place**
    UPDATE, deliberately: draft chunks aren't append-only body chunks, so
    the handle (and every ``[dc<id>]`` reference) survives an edit, and the
    ``content_sha`` bump re-derives the embedding via the worker's
    staleness check. A group mapping to no hub leaves its ``[pc…]``
    untouched.

    Idempotent: a re-run converges onto the hubs the first run minted
    (``attach``), evidence edges skip-if-present, and the prose already
    reads ``[fi…]`` — a no-op second pass.

    Attribution: evidence edges carry ``meta.origin='draft-backfill'`` —
    the fingerprint separating a backfill edge from chase's
    (``set_by='chase'``) and hand-mint's, though all three share the
    ``set_by='agent'`` actor and fill the same claim graph.

    **Decomposition**: each group's writes go through
    :func:`precis.taproot.hub.apply_extraction`, not a single
    ``apply_placement`` — atoms mint/converge + attach the primary
    supporter; remaining supporters attach ``corroborates`` to **every**
    atom. No composite is passed, so no hub lands without its own evidence
    edge (module docstring, "No composite hub"). The cite run rewrites to
    one ``[fi<hub>]`` per atom hub, and only when **every** atom landed: an
    atom held for review leaves the prose as ``[pc…]`` rather than a cite
    that covers part of the sentence.
    """
    from precis.taproot.hub import _DEFAULT_ROLE, apply_extraction, attach_evidence

    text, draft_ref_id, chunk_kind = _read_draft_chunk(store, chunk_id)

    def _todo_fn(claim: CanonicalClaim, placement: Placement) -> None:
        _file_review_todo(store, claim, placement, chunk_id=chunk_id, set_by=set_by)

    def _edge_meta(handle: str, *, arm: str | None = None) -> dict[str, Any]:
        # A pc handle names its chunk, so the edge grounds at its passage; a
        # pa-arm dug handle IS the located pc<chunk_id>, so it grounds too —
        # `arm` carries the fingerprint of which arm minted the edge when the
        # handle alone can't say (a dug source is a pc handle from the pa arm).
        # No support/caveats: a mechanical draft-citation edge carries no
        # verdict (nothing read the passage against the claim), so it is born
        # withheld behind nanopub.preflight.withheld_edges until a verifier
        # certifies it.
        return {
            "source_handle": handle,
            "origin": "draft-backfill",
            "arm": arm or ("pa" if handle.startswith("pa") else "pc"),
            "draft_chunk": f"dc{chunk_id}",
        }

    def _supporter_edge(plan: GroupPlan, s_idx: int) -> tuple[int, dict[str, Any]]:
        """``(paper_ref_id, edge meta)`` for supporter ``s_idx`` — the pa
        arm's dug ``pc<chunk_id>`` source (grounded at the located passage)
        when one exists, else the supporter's own handle."""
        handle, paper_ref_id = plan.supporters[s_idx]
        dug = plan.dug_sources[s_idx] if plan.dug_sources is not None else None
        arm = "pa" if plan.group.kind == "pa" else None
        return paper_ref_id, _edge_meta(dug or handle, arm=arm)

    plans: list[GroupPlan] = []
    # (start, end, replacement) edits, applied right-to-left so offsets hold.
    edits: list[tuple[int, int, str]] = []

    for group in segment_cite_groups(text):
        # Re-derive per-group HERE (not batched) so this group's block() ANN
        # sees any hub an earlier group in this same chunk just minted.
        plan = _plan_group(
            store,
            embedder,
            group,
            ref_level=ref_level,
            extract_fn=extract_fn,
            block_fn=block_fn,
            judge_fn=judge_fn,
            merge_confirm_fn=merge_confirm_fn,
            locate_fn=locate_fn,
            chunk_kind=chunk_kind,
            campaign=campaign,
            verify_batch_fn=verify_batch_fn,
            qualify_fn=qualify_fn,
        )
        plans.append(plan)
        if plan.action == "reground":
            # A cite refinement, not a promote: rewrite the whole [pa…] run to
            # the located [pc<chunk>] sequence (one pc per pa, same count — the
            # [pc] path folds them to a hub on a later run). No hub minted here.
            cites = plan.group.pc_cites
            if len(plan.reground_targets) != len(cites):
                # Invariant backstop (should be unreachable — the planner routes
                # a run with any unresolved/unlocated cite to a no-write skip): a
                # count mismatch would collapse N tokens into <N and erase one.
                plan.action = "error"
                plan.note = (
                    f"reground count mismatch: {len(plan.reground_targets)} "
                    f"target(s) for {len(cites)} cite(s) — run skipped, prose "
                    "left as [pa…]"
                )
                continue
            replacement = "".join(f"[pc{cid}]" for cid in plan.reground_targets)
            edits.append((cites[0].start, cites[-1].end, replacement))
            plan.note = f"re-grounded [pa]→{replacement}"
            _record_reground_citations(store, plan, set_by=set_by)
            continue
        if not plan.atom_plans:
            # no-claim / unresolved / ungroundable / stub-fetch-first /
            # reground-nomatch — prose left untouched ([pc…] or [pa…]).
            continue

        # Per-chunk mint cap (gr204874): refuse a group whose NEW atom hubs
        # would push its primary grounding chunk past the per-run limit.
        # Pass one MintCap across apply_chunk calls to span a whole run;
        # the default is fresh per call (this draft chunk's run).
        if mint_cap is None:
            mint_cap = MintCap()
        n_new = sum(
            1 for _c, pl in plan.atom_plans if pl.action in ("new", "new_contradicts")
        )
        if not mint_cap.admit(plan.supporters[0][0], n_new):
            plan.action = "mint-capped"
            plan.note = (
                f"{plan.supporters[0][0]} already seeded {mint_cap.limit} new "
                "hub(s) this run - group skipped, prose left as [pc…]"
            )
            continue

        # Isolate each group's writes: a mid-loop failure (transient DB /
        # LLM error) on one group must not abort the batch or strand the
        # earlier groups' prose rewrites — those are applied below regardless.
        # hub mint + evidence commit per call (own tx per atom);
        # full mint-and-prose atomicity isn't available through the draft
        # edit door, so on failure we rewrite prose only for the groups whose
        # target hubs actually landed, and a re-run converges onto them
        # (idempotent) — including any atom that landed before a later
        # atom in the same call raised.
        #
        # ``hub_landed`` — not ``plan.hub_ref_id`` — is the "did this call's
        # write commit?" signal: for ``attach``, ``plan.hub_ref_id`` is
        # already populated at plan time (the ANN-matched candidate, set in
        # ``_run_cascade`` before any write runs), so it's non-None even when
        # ``apply_extraction`` below raises. Gating the except-path skip on
        # ``plan.hub_ref_id`` would append an ``[fi<hub>]`` cite for a hub
        # whose evidence edge never landed this call — silent draft
        # corruption. ``hub_landed`` only flips True once ``apply_extraction``
        # has actually returned with a target hub id.
        # Per-atom evidence scoping (plan.atom_supporters set — every group
        # that reaches the write passed the dig-down gate, so this is the
        # only live path): atom i's primary edge goes to the FIRST supporter
        # whose own passage verified it, and each other verified supporter
        # corroborates that atom only — an atom never gets an edge from a
        # passage that doesn't assert it. The pa arm's dug passage names its
        # pc<chunk_id> on the edge so the edge grounds at the located chunk.
        atom_papers: list[tuple[int, dict[str, Any] | None] | None] | None = (
            [_supporter_edge(plan, v[0]) for v in plan.atom_supporters]
            if plan.atom_supporters is not None
            else None
        )
        hub_landed = False
        try:
            outcome = apply_extraction(
                store,
                atoms=plan.atom_plans,
                composite=None,
                explains=plan.explains,
                origin=f"dc{chunk_id}",
                paper_ref_id=plan.supporters[0][1],
                meta=_edge_meta(plan.supporters[0][0]),
                set_by=set_by,
                todo_fn=_todo_fn,
                atom_papers=atom_papers,
            )
            if len(outcome.atom_hub_index) < len(plan.atom_plans):
                # needs_review — todo(s) filed, prose left as [pc…]. Sibling
                # atoms that landed keep their hub and evidence edge; a
                # re-run converges onto them.
                plan.note = "risky merge — filed for review, prose left as [pc…]"
                continue
            plan.cite_hub_ids = list(dict.fromkeys(outcome.atom_hub_ids))
            plan.hub_ref_id = plan.cite_hub_ids[0]
            plan.explains_skipped = outcome.explains_skipped
            hub_landed = True
            # Remaining supporters → corroborating evidence: only the
            # supporters whose own passage verified that atom (the first
            # already carried its primary edge).
            for atom_idx, atom_hub_id in outcome.atom_hub_index.items():
                extra = (
                    [j for j in plan.atom_supporters[atom_idx][1:]]
                    if plan.atom_supporters is not None
                    else list(range(1, len(plan.supporters)))
                )
                for s_idx in extra:
                    paper_ref_id, meta = _supporter_edge(plan, s_idx)
                    attach_evidence(
                        store,
                        hub_ref_id=atom_hub_id,
                        paper_ref_id=paper_ref_id,
                        role=_DEFAULT_ROLE,
                        meta=meta,
                        set_by=set_by,
                    )
        except Exception as exc:  # isolate one group, keep the batch going
            if not hub_landed:
                plan.action = "error"
                plan.cite_hub_ids = []  # a plan-time all-attach run: not written
                plan.note = f"write failed, prose left as [pc…]: {exc}"
                continue  # no hub write landed on this call — nothing to point prose at
            # The hub and its primary-supporter edge committed; a later
            # supporter attach raised. The prose still points at the landed
            # hub (a re-run attaches the missing supporters, skip-if-present),
            # so the report must say so rather than claim the prose was left.
            plan.action = "partial"
            plan.note = (
                f"hub landed, prose rewritten to {_fi_run(plan.cite_hub_ids)}, "
                f"but a supporter evidence edge failed — re-run to attach it: {exc}"
            )

        # Replace the whole contiguous same-kind run (cites + inter-cite
        # whitespace) with one [fi<hub>] per landed atom hub in one
        # span-replace — no leftover "" edits, no chunk-wide cleanup regex
        # (which corrupted unrelated markdown). Works identically for a
        # [pc…] run and a ref-level [pa…] run.
        cites = plan.group.pc_cites
        edits.append((cites[0].start, cites[-1].end, _fi_run(plan.cite_hub_ids)))

    result = ChunkBackfill(chunk_id=chunk_id, draft_ref_id=draft_ref_id, plans=plans)
    if not edits:
        return result

    new_text = text
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        new_text = new_text[:start] + replacement + new_text[end:]

    draft_handler.edit(
        id=f"dc{chunk_id}",
        text=new_text,
        source={"authored_by": "taproot-backfill"},
    )
    result.rewritten_text = new_text
    return result
