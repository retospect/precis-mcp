"""Whole-draft taproot backfill — a draft chunk's legacy paper citations →
claim-hub cites, through the **same** canonicalizer cascade the forward chase
bridge uses.

Two citation forms are backfilled, from the two grammars in the draft prose:

* **``[pc<id>]``** (paper-**chunk**) — the legacy grounded cite. Runs the full
  cascade and rewrites ``[pc]`` → ``[fi<hub>]`` (the original arm).
* **``[pa<id>]``** (whole-**paper**) — the ``[pa]`` arm
  (the shipped taproot-draft-pa-arm proposal, git history). A ``[pa]`` cite
  is one of two
  states, classified by the cited paper's body-block count:
  - **stub** (0 blocks, un-fetched) — **skipped** ("fetch first"): an evidence
    edge would be ungroundable, and citing an unread paper as evidence is
    never minted. Routes via fetch, then re-ground.
  - **fetched** (>0 blocks) — the honest grounding is a specific passage, so
    the *default* is a ``[pa]``→``[pc]`` **re-ground**: a ``LocateFn`` (lexical
    pick + Tier.MEDIUM confirm, the arm's one LLM call) picks the supporting
    passage and the ``[pa<id>]`` run rewrites to ``[pc<chunk>]``, which the
    existing ``[pc]`` path then promotes (two-step). Only under an explicit
    ``ref_level=True`` override does a fetched ``[pa]`` instead promote
    **ref-level** (whole-paper, ``ungrounded`` per ``seed_claim_hub``'s counter)
    and rewrite ``[pa]`` → ``[fi<hub>]`` — for whole-paper claims with no single
    grounding passage.

**Grounding prose is a precondition, both arms** (gripe 245842). An evidence
edge grounded on a paper's title/author front-matter block says "this paper
exists", not "this passage supports the claim" — a vacuous "bibliography-stub"
hub. A title block is short and dense with exactly the citing span's topic
words, so it *wins* :func:`_default_locate`'s unigram overlap; filtering it out
of the candidate pool (:func:`_read_paper_chunks`) is what stops the ``[pa]``
arm re-grounding there, and :func:`_ungroundable_handles` re-checks the ``[pc]``
arm, whose handle names its chunk outright. Both degrade to a **skip** that
leaves the prose untouched (``reground-nomatch`` / ``ungroundable``), never to a
wrong grounding. The test is prose-presence, not ``ord``: an abstract is often
``ord`` 0-2 and grounds fine, and a numeric table grounds a numeric claim.

Motivation. Most claims in the corpus were first written with raw
``[pc<id>]`` / ``[pa<id>]`` paper citations, before taproot claim hubs existed.
This module walks a draft chunk's existing paper cites and, for each, runs
``extract_claim → block → dedup_judge → place → apply_extraction`` (mirroring
:func:`precis.workers.chase._taproot_bridge`) so a claim **converges onto an
existing hub** rather than minting a near-duplicate — the whole point of
routing through the cascade instead of :func:`precis.taproot.authoring.seed_claim_hub`'s
``pub_id``-only (byte-identical) convergence. It then (on write) rewrites the
prose ``[pc<id>]`` → ``[fi<hub>]``.

Two deliberate design choices, both answering "is per-sentence enough?":

* **Cite-group anchored, not per-sentence.** The ``[pc<id>]`` markers already
  partition the prose into grounded spans — a better, *deterministic*
  segmenter than a sentence splitter, and it matches the definition "a claim
  is whatever a citation grounds." A single sentence can bundle two
  independently-cited claims; a claim can span two sentences. The cite is the
  anchor. Contiguous bare pc-cites (``[pc1][pc2]``) grounding one span
  collapse to **one** hub with two evidence edges → a single ``[fi<hub>]``
  cite (backfill dedups redundant citations for free).
* **On-demand, dry-run-first, one chunk at a time.** This is NOT a corpus
  sweep. A corpus-wide converging pass belongs to a worker (reconcile with
  the hub-refine pass), gated by a quality bar and the cascade's tuned
  merge threshold — not a one-shot CLI blast over 194 chunks. The per-chunk
  human-in-the-loop *is* the safety gate; :func:`plan_chunk` (dry-run)
  writes nothing.

The extract / block / judge functions are injected (defaulting to the real
:mod:`precis.taproot.canon` ones) so the segmenter + orchestration are
unit-testable with deterministic fakes and no LLM/embedder — mirroring how
:func:`precis.taproot.canon.place` takes a ``merge_confirm_fn``.

**Decomposition (docs/backlog/taproot-atomic-claims.md).** ``extract_fn``
returns a :class:`~precis.taproot.canon.ClaimExtraction` — zero or more
AIDA-atomic claims plus an optional bundling ``compound``. Per group, the
cascade tail (``block`` → ``dedup_judge`` → ``place``) runs once per atom
*and*, when present, once for the compound (:func:`_run_cascade`); the
resulting ``(claim, Placement)`` pairs are handed to
:func:`precis.taproot.hub.apply_extraction` (the write door), which mints/
converges each atom, mints/converges the compound with **no** evidence edge,
links ``atom --conjunct-of--> compound``, and folds ``not_claims`` into the
compound's audit memo. The prose collapse always targets the compound when
one exists (``[fi<compound_hub>]``); an already-atomic extraction has no
compound (step-1 invariant) and targets its lone atom instead — the "one
cite-group, one ``[fi<hub>]``" rewrite invariant holds either way. Supporter
papers beyond the first (``plan.supporters[1:]``) attach ``corroborates`` to
**every** atom hub, never the compound (step 3) — the cited passage asserts
all conjuncts, and the edges are LLM-free, so supporters × atoms is cheap.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.store.types import ActorSlug
from precis.taproot.canon import (
    Candidate,
    CanonicalClaim,
    ClaimExtraction,
    NotClaim,
    Placement,
    Verdict,
    block,
    dedup_judge,
    extract_claim,
    merge_confirm,
    place,
)
from precis.taproot.grounding import has_grounding_prose
from precis.utils.draft_markup import strip_markers
from precis.utils.mentions import DRAFT_MARKUP_PATTERN

if TYPE_CHECKING:
    from precis.store.store import Store

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
    #: ``"error"`` — a write failed for this group (isolated; batch continues);
    #: else the cascade action: ``"attach"`` / ``"new"`` /
    #: ``"new_contradicts"`` / ``"needs_review"``.
    action: str
    #: The **target** claim/placement for this group's ``action``/
    #: ``hub_ref_id``/``note`` above — the compound's when the extraction
    #: decomposed (:attr:`compound_plan` non-``None``), else the lone atom's
    #: (an already-atomic extraction has no compound, step-1 invariant).
    #: ``None`` for no-claim / unresolved / ungroundable / stub-fetch-first /
    #: reground(-nomatch).
    claim: CanonicalClaim | None = None
    placement: Placement | None = None
    #: Every atom's ``(claim, Placement)`` pair from :func:`_run_cascade`'s
    #: per-atom cascade tail, in extraction order — handed to
    #: :func:`precis.taproot.hub.apply_extraction` as its ``atoms=`` arg.
    #: Empty for no-claim / unresolved / ungroundable / stub-fetch-first /
    #: reground(-nomatch).
    atom_plans: list[tuple[CanonicalClaim, Placement]] = field(default_factory=list)
    #: The compound's own ``(claim, Placement)`` pair, or ``None`` when the
    #: extraction didn't decompose (a lone atom has no compound).
    compound_plan: tuple[CanonicalClaim, Placement] | None = None
    #: Rejected conjuncts (:func:`extract_claim`'s ``not_claims``) — folded
    #: into the compound hub's audit memo by
    #: :func:`precis.taproot.hub.apply_extraction` (step 8). Empty when the
    #: extraction rejected nothing (including the no-decomposition case).
    not_claims: tuple[NotClaim, ...] = ()
    #: (handle, paper_ref_id) for each resolved supporter — for a fetched-``[pa]``
    #: ref-level promote, only the fetched supporters (stubs never mint evidence).
    supporters: list[tuple[str, int]] = field(default_factory=list)
    #: The hub this group's prose cite targets once acted on — the compound
    #: hub when one exists, else the lone atom hub (the "one cite-group, one
    #: ``[fi<hub>]``" collapse target). Populated at plan time for ``attach``
    #: (the matched candidate); set post-write by :func:`apply_chunk` for
    #: ``new``/``new_contradicts``. ``None`` for no-claim / needs_review /
    #: unresolved / stub-fetch-first / reground-needed (prose left untouched).
    hub_ref_id: int | None = None
    #: True for a fetched-``[pa]`` ref-level promote — the evidence edge cites
    #: the whole paper (no grounding passage), so it lands ``ungrounded``.
    ungrounded: bool = False
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
    note: str = ""


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
    def n_ungrounded(self) -> int:
        """Ref-level (whole-paper, no grounding passage) evidence edges this
        chunk's backfill landed — the ``[pa]`` arm's ``ref_level`` promotes.
        Counts only groups whose hub actually committed (``hub_ref_id`` set)."""
        return sum(1 for p in self.plans if p.ungrounded and p.hub_ref_id is not None)


# Injected cascade functions (default to the real canon ones) — keeps the
# segmenter + orchestration testable without an LLM/embedder. ``merge_confirm``
# is threaded to :func:`place` (a low-confidence ``same`` escalates to a BIG
# confirm — the same behaviour the chase bridge accepts).
ExtractFn = Callable[[str], ClaimExtraction]
BlockFn = Callable[[CanonicalClaim, Any, Any], list[Candidate]]
JudgeFn = Callable[[str, str], Verdict]
MergeConfirmFn = Callable[[str, str], Verdict]

#: Suggest the grounding passage for a ``[pa]``→``[pc]`` re-ground: given the
#: claim span and the cited paper's body chunks ``(chunk_id, ord, text)``,
#: return the chosen chunk (same tuple) or ``None`` when no passage supports the
#: span. Injected so tests run with a deterministic fake and no LLM/embedder
#: (the default reuses chase's lexical pick + Tier.MEDIUM ``_locate_chunk_in_target``
#: confirm — the arm's one LLM call, on the write/dry-run path only).
LocateFn = Callable[[str, list[tuple[int, int, str]]], tuple[int, int, str] | None]

_MERGE_CONFIRM_DEFAULT = merge_confirm

_TOKEN_RE = re.compile(r"\w+")


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
    if not tokens:
        return None  # empty/too-short span: nothing to ground against, don't guess

    def _overlap(text: str) -> int:
        return len(tokens & {w.lower() for w in _TOKEN_RE.findall(text) if len(w) > 2})

    from precis.workers._chase_llm import _locate_chunk_in_target

    best = max(chunks, key=lambda c: _overlap(c[2]))
    return _locate_chunk_in_target(
        claim=span,
        proposed=best,
        alternates=[c for c in chunks if c[0] != best[0]][:3],
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
    # ``refs.deleted_at``, not ``chunks.retired_at``) but is dead text: a
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


def segment_cite_groups(text: str) -> list[CiteGroup]:
    """Partition ``text`` into grounded cite-groups.

    Each bare paper cite (``[pc<id>]`` or ``[pa<id>]``) grounds the prose since
    the previous cite (of any kind) or the chunk start — "what this citation
    newly asserts", so this assumes the citation-follows-claim style (a prefix
    cite ``[pc1] claim`` grounds the empty span before it → no-claim → left
    as-is, never misattributed). Contiguous bare cites of the **same kind** —
    nothing but whitespace between them (``[pc1][pc2]`` / ``[pc1] [pc2]``) —
    share one span (multiple papers, one claim) and collapse to one group.

    A cite that is neither a bare pc nor a bare pa (``[fi…]``, ``[¶…]``,
    ``[§…]``, a pinned cite) is a hard boundary: it is not an anchor, but it
    **breaks contiguity**, so an anchor right after one (``…fact[fi9][pc2]``)
    starts its OWN group rather than folding back across it. A **kind switch**
    breaks contiguity the same way: a ``[pa]`` immediately after a ``[pc]``
    (``[pc1][pa2]``) never folds into the pc-run — a whole-paper cite and a
    passage cite are routed differently, so they are always separate groups.
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
        # not this claim) — cleaner input for extract_claim.
        span = _LEADING_PUNCT_RE.sub("", strip_markers(text[prev_end:start]).strip())
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


