"""``anki_sync`` — the store-taking core of the AnkiWeb sync tick (§A).

Refactored out of ``cli/anki_sync.py::_run_sync`` so the exact same guts run
either as ``precis anki-sync`` (an operator's ad-hoc/cron invocation) or as
the ``anki_sync`` scheduler cadence (:mod:`precis.workers.scheduler`) — one
implementation, no drift. Unlike the CLI, this module never calls
``sys.exit``: it raises on failure so each caller reacts in its own idiom —
the CLI translates an exception to an exit code; the scheduler cadence
wrapper logs-and-continues like every other cadence's work.

Single-runner: the same fixed pg advisory-lock key the CLI has always used
(:data:`_ANKI_SYNC_LOCK`) serializes a cadence-fired tick against a
concurrent manual ``precis anki-sync`` run on the account.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from precis.anki.sync import AnkiSyncError

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: A fixed advisory-lock key so concurrent runners serialise on the account
#: (same key the CLI has always used — this module is what it now delegates
#: to, so the lock is unchanged, not duplicated).
_ANKI_SYNC_LOCK = 0x616E6B69  # "anki"


class AnkiSyncMisconfigured(AnkiSyncError):
    """Required ``PRECIS_ANKI_*`` config (user/password/mirror dir) is unset."""


def retired_ref_ids(store: Store, *, window_days: int = 90) -> list[int]:
    """Recently soft-deleted *authored* cards (the card_forge retire/rewrite
    path, or a manual delete) whose Anki notes should be removed. Foreign
    projections are excluded — they were never pushed under a precis guid, and
    the 2026-07 incident soft-deleted ~93k of them (no point shipping that list
    to the mirror every tick)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE kind='anki' AND retired_at IS NOT NULL "
            "AND retired_at >= now() - make_interval(days => %s) "
            "AND COALESCE(meta->>'source','') != 'anki-foreign'",
            (window_days,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def run_anki_sync(
    store: Store,
    cfg: Any,
    *,
    limit: int = 10000,
    dry_run: bool = False,
    fix: bool = False,
    project: bool = False,
    no_retire: bool = False,
) -> str:
    """One sync tick. Returns a human-readable summary line (possibly
    multi-line). Raises on failure — never ``sys.exit``:

    * :class:`AnkiSyncMisconfigured` — ``cfg.anki_user`` / ``anki_password`` /
      ``anki_mirror_dir`` unset (a caller should surface this once, loudly;
      the cadence wrapper logs it like any other cadence exception).
    * :class:`precis.anki.sync.AnkiNotInstalled` — the ``anki`` wheel isn't
      importable on this runner.
    * :class:`precis.anki.sync.AnkiSyncError` — the guarded sync itself
      failed or aborted (a ``FULL_UPLOAD`` risk — never allowed).
    """
    from precis.anki.notes import spec_from_ref
    from precis.anki.sync import sync_tick

    refs = store.list_refs(kind="anki", limit=limit)
    specs = [s for s in (spec_from_ref(r) for r in refs) if s is not None]
    retire_ids = [] if no_retire else retired_ref_ids(store)

    if dry_run:
        return (
            f"anki-sync [DRY-RUN]: {len(specs)} cloze card(s) would sync, "
            f"{len(retire_ids)} retired ref(s) would be removed from the mirror."
        )

    if not cfg.anki_user or not cfg.anki_password:
        raise AnkiSyncMisconfigured("set PRECIS_ANKI_USER and PRECIS_ANKI_PASSWORD.")
    if not cfg.anki_mirror_dir:
        raise AnkiSyncMisconfigured("set PRECIS_ANKI_MIRROR_DIR.")
    mirror_dir = Path(cfg.anki_mirror_dir).expanduser()
    mirror_dir.mkdir(parents=True, exist_ok=True)
    mirror_path = str(mirror_dir / "mirror.anki2")

    # Single-runner guard: only one sync per account at a time.
    with store.pool.connection() as conn:
        lock_row = conn.execute(
            "select pg_try_advisory_lock(%s)", (_ANKI_SYNC_LOCK,)
        ).fetchone()
        got = lock_row[0] if lock_row else False
        if not got:
            return "anki-sync: another sync holds the lock; skipping."
        try:
            result, stats = sync_tick(
                mirror_path=mirror_path,
                user=cfg.anki_user,
                password=cfg.anki_password,
                specs=specs,
                deck=cfg.anki_deck,
                fix=fix or cfg.anki_fix_enabled,
                project=project or cfg.anki_project_enabled,
                retire_ref_ids=retire_ids,
            )
            now = datetime.now(UTC).isoformat()
            for ref_id, st in stats.items():
                # FLAT keys — `meta_patch` is a shallow jsonb `||` merge, so a
                # nested `{"anki": {...}}` would REPLACE the whole meta.anki
                # object (wiping guid/content_sha the projection dedups on —
                # the 2026-07 incident). Patch top-level keys only.
                store.update_ref(
                    ref_id,
                    meta_patch={"anki_stats": st, "anki_synced_at": now},
                )
            lines = [f"anki-sync: {result.summary()}"]
            if result.all_cards is not None:
                from precis.anki.project import project_cards

                proj = project_cards(store, result.all_cards)
                lines.append(f"anki-sync: {proj.summary()}")
            summary = "\n".join(lines)
            if result.aborted:
                raise AnkiSyncError(f"sync aborted: {summary}")
            return summary
        finally:
            conn.execute("select pg_advisory_unlock(%s)", (_ANKI_SYNC_LOCK,))


__all__ = [
    "AnkiSyncMisconfigured",
    "retired_ref_ids",
    "run_anki_sync",
]
