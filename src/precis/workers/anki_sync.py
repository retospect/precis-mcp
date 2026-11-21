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
turn; a per-user advisory lock (:data:`precis.anki.notes.ANKI_SYNC_LOCK` + the login's own
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

from precis.anki.notes import ANKI_SYNC_LOCK
from precis.anki.sync import AnkiNotInstalled, AnkiSyncError
from precis.errors import PrecisError
from precis.learning import LearningStore
from precis.store.advisory import try_xact_advisory_lock
from precis.user_context import user_scope

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)


class AnkiSyncMisconfigured(AnkiSyncError):
    """Required Anki config (a user's credentials, or the mirror dir) is unset."""


class AnkiSyncBusy(AnkiSyncError):
    """Every requested login's per-account sync lock is held elsewhere.

    Raised by :func:`run_anki_sync` when no requested login actually synced
    (a concurrent run — another cadence fire, a manual ``precis anki-sync``,
    or an on-demand ``anki_sync`` job — holds each lock). It subclasses
    :class:`AnkiSyncError` so naive ``except AnkiSyncError`` callers keep
    working, but it is NOT a sync failure: callers must catch it first and
    leave the shared failure backoff untouched — a tick that ran nothing
    proves neither that AnkiWeb is healthy again (clearing the backoff)
    nor that it is still hurting (widening it)."""


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

    if dry_run:
        refs = store.list_refs(kind="anki", owner_login=login, limit=limit)
        specs = [s for s in (spec_from_ref(r) for r in refs) if s is not None]
        retire_ids = [] if no_retire else retired_ref_ids(store, login=login)
        boosts = sum(1 for s in specs if s.boost)
        boost_note = (
            f", {boosts} boost(s) would bring due forward to today" if boosts else ""
        )
        return (
            f"{prefix} [DRY-RUN]: {len(specs)} cloze card(s) would sync, "
            f"{len(retire_ids)} retired ref(s) would be removed from the "
            f"mirror{boost_note}."
        )

    # Per-user advisory lock: only one sync per AnkiWeb account at a time,
    # but two different users' syncs never contend on each other's lock.
    # Transaction-scoped on a dedicated connection (gr463966): a session
    # lock is broken behind pgbouncer transaction pooling. The lock
    # connection only holds the lock; the sync itself uses the store pool.
    if store.dsn is None:
        raise AnkiSyncMisconfigured("the store has no DSN to hold the sync lock on.")
    with try_xact_advisory_lock(store.dsn, ANKI_SYNC_LOCK, text_key=login) as got:
        if not got:
            raise AnkiSyncBusy(f"{prefix}: another sync holds the lock; skipping.")
        # The claim catch-up is a write: it belongs INSIDE the lock (a busy
        # run leaves every row alone) and below the dry-run return (a
        # listing pass counts cards but never mutates ownership).
        claimed = store.claim_unowned_refs("anki", login) if claim else 0
        claim_note = (
            f" ({claimed} unowned ref(s) claimed for {login})" if claimed else ""
        )
        refs = store.list_refs(kind="anki", owner_login=login, limit=limit)
        specs = [s for s in (spec_from_ref(r) for r in refs) if s is not None]
        retire_ids = [] if no_retire else retired_ref_ids(store, login=login)

        account = store.get_web_user(login)
        if account is None or not account.enabled:
            return f"{prefix}: account is no longer active; skipping."
        creds = get_user_credentials(store, login)
        if creds is None:  # pragma: no cover - anki_logins() already filtered these out
            raise AnkiSyncMisconfigured(
                f"{login!r} has no AnkiWeb credentials configured on /account."
            )
        email, password = creds

        mirror_dir = root / login
        mirror_dir.mkdir(parents=True, exist_ok=True)
        mirror_path = str(mirror_dir / "mirror.anki2")

        result, stats = sync_tick(
            mirror_path=mirror_path,
            user=email,
            password=password,
            specs=specs,
            deck=cfg.anki_deck,
            fix=fix,
            project=project,
            retire_ref_ids=retire_ids,
        )
        now = datetime.now(UTC).isoformat()
        # Acknowledge the revision from the pushed snapshot. A retag that
        # lands mid-tick has a different revision and stays pending.
        pushed_revisions = (
            {}
            if result.aborted
            else {
                s.ref_id: s.pending_deck_revision
                for s in specs
                if s.pending_deck_revision is not None
            }
        )
        for ref_id in result.deleted_ref_ids:
            # Specs are owner-scoped authored refs. Retiring keeps the deletion
            # durable across ticks; a new explicit add gets a new ref/guid.
            store.retire_ref(ref_id)
        for ref_id, st in stats.items():
            # FLAT keys — `meta_patch` is a shallow jsonb `||` merge, so a
            # nested `{"anki": {...}}` would REPLACE the whole meta.anki
            # object (wiping guid/content_sha the projection dedups on —
            # the 2026-07 incident). Patch top-level keys only.
            patch: dict[str, Any] = {"anki_stats": st, "anki_synced_at": now}
            if ref_id in pushed_revisions:
                patch["anki_synced_deck_revision"] = pushed_revisions[ref_id]
            store.update_ref(ref_id, meta_patch=patch)
        if not result.aborted:
            for spec in specs:
                if spec.restore_pending and spec.ref_id in stats:
                    store.clear_ref_meta_keys(
                        spec.ref_id, ["anki_restore_pending"], source="anki_restore"
                    )
            # Consumed boosts only (a mirror card actually moved — its due
            # date rode this push forward). A `meta ||` merge cannot express
            # a delete, so the marker clears through the dedicated key
            # remover; unconsumed boosts (note not yet in the mirror, or no
            # movable card yet) stay queued for a later tick.
            for ref_id in result.boosted_ref_ids:
                store.clear_ref_meta_keys(
                    ref_id, ["anki_boost_due"], source="anki_boost"
                )
        lines = [f"{prefix}: {result.summary()}{claim_note}"]
        if result.all_cards is not None:
            from precis.anki.project import project_cards

            proj = project_cards(store, result.all_cards, owner_login=login)
            lines.append(f"{prefix}: {proj.summary()}")
        summary = "\n".join(lines)
        if result.aborted:
            raise AnkiSyncError(f"sync aborted for {login}: {summary}")
        if result.review_ids:
            try:
                with user_scope(account.id):
                    LearningStore(store).record_anki_reviews(result.review_ids)
            except PrecisError as exc:
                raise AnkiSyncError(
                    f"could not record learning activity for {login}"
                ) from exc
        return summary


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
    honor_cfg_flags: bool = True,
) -> str:
    """One sync tick over every user with AnkiWeb credentials configured (or
    just ``login``, when given). Returns a human-readable summary — one line
    (or a few) per user, joined by newlines. Raises on failure — never
    ``sys.exit``:

    * :class:`AnkiSyncMisconfigured` — ``login`` was given but has no
      credentials configured, or ``cfg.anki_mirror_dir`` is unset (a caller
      should surface this once, loudly; the cadence wrapper logs it like any
      other cadence exception). No configured users at all is NOT this case
      — the cadence fires on its own schedule and must not spam errors when
      nobody has visited ``/account`` yet, so that returns a summary line
      instead.
    * :class:`precis.anki.sync.AnkiNotInstalled` — the ``anki`` wheel isn't
      importable on this runner.
    * :class:`AnkiSyncBusy` — every requested login's per-account lock was
      held by a concurrent run: nothing synced, and the summary says so per
      login. A caller with a shared failure backoff must leave it alone —
      not a clean tick, not a failed one. A busy login alongside a clean
      sync stays a line in the returned summary (partial busy is not this
      case: something did run).
    * :class:`precis.anki.sync.AnkiSyncError` — a user's guarded sync itself
      failed or aborted (a ``FULL_UPLOAD`` risk — never allowed). One user's
      failure doesn't stop the others. After all accounts are attempted,
      the summary is raised as ``AnkiSyncError`` if any sync failed,
      including bare AnkiWeb errors such as a 429. If the failures were
      all local setup errors, the summary keeps the first exception's type.

    ``honor_cfg_flags`` (default ``True``, the operator-triggers' posture):
    ``cfg.anki_fix_enabled`` / ``cfg.anki_project_enabled`` may turn the
    precis-fix pass and the whole-collection foreign-card projection on
    even when ``fix``/``project`` are ``False``. The on-demand
    ``anki_sync`` job passes ``False`` so its documented scope — "sync this
    login's authored cards", no LLM fix pass, no foreign projection —
    holds even on a host whose env flags are set.
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

    if honor_cfg_flags:
        fix = fix or cfg.anki_fix_enabled
        project = project or cfg.anki_project_enabled

    # The claim-unowned-refs catch-up only fires when there's a single
    # configured user fleet-wide (not merely a single ``--user`` filter) —
    # with more than one, an unowned legacy row has no unambiguous owner.
    solo = all_logins[0] if len(all_logins) == 1 else None

    lines: list[str] = []
    sync_failed = False
    local_error: AnkiSyncMisconfigured | AnkiNotInstalled | None = None
    busy: list[str] = []
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
        except AnkiSyncBusy as exc:
            busy.append(user_login)
            lines.append(str(exc))
        except (AnkiSyncMisconfigured, AnkiNotInstalled) as exc:
            if local_error is None:
                local_error = exc
            lines.append(f"anki-sync[{user_login}]: sync failed: {exc}")
        except Exception as exc:
            # A bare AnkiWeb / ``anki``-pylib error escaping ``sync_tick``'s
            # guard (a 429, a network abort, ``anki.errors.SyncError``) —
            # fold it into the per-user failure set so one user's crash can
            # neither abort the fan-out nor slip past a caller's
            # ``except AnkiSyncError`` failure backoff.
            sync_failed = True
            lines.append(f"anki-sync[{user_login}]: sync failed: {exc}")

    summary = "\n".join(lines)
    if sync_failed:
        raise AnkiSyncError(summary)
    if local_error is not None:
        raise type(local_error)(summary) from local_error
    if len(busy) == len(logins):
        raise AnkiSyncBusy(summary)
    return summary


__all__ = [
    "AnkiSyncBusy",
    "AnkiSyncMisconfigured",
    "retired_ref_ids",
    "run_anki_sync",
]