def _read_draft_chunk(store: Store, chunk_id: int) -> tuple[str, int]:
    """``(text, draft_ref_id)`` for a live draft body chunk. Read-only.

    Raises:
        BadInput: no such body chunk, or its owning ref isn't a live draft.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT c.text, c.ref_id, r.kind, r.deleted_at "
            "FROM chunks c JOIN refs r ON r.ref_id = c.ref_id "
            "WHERE c.chunk_id = %s AND c.ord >= 0 AND c.retired_at IS NULL",
            (chunk_id,),
        ).fetchone()
    if row is None:
        raise BadInput(
            f"no live draft body chunk with chunk_id={chunk_id}",
            next="pass a dc<id> handle for a live draft chunk",
        )
    text, ref_id, kind, deleted_at = row
    if kind != "draft" or deleted_at is not None:
        raise BadInput(
            f"chunk_id={chunk_id} belongs to a {kind!r} ref (ref_id={ref_id}), "
            "not a live draft",
        )
    return str(text), int(ref_id)


# ── planning (read-only, the dry-run core) ─────────────────────────────────


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
    ungrounded: bool = False,
) -> GroupPlan:
    """The shared extract → block → judge → place tail, once supporters are
    resolved. Runs the block/judge/place cascade once per atom **and**, when
    the extraction decomposed, once more for the compound
    (docs/backlog/taproot-atomic-claims.md step 2) — canon (LLM/ANN) stays
    per-claim; only the write door (:func:`precis.taproot.hub.apply_extraction`,
    called from :func:`apply_chunk`) is decomposition-aware. ``ungrounded``
    marks a ref-level ``[pa]`` promote (whole-paper edge, no grounding
    passage)."""
    extraction = extract_fn(group.span_text)
    if extraction.is_empty:
        return GroupPlan(
            group=group,
            action="no-claim",
            supporters=supporters,
            note="span asserts nothing groundable",
        )

    def _place_one(claim: CanonicalClaim) -> tuple[CanonicalClaim, Placement]:
        candidates = block_fn(claim, store, embedder)
        judged = [(cand, judge_fn(claim.sentence, cand.claim)) for cand in candidates]
        return claim, place(claim, judged, merge_confirm_fn=merge_confirm_fn)

    atom_plans = [_place_one(atom) for atom in extraction.atoms]
    compound_plan = _place_one(extraction.compound) if extraction.compound else None

    # Prose/reporting target: the compound when the extraction decomposed,
    # else the lone atom (step-1 invariant: a multi-atom extraction always
    # carries a compound, so this is never ambiguous).
    target_claim, target_placement = compound_plan or atom_plans[0]

    return GroupPlan(
        group=group,
        action=target_placement.action,
        claim=target_claim,
        placement=target_placement,
        atom_plans=atom_plans,
        compound_plan=compound_plan,
        not_claims=extraction.not_claims,
        supporters=supporters,
        hub_ref_id=target_placement.hub_ref_id,
        ungrounded=ungrounded,
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
                    "or re-run with --ref-level to promote whole-paper (ungrounded)"
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
) -> GroupPlan:
    """Route a whole-paper ``[pa]`` cite-group by its cited paper(s)' state.

    * **All stubs** (0 body blocks) — ``stub-fetch-first``: an evidence edge
      would be ungroundable and we never cite an un-fetched paper as evidence.
      No write; the ``[pa]`` prose is left for a later fetch → re-ground.
    * **Mixed** (a contiguous same-kind run with some stub, some fetched, e.g.
      ``[pa_stub][pa_fetched]``) — also ``stub-fetch-first``, **no write.** The
      prose collapse rewrites the *whole* contiguous run to one ``[fi<hub>]``,
      so promoting only the fetched supporters would silently **erase** the
      stub's token (draft chunks are append-only — irreversible). Fetch the
      stub(s) first; then the whole run is cleanly promotable/re-groundable.
    * **All fetched, default mode** — ``reground`` / ``reground-nomatch``: the
      honest grounding is a specific passage, so :func:`_plan_reground` locates
      it and rewrites ``[pa]``→``[pc]`` (no hub). This is the arm's default.
    * **All fetched, ``ref_level=True``** — the explicit whole-paper override:
      run the cascade over all (fetched) supporters and mint a ref-level
      (``ungrounded``) evidence edge, rewriting ``[pa]`` → ``[fi<hub>]``. For
      whole-paper claims with no single grounding passage.
    """
    fetched = [
        (h, rid) for (h, rid) in supporters if store.blocks.count_blocks(rid) > 0
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
    # Explicit ref-level override: every supporter is fetched here (a mixed run
    # was routed to stub-fetch-first above), so collapsing the whole contiguous
    # run to one [fi<hub>] never erases an un-minted token.
    return _run_cascade(
        store,
        embedder,
        group,
        fetched,
        extract_fn=extract_fn,
        block_fn=block_fn,
        judge_fn=judge_fn,
        merge_confirm_fn=merge_confirm_fn,
        ungrounded=True,
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
) -> GroupPlan:
    """Resolve → route for ONE cite-group (read-only).

    Shared by :func:`plan_chunk` (dry-run) and :func:`apply_chunk`; the
    latter calls it per-group *after* minting the previous group's hub, so a
    later group's ``block`` ANN sees hubs earlier groups in the same chunk
    just minted (intra-chunk convergence). A ``pc`` group runs the cascade
    directly; a ``pa`` group routes through :func:`_plan_pa_group`
    (stub-skip / re-ground / ref-level promote).
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
    )


