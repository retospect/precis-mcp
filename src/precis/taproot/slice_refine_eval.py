"""hub_refine dry-run harness — "what would this pass attach?"

Faithfully replays ``workers/hub_refine.py``'s discover -> precheck -> verify
sequence (see that module's ``_refine_one_hub``, the reference this mirrors)
for a caller-supplied slice of claim-hub ``ref_id``s, over a real corpus, but
performs **zero writes**: no ``attach_evidence``, no ``update_ref``, no
rejection-memo mutation, no commit. It reuses the exact real read-only
primitives (``_attached_paper_ids``, ``embed_query``, ``store.search_blocks``,
``_verify_support_with_caveats``) so its verdicts are what a live pass would
have done, not an approximation.

This is a **validation harness the builder runs deliberately** (like
``precis.taproot.eval_canon``), not something the offline test gate executes.
With the real ``verify_fn`` (default) it makes one live MEDIUM-tier LLM call
per surviving candidate — real cost, run it against prod deliberately, never
from ``scripts/test``.

CLI: ``python -m precis.taproot.slice_refine_eval <ref_id> [<ref_id> ...]``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

from precis.utils.embed_query import embed_query
from precis.workers._chase_llm import _verify_support_with_caveats, is_corroborating
from precis.workers.hub_refine import (
    _META_REJECTED,
    _attached_paper_ids,
    _min_sim_default,
    _topk_default,
)

VerifyFn = Any  # Callable[..., dict[str, Any] | None] — kept loose to match _verify_support_with_caveats


@dataclass
class Candidate:
    """One candidate paper surfaced for a hub, with whatever fields its
    bucket populates (unused fields stay at their default)."""

    paper_ref_id: int
    ref_slug: str | None
    support: str | None = None
    contradicts: bool | None = None
    caveats: list[str] | None = None
    source_handle: str | None = None
    chunk_pos: int | None = None
    chunk_excerpt: str | None = None
    score: float | None = None


@dataclass
class HubEval:
    """The would-be outcome of one hub's refine pass, computed read-only."""

    hub_ref_id: int
    claim: str
    existing_edges: int | None
    would_attach: list[Candidate] = field(default_factory=list)
    would_reject: list[Candidate] = field(default_factory=list)
    skipped_attached: list[Candidate] = field(default_factory=list)
    skipped_rejected: list[Candidate] = field(default_factory=list)
    verify_failed: int = 0
    unexpected: int = 0
    discovery_skipped: bool = False

    def format(self) -> str:
        lines = [
            f"hub #{self.hub_ref_id} — {self.claim!r}",
            f"  existing edges: "
            f"{self.existing_edges if self.existing_edges is not None else '?'}",
        ]
        if self.discovery_skipped:
            lines.append("  discovery skipped (no query vector)")
            return "\n".join(lines)
        lines.append(f"  would_attach ({len(self.would_attach)}):")
        for c in self.would_attach:
            lines.append(
                f"    paper #{c.paper_ref_id} ({c.ref_slug or '-'}) "
                f"support={c.support} contradicts={c.contradicts} "
                f"caveats={c.caveats or []} "
                f"handle={c.source_handle} pos={c.chunk_pos}"
            )
        lines.append(f"  would_reject ({len(self.would_reject)}):")
        for c in self.would_reject:
            lines.append(
                f"    paper #{c.paper_ref_id} ({c.ref_slug or '-'}) "
                f"support={c.support} contradicts={c.contradicts} pos={c.chunk_pos}"
            )
        lines.append(
            f"  skipped_attached ({len(self.skipped_attached)}): "
            + ", ".join(
                f"#{c.paper_ref_id}({c.ref_slug or '-'})" for c in self.skipped_attached
            )
        )
        lines.append(
            f"  skipped_rejected ({len(self.skipped_rejected)}): "
            + ", ".join(
                f"#{c.paper_ref_id}({c.ref_slug or '-'})" for c in self.skipped_rejected
            )
        )
        lines.append(
            f"  verify_failed={self.verify_failed} unexpected={self.unexpected}"
        )
        return "\n".join(lines)


@dataclass
class SliceReport:
    """The harness's output — one :class:`HubEval` per hub that still
    existed at read time, plus a summary tally."""

    hubs: list[HubEval] = field(default_factory=list)

    def format(self) -> str:
        blocks = [h.format() for h in self.hubs]
        total_attach = sum(len(h.would_attach) for h in self.hubs)
        total_reject = sum(len(h.would_reject) for h in self.hubs)
        gained = sum(1 for h in self.hubs if h.would_attach)
        summary = (
            f"\n--- summary: {len(self.hubs)} hub(s), "
            f"{total_attach} would-attach, {total_reject} would-reject, "
            f"{gained} hub(s) gained >=1 would-attach ---"
        )
        return "\n\n".join(blocks) + summary

    def to_dict(self) -> dict[str, Any]:
        return {"hubs": [asdict(h) for h in self.hubs]}


