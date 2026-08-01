"""Whole-draft taproot backfill — a draft chunk's legacy paper citations →
claim-hub cites, through the **same** canonicalizer cascade the forward chase
bridge uses.

Two citation forms are backfilled, from the two grammars in the draft prose:

* **``[pc<id>]``** (paper-**chunk**) — the legacy grounded cite. Runs the full
  cascade and rewrites ``[pc]`` → ``[fi<hub>]`` (the original arm).
* **``[pa<id>]``** (whole-**paper**) — the ``[pa]`` arm
  (``docs/proposals/taproot-draft-pa-arm.md``). A ``[pa]`` cite is one of two
  states, classified by the cited paper's body-block count:
  - **stub** (0 blocks, un-fetched) — **skipped** ("fetch first"): an evidence
    edge would be ungroundable, and citing an unread paper as evidence is
    never minted. Routes via fetch, then re-ground.
  - **fetched** (>0 blocks) — the honest grounding is a specific passage, so
    the *default* is to leave it for a ``[pa]``→``[pc]`` re-ground (deferred to
    the arm's slice 2). Only under an explicit ``ref_level=True`` override does
    a fetched ``[pa]`` promote **ref-level** (whole-paper, ``ungrounded`` per
    ``seed_claim_hub``'s counter) and rewrite ``[pa]`` → ``[fi<hub>]`` — for
    whole-paper claims with no single grounding passage.

Motivation. Most claims in the corpus were first written with raw
``[pc<id>]`` / ``[pa<id>]`` paper citations, before taproot claim hubs existed.
This module walks a draft chunk's existing paper cites and, for each, runs
``extract_claim → block → dedup_judge → place → apply_placement`` (mirroring
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
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from precis.errors import BadInput
from precis.taproot.canon import (
    Candidate,
    CanonicalClaim,
    Placement,
    Verdict,
    block,
    dedup_judge,
    extract_claim,
    merge_confirm,
    place,
)
from precis.utils.draft_markup import strip_markers
from precis.utils.mentions import DRAFT_MARKUP_PATTERN

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
    #: ``"no-claim"`` — the span asserts nothing groundable (extract → None);
    #: ``"unresolved"`` — a handle didn't resolve to a paper (skipped);
    #: ``"stub-fetch-first"`` — a ``[pa]`` cite to an un-fetched stub (0 blocks),
    #: skipped pending fetch (no write, prose left as ``[pa]``);
    #: ``"reground-needed"`` — a fetched ``[pa]`` in the default mode, left for a
    #: ``[pa]``→``[pc]`` re-ground (no write) unless ``ref_level`` is set;
    #: ``"error"`` — a write failed for this group (isolated; batch continues);
    #: else the cascade action: ``"attach"`` / ``"new"`` /
    #: ``"new_contradicts"`` / ``"needs_review"``.
    action: str
    claim: CanonicalClaim | None = None
    placement: Placement | None = None
    #: (handle, paper_ref_id) for each resolved supporter — for a fetched-``[pa]``
    #: ref-level promote, only the fetched supporters (stubs never mint evidence).
    supporters: list[tuple[str, int]] = field(default_factory=list)
    #: The hub this group cites once acted on — the matched hub for
    #: ``attach``, or (after :func:`apply_chunk`) the minted hub for
    #: ``new``/``new_contradicts``. ``None`` for no-claim / needs_review /
    #: unresolved / stub-fetch-first / reground-needed (prose left untouched).
    hub_ref_id: int | None = None
    #: True for a fetched-``[pa]`` ref-level promote — the evidence edge cites
    #: the whole paper (no grounding passage), so it lands ``ungrounded``.
    ungrounded: bool = False
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
ExtractFn = Callable[[str], CanonicalClaim | None]
BlockFn = Callable[[CanonicalClaim, Any, Any], list[Candidate]]
JudgeFn = Callable[[str, str], Verdict]
MergeConfirmFn = Callable[[str, str], Verdict]

_MERGE_CONFIRM_DEFAULT = merge_confirm


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


def _read_draft_chunk(store: Any, chunk_id: int) -> tuple[str, int]:
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
    store: Any,
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
    resolved. ``ungrounded`` marks a ref-level ``[pa]`` promote (whole-paper
    edge, no grounding passage)."""
    claim = extract_fn(group.span_text)
    if claim is None:
        return GroupPlan(
            group=group,
            action="no-claim",
            supporters=supporters,
            note="span asserts nothing groundable",
        )

    candidates = block_fn(claim, store, embedder)
    judged = [(cand, judge_fn(claim.sentence, cand.claim)) for cand in candidates]
    placement = place(claim, judged, merge_confirm_fn=merge_confirm_fn)
    return GroupPlan(
        group=group,
        action=placement.action,
        claim=claim,
        placement=placement,
        supporters=supporters,
        hub_ref_id=placement.hub_ref_id,
        ungrounded=ungrounded,
        note=placement.reason,
    )


