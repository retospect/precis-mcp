"""``precis classify {role3,topics}`` — targeted-scope drivers for the two
Controlled chunk tagging chunk/paper classifier cascades (``workers/classify.py``,
``workers/classify_topics.py``).

The worker passes (``--only classify`` / ``--only classify_topics``) sweep the
whole corpus FIFO. This CLI is for the other case: classify a *specific* set
of papers on demand (e.g. right after fetching a paper's citation graph, or
backfilling one topic) without waiting for/relying on the global sweep. Scope
is resolved to a concrete ``ref_ids`` list (or ``None`` for a global sweep,
``topics --all`` only) once, then fed to the pass repeatedly until a cycle
claims nothing.

    precis classify role3 --cites-of 43020
    precis classify role3 --topic nanobuds
    precis classify role3 --ref-ids 43020,43021,43099

    precis classify topics --all
    precis classify topics --cites-of 43020
    precis classify topics --topic nanobuds
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from precis.cli._common import resolve_dsn
from precis.store import Store


def _add_scope_args(sub: Any, *, include_all: bool) -> None:
    """Register the shared scope-selector group + ``--batch-size``/
    ``--database-url`` on a ``role3``/``topics`` subparser. ``include_all``
    adds the ``--all`` global-sweep selector (``topics`` only — ``role3`` has
    no equivalent CLI entry point for its FIFO sweep, that's the worker
    pass)."""
    scope = sub.add_mutually_exclusive_group(required=True)
    if include_all:
        scope.add_argument(
            "--all",
            action="store_true",
            help="Global sweep — every paper/patent lacking a current-version "
            "marker tag (ref_ids=None).",
        )
    scope.add_argument(
        "--cites-of",
        type=int,
        default=None,
        metavar="REF_ID",
        help="Classify every paper cited by ref REF_ID (its 'cites' out-links).",
    )
    scope.add_argument(
        "--topic",
        default=None,
        metavar="SLUG",
        help="Classify every paper tagged topic:<SLUG>.",
    )
    scope.add_argument(
        "--ref-ids",
        default=None,
        metavar="CSV",
        help="Explicit comma-separated ref ids to classify.",
    )
    sub.add_argument(
        "--batch-size",
        type=int,
        default=16,
        help="Claimed per cycle (default 16, mirrors the worker cap).",
    )
    sub.add_argument("--database-url", default=None, help="Postgres DSN override.")


def add_parser(subparsers: Any) -> None:
    p = subparsers.add_parser(
        "classify", help="Chunk/paper classifier cascades — targeted-scope drivers."
    )
    csub = p.add_subparsers(dest="classify_cmd", required=True)

    r = csub.add_parser(
        "role3",
        help="Run the junk-gate -> ROLE3 (own/background/furniture) cascade "
        "over a specific set of papers, not the global FIFO backfill.",
    )
    _add_scope_args(r, include_all=False)

    t = csub.add_parser(
        "topics",
        help="Run the tier-0/tier-1 topic-dossier cascade over a "
        "specific set of papers, or --all for the global sweep.",
    )
    _add_scope_args(t, include_all=True)


def _resolve_cites_of(store: Store, ref_id: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT l.dst_ref_id FROM links l "
            "JOIN refs r ON r.ref_id = l.dst_ref_id "
            "WHERE l.src_ref_id = %s AND l.relation = 'cites' "
            "AND r.kind = 'paper' AND r.deleted_at IS NULL",
            (ref_id,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _resolve_topic(store: Store, slug: str) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT rt.ref_id FROM ref_tags rt "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "JOIN refs r ON r.ref_id = rt.ref_id "
            "WHERE t.namespace = 'OPEN' AND t.value = %s "
            "AND r.kind = 'paper' AND r.deleted_at IS NULL",
            (f"topic:{slug}",),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _resolve_scope(store: Store, args: argparse.Namespace) -> list[int] | None:
    """Shared scope resolver for both ``role3`` and ``topics``. Returns
    ``None`` only for ``topics --all`` (global sweep, ``ref_ids=None``);
    ``role3``'s parser has no ``--all`` so ``getattr`` falls through to the
    other three selectors, always yielding a (possibly empty) list."""
    if getattr(args, "all", False):
        return None
    if args.cites_of is not None:
        return _resolve_cites_of(store, args.cites_of)
    if args.topic is not None:
        return _resolve_topic(store, args.topic)
    return [int(x) for x in args.ref_ids.split(",") if x.strip()]


def _cmd_role3(store: Store, args: argparse.Namespace) -> None:
    from precis.utils.llm.router import DispatchClient, Tier
    from precis.workers.classify import run_classify_pass

    ref_ids = _resolve_scope(store, args)
    if not ref_ids:
        print("classify role3: no papers matched scope")
        return

    # Mirrors cli/worker.py's `classify` pass wiring exactly (the LLM routing seam
    # dispatch seam + the Tier 2 escalate-client shape).
    client = DispatchClient(
        tier=Tier.SMALL,
        model=os.environ.get("PRECIS_CLASSIFY_MODEL") or "summarizer",
        source="classify",
        log_call=True,
        log_blobs=False,
    )
    escalate_model = os.environ.get("PRECIS_CLASSIFY_ESCALATE_MODEL") or None
    escalate_client = (
        DispatchClient(
            tier=Tier.SMALL,
            model=escalate_model,
            source="classify",
            log_call=True,
            log_blobs=False,
        )
        if escalate_model
        else None
    )

    print(f"classify role3: {len(ref_ids)} paper(s) in scope")

    total_claimed = total_ok = total_failed = 0
    dist: dict[str, int] = {}
    while True:
        r = run_classify_pass(
            store,
            client=client,
            batch_size=args.batch_size,
            escalate_client=escalate_client,
            ref_ids=ref_ids,
        )
        if r["claimed"] == 0:
            break
        total_claimed += r["claimed"]
        total_ok += r["ok"]
        total_failed += r["failed"]
        for k, v in (r.get("dist") or {}).items():
            dist[k] = dist.get(k, 0) + v
        print(f"  ...claimed {total_claimed}, ok {total_ok}, failed {total_failed}")

    print(
        f"classify role3: done — {total_claimed} chunk(s) claimed, "
        f"{total_ok} ok, {total_failed} failed, distribution {dist}"
    )


def _cmd_topics(store: Store, args: argparse.Namespace) -> None:
    from precis.utils.llm.router import DispatchClient, Tier
    from precis.workers.classify_topics import run_classify_topics_pass

    ref_ids = _resolve_scope(store, args)
    if ref_ids is not None and not ref_ids:
        print("classify topics: no papers matched scope")
        return

    # Admin CLI: always sweeps the FULL taxonomy (a deliberate, node-targeted
    # full backfill) — unlike cli/worker.py's rotation `classify_topics` pass,
    # which filters through per-topic `service_config` gates.
    # Shares the dispatch seam. No escalate client — tier 2 is
    # unimplemented for topics (a topic-dossiers open question).
    client = DispatchClient(
        tier=Tier.SMALL,
        model=os.environ.get("PRECIS_CLASSIFY_TOPICS_MODEL") or "summarizer",
        source="classify_topics",
        log_call=True,
        log_blobs=False,
    )

    scope_desc = "the whole corpus" if ref_ids is None else f"{len(ref_ids)} paper(s)"
    print(f"classify topics: sweeping {scope_desc}")

    total_claimed = total_ok = total_failed = 0
    dist: dict[str, int] = {}
    while True:
        r = run_classify_topics_pass(
            store, client=client, batch_size=args.batch_size, ref_ids=ref_ids
        )
        if r["claimed"] == 0:
            break
        total_claimed += r["claimed"]
        total_ok += r["ok"]
        total_failed += r["failed"]
        for k, v in (r.get("dist") or {}).items():
            dist[k] = dist.get(k, 0) + v
        print(f"  ...claimed {total_claimed}, ok {total_ok}, failed {total_failed}")

    print(
        f"classify topics: done — {total_claimed} paper(s) processed, "
        f"{total_ok} ok, {total_failed} failed, distribution {dist}"
    )


def run(args: argparse.Namespace) -> None:
    store = Store.connect(resolve_dsn(args.database_url))
    if args.classify_cmd == "role3":
        _cmd_role3(store, args)
    elif args.classify_cmd == "topics":
        _cmd_topics(store, args)