def _fetch_hub_row(conn: Any, ref_id: int) -> tuple[str, dict[str, Any]] | None:
    """``(title, meta)`` for a live hub ref — ``None`` if it's gone.

    Mirrors ``hub_refine._fetch_hub_info`` exactly (a plain SELECT, no
    row lock — this harness never claims/mutates anything).
    """
    row = conn.execute(
        "SELECT title, meta FROM refs WHERE ref_id = %s AND deleted_at IS NULL",
        (ref_id,),
    ).fetchone()
    if row is None:
        return None
    return str(row[0] or ""), dict(row[1] or {})


def _count_existing_edges(conn: Any, hub_ref_id: int) -> int | None:
    """Count of ``corroborates``/``establishes`` evidence edges already
    landed on this hub — context for the report, not used for any
    decision (that's ``_attached_paper_ids``, the paper-id set)."""
    row = conn.execute(
        "SELECT count(*) FROM links "
        "WHERE dst_ref_id = %s AND relation IN ('corroborates', 'establishes')",
        (hub_ref_id,),
    ).fetchone()
    if row is None:
        return None
    return int(row[0])


def _eval_one_hub(
    conn: Any,
    store: Any,
    hub_ref_id: int,
    *,
    embedder: Any,
    topk: int,
    min_sim: float | None,
    verify_fn: VerifyFn,
) -> HubEval | None:
    """Read-only replay of ``hub_refine._refine_one_hub`` for one hub.

    Returns ``None`` when the hub is gone (deleted between listing and
    reading it here) — nothing to report.
    """
    info = _fetch_hub_row(conn, hub_ref_id)
    if info is None:
        return None
    title, meta = info
    claim_sentence = title.strip()
    scope = dict(meta.get("scope") or {})
    rejected: dict[str, Any] = dict(meta.get(_META_REJECTED) or {})
    attached = _attached_paper_ids(conn, hub_ref_id)
    existing_edges = _count_existing_edges(conn, hub_ref_id)

    hub_eval = HubEval(
        hub_ref_id=hub_ref_id, claim=claim_sentence, existing_edges=existing_edges
    )

    query_vec = embed_query(embedder, claim_sentence) if claim_sentence else None
    if not claim_sentence or query_vec is None:
        hub_eval.discovery_skipped = True
        return hub_eval

    candidates = store.search_blocks(
        q=claim_sentence,
        query_vec=query_vec,
        mode="semantic",
        kind="paper",
        limit=topk,
        max_distance=min_sim,
    )
    seen_papers: set[int] = set()
    for block, ref, score in candidates:
        paper_ref_id = int(ref.id)
        if paper_ref_id == hub_ref_id or paper_ref_id in seen_papers:
            continue
        seen_papers.add(paper_ref_id)

        if paper_ref_id in attached:
            hub_eval.skipped_attached.append(
                Candidate(paper_ref_id=paper_ref_id, ref_slug=ref.slug, score=score)
            )
            continue
        if str(paper_ref_id) in rejected:
            hub_eval.skipped_rejected.append(
                Candidate(paper_ref_id=paper_ref_id, ref_slug=ref.slug, score=score)
            )
            continue

        verification = verify_fn(
            claim=claim_sentence,
            scope=scope,
            target_cite_key=ref.slug or f"ref:{paper_ref_id}",
            target_chunk_ord=block.pos,
            target_chunk_text=block.text,
        )
        if verification is None:
            hub_eval.verify_failed += 1
            continue
        supports = verification.get("supports")
        contradicts = bool(verification.get("contradicts"))
        cand = Candidate(
            paper_ref_id=paper_ref_id,
            ref_slug=ref.slug,
            support=supports,
            contradicts=contradicts,
            caveats=list(verification.get("caveats") or []),
            source_handle=f"pc{block.id}",
            chunk_pos=block.pos,
            chunk_excerpt=block.text[:240],
            score=score,
        )
        # Same gate as hub_refine's write door (shared helper), so this
        # read-only replay reflects what the pass WOULD attach — a "partial"
        # flagged contradicts lands in would_reject, not would_attach.
        if is_corroborating(verification):
            hub_eval.would_attach.append(cand)
        elif supports in ("partial", "no"):
            hub_eval.would_reject.append(cand)
        else:
            hub_eval.unexpected += 1

    return hub_eval


