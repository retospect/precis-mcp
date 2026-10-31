"""``precis card-describe`` — run or preview the card_describe worker pass.

``--dry-run`` prints the claimable count per ref kind — the queue the
worker would spend Sonnet calls on (the watermark-bound new/edited set,
never the pre-existing corpus) — and writes nothing, spends nothing.
Without it, runs one ``run_card_describe_pass`` batch on the BIG tier.
"""

from __future__ import annotations

import argparse

from precis.cli._common import bind_process_store, resolve_dsn


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``card-describe`` subparser on ``sub``."""
    p = sub.add_parser(
        "card-describe",
        help="Generate or preview LLM card_description chunks for non-text refs.",
        description=(
            "Run one card_describe worker pass (BIG tier), or with "
            "--dry-run print the claimable count per kind without spending."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the claimable count per kind; write nothing, spend nothing.",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=16,
        help="Max refs to describe in one pass (default: 16).",
    )
    p.add_argument(
        "--database-url",
        default=None,
        help="Postgres DSN (default: PRECIS_DATABASE_URL / config).",
    )
    return p


def run(args: argparse.Namespace) -> None:
    from precis.store import Store

    store = Store.connect(resolve_dsn(args.database_url))
    bind_process_store(store)
    if args.dry_run:
        from precis.workers.card_describe import claimable_counts

        with store.pool.connection() as conn:
            counts = claimable_counts(conn)
        for kind, n in sorted(counts.items()):
            print(f"{kind}: {n}")
        print(f"total: {sum(counts.values())}")
        return
    from precis.utils.llm.router import DispatchClient, Tier
    from precis.workers.card_describe import (
        DESCRIBE_MAX_TOKENS,
        run_card_describe_pass,
    )

    client = DispatchClient(
        tier=Tier.BIG,
        max_tokens=DESCRIBE_MAX_TOKENS,
        source="card_describe",
        log_call=True,
        log_blobs=False,
    )
    r = run_card_describe_pass(store, client=client, batch_size=args.batch_size)
    line = f"claimed={r['claimed']} ok={r['ok']} failed={r['failed']}"
    if r.get("deferred"):
        line += f" deferred={r['deferred']}"
    print(line)
