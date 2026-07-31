"""Whole-draft taproot backfill — a draft chunk's legacy paper-chunk
citations → claim-hub cites, through the **same** canonicalizer cascade the
forward chase bridge uses.

Motivation. Most claims in the corpus were first written with raw
``[pc<id>]`` paper-chunk citations, before taproot claim hubs existed. This
module walks a draft chunk's existing ``[pc<id>]`` cites and, for each, runs
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

#: Leading whitespace + a prior sentence's trailing terminator, trimmed off a
#: grounded span so extract_claim reads the claim, not ". "/", " residue.
_LEADING_PUNCT_RE = re.compile(r"^[\s.,;:!?)—–-]+")


@dataclass(frozen=True)
class PcCite:
    """One bare ``[pc<id>]`` citation and its exact position in the text."""

    handle: str  # "pc293"
    start: int  # offset of the "[" in the source text
    end: int  # offset just past the "]"
    raw: str  # the exact matched marker, e.g. "[pc293]"


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


@dataclass
class GroupPlan:
    """The planned outcome for one :class:`CiteGroup` — read-only until
    :func:`apply_chunk` acts on it."""

    group: CiteGroup
    #: ``"no-claim"`` — the span asserts nothing groundable (extract → None);
    #: ``"unresolved"`` — a pc handle didn't resolve to a paper (skipped);
    #: ``"error"`` — a write failed for this group (isolated; batch continues);
    #: else the cascade action: ``"attach"`` / ``"new"`` /
    #: ``"new_contradicts"`` / ``"needs_review"``.
    action: str
    claim: CanonicalClaim | None = None
    placement: Placement | None = None
    #: (pc_handle, paper_ref_id) for each resolved supporter.
    supporters: list[tuple[str, int]] = field(default_factory=list)
    #: The hub this group cites once acted on — the matched hub for
    #: ``attach``, or (after :func:`apply_chunk`) the minted hub for
    #: ``new``/``new_contradicts``. ``None`` for no-claim / needs_review /
    #: unresolved (prose is left untouched).
    hub_ref_id: int | None = None
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


def _iter_bare_pc(text: str) -> list[PcCite]:
    """Every bare ``[pc<id>]`` cite (no pin) in ``text``, in order.

    Uses the single-sourced :data:`DRAFT_MARKUP_PATTERN` so this never drifts
    from the draft grammar: a match is a candidate iff its ``bare`` group is
    ``pc<digits>`` **and** it carries no ``pin`` group (a ``[pc…+pa…]`` or a
    hub-pinned ``[fi…>pc…]`` is already-authored, skipped).
    """
    out: list[PcCite] = []
    for m in DRAFT_MARKUP_PATTERN.finditer(text):
        bare = m.groupdict().get("bare")
        if not bare or not _PC_HANDLE_RE.match(bare):
            continue
        if m.groupdict().get("pin"):
            continue  # pinned — already authored
        out.append(PcCite(handle=bare, start=m.start(), end=m.end(), raw=m.group(0)))
    return out


def segment_cite_groups(text: str) -> list[CiteGroup]:
    """Partition ``text`` into grounded cite-groups.

    Each bare pc-cite grounds the prose since the previous cite (of any kind)
    or the chunk start — "what this citation newly asserts", so this assumes
    the citation-follows-claim style (a prefix cite ``[pc1] claim`` grounds
    the empty span before it → no-claim → left as-is, never misattributed).
    Contiguous bare pc-cites — nothing but whitespace between them
    (``[pc1][pc2]`` / ``[pc1] [pc2]``) — share one span (multiple papers, one
    claim) and collapse to one group.

    A non-pc cite (``[fi…]``, ``[¶…]``, ``[§…]``) is a hard boundary: it is
    not an anchor, but it **breaks contiguity**, so a pc-cite right after one
    (``…fact[fi9][pc2]``) starts its OWN group rather than folding back across
    the fi-cite into an earlier pc-run — its span is the (here empty) text
    after the fi-cite, never a re-read across it.
    """
    # All markers (any kind) give the span boundaries; only bare pc-cites are
    # anchors. Walk markers in order; a pc-cite folds into the current group
    # ONLY when the immediately-preceding marker was also a pc-cite (an
    # unbroken pc-run) — an intervening non-pc marker resets contiguity.
    pc_by_start: dict[int, PcCite] = {c.start: c for c in _iter_bare_pc(text)}
    markers: list[tuple[int, int, bool]] = [
        (m.start(), m.end(), m.start() in pc_by_start)
        for m in DRAFT_MARKUP_PATTERN.finditer(text)
    ]

    groups: list[CiteGroup] = []
    prev_end = 0
    prev_was_pc = False
    for start, end, is_pc in markers:
        if not is_pc:
            prev_end = end
            prev_was_pc = False
            continue
        cite = pc_by_start[start]
        # The prose since the previous marker, minus the previous sentence's
        # trailing terminator (a leading ". " / ", " belongs to that sentence,
        # not this claim) — cleaner input for extract_claim.
        span = _LEADING_PUNCT_RE.sub("", strip_markers(text[prev_end:start]).strip())
        if not span and prev_was_pc and groups:
            # Unbroken pc-run (only whitespace since the last pc-cite): same
            # grounded span, another supporting paper.
            groups[-1].pc_cites.append(cite)
        else:
            groups.append(CiteGroup(span_text=span, pc_cites=[cite]))
        prev_end = end
        prev_was_pc = True
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


def _plan_group(
    store: Any,
    embedder: Any,
    group: CiteGroup,
    *,
    extract_fn: ExtractFn,
    block_fn: BlockFn,
    judge_fn: JudgeFn,
    merge_confirm_fn: MergeConfirmFn,
) -> GroupPlan:
    """Resolve → extract → cascade for ONE cite-group (read-only).

    Shared by :func:`plan_chunk` (dry-run) and :func:`apply_chunk`; the
    latter calls it per-group *after* minting the previous group's hub, so a
    later group's ``block`` ANN sees hubs earlier groups in the same chunk
    just minted (intra-chunk convergence).
    """
    from precis.taproot.authoring import resolve_paper_ref_id

    # Resolve supporters first — an unresolvable pc handle means we can't
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
            note=f"no pc handle resolved to a paper: {unresolved}",
        )

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
        note=placement.reason,
    )


def plan_chunk(
    store: Any,
    embedder: Any,
    chunk_id: int,
    *,
    extract_fn: ExtractFn = extract_claim,
    block_fn: BlockFn = block,
    judge_fn: JudgeFn = dedup_judge,
    merge_confirm_fn: MergeConfirmFn = _MERGE_CONFIRM_DEFAULT,
) -> ChunkBackfill:
    """Plan the backfill of one draft chunk — writes **nothing**.

    For each pc-cite group: resolve its supporter papers, extract the claim
    (``None`` → ``no-claim``, prose left as-is), then run the canonicalizer
    cascade (``block`` ANN over existing hubs → ``dedup_judge`` → ``place``)
    to decide whether the claim **converges onto an existing hub**
    (``attach``) or would mint a ``new`` one. This is what the CLI
    ``--dry-run`` reports; it is LLM- and embedder-bearing (that is inherent
    — convergence can't be known without the ANN + judge).
    """
    text, draft_ref_id = _read_draft_chunk(store, chunk_id)
    plans = [
        _plan_group(
            store,
            embedder,
            group,
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
    extract_fn: ExtractFn = extract_claim,
    block_fn: BlockFn = block,
    judge_fn: JudgeFn = dedup_judge,
    merge_confirm_fn: MergeConfirmFn = _MERGE_CONFIRM_DEFAULT,
) -> ChunkBackfill:
    """Apply the backfill: mint/converge each claim hub through the cascade,
    attach its supporter papers as evidence, then rewrite the chunk prose
    ``[pc…]`` → ``[fi<hub>]`` via the draft edit door.

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
        return {
            "support": "yes",
            "caveats": [],
            "source_handle": handle,
            "origin": "draft-backfill",
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
            extract_fn=extract_fn,
            block_fn=block_fn,
            judge_fn=judge_fn,
            merge_confirm_fn=merge_confirm_fn,
        )
        plans.append(plan)
        if plan.claim is None or plan.placement is None:
            continue  # no-claim / unresolved — prose left as [pc…]

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

        # Collapse the whole contiguous pc-run (cites + inter-cite whitespace)
        # to a SINGLE [fi<hub>] with one span-replace — no leftover "" edits,
        # no chunk-wide cleanup regex (which corrupted unrelated markdown).
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