def eval_hub_slice(
    store: Any,
    ref_ids: list[int],
    *,
    embedder: Any,
    topk: int | None = None,
    min_sim: float | None = None,
    verify_fn: VerifyFn = _verify_support_with_caveats,
    progress: bool = True,
) -> SliceReport:
    """Replay hub_refine's read path (discover -> precheck -> verify) over
    ``ref_ids``, writing nothing, and return the would-be outcomes.

    ``embedder`` is required (unlike ``hub_refine.run_hub_refine_pass``,
    which degrades to a no-op without one) — pass whatever ``make_embedder``
    returns, or ``None`` to force every hub's discovery to skip (still a
    valid, if uninteresting, report).

    ``verify_fn`` defaults to the real
    :func:`~precis.workers._chase_llm._verify_support_with_caveats` (a live
    MEDIUM-tier LLM dispatch per surviving candidate) — inject a stub for an
    offline unit test.

    ``progress`` streams one flushed line per hub to stderr as it's judged,
    mirroring :func:`~precis.taproot.eval_canon.eval_canonicalization`.
    """
    resolved_topk = topk if topk is not None else _topk_default()
    resolved_min_sim = min_sim if min_sim is not None else _min_sim_default()

    hubs: list[HubEval] = []
    total = len(ref_ids)
    for i, hub_ref_id in enumerate(ref_ids, start=1):
        with store.pool.connection() as conn:
            hub_eval = _eval_one_hub(
                conn,
                store,
                hub_ref_id,
                embedder=embedder,
                topk=resolved_topk,
                min_sim=resolved_min_sim,
                verify_fn=verify_fn,
            )
        if hub_eval is None:
            if progress:
                print(
                    f"[{i}/{total}] hub #{hub_ref_id} -- gone (skipped)",
                    file=sys.stderr,
                    flush=True,
                )
            continue
        hubs.append(hub_eval)
        if progress:
            print(
                f"[{i}/{total}] hub #{hub_ref_id} -- "
                f"attach={len(hub_eval.would_attach)} "
                f"reject={len(hub_eval.would_reject)} "
                f"skip_attached={len(hub_eval.skipped_attached)} "
                f"skip_rejected={len(hub_eval.skipped_rejected)} "
                f"failed={hub_eval.verify_failed}"
                f"{'  (discovery skipped)' if hub_eval.discovery_skipped else ''}",
                file=sys.stderr,
                flush=True,
            )

    return SliceReport(hubs=hubs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run hub_refine over a slice of claim-hub ref_ids: reports "
            "what discover -> precheck -> verify WOULD attach/reject, "
            "writing nothing."
        )
    )
    parser.add_argument("ref_ids", type=int, nargs="+", help="hub ref_id(s)")
    parser.add_argument(
        "--embedder", default="bge-m3", choices=["bge-m3", "remote", "mock"]
    )
    parser.add_argument("--topk", type=int, default=None)
    parser.add_argument("--min-sim", type=float, default=None)
    parser.add_argument(
        "--json", default=None, help="also dump the report as JSON here"
    )
    parser.add_argument("--dsn", default=None, help="database DSN (else resolved)")
    parser.add_argument(
        "--embedder-url",
        default=None,
        help="remote embedder base URL (default: config.embedder_url / "
        "PRECIS_EMBEDDER_URL)",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    from precis.cli._common import resolve_dsn
    from precis.embedder import make_embedder
    from precis.store import Store

    dsn = args.dsn if args.dsn else resolve_dsn(None)
    store = Store.connect(dsn)
    try:
        from precis.config import load_config

        embedder_url = args.embedder_url or load_config().embedder_url
        embedder = make_embedder(
            args.embedder, dim=store.embedding_dim(), url=embedder_url
        )
        # An in-process bge-m3 embedder (url=None) never loads on its own:
        # embed() fast-fails via _raise_if_warming until warmup() (or the
        # server's background thread) loads the weights. Without this, every
        # discovery here silently degrades to "no query vector" and the whole
        # slice reports 0 candidates — a meaningless empty gate. warmup() is a
        # no-op / absent on the remote HTTP embedder, so guard on hasattr.
        if hasattr(embedder, "warmup"):
            embedder.warmup()
        report = eval_hub_slice(
            store,
            args.ref_ids,
            embedder=embedder,
            topk=args.topk,
            min_sim=args.min_sim,
        )
        print(report.format())
        if args.json:
            with open(args.json, "w") as f:
                json.dump(report.to_dict(), f, indent=2, default=str)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "Candidate",
    "HubEval",
    "SliceReport",
    "eval_hub_slice",
]
