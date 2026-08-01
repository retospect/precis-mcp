"""``precis taproot mint`` — mint claim hubs from a cite-seeded JSON spec.

    precis taproot mint --spec spec.json
    precis taproot mint --json '[{"sentence": "...", "scope": {}, \
"supporters": [{"paper": "pa5", "role": "corroborates"}]}]'
    precis taproot mint --spec spec.json --dry-run
    precis taproot mint --spec spec.json --format json

A thin CLI skin over :func:`precis.taproot.authoring.seed_claim_hub`, itself
a helper over the existing taproot write door (:mod:`precis.taproot.hub`) —
for a human/backfill to mint a claim hub from a draft's already-existing
paper-chunk citations, not a second write path.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from precis.cli._common import resolve_dsn


def add_parser(subparsers: Any) -> None:
    """Register the ``taproot`` subcommand group (``mint`` / ``refine`` /
    ``backfill``)."""
    p = subparsers.add_parser(
        "taproot", help="Taproot claim-hub authoring (mint hubs from citations)."
    )
    tsub = p.add_subparsers(dest="taproot_cmd", required=True)

    m = tsub.add_parser(
        "mint",
        help="Mint/attach claim hubs from a JSON spec of cited claims.",
    )
    spec_group = m.add_mutually_exclusive_group(required=True)
    spec_group.add_argument("--spec", help="Path to a JSON spec file.")
    spec_group.add_argument(
        "--json", dest="json_spec", help="Inline JSON spec (same shape as --spec)."
    )
    m.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve papers + print what WOULD be minted/attached; write nothing.",
    )
    m.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    m.add_argument(
        "--set-by",
        default="agent",
        help="set_by actor slug for the writes (default: agent).",
    )
    m.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")

    r = tsub.add_parser(
        "refine",
        help="Link one claim hub as a sharper/reworded version of another "
        "(mint the sharper claim first, then refine).",
    )
    r.add_argument(
        "--from",
        dest="from_hub",
        required=True,
        help="Sharper/newer claim hub (fi<id> handle, pub_id, or ref_id).",
    )
    r.add_argument(
        "--to",
        dest="to_hub",
        required=True,
        help="Coarser/original claim hub this one refines (same forms).",
    )
    r.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve both hubs + print what WOULD be linked; write nothing.",
    )
    r.add_argument(
        "--set-by",
        default="agent",
        help="set_by actor slug for the write (default: agent).",
    )
    r.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")

    b = tsub.add_parser(
        "backfill",
        help="Convert a draft chunk's legacy [pc<id>]/[pa<id>] cites into "
        "claim-hub [fi<id>] cites, converging onto existing hubs (dry-run by "
        "default). A fetched whole-paper [pa] is re-grounded to a passage "
        "[pc] (then promotable); stub [pa] cites are skipped (fetch first).",
    )
    b_target = b.add_mutually_exclusive_group(required=True)
    b_target.add_argument("--chunk", help="One draft chunk handle, e.g. dc1652005.")
    b_target.add_argument(
        "--draft", help="A draft slug — backfill every body chunk in it."
    )
    b.add_argument(
        "--apply",
        action="store_true",
        help="Mint/converge hubs + rewrite prose. Default (omitted) is a "
        "read-only dry-run that writes nothing.",
    )
    b.add_argument(
        "--ref-level",
        action="store_true",
        help="[pa] arm: promote a FETCHED whole-paper [pa<id>] cite ref-level "
        "(ungrounded, no passage) and rewrite it to [fi<hub>]. Default (omitted) "
        "re-grounds a fetched [pa]->[pc] at the located passage (no hub yet); "
        "stub [pa] cites are always skipped either way.",
    )
    b.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    b.add_argument(
        "--set-by",
        default="agent",
        help="set_by actor slug for the writes (default: agent; edges are "
        "fingerprinted by meta.origin='draft-backfill', distinct from the "
        "chase pilot's set_by='chase').",
    )
    b.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")

    bg = tsub.add_parser(
        "backfill-grounding",
        help="Upgrade ref-level taproot/draft citation edges to chunk-grounded "
        "(pc/dc).",
    )
    bg.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what WOULD be grounded/resynced; write nothing.",
    )
    bg.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    bg.add_argument(
        "--database-url", default=None, help="Override PRECIS_DATABASE_URL."
    )


def _load_spec(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.spec:
        with open(args.spec, encoding="utf-8") as f:
            raw = json.load(f)
    else:
        raw = json.loads(args.json_spec)
    if not isinstance(raw, list):
        print("taproot mint: spec must be a JSON array of claims", file=sys.stderr)
        sys.exit(2)
    return raw


def _dry_run_result(store: Any, entry: dict[str, Any]) -> dict[str, Any]:
    from precis.identity import make_pub_id, make_taproot_hub_paper_id
    from precis.taproot.authoring import (
        _evidence_edge_exists,
        find_hub_by_pub_id,
        resolve_paper_ref_id,
    )
    from precis.taproot.hub import _DEFAULT_ROLE, _grounding_chunk_ord

    sentence = entry.get("sentence", "")
    scope = entry.get("scope") or {}
    supporters = entry.get("supporters", [])

    pub_id = make_pub_id(make_taproot_hub_paper_id(sentence, scope))
    hub_ref_id = find_hub_by_pub_id(store, pub_id)

    attached = 0
    already = 0
    ungrounded = 0
    for supporter in supporters:
        # Resolution only -- raises BadInput on an unresolvable (or
        # non-paper/patent) paper, same as the real (write) path would, so
        # a dry-run catches a bad spec before anything is minted.
        paper_ref_id = resolve_paper_ref_id(store, supporter.get("paper"))
        role = supporter.get("role") or _DEFAULT_ROLE
        src_ord = _grounding_chunk_ord(
            store,
            paper_ref_id=paper_ref_id,
            meta={"source_handle": supporter.get("source_handle")},
        )
        # A brand-new hub has no existing evidence at all -- every
        # supporter would be a fresh attach. For a pre-existing hub, check
        # the actual (paper, hub, role, chunk) edge -- reporting every
        # supporter as "already" just because the hub pre-exists was
        # inaccurate for a genuinely new supporter/passage on an old hub.
        if hub_ref_id is not None and _evidence_edge_exists(
            store,
            paper_ref_id=paper_ref_id,
            hub_ref_id=hub_ref_id,
            role=role,
            src_ord=src_ord,
        ):
            already += 1
        else:
            attached += 1
            if src_ord is None:
                ungrounded += 1

    return {
        "pub_id": pub_id,
        "hub_ref_id": hub_ref_id,
        "attached": attached,
        "already": already,
        "ungrounded": ungrounded,
        "sentence": sentence,
        "dry_run": True,
    }


def _preflight_resolve_supporters(store: Any, claims: list[dict[str, Any]]) -> None:
    """Resolve every supporter's ``paper`` across every claim, read-only.

    Runs BEFORE any write so a bad spec anywhere in the batch (an
    unresolvable handle, or one that resolves to a non-paper/patent ref —
    :func:`~precis.taproot.authoring.resolve_paper_ref_id` now rejects
    those) fails closed: nothing gets partially minted to prod for the
    claims that precede the bad one.

    Raises:
        BadInput: same as :func:`~precis.taproot.authoring.resolve_paper_ref_id`
            (or a missing ``'paper'`` field, mirroring
            :func:`~precis.taproot.authoring.seed_claim_hub`'s own check).
    """
    from precis.errors import BadInput
    from precis.taproot.authoring import resolve_paper_ref_id

    for entry in claims:
        for supporter in entry.get("supporters", []):
            paper = supporter.get("paper")
            if paper is None:
                raise BadInput("supporter missing required 'paper' field")
            resolve_paper_ref_id(store, paper)


def _print_results(results: list[dict[str, Any]], fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(results, indent=2))
        return

    total_ungrounded = 0
    for r in results:
        snippet = (r.get("sentence") or "").strip()
        if len(snippet) > 60:
            snippet = snippet[:57] + "..."
        suffix = "  [DRY-RUN]" if r.get("dry_run") else ""
        collapsed = r.get("collapsed") or []
        collapsed_note = f", {len(collapsed)} collapsed" if collapsed else ""
        ungrounded = int(r.get("ungrounded") or 0)
        total_ungrounded += ungrounded
        ungrounded_note = f", {ungrounded} ungrounded" if ungrounded else ""
        print(
            f"{r['pub_id']}  {snippet}  "
            f"(+{r['attached']} evidence, {r['already']} already"
            f"{collapsed_note}{ungrounded_note}){suffix}"
        )

    # Nudge: an ungrounded edge cites the whole paper (pa<id>), not the
    # passage (pc<id>) — supplying source_handle makes the citation tree
    # resolve to the supporting chunk.
    if total_ungrounded:
        print(
            f"note: {total_ungrounded} evidence edge(s) attached ref-level "
            "(no grounding chunk) — add source_handle=<pc<id>> per supporter "
            "so the edge cites the passage, not just the paper.",
            file=sys.stderr,
        )


def _run_mint(args: argparse.Namespace) -> None:
    from precis.errors import BadInput
    from precis.store import Store
    from precis.taproot.authoring import seed_claim_hub

    claims = _load_spec(args)
    store = Store.connect(resolve_dsn(args.database_url))
    results: list[dict[str, Any]] = []

    try:
        if not args.dry_run:
            # Pre-flight: read-only resolution of every supporter, before
            # any claim's hub gets minted -- a bad claim N never leaves
            # claims 0..N-1 written while hiding that partial write.
            _preflight_resolve_supporters(store, claims)

        for entry in claims:
            sentence = entry.get("sentence", "")
            if args.dry_run:
                results.append(_dry_run_result(store, entry))
                continue
            scope = entry.get("scope") or {}
            supporters = entry.get("supporters", [])
            out = seed_claim_hub(
                store,
                sentence=sentence,
                scope=scope,
                supporters=supporters,
                set_by=args.set_by,
            )
            out["sentence"] = sentence
            results.append(out)
    except BadInput as exc:
        print(f"taproot mint: error: {exc.cause}", file=sys.stderr)
        if results:
            # A write can still fail mid-batch for some other reason after
            # pre-flight passed (e.g. a concurrent delete) -- never hide a
            # partial run; show what already committed before exiting.
            print(
                "taproot mint: already-committed before the failure:",
                file=sys.stderr,
            )
            _print_results(results, args.format)
        sys.exit(1)
    finally:
        store.close()

    _print_results(results, args.format)


def _run_refine(args: argparse.Namespace) -> None:
    from precis.errors import BadInput
    from precis.store import Store
    from precis.taproot.authoring import resolve_hub_ref_id
    from precis.taproot.hub import link_claims

    store = Store.connect(resolve_dsn(args.database_url))
    try:
        from_hub = resolve_hub_ref_id(store, args.from_hub)
        to_hub = resolve_hub_ref_id(store, args.to_hub)
        if from_hub == to_hub:
            print(
                "taproot refine: error: --from and --to resolve to the same "
                f"hub (ref_id={from_hub}); a claim can't refine itself",
                file=sys.stderr,
            )
            sys.exit(1)
        if args.dry_run:
            print(
                f"[DRY-RUN] would link fi{from_hub} --refines--> fi{to_hub}",
            )
            return
        wrote = link_claims(
            store,
            from_hub_ref_id=from_hub,
            to_hub_ref_id=to_hub,
            set_by=args.set_by,
        )
        verb = "linked" if wrote else "already linked"
        print(f"{verb}: fi{from_hub} --refines--> fi{to_hub}")
    except BadInput as exc:
        print(f"taproot refine: error: {exc.cause}", file=sys.stderr)
        sys.exit(1)
    finally:
        store.close()


def _resolve_backfill_chunks(store: Any, args: argparse.Namespace) -> list[int]:
    """The draft body chunk_ids to backfill: one for ``--chunk``, all of a
    draft's body chunks (in reading order) for ``--draft``."""
    from precis.errors import BadInput

    if args.chunk:
        token = args.chunk.strip().removeprefix("dc")
        if not token.isdigit():
            raise BadInput(f"--chunk must be a dc<id> handle, got {args.chunk!r}")
        return [int(token)]

    # A draft slug lives in ref_identifiers (id_kind='cite_key'), not a
    # refs.slug column — resolve through the canonical store lookup, not
    # hand-rolled SQL (which drifted against the schema, gr schema-drift test).
    ref = store.get_ref(kind="draft", id=args.draft.strip())
    if ref is None:
        raise BadInput(f"no live draft with slug {args.draft.strip()!r}")
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "AND retired_at IS NULL ORDER BY ord",
            (int(ref.id),),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _print_backfill(results: list[Any], fmt: str, *, applied: bool) -> None:
    if fmt == "json":
        payload = [
            {
                "chunk_id": r.chunk_id,
                "draft_ref_id": r.draft_ref_id,
                "applied": applied,
                "rewritten": r.rewritten_text is not None,
                "n_ungrounded": r.n_ungrounded,
                "groups": [
                    {
                        "action": p.action,
                        "kind": p.group.kind,
                        "handles": p.group.handles,
                        "hub_ref_id": p.hub_ref_id,
                        "ungrounded": p.ungrounded,
                        "reground_targets": p.reground_targets,
                        "claim": p.claim.sentence if p.claim else None,
                        "note": p.note,
                    }
                    for p in r.plans
                ],
            }
            for r in results
        ]
        print(json.dumps(payload, indent=2))
        return

    tag = "" if applied else "  [DRY-RUN]"
    for r in results:
        if not r.plans:
            print(
                f"dc{r.chunk_id}: no [pc<id>]/[pa<id>] cites (already converted){tag}"
            )
            continue
        counts: dict[str, int] = {}
        for p in r.plans:
            counts[p.action] = counts.get(p.action, 0) + 1
        summary = ", ".join(f"{n} {a}" for a, n in sorted(counts.items()))
        ung = r.n_ungrounded
        ung_note = f"  ({ung} ref-level/ungrounded)" if ung else ""
        print(
            f"dc{r.chunk_id}: {len(r.plans)} cite-group(s) — {summary}{ung_note}{tag}"
        )
        for p in r.plans:
            handles = "+".join(p.group.handles)
            if p.action == "attach":
                arrow = f"→ fi{p.hub_ref_id} (converge)"
            elif p.action in ("new", "new_contradicts"):
                arrow = f"→ fi{p.hub_ref_id}" if p.hub_ref_id else "→ new hub"
            elif p.action == "reground":
                pcs = "".join(f"[pc{c}]" for c in p.reground_targets)
                arrow = f"→ {pcs} (re-ground)"
            else:
                arrow = f"({p.action})"
            if p.ungrounded and p.hub_ref_id is not None:
                arrow += " [ref-level]"  # whole-paper [pa] promote, any action
            claim = (p.claim.sentence[:70] + "…") if p.claim else p.note
            print(f"    [{handles}] {arrow}  {claim}")


def _run_backfill(args: argparse.Namespace) -> None:
    from precis.config import load_config
    from precis.errors import BadInput
    from precis.runtime import build_runtime
    from precis.taproot.backfill import apply_chunk, plan_chunk

    cfg = load_config()
    dsn = resolve_dsn(args.database_url)
    if dsn:
        cfg = cfg.model_copy(update={"database_url": dsn})
    runtime = build_runtime(cfg)
    store = runtime.store
    embedder = getattr(runtime.hub, "embedder", None)
    if store is None:
        print("taproot backfill: no database configured", file=sys.stderr)
        sys.exit(2)
    if embedder is None:
        print(
            "taproot backfill: no embedder configured — the hub ANN "
            "convergence step needs one (set config.embedder / PRECIS_EMBEDDER_URL)",
            file=sys.stderr,
        )
        sys.exit(2)

    results: list[Any] = []
    try:
        chunk_ids = _resolve_backfill_chunks(store, args)
        draft_handler = runtime.hub.handler_for("draft") if args.apply else None
        ref_level = getattr(args, "ref_level", False)
        for cid in chunk_ids:
            if args.apply:
                results.append(
                    apply_chunk(
                        store,
                        embedder,
                        draft_handler,
                        cid,
                        set_by=args.set_by,
                        ref_level=ref_level,
                    )
                )
            else:
                results.append(plan_chunk(store, embedder, cid, ref_level=ref_level))
    except BadInput as exc:
        print(f"taproot backfill: error: {exc.cause}", file=sys.stderr)
        if results:
            _print_backfill(results, args.format, applied=args.apply)
        sys.exit(1)
    finally:
        if hasattr(store, "close"):
            store.close()

    _print_backfill(results, args.format, applied=args.apply)


_DRAFT_REF_LEVEL_MENTION_SQL = """
    SELECT {select}
    FROM links l
    JOIN refs r ON r.ref_id = l.src_ref_id
    WHERE r.kind = 'draft'
      AND l.relation IN ('cites', 'related-to')
      AND l.meta->>'auto' = 'mention'
      AND l.src_chunk_id IS NULL
"""

_PAPER_EVIDENCE_CANDIDATE_SQL = """
    SELECT l.link_id, l.src_ref_id, l.dst_ref_id, l.relation, l.meta
    FROM links l
    JOIN refs s ON s.ref_id = l.src_ref_id
    WHERE s.kind IN ('paper', 'patent')
      AND l.relation IN ('corroborates', 'establishes', 'contradicts')
      AND l.src_chunk_id IS NULL
      AND l.meta->>'source_handle' IS NOT NULL
      AND l.meta->>'source_handle' <> 'null'
"""


def _backfill_grounding(store: Any, *, dry_run: bool) -> dict[str, Any]:
    """Upgrade ref-level taproot/draft citation edges to chunk-grounded.

    Two independent passes, both idempotent (a live edge that's already
    grounded, or one that stays unresolvable, is a no-op — never an
    error) and both re-runnable (safe to invoke again after new drafts /
    evidence edges land):

    PART A — draft ``cites``/``related-to`` auto-mention edges. Re-running
    the (already-fixed) draft autolinker
    (:meth:`~precis.handlers.draft.DraftHandler._sync_draft_links`) over
    every draft that still carries a ref-level auto-mention edge migrates
    it to chunk-grounded — the resync drops the stale ref-level rows and
    re-adds them at the citing chunk's ord.

    PART B — paper/patent evidence edges (``corroborates`` / ``establishes``
    / ``contradicts``) that carry a ``meta.source_handle`` but were never
    grounded (written before the grounding fix, or via a path that didn't
    thread ``src_pos``). Resolves the handle to its chunk via
    :func:`precis.taproot.hub._grounding_chunk_ord` and sets
    ``src_chunk_id`` directly with a bare ``UPDATE`` — there's no handler
    write path for "re-ground an existing edge in place". A
    ``UniqueViolation`` (a chunk-grounded edge for that exact tuple already
    exists) is caught per-row and counted, never aborting the run.

    ``dry_run=True`` writes nothing — every count below reports what WOULD
    happen.
    """
    from psycopg.errors import UniqueViolation

    from precis.dispatch import Hub
    from precis.handlers.draft import DraftHandler
    from precis.taproot.hub import _grounding_chunk_ord

    result: dict[str, Any] = {
        "dry_run": dry_run,
        "drafts_found": 0,
        "drafts_resynced": 0,
        "draft_edges_before": 0,
        "draft_edges_after": 0,
        "paper_candidates": 0,
        "paper_edges_grounded": 0,
        "unresolved": 0,
        "skipped_collision": 0,
    }

    # ── Part A: draft cites/related-to resync ───────────────────────────
    with store.pool.connection() as conn:
        draft_ref_ids = [
            int(row[0])
            for row in conn.execute(
                _DRAFT_REF_LEVEL_MENTION_SQL.format(select="DISTINCT l.src_ref_id")
            ).fetchall()
        ]
        draft_edges_before = int(
            conn.execute(
                _DRAFT_REF_LEVEL_MENTION_SQL.format(select="count(*)")
            ).fetchone()[0]
        )
    result["drafts_found"] = len(draft_ref_ids)
    result["draft_edges_before"] = draft_edges_before

    if dry_run:
        # Nothing is written; every candidate draft WOULD be resynced.
        result["drafts_resynced"] = len(draft_ref_ids)
        result["draft_edges_after"] = draft_edges_before
    else:
        handler = DraftHandler(hub=Hub(store=store))
        for ref_id in draft_ref_ids:
            handler._sync_draft_links(ref_id)
        result["drafts_resynced"] = len(draft_ref_ids)
        with store.pool.connection() as conn:
            result["draft_edges_after"] = int(
                conn.execute(
                    _DRAFT_REF_LEVEL_MENTION_SQL.format(select="count(*)")
                ).fetchone()[0]
            )

    # ── Part B: paper/patent evidence edges with a stored source_handle ─
    with store.pool.connection() as conn:
        candidates = conn.execute(_PAPER_EVIDENCE_CANDIDATE_SQL).fetchall()
    result["paper_candidates"] = len(candidates)

    for link_id, src_ref_id, _dst_ref_id, _relation, meta in candidates:
        ord_ = _grounding_chunk_ord(store, paper_ref_id=src_ref_id, meta=meta or {})
        if ord_ is None:
            result["unresolved"] += 1
            continue
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = %s",
                (src_ref_id, ord_),
            ).fetchone()
        if row is None:
            result["unresolved"] += 1
            continue
        if dry_run:
            result["paper_edges_grounded"] += 1
            continue
        chunk_id = int(row[0])
        try:
            with store.pool.connection() as conn:
                conn.execute(
                    "UPDATE links SET src_chunk_id = %s WHERE link_id = %s",
                    (chunk_id, int(link_id)),
                )
        except UniqueViolation:
            result["skipped_collision"] += 1
            continue
        result["paper_edges_grounded"] += 1

    return result


