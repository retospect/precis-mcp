"""``precis anki-sync`` — the headless AnkiWeb sync tick (slice 2).

The "occasional tick" the design calls for: this used to be invoked ONLY by a
dedicated cron on the single designated runner; §A folds that cadence onto
the decentralized ``scheduler`` worker pass too (``anki_sync`` in
:mod:`precis.workers.scheduler`) — this subcommand stays for a manual /
ad-hoc run, delegating its guts to :func:`precis.workers.anki_sync.run_anki_sync`
so the two triggers share one implementation. It reads precis `anki` refs,
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
from typing import TYPE_CHECKING, Any

from precis.cli._common import resolve_dsn

# Re-exported for backward compat — a couple of tests exercise this directly
# (the same helper now backs both the CLI and the ``anki_sync`` scheduler
# cadence, workers/scheduler.py).
from precis.workers.anki_sync import (
    retired_ref_ids as _retired_ref_ids,  # noqa: F401  re-exported for tests
)

if TYPE_CHECKING:
    from precis.store.store import Store


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
    p.add_argument(
        "--project",
        action="store_true",
        help="Also project ALL Anki cards into PG as read-only refs (searchable).",
    )
    p.add_argument(
        "--no-retire",
        action="store_true",
        help=(
            "Do not remove notes for soft-deleted precis cards from the mirror "
            "(retirement is on by default; own-guid notes only)."
        ),
    )


def run(args: argparse.Namespace) -> None:
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
    # Close the pool on every exit path (incl. sys.exit). Otherwise the psycopg
    # ConnectionPool is finalized at interpreter shutdown, where Python 3.14
    # raises PythonFinalizationError ("cannot join thread at interpreter
    # shutdown") — the daemon then exits non-zero even though the sync succeeded.
    try:
        _run_sync(args, cfg, store)
    finally:
        store.pool.close()


def _run_sync(args: argparse.Namespace, cfg: Any, store: Store) -> None:
    from precis.anki.sync import AnkiNotInstalled, AnkiSyncError
    from precis.workers.anki_sync import AnkiSyncMisconfigured, run_anki_sync

    try:
        summary = run_anki_sync(
            store,
            cfg,
            limit=args.limit,
            dry_run=args.dry_run,
            fix=args.fix,
            project=args.project,
            no_retire=args.no_retire,
        )
    except AnkiSyncMisconfigured as e:
        # Same exit code as before the refactor (2 = misconfigured, distinct
        # from a real sync failure below) — check this BEFORE the broader
        # AnkiSyncError it subclasses.
        print(f"anki-sync: {e}", file=sys.stderr)
        sys.exit(2)
    except AnkiNotInstalled as e:
        print(f"anki-sync: {e}", file=sys.stderr)
        sys.exit(3)
    except AnkiSyncError as e:
        print(f"anki-sync: sync failed: {e}", file=sys.stderr)
        sys.exit(1)
    print(summary)


__all__ = ["add_parser", "run"]
