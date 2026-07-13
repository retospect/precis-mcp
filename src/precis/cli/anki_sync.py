"""``precis anki-sync`` — the headless AnkiWeb sync tick (slice 2).

The "occasional tick" the design calls for: a cron on the single designated
runner (the Mac `precis-infra` stack) invokes this. It reads precis `anki` refs,
upserts them into the local `.anki2` mirror by deterministic guid, drives a
*guarded* AnkiWeb sync (bootstrap-download / incremental / abort-on-lossy-upload),
and writes the decay stats back into each ref's ``meta.anki_stats``.

Single-runner: a pg advisory lock ensures only one sync touches the account at a
time (two mirrors on one account would manufacture a full-sync conflict).
Default-off behind ``PRECIS_ANKI_ENABLED``; the `anki` wheel is lazy-imported.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from precis.cli._common import resolve_dsn

#: A fixed advisory-lock key so concurrent runners serialise on the account.
_ANKI_SYNC_LOCK = 0x616E6B69  # "anki"


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "anki-sync",
        help="Sync precis anki cards to AnkiWeb + read decay stats back.",
        description=(
            "Headless AnkiWeb sync for the `anki` cloze kind. Gated behind "
            "PRECIS_ANKI_ENABLED; needs the `anki` wheel + PRECIS_ANKI_USER / "
            "PRECIS_ANKI_PASSWORD / PRECIS_ANKI_MIRROR_DIR."
        ),
    )
    p.add_argument("--database-url", default=None)
    p.add_argument("--limit", type=int, default=10000, help="Max anki refs to sync.")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="List how many cards would sync; do not touch AnkiWeb.",
    )
    p.add_argument(
        "--fix",
        action="store_true",
        help="Also run the precis-fix pass (LLM-rewrite `precis-fix`-tagged cards).",
    )


def run(args: argparse.Namespace) -> None:
    from precis.anki.notes import spec_from_ref
    from precis.anki.sync import AnkiNotInstalled, AnkiSyncError, sync_tick
    from precis.config import load_config
    from precis.runtime import build_runtime

    cfg = load_config()
    if not cfg.anki_enabled:
        print(
            "anki-sync: disabled — set PRECIS_ANKI_ENABLED=1 on the sync runner.",
            file=sys.stderr,
        )
        sys.exit(2)

    dsn = resolve_dsn(args.database_url, cfg=cfg)
    cfg = cfg.model_copy(update={"database_url": dsn})
    store = build_runtime(cfg).store
    if store is None:
        print("anki-sync: no database configured.", file=sys.stderr)
        sys.exit(2)

    refs = store.list_refs(kind="anki", limit=args.limit)
    specs = [s for s in (spec_from_ref(r) for r in refs) if s is not None]

    if args.dry_run:
        print(f"anki-sync [DRY-RUN]: {len(specs)} cloze card(s) would sync.")
        return

    if not cfg.anki_user or not cfg.anki_password:
        print(
            "anki-sync: set PRECIS_ANKI_USER and PRECIS_ANKI_PASSWORD.",
            file=sys.stderr,
        )
        sys.exit(2)
    if not cfg.anki_mirror_dir:
        print("anki-sync: set PRECIS_ANKI_MIRROR_DIR.", file=sys.stderr)
        sys.exit(2)
    mirror_dir = Path(cfg.anki_mirror_dir).expanduser()
    mirror_dir.mkdir(parents=True, exist_ok=True)
    mirror_path = str(mirror_dir / "mirror.anki2")

    # Single-runner guard: only one sync per account at a time.
    with store.pool.connection() as conn:
        got = conn.execute(
            "select pg_try_advisory_lock(%s)", (_ANKI_SYNC_LOCK,)
        ).fetchone()[0]
        if not got:
            print("anki-sync: another sync holds the lock; skipping.", file=sys.stderr)
            return
        try:
            try:
                result, stats = sync_tick(
                    mirror_path=mirror_path,
                    user=cfg.anki_user,
                    password=cfg.anki_password,
                    specs=specs,
                    deck=cfg.anki_deck,
                    fix=args.fix or cfg.anki_fix_enabled,
                )
            except AnkiNotInstalled as e:
                print(f"anki-sync: {e}", file=sys.stderr)
                sys.exit(3)
            except AnkiSyncError as e:
                print(f"anki-sync: sync failed: {e}", file=sys.stderr)
                sys.exit(1)

            now = datetime.now(UTC).isoformat()
            for ref_id, st in stats.items():
                store.update_ref(
                    ref_id,
                    meta_patch={"anki_stats": st, "anki": {"last_synced": now}},
                )
            print(f"anki-sync: {result.summary()}")
            if result.aborted:
                sys.exit(1)
        finally:
            conn.execute("select pg_advisory_unlock(%s)", (_ANKI_SYNC_LOCK,))


__all__ = ["add_parser", "run"]