def _print_backfill_grounding(result: dict[str, Any], fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(result, indent=2))
        return

    suffix = "  [DRY-RUN]" if result["dry_run"] else ""
    print(
        f"drafts: {result['drafts_found']} candidate(s), "
        f"{result['draft_edges_before']} ref-level auto-mention edge(s) -- "
        f"resynced {result['drafts_resynced']} draft(s){suffix}"
    )
    if not result["dry_run"]:
        print(
            "  ref-level auto-mention edges remaining after resync: "
            f"{result['draft_edges_after']}"
        )
    print(
        f"paper/patent evidence: {result['paper_candidates']} candidate(s) -- "
        f"grounded {result['paper_edges_grounded']}, "
        f"unresolved {result['unresolved']}, "
        f"skipped_collision {result['skipped_collision']}{suffix}"
    )


def _run_backfill_grounding(args: argparse.Namespace) -> None:
    from precis.store import Store

    store = Store.connect(resolve_dsn(args.database_url))
    try:
        result = _backfill_grounding(store, dry_run=args.dry_run)
    finally:
        store.close()

    _print_backfill_grounding(result, args.format)


def run(args: argparse.Namespace) -> None:
    """Execute ``precis taproot <taproot_cmd>``."""
    if args.taproot_cmd == "mint":
        _run_mint(args)
    elif args.taproot_cmd == "refine":
        _run_refine(args)
    elif args.taproot_cmd == "backfill":
        _run_backfill(args)
    elif args.taproot_cmd == "backfill-grounding":
        _run_backfill_grounding(args)
    else:
        print(f"taproot: unknown subcommand {args.taproot_cmd!r}", file=sys.stderr)
        sys.exit(2)


__all__ = ["add_parser", "run"]
