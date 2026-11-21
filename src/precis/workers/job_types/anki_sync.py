"""``anki_sync`` job_type — an on-demand AnkiWeb sync of ONE user's cards.

The scheduler's ``anki_sync`` cadence (24h29m) and the ``precis anki-sync``
CLI were the only ways to push freshly edited cards (gr747312). This is the
non-SSH door::

    put(kind='job', job_type='anki_sync', parent_id=<todo id>,
        params={'user': '<own login>'})

It runs :func:`precis.workers.anki_sync.run_anki_sync` for ``params.user``
only — ``honor_cfg_flags=False``, so the host's configured precis-fix and
foreign-card projection env flags stay out of scope — and every guard of
the shared core holds: the per-account advisory lock and the refusal of a
``FULL_UPLOAD``.

**Caller binding.** ``params.user`` must name the *authenticated caller's
own login*: :func:`validate` resolves :func:`precis.profiles.acting_login`
and refuses a target that isn't it (an omitted ``user`` binds to the
caller). An unidentified caller (the generic ``agent`` actor — a stdio
serve with no ``PRECIS_ACTING_USER``, a worker mint) is refused. The
submit door stamps ``meta.owner_login``; :func:`_dispatch` refuses a job
without it, so a todo-minted job cannot sync any account.

On top of that, a manual trigger must not hammer AnkiWeb into a 429:

* it refuses while the cadence's failure backoff
  (``anki_sync:next_attempt_at``) is still running, and a failed manual run
  widens that same backoff — including bare AnkiWeb / ``anki``-pylib errors
  :func:`run_anki_sync` folds into ``AnkiSyncError``;
* it is rate-limited per user (:data:`MIN_INTERVAL_S`, ten minutes) — the
  stamp is claimed atomically when the run starts
  (:func:`_claim_cooldown`), so two concurrently dispatched jobs cannot
  both pass the check, and a failed run counts too.

A lock-busy result (:class:`~precis.workers.anki_sync.AnkiSyncBusy`) —
another sync already holds the account's lock — fails the job WITHOUT
touching the shared backoff: no sync ran, so clearing it would lie about
AnkiWeb's health.

The job is held queued until a host advertises the ``anki_sync`` capability
(``PRECIS_ANKI_ENABLED``, ``PRECIS_ANKI_MIRROR_DIR``, and the ``anki`` wheel), see
:func:`precis.workers.capability_probe.probe_soft_signals`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.workers.job_types import JobTypeSpec

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

JOB_TYPE = "anki_sync"

#: Host capability token (a soft signal in the heartbeat) the claim gate
#: holds this job for.
ANKI_SYNC_RESOURCE = "anki_sync"

#: Minimum seconds between two manual syncs of the same user.
MIN_INTERVAL_S = 10 * 60

DESCRIPTION = (
    "Sync one user's anki cards to their own AnkiWeb account now "
    "(rate-limited, honours the cadence's failure backoff)."
)

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The web login whose cards to sync — their own AnkiWeb account.
        # Optional once the caller is identified: it binds to your login,
        # and an identified session cannot sync someone else's account.
        "user": {"type": "string"},
        # List what would sync; do not touch AnkiWeb (not rate-limited).
        "dry_run": {"type": "boolean"},
    },
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS = frozenset({"job_inproc"})
REQUIRES: frozenset[str] = frozenset()


def conditional_requires(params: dict[str, Any]) -> frozenset[str]:
    """Require a configured Anki sync runner."""
    return frozenset({ANKI_SYNC_RESOURCE})


def _stamp_key(login: str) -> str:
    return f"anki_sync:manual_at:{login}"


def _refusal(store: Store, login: str) -> str | None:
    """Why a manual sync for ``login`` must not run now, or ``None``.

    Read-only — the check that feeds a refusal message at submit and at
    dispatch; the dispatch-time rate limit itself is enforced atomically
    by :func:`_claim_cooldown`.
    """
    from precis.utils.timeutil import as_utc
    from precis.workers.scheduler import _anki_backoff_until

    now = datetime.now(UTC)
    until = _anki_backoff_until(store)
    if until is not None and now < until:
        return (
            "AnkiWeb sync is backing off after failures; next attempt allowed "
            f"{until.isoformat()}"
        )
    raw = store.get_setting(_stamp_key(login))
    if raw:
        try:
            last = as_utc(raw)
        except (TypeError, ValueError):
            last = None
        if last is not None:
            wait = MIN_INTERVAL_S - (now - last).total_seconds()
            if wait > 0:
                return (
                    f"{login!r} synced manually {int(MIN_INTERVAL_S - wait)}s ago; "
                    f"retry in {int(wait) + 1}s"
                )
    return None


#: Atomic claim of the per-login manual-sync cooldown: a single upsert whose
#: ``WHERE`` admits the stamp only when the stored one predates the cutoff.
#: Two concurrently dispatched jobs therefore cannot both pass — the loser's
#: ``ON CONFLICT`` re-read sees the winner's fresh stamp, fails the ``WHERE``
#: and returns no row.
_CLAIM_COOLDOWN_SQL = (
    "INSERT INTO app_state (key, value) VALUES (%s, %s) "
    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
    "updated_at = now() "
    "WHERE app_state.value <= %s "
    "RETURNING key"
)


def _claim_cooldown(store: Store, login: str) -> bool:
    """Stamp ``login``'s manual-sync time iff its cooldown has expired;
    returns whether THIS caller won the window.

    Every stamp is written by ``datetime.now(UTC).isoformat()`` — a fixed
    ``+00:00`` shape whose lexical order is chronological — so the plain
    string ``<=`` against the cutoff is a correct "is older than" test, and
    a missing or malformed value (which sorts before any cutoff) can never
    lock a user out.
    """
    now = datetime.now(UTC)
    cutoff = (now - timedelta(seconds=MIN_INTERVAL_S)).isoformat()
    with store.pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                _CLAIM_COOLDOWN_SQL,
                (_stamp_key(login), now.isoformat(), cutoff),
            ).fetchone()
    return row is not None


def validate(store: Store, params: dict[str, Any]) -> None:
    """Submit-time gate: bind the target to the caller, then refuse what
    the run would refuse anyway."""
    from precis.anki.creds import user_anki_configured
    from precis.profiles import acting_login

    if not isinstance(params.get("dry_run", False), bool):
        raise BadInput(
            "anki_sync params.dry_run must be a boolean",
            next="pass true or false as a boolean, or omit params.dry_run",
        )

    caller = acting_login(store)
    if caller is None:
        raise BadInput(
            "anki_sync needs an identified caller — it only syncs your own "
            "account, and this session has no authenticated login",
            next="submit from a session bound to your account (web or MCP login)",
        )
    requested = str(params.get("user") or "").strip()
    if requested and requested != caller:
        raise BadInput(
            f"anki_sync only syncs your own account — {requested!r} "
            f"is not your login ({caller!r})",
            next=f"params={{'user': {caller!r}}}, or omit params.user",
        )
    login = caller
    if not login:
        raise BadInput(
            "anki_sync needs params.user (your own login)",
            next="params={'user': '<login>'}",
        )
    # Persist the bound target: ``params`` is the dict ``JobHandler.put``
    # stores on ``meta.params``, so what was validated is what runs.
    params["user"] = login
    if not user_anki_configured(store, login):
        raise BadInput(
            f"{login!r} has no AnkiWeb credentials configured",
            next="add them on /account, then retry",
        )
    if not params.get("dry_run"):
        why = _refusal(store, login)
        if why is not None:
            raise BadInput(why, next="wait, then retry the same put")


def _dispatch(ctx: Any, spec: Any) -> None:
    from precis.anki.sync import AnkiNotInstalled, AnkiSyncError
    from precis.config import load_config
    from precis.workers.anki_sync import (
        AnkiSyncBusy,
        AnkiSyncMisconfigured,
        run_anki_sync,
    )
    from precis.workers.scheduler import _anki_record_failure

    params = (ctx.meta or {}).get("params") or {}
    login = str(params.get("user") or "").strip()
    dry_run = params.get("dry_run", False)
    if not isinstance(dry_run, bool):
        ctx.record_failure("anki_sync: params.dry_run must be a boolean")
        return
    if not login:
        ctx.record_failure("anki_sync: params.user is required")
        return
    # Verified owner binding: the submit door stamps meta.owner_login from
    # the authenticated caller. A todo-minted job never carries it, so it
    # cannot sync an arbitrary configured account.
    if str((ctx.meta or {}).get("owner_login") or "") != login:
        ctx.record_failure(
            "anki_sync: no verified owner binding for "
            f"{login!r} — submit it with put(kind='job') from your own session"
        )
        return
    cfg = load_config()
    if not cfg.anki_enabled:
        ctx.record_failure("anki_sync: PRECIS_ANKI_ENABLED is off on this host")
        return
    if not dry_run:
        why = _refusal(ctx.store, login)
        if why is not None:
            ctx.record_failure(f"anki_sync: {why}")
            return
        if not _claim_cooldown(ctx.store, login):
            ctx.record_failure(
                f"anki_sync: {login!r} hit the {MIN_INTERVAL_S // 60}-minute "
                "manual-sync cooldown — a concurrent run stamped it between "
                "the check and the claim; retry once it expires"
            )
            return
    try:
        summary = run_anki_sync(
            ctx.store, cfg, dry_run=dry_run, login=login, honor_cfg_flags=False
        )
    except AnkiSyncMisconfigured as exc:
        # A deterministic config gap (creds, mirror dir, DSN) — not AnkiWeb
        # pressure, so the shared backoff is left alone.
        ctx.record_failure(f"anki_sync: {exc}")
        return
    except AnkiNotInstalled as exc:
        ctx.record_failure(f"anki_sync: {exc}")
        return
    except AnkiSyncBusy as exc:
        # A live sync already holds the account's lock — its run covers
        # these cards. Nothing synced here: the shared backoff is neither
        # widened nor cleared.
        ctx.record_failure(f"anki_sync: not run — {exc}")
        return
    except AnkiSyncError as exc:
        if not dry_run:
            _anki_record_failure(ctx.store)
        ctx.record_failure(f"anki_sync: sync failed: {exc}")
        return
    except Exception as exc:
        # An unexpected error escaping run_anki_sync (outside the per-user
        # fold) — same posture as the cadence wrapper: count it against the
        # backoff rather than let a manual trigger hammer a sick AnkiWeb.
        if not dry_run:
            _anki_record_failure(ctx.store)
        ctx.record_failure(f"anki_sync: sync failed: {exc}")
        return
    # A manual success leaves the shared backoff alone: it may hold a failure
    # another account recorded meanwhile, and a no-op run proves nothing
    # about AnkiWeb's health. Only the cadence clears it.
    ctx.append_chunk("job_summary", summary)


SPEC = JobTypeSpec(
    name=JOB_TYPE,
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    dispatch=_dispatch,
    validate=validate,
    conditional_requires=conditional_requires,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["ANKI_SYNC_RESOURCE", "MIN_INTERVAL_S", "SPEC", "load"]