def _plan_pa_group(
    store: Any,
    embedder: Any,
    group: CiteGroup,
    supporters: list[tuple[str, int]],
    *,
    ref_level: bool,
    extract_fn: ExtractFn,
    block_fn: BlockFn,
    judge_fn: JudgeFn,
    merge_confirm_fn: MergeConfirmFn,
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
    * **All fetched, default mode** — ``reground-needed``: the honest grounding
      is a specific passage, so a fetched ``[pa]`` is left for a ``[pa]``→
      ``[pc]`` re-ground (the arm's slice 2). No write.
    * **All fetched, ``ref_level=True``** — the explicit whole-paper override:
      run the cascade over all (fetched) supporters and mint a ref-level
      (``ungrounded``) evidence edge, rewriting ``[pa]`` → ``[fi<hub>]``. For
      whole-paper claims with no single grounding passage.
    """
    fetched = [(h, rid) for (h, rid) in supporters if store.count_blocks(rid) > 0]
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
        return GroupPlan(
            group=group,
            action="reground-needed",
            supporters=fetched,
            note=(
                "fetched [pa] — re-ground to a passage [pc] first, or re-run "
                "with --ref-level to promote whole-paper (ungrounded)"
            ),
        )
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
    store: Any,
    embedder: Any,
    group: CiteGroup,
    *,
    extract_fn: ExtractFn,
    block_fn: BlockFn,
    judge_fn: JudgeFn,
    merge_confirm_fn: MergeConfirmFn,
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
    # hub (mirrors seed_claim_hub's paper-sourced invariant, ADR 0073).
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
    store: Any,
    embedder: Any,
    chunk_id: int,
    *,
    ref_level: bool = False,
    extract_fn: ExtractFn = extract_claim,
    block_fn: BlockFn = block,
    judge_fn: JudgeFn = dedup_judge,
    merge_confirm_fn: MergeConfirmFn = _MERGE_CONFIRM_DEFAULT,
) -> ChunkBackfill:
    """Plan the backfill of one draft chunk — writes **nothing**.

    For each paper cite-group: resolve its supporter papers, then route by
    kind. A ``[pc]`` group extracts the claim (``None`` → ``no-claim``, prose
    left as-is) and runs the canonicalizer cascade (``block`` ANN over
    existing hubs → ``dedup_judge`` → ``place``) to decide whether the claim
    **converges onto an existing hub** (``attach``) or would mint a ``new``
    one. A ``[pa]`` group classifies by block-count: stub → ``stub-fetch-first``,
    fetched → ``reground-needed`` unless ``ref_level`` promotes it whole-paper.
    This is what the CLI ``--dry-run`` reports; it is LLM- and embedder-bearing
    (that is inherent — convergence can't be known without the ANN + judge).
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
        )
        for group in segment_cite_groups(text)
    ]
    return ChunkBackfill(chunk_id=chunk_id, draft_ref_id=draft_ref_id, plans=plans)


def _file_review_todo(
    store: Any,
    claim: CanonicalClaim,
    placement: Placement,
    *,
    chunk_id: int,
    set_by: str,
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


def apply_chunk(
    store: Any,
    embedder: Any,
    draft_handler: Any,
    chunk_id: int,
    *,
    set_by: str = "agent",
    ref_level: bool = False,
    extract_fn: ExtractFn = extract_claim,
    block_fn: BlockFn = block,
    judge_fn: JudgeFn = dedup_judge,
    merge_confirm_fn: MergeConfirmFn = _MERGE_CONFIRM_DEFAULT,
) -> ChunkBackfill:
    """Apply the backfill: mint/converge each claim hub through the cascade,
    attach its supporter papers as evidence, then rewrite the chunk prose
    ``[pc…]``/``[pa…]`` → ``[fi<hub>]`` via the draft edit door.

    ``ref_level`` (the ``[pa]`` arm's whole-paper override) is threaded to
    :func:`_plan_group`: without it a fetched ``[pa]`` group is left
    ``reground-needed`` (prose untouched); with it the fetched ``[pa]`` is
    promoted to a ref-level (``ungrounded``) hub edge and its token rewritten
    to ``[fi<hub>]``. A stub ``[pa]`` is always skipped regardless.

    The prose rewrite goes through ``draft_handler.edit`` (a whole-chunk
    rewrite) — **never** a raw ``UPDATE`` — so the chunk's DELETE+INSERT
    embedding/summary cascade re-runs (draft body chunks are append-only;
    an in-place update leaves stale ``chunk_embeddings``). A group that maps
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
    """
    from precis.taproot.hub import _DEFAULT_ROLE, apply_placement, attach_evidence

    text, draft_ref_id = _read_draft_chunk(store, chunk_id)

    def _todo_fn(claim: CanonicalClaim, placement: Placement) -> None:
        _file_review_todo(store, claim, placement, chunk_id=chunk_id, set_by=set_by)

    def _edge_meta(handle: str) -> dict[str, Any]:
        # A pa handle names no chunk, so _grounding_chunk_ord returns None and
        # the edge lands ref-level (ungrounded) — the [pa]-arm override; a pc
        # handle grounds at its passage. `arm` fingerprints which for queries.
        return {
            "support": "yes",
            "caveats": [],
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
        )
        plans.append(plan)
        if plan.claim is None or plan.placement is None:
            # no-claim / unresolved / stub-fetch-first / reground-needed —
            # prose left untouched ([pc…] or [pa…]).
            continue

        # Isolate each group's writes: a mid-loop failure (transient DB /
        # LLM error) on one group must not abort the batch or strand the
        # earlier groups' prose rewrites — those are applied below regardless.
        # hub mint + evidence commit per call (own tx); full mint-and-prose
        # atomicity isn't available through the draft edit door, so on failure
        # we rewrite prose only for the groups whose hub actually landed, and
        # a re-run converges onto them (idempotent).
        try:
            hub_ref_id = apply_placement(
                store,
                plan.claim,
                plan.placement,
                paper_ref_id=plan.supporters[0][1],
                meta=_edge_meta(plan.supporters[0][0]),
                set_by=set_by,
                todo_fn=_todo_fn,
            )
            if hub_ref_id is None:  # needs_review — todo filed, prose left as [pc…]
                plan.note = "risky merge — filed for review, prose left as [pc…]"
                continue
            plan.hub_ref_id = hub_ref_id
            # Remaining supporter papers → corroborating evidence on the hub.
            for handle, paper_ref_id in plan.supporters[1:]:
                attach_evidence(
                    store,
                    hub_ref_id=hub_ref_id,
                    paper_ref_id=paper_ref_id,
                    role=_DEFAULT_ROLE,
                    meta=_edge_meta(handle),
                    set_by=set_by,
                )
        except Exception as exc:  # isolate one group, keep the batch going
            plan.action = "error"
            plan.note = f"write failed, prose left as [pc…]: {exc}"
            if plan.hub_ref_id is None:
                continue  # no hub landed — nothing to point prose at

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