def plan_chunk(
    store: Store,
    embedder: Any,
    chunk_id: int,
    *,
    ref_level: bool = False,
    extract_fn: ExtractFn = extract_claim,
    block_fn: BlockFn = block,
    judge_fn: JudgeFn = dedup_judge,
    merge_confirm_fn: MergeConfirmFn = _MERGE_CONFIRM_DEFAULT,
    locate_fn: LocateFn = _default_locate,
) -> ChunkBackfill:
    """Plan the backfill of one draft chunk — writes **nothing**.

    For each paper cite-group: resolve its supporter papers, then route by
    kind. A ``[pc]`` group extracts the claim (an empty
    :class:`~precis.taproot.canon.ClaimExtraction` → ``no-claim``, prose left
    as-is) and runs the canonicalizer cascade (``block`` ANN over existing
    hubs → ``dedup_judge`` → ``place``) once per atom and, if the extraction
    decomposed, once for the compound, to decide whether each **converges
    onto an existing hub** (``attach``) or would mint a ``new`` one. A
    ``[pa]`` group classifies by block-count: stub → ``stub-fetch-first``,
    fetched → ``reground`` (locate the passage, rewrite ``[pa]``→``[pc]``;
    ``reground-nomatch`` if none found) unless ``ref_level`` promotes it
    whole-paper. This is what the CLI ``--dry-run`` reports; it is LLM- and
    embedder-bearing (inherent — neither convergence nor the grounding passage
    can be known without the ANN + judge/locate).
    """
    text, draft_ref_id = _read_draft_chunk(store, chunk_id)
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
    write on this path to stay atomic with)."""
    from precis.store.types import Tag

    title = f"taproot: review backfill merge for draft chunk dc{chunk_id}"
    with store.tx() as c:
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


def _record_reground_citations(
    store: Store, plan: GroupPlan, *, set_by: ActorSlug
) -> int:
    """Persist the claim-passage binding a re-ground already proved.

    :func:`_default_locate` runs a Tier.MEDIUM confirm that the group's span is
    supported by the chosen chunk, and then the plan keeps only the ``chunk_id``
    — the proposition and the judgement are dropped, leaving the rewritten
    ``[pc]`` a bare pointer at a paragraph. ``kind='citation'``
    ([[precis-citation-help]]'s optional verification record) is exactly that
    missing rung, so mint one per located supporter: the claim is the cite-group
    span ("a claim is whatever a citation grounds"), the source is the located
    passage.

    No ``verifier_confidence`` is recorded — the locate returns a decision, not
    a score, and inventing one would misrepresent it. ``source_quote`` is the
    whole located chunk, not a pinpointed excerpt: this arm's locate confirms a
    *passage*, and narrowing to verbatim words is
    :mod:`precis.taproot.reground`'s job (its ``GroundedRecord`` validates a
    quote for uniqueness), not something to fake here.

    Marked with the lowercase **open** tag ``origin:draft-backfill``, mirroring
    the ``meta.origin`` fingerprint this module already stamps on evidence
    edges. Deliberately *not* the closed ``ORIGIN:`` axis: that vocabulary
    means "where the content came from" (``wikipedia``) and its members are
    fenced out of default search, which would hide these records from the
    readers that most need them.

    Best-effort and isolated: an audit record is never a precondition for the
    prose rewrite, so a failure is noted on the plan and the batch continues.
    Returns the number of records minted.
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
    extract_fn: ExtractFn = extract_claim,
    block_fn: BlockFn = block,
    judge_fn: JudgeFn = dedup_judge,
    merge_confirm_fn: MergeConfirmFn = _MERGE_CONFIRM_DEFAULT,
    locate_fn: LocateFn = _default_locate,
) -> ChunkBackfill:
    """Apply the backfill: mint/converge each claim hub through the cascade,
    attach its supporter papers as evidence, then rewrite the chunk prose
    ``[pc…]``/``[pa…]`` → ``[fi<hub>]`` via the draft edit door.

    ``ref_level`` (the ``[pa]`` arm's whole-paper override) is threaded to
    :func:`_plan_group`: without it a fetched ``[pa]`` group is **re-grounded**
    — :data:`locate_fn` picks the supporting passage and the ``[pa<id>]`` run is
    rewritten ``[pa]``→``[pc<chunk>]`` (no hub; the existing ``[pc]`` path
    promotes it on a later run). With ``ref_level`` the fetched ``[pa]`` is
    instead promoted to a ref-level (``ungrounded``) hub edge and its token
    rewritten to ``[fi<hub>]``. A stub ``[pa]`` is always skipped regardless.

    The prose rewrite goes through ``draft_handler.edit``, i.e.
    :meth:`precis.store._draft_ops.DraftOps.edit_text` — an **in-place**
    ``UPDATE`` that bumps ``content_sha`` and logs an ``edited`` event with
    ``prev_text``. Draft chunks are updated in place *on purpose*: the handle
    (and every ``[dc<id>]`` reference to it) survives, which a DELETE+INSERT
    would break. The sha bump is what re-derives the embedding — the worker's
    staleness check re-claims any chunk whose ``chunk_embeddings.content_sha``
    no longer matches. (The append-only rule is about *paper* body chunks,
    whose ``content_sha`` is ``NULL`` and never refreshed.) A group that maps
    to no hub (no-claim / needs_review / unresolved) leaves its ``[pc…]``
    untouched.

    Idempotent: a re-run re-derives the same claims, the cascade converges
    onto the hubs the first run minted (``attach``), evidence edges are
    skipped-if-present, and the prose already reads ``[fi…]`` so there are no
    pc-cites left to rewrite → a no-op second pass.

    Attribution: evidence edges carry ``meta.origin='draft-backfill'`` — the
    fingerprint that separates a backfill edge from the chase pilot's
    (``set_by='chase'``) and lets it be queried apart from the hand-mint
    on-ramp, with which it shares the registered ``set_by='agent'`` actor
    (``backfill`` is not a seeded actor; ``meta.origin`` carries the
    distinction instead), while all three fill the same claim graph.

    **Decomposition.** Each group's writes go through
    :func:`precis.taproot.hub.apply_extraction` (not a single
    ``apply_placement`` call): every atom mints/converges + attaches the
    primary supporter as evidence (``needs_review`` files a todo and
    contributes no hub); the compound (if any) mints/converges with **no**
    evidence edge and gets the ``not_claims`` audit memo; every successfully
    placed atom is ``conjunct-of``-linked to the compound. Remaining
    supporters (``plan.supporters[1:]``) then attach ``corroborates`` to
    **every** atom hub (never the compound). The prose rewrite target is the
    compound hub when one landed, else the lone atom hub.
    """
    from precis.taproot.hub import _DEFAULT_ROLE, apply_extraction, attach_evidence

    text, draft_ref_id = _read_draft_chunk(store, chunk_id)

    def _todo_fn(claim: CanonicalClaim, placement: Placement) -> None:
        _file_review_todo(store, claim, placement, chunk_id=chunk_id, set_by=set_by)

    def _edge_meta(handle: str) -> dict[str, Any]:
        # A pa handle names no chunk, so _grounding_chunk_ord returns None and
        # the edge lands ref-level (ungrounded) — the [pa]-arm override; a pc
        # handle grounds at its passage. `arm` fingerprints which for queries.
        # No support/caveats: a mechanical draft-citation edge carries no
        # verdict (nothing read the passage against the claim), so it is born
        # withheld behind nanopub.preflight.withheld_edges until a verifier
        # certifies it.
        return {
            "source_handle": handle,
            "origin": "draft-backfill",
            "arm": "pa" if handle.startswith("pa") else "pc",
            "draft_chunk": f"dc{chunk_id}",
        }

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
        if not plan.atom_plans and plan.compound_plan is None:
            # no-claim / unresolved / ungroundable / stub-fetch-first /
            # reground-nomatch — prose left untouched ([pc…] or [pa…]).
            continue

        # Isolate each group's writes: a mid-loop failure (transient DB /
        # LLM error) on one group must not abort the batch or strand the
        # earlier groups' prose rewrites — those are applied below regardless.
        # hub mint + evidence commit per call (own tx per atom/compound);
        # full mint-and-prose atomicity isn't available through the draft
        # edit door, so on failure we rewrite prose only for the groups whose
        # target hub actually landed, and a re-run converges onto them
        # (idempotent) — including any atom that landed before a later
        # atom/compound in the same call raised.
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
        hub_landed = False
        try:
            outcome = apply_extraction(
                store,
                atoms=plan.atom_plans,
                compound=plan.compound_plan,
                not_claims=plan.not_claims,
                paper_ref_id=plan.supporters[0][1],
                meta=_edge_meta(plan.supporters[0][0]),
                set_by=set_by,
                todo_fn=_todo_fn,
            )
            target_hub_id = (
                outcome.compound_hub_id
                if outcome.compound_hub_id is not None
                else (outcome.atom_hub_ids[0] if outcome.atom_hub_ids else None)
            )
            if (
                target_hub_id is None
            ):  # needs_review — todo(s) filed, prose left as [pc…]
                plan.note = "risky merge — filed for review, prose left as [pc…]"
                continue
            plan.hub_ref_id = target_hub_id
            hub_landed = True
            # Remaining supporter papers → corroborating evidence on EVERY
            # atom hub (the cited passage asserts all conjuncts; never the
            # compound, step 3).
            for handle, paper_ref_id in plan.supporters[1:]:
                for atom_hub_id in outcome.atom_hub_ids:
                    attach_evidence(
                        store,
                        hub_ref_id=atom_hub_id,
                        paper_ref_id=paper_ref_id,
                        role=_DEFAULT_ROLE,
                        meta=_edge_meta(handle),
                        set_by=set_by,
                    )
        except Exception as exc:  # isolate one group, keep the batch going
            plan.action = "error"
            plan.note = f"write failed, prose left as [pc…]: {exc}"
            if not hub_landed:
                continue  # no hub write landed on this call — nothing to point prose at

        # Collapse the whole contiguous same-kind run (cites + inter-cite
        # whitespace) to a SINGLE [fi<hub>] with one span-replace — no leftover
        # "" edits, no chunk-wide cleanup regex (which corrupted unrelated
        # markdown). Works identically for a [pc…] run and a ref-level [pa…] run.
        cites = plan.group.pc_cites
        edits.append((cites[0].start, cites[-1].end, f"[fi{plan.hub_ref_id}]"))

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
