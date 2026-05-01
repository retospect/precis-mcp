"""``precis jobs dedupe-papers`` — remove duplicate paper refs.

Pre-fix ingest only deduped on DOI, so DOI-less bundles (e.g.
``text_rescue`` extracts) accumulated one extra row per rerun with a
``-N`` slug suffix. This command finds refs that share a content
identity key (``pdf_hash`` / ``doi`` / ``arxiv_id``), collapses each
group to its lowest-id survivor, and hard-deletes the rest. Blocks,
tags, and links cascade away via the ``ON DELETE CASCADE`` on
``refs.id``.

Dry-run by default. Pass ``--apply`` to actually delete.
"""

from __future__ import annotations

import argparse

from precis.cli._common import resolve_dsn


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``dedupe-papers`` subparser on ``sub``."""
    dp = sub.add_parser(
        "dedupe-papers",
        help="Remove duplicate paper refs sharing pdf_hash / doi / arxiv_id.",
    )
    dp.add_argument("--database-url", default=None)
    dp.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete duplicates (default: dry-run only prints).",
    )
    dp.add_argument(
        "--key",
        choices=("pdf_hash", "doi", "arxiv_id", "all"),
        default="all",
        help="Which identity key to dedupe on (default: all).",
    )
    return dp


def run(args: argparse.Namespace) -> None:
    """Implements ``precis jobs dedupe-papers``."""
    from precis.config import load_config
    from precis.store import Store

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)

    keys = ("pdf_hash", "doi", "arxiv_id") if args.key == "all" else (args.key,)

    store = Store.connect(dsn)
    try:
        total_groups = 0
        total_dupes = 0
        # Track IDs already scheduled for deletion so that a paper
        # surfaced under multiple keys (e.g. shared pdf_hash AND
        # shared doi) isn't double-counted and doesn't mess with the
        # "keep lowest id" rule across keys.
        victim_ids: set[int] = set()

        with store.pool.connection() as conn:
            for key in keys:
                rows = conn.execute(
                    "WITH live AS ("
                    "  SELECT id, slug, meta->>%s AS v "
                    "  FROM refs "
                    "  WHERE kind = 'paper' AND deleted_at IS NULL "
                    "    AND meta ? %s AND meta->>%s <> ''"
                    ") "
                    "SELECT v, "
                    "       array_agg(id ORDER BY id) AS ids, "
                    "       array_agg(slug ORDER BY id) AS slugs "
                    "FROM live "
                    "GROUP BY v "
                    "HAVING count(*) > 1",
                    (key, key, key),
                ).fetchall()
                if not rows:
                    continue
                print(f"== Duplicates by {key} ({len(rows)} groups) ==")
                for value, ids, slugs in rows:
                    # Skip groups whose survivor is already a victim
                    # of an earlier-key pass — pathological, but cheap
                    # to guard against.
                    keep, *rest = zip(ids, slugs, strict=True)
                    keep_id, keep_slug = keep
                    dupe_pairs = [
                        (rid, rslug) for rid, rslug in rest if rid not in victim_ids
                    ]
                    if not dupe_pairs:
                        continue
                    total_groups += 1
                    total_dupes += len(dupe_pairs)
                    preview = (value or "")[:60]
                    print(
                        f"  {key}={preview!r}: keep #{keep_id} "
                        f"{keep_slug!r}, remove "
                        + ", ".join(f"#{rid} {rslug!r}" for rid, rslug in dupe_pairs)
                    )
                    for rid, _slug in dupe_pairs:
                        victim_ids.add(rid)

        verb = "would remove" if not args.apply else "removing"
        print(
            f"dedupe-papers: {verb} {total_dupes} duplicate refs across "
            f"{total_groups} groups [keys={','.join(keys)}]"
        )

        if args.apply and victim_ids:
            with store.tx() as conn:
                conn.execute(
                    "DELETE FROM refs WHERE id = ANY(%s)",
                    (sorted(victim_ids),),
                )
            print(f"dedupe-papers: deleted {len(victim_ids)} refs")
        elif args.apply:
            print("dedupe-papers: nothing to delete")
        else:
            print("dedupe-papers: dry-run (pass --apply to delete)")
    finally:
        store.close()


__all__ = ["add_parser", "run"]
