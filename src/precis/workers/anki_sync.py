"""``anki_sync`` — the store-taking core of the AnkiWeb sync tick (§A).

Refactored out of ``cli/anki_sync.py::_run_sync`` so the exact same guts run
either as ``precis anki-sync`` (an operator's ad-hoc/cron invocation) or as
the ``anki_sync`` scheduler cadence (:mod:`precis.workers.scheduler`) — one
implementation, no drift. Unlike the CLI, this module never calls
``sys.exit``: it raises on failure so each caller reacts in its own idiom —
the CLI translates an exception to an exit code; the scheduler cadence
wrapper logs-and-continues like every other cadence's work.

Per-user: each web user brings their own AnkiWeb credentials
(:mod:`precis.anki.creds`, self-service from ``/account``) and their own
``.anki2`` mirror under ``<anki_mirror_dir>/<login>/``. A default call
(``login=None``) fans out over :func:`precis.anki.creds.anki_logins` —
every enabled web user with credentials configured — syncing each one in
turn; a per-user advisory lock (:data:`_ANKI_SYNC_LOCK` + the login's own
hash) serialises a cadence-fired tick against a concurrent manual
``precis anki-sync --user <login>`` run for *that* login only, so two
users' syncs never block each other. A single user's failure doesn't stop
the others — every user is attempted, and a single :class:`AnkiSyncError`
is raised at the end if any of them aborted.
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

#: Fixed advisory-lock key namespace — paired with ``hashtext(login)`` (the
#: two-key ``pg_try_advisory_lock`` form) so concurrent runners serialise
#: per-user rather than fleet-wide.
_ANKI_SYNC_LOCK = 0x616E6B69  # "anki"


class AnkiSyncMisconfigured(AnkiSyncError):
    """Required Anki config (a user's credentials, or the mirror dir) is unset."""


def retired_ref_ids(store: Store, *, login: str, window_days: int = 90) -> list[int]:
    """Recently soft-deleted *authored* cards owned by ``login`` (the
    card_forge retire/rewrite path, or a manual delete) whose Anki notes
    should be removed. Foreign projections are excluded — they were never
    pushed under a precis guid, and the 2026-07 incident soft-deleted ~93k of
    them (no point shipping that list to the mirror every tick)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE kind='anki' AND owner_login = %s "
            "AND retired_at IS NOT NULL "
            "AND retired_at >= now() - make_interval(days => %s) "
            "AND COALESCE(meta->>'source','') != 'anki-foreign'",
            (login, window_days),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _sync_one_user(
    store: Store,
    cfg: Any,
    login: str,
    root: Path,
    *,
    limit: int,
    dry_run: bool,
    fix: bool,
    project: bool,
    no_retire: bool,
    claim: bool,
) -> str:
    """One user's tick. Returns their summary line(s); raises
    :class:`AnkiSyncError` (or a subclass) on failure — the caller collects
    per-user failures rather than letting one abort the whole fan-out."""
    from precis.anki.creds import get_user_credentials
    from precis.anki.notes import spec_from_ref
    from precis.anki.sync import sync_tick

    prefix = f"anki-sync[{login}]"

    claimed = store.claim_unowned_refs("anki", login) if claim else 0
    claim_note = f" ({claimed} unowned ref(s) claimed for {login})" if claimed else ""

    refs = store.list_refs(kind="anki", owner_login=login, limit=limit)
    specs = [s for s in (spec_from_ref(r) for r in refs) if s is not None]
    retire_ids = [] if no_retire else retired_ref_ids(store, login=login)

    if dry_run:
        return (
            f"{prefix} [DRY-RUN]: {len(specs)} cloze card(s) would sync, "
            f"{len(retire_ids)} retired ref(s) would be removed from the "
            f"mirror.{claim_note}"
        )

    creds = get_user_credentials(store, login)
    if creds is None:  # pragma: no cover - anki_logins() already filtered these out
        raise AnkiSyncMisconfigured(
            f"{login!r} has no AnkiWeb credentials configured on /account."
        )
    email, password = creds

    mirror_dir = root / login
    mirror_dir.mkdir(parents=True, exist_ok=True)
    mirror_path = str(mirror_dir / "mirror.anki2")

    # Per-user advisory lock: only one sync per AnkiWeb account at a time,
    # but two different users' syncs never contend on each other's lock.
    with store.pool.connection() as conn:
        lock_row = conn.execute(
            "select pg_try_advisory_lock(%s, hashtext(%s))", (_ANKI_SYNC_LOCK, login)
        ).fetchone()
        got = lock_row[0] if lock_row else False
        if not got:
            return f"{prefix}: another sync holds the lock; skipping."
        try:
            result, stats = sync_tick(
                mirror_path=mirror_path,
                user=email,
                password=password,
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
            lines = [f"{prefix}: {result.summary()}{claim_note}"]
            if result.all_cards is not None:
                from precis.anki.project import project_cards

                proj = project_cards(store, result.all_cards, owner_login=login)
                lines.append(f"{prefix}: {proj.summary()}")
            summary = "\n".join(lines)
            if result.aborted:
                raise AnkiSyncError(f"sync aborted for {login}: {summary}")
            return summary
        finally:
            conn.execute(
                "select pg_advisory_unlock(%s, hashtext(%s))", (_ANKI_SYNC_LOCK, login)
            )


def run_anki_sync(
    store: Store,
    cfg: Any,
    *,
    limit: int = 10000,
    dry_run: bool = False,
    fix: bool = False,
    project: bool = False,
    no_retire: bool = False,
    login: str | None = None,
) -> str:
    """One sync tick over every user with AnkiWeb credentials configured (or
    just ``login``, when given). Returns a human-readable summary — one line
    (or a few) per user, joined by newlines. Raises on failure — never
    ``sys.exit``:

    * :class:`AnkiSyncMisconfigured` — ``login`` was given but has no
      credentials configured, or ``cfg.anki_mirror_dir`` is unset (a caller
      should surface this once, loudly; the cadence wrapper logs it like any
      other cadence exception). No configured users at all is NOT this case
      — the cadence fires every 30 minutes and must not spam errors when
      nobody has visited ``/account`` yet, so that returns a summary line
      instead.
    * :class:`precis.anki.sync.AnkiNotInstalled` — the ``anki`` wheel isn't
      importable on this runner.
    * :class:`precis.anki.sync.AnkiSyncError` — a user's guarded sync itself
      failed or aborted (a ``FULL_UPLOAD`` risk — never allowed). One user's
      failure doesn't stop the others; if any user's sync raised, a single
      ``AnkiSyncError`` summarising all of them is raised after every user
      has been attempted.
    """
    from precis.anki.creds import anki_logins, user_anki_configured

    all_logins = anki_logins(store)
    if login is not None:
        if not user_anki_configured(store, login):
            raise AnkiSyncMisconfigured(
                f"{login!r} has no AnkiWeb credentials configured on /account."
            )
        logins = [login]
    else:
        logins = all_logins

    if not logins:
        return "anki-sync: no user has AnkiWeb credentials — add them on /account"

    if not cfg.anki_mirror_dir:
        raise AnkiSyncMisconfigured("set PRECIS_ANKI_MIRROR_DIR.")
    root = Path(cfg.anki_mirror_dir).expanduser()

    # The claim-unowned-refs catch-up only fires when there's a single
    # configured user fleet-wide (not merely a single ``--user`` filter) —
    # with more than one, an unowned legacy row has no unambiguous owner.
    solo = all_logins[0] if len(all_logins) == 1 else None

    lines: list[str] = []
    failed: list[str] = []
    for user_login in logins:
        try:
            lines.append(
                _sync_one_user(
                    store,
                    cfg,
                    user_login,
                    root,
                    limit=limit,
                    dry_run=dry_run,
                    fix=fix,
                    project=project,
                    no_retire=no_retire,
                    claim=(user_login == solo),
                )
            )
        except AnkiSyncError as exc:
            failed.append(user_login)
            lines.append(f"anki-sync[{user_login}]: sync failed: {exc}")

    summary = "\n".join(lines)
    if failed:
        raise AnkiSyncError(summary)
    return summary


__all__ = [
    "AnkiSyncMisconfigured",
    "retired_ref_ids",
    "run_anki_sync",
]
