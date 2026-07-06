"""Version-gated oracle re-ingest.

Runs at boot time on every precis-mcp process that has the oracle
handler registered. Compares the bundled oracle YAML's
*(wheel_version, sha256)* tuple against what's recorded in the
``system`` table; re-ingests only when the local copy is **strictly
newer**, holding a Postgres **transaction-scoped** advisory lock
(``pg_try_advisory_xact_lock``) across one transaction that spans the
whole re-ingest so concurrent boots can't race. A *session*-level lock
would be unsafe here — through pgbouncer transaction pooling it strands
on a recycled backend and re-acquires re-entrantly (false success),
which orphaned oracle refs in prod. The re-ingest is also idempotent
(``ingest_paper`` overwrites in place, keeping ref_ids stable), so even
without the lock a race converges instead of orphaning.

Why version + sha256:

- ``version`` (a monotonic integer derived from ``precis.__version__``)
  prevents older boxes from stomping a newer one's ingested data
  during a rolling deploy.
- ``sha256`` lets us skip the actual re-ingest when the wheel bumped
  but the YAML didn't change.

State is persisted via four ``system`` rows::

    corpus.oracle.version       -> int as string
    corpus.oracle.sha256        -> hex digest
    corpus.oracle.ingested_at   -> ISO timestamp
    corpus.oracle.ingested_by   -> hostname (forensic)

Failure mode: any exception inside the boot path is swallowed with a
warning log. Oracle search keeps working against whatever was already
ingested — degraded gracefully, never breaks startup.
"""

from __future__ import annotations

import hashlib
import logging
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import precis as _precis_pkg
from precis.jobs.ingest_oracles import bundled_oracle_dir, ingest_directory

if TYPE_CHECKING:
    from psycopg import Connection

log = logging.getLogger(__name__)


# Keys under the ``system`` table for oracle corpus state. Namespaced
# so future corpora (``corpus.wisdom.*``, ``corpus.foo.*``) coexist
# without colliding.
_KEY_VERSION = "corpus.oracle.version"
_KEY_SHA256 = "corpus.oracle.sha256"
_KEY_INGESTED_AT = "corpus.oracle.ingested_at"
_KEY_INGESTED_BY = "corpus.oracle.ingested_by"


# Stable advisory-lock id derived from the corpus name. ``hashtext``
# would do this in SQL, but we want the same value either way; SHA-256
# of the namespace, taken modulo 2**63 to fit signed BIGINT.
def _advisory_lock_id() -> int:
    digest = hashlib.sha256(b"precis.corpus.oracle").digest()
    return int.from_bytes(digest[:8], "big", signed=True) >> 1


@dataclass(frozen=True)
class CorpusState:
    """Local-on-disk corpus version + content hash."""

    version: int
    sha256: str


def wheel_version_int(version_str: str | None = None) -> int:
    """Pack ``precis.__version__`` into a single comparable integer.

    Format: ``major*10**12 + minor*10**6 + patch`` so a v6.0.0 wheel
    yields ``6_000_000_000_000`` and a v6.1.2 yields
    ``6_000_001_000_002``. Pre-release suffixes (``a0``, ``rc1``,
    ``.dev3``) are stripped from the patch component so an alpha
    sorts immediately after the released version (a tiny lie but
    one that keeps the gate simple — alphas test against released
    state and that's the right default).

    On any parse error returns ``0``; that means the boot logic
    treats this build as the oldest possible, never overwriting
    real production state. Failsafe by design.
    """
    raw = (version_str or getattr(_precis_pkg, "__version__", "0.0.0")).strip()
    parts = raw.split(".", 2)
    if len(parts) < 3:
        parts = parts + ["0"] * (3 - len(parts))
    try:
        major = int(parts[0])
        minor = int(parts[1])
        # Strip any non-digit tail: "0a0" -> "0".
        patch_str = ""
        for ch in parts[2]:
            if ch.isdigit():
                patch_str += ch
            else:
                break
        patch = int(patch_str or "0")
        return major * 10**12 + minor * 10**6 + patch
    except (ValueError, IndexError):
        log.warning("wheel_version_int: could not parse %r; treating as 0", raw)
        return 0


def compute_corpus_state(src_dir: Path) -> CorpusState | None:
    """Hash every YAML under ``src_dir`` and pair it with the wheel version.

    Returns ``None`` when the directory has no YAML files (e.g. an
    sdist that excluded data) — the caller treats that as
    "nothing to ingest" and skips silently. The hash incorporates
    the filename + content so a renamed file invalidates the cache.
    """
    yaml_files = sorted(src_dir.glob("*.yaml")) + sorted(src_dir.glob("*.yml"))
    if not yaml_files:
        return None

    h = hashlib.sha256()
    for yp in sorted(yaml_files, key=lambda p: p.name):
        h.update(yp.name.encode("utf-8"))
        h.update(b"\0")
        h.update(yp.read_bytes())
        h.update(b"\0")
    return CorpusState(version=wheel_version_int(), sha256=h.hexdigest())


# ---------------------------------------------------------------------------
# system-table state I/O
# ---------------------------------------------------------------------------


def _read_state(store: Any) -> tuple[int, str] | None:
    """Read ``(version, sha256)`` from the ``system`` table.

    Returns ``None`` when no record exists yet (first boot). DB
    failures are logged and surfaced as ``None`` so the caller can
    decide how to react — :func:`maybe_reingest` treats that as
    "can't compare, skip ingest" rather than "never ingested,
    please re-embed everything". The previous behaviour conflated
    the two and caused a ~10 s reingest on every boot when the
    Store API surface drifted.

    A missing ``get_setting`` method is a programming error, not a
    runtime failure — re-raise it so it shows up in tests and
    boot logs instead of silently triggering a re-ingest loop.
    """
    try:
        v_raw = store.get_setting(_KEY_VERSION)
        s_raw = store.get_setting(_KEY_SHA256)
    except AttributeError:
        # Wrong store surface (e.g. tests passing a stub without
        # ``get_setting``). Loud failure — never silently reingest.
        raise
    except Exception as exc:  # pragma: no cover — DB unavailable
        log.warning("oracle_sync: cannot read system state: %s", exc)
        # Sentinel tuple distinct from None: "unknown, do not ingest".
        return -1, ""
    if v_raw is None and s_raw is None:
        return None
    try:
        version = int(v_raw or "0")
    except ValueError:
        version = 0
    return version, s_raw or ""


def _write_state(store: Any, state: CorpusState) -> None:
    """Persist the four corpus-state rows in a best-effort sequence.

    Each ``set_setting`` call is idempotent (upsert). Failure to
    write any row is logged but doesn't roll back the others —
    partial state still gates correctly on the next boot via
    sha256 mismatch.
    """
    now = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")
    host = socket.gethostname() or "unknown"
    try:
        store.set_setting(_KEY_VERSION, str(state.version))
        store.set_setting(_KEY_SHA256, state.sha256)
        store.set_setting(_KEY_INGESTED_AT, now)
        store.set_setting(_KEY_INGESTED_BY, host)
    except Exception as exc:  # pragma: no cover — DB unavailable
        log.warning("oracle_sync: cannot write system state: %s", exc)


def _write_state_conn(conn: Connection, state: CorpusState) -> None:
    """Persist the four corpus-state rows on the caller's transaction.

    Used by the Postgres re-ingest path so the version/sha markers commit
    **atomically with the data** — a crash mid-ingest can't leave the
    marker claiming a corpus that didn't fully land.
    """
    now = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")
    host = socket.gethostname() or "unknown"
    for key, val in (
        (_KEY_VERSION, str(state.version)),
        (_KEY_SHA256, state.sha256),
        (_KEY_INGESTED_AT, now),
        (_KEY_INGESTED_BY, host),
    ):
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (key, val),
        )


# ---------------------------------------------------------------------------
# Boot-time entry point
# ---------------------------------------------------------------------------


def maybe_reingest(
    store: Any,
    embedder: Any,
    *,
    src_dir: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Reconcile the bundled oracle YAML against the DB-recorded state.

    Returns a dict describing the outcome (``status``: one of
    ``up_to_date``, ``older_local``, ``ingested``, ``locked``,
    ``no_data``, ``no_store``, ``error``). Never raises — every
    error path logs a warning and returns ``status='error'`` so
    boot continues even if the gate trips.

    ``force=True`` bypasses both the version and sha256 gates,
    re-ingesting unconditionally (still under the advisory lock).
    Used by the ``--force`` CLI flag for the "I just edited the
    YAML in production, don't make me wait for the next release"
    case.
    """
    if store is None:
        return {"status": "no_store"}

    if src_dir is None:
        src_dir = bundled_oracle_dir()
    if src_dir is None:
        return {"status": "no_data", "reason": "bundled oracle dir not found"}
    src_dir = Path(src_dir)
    if not src_dir.is_dir():
        return {"status": "no_data", "reason": f"{src_dir} is not a directory"}

    local = compute_corpus_state(src_dir)
    if local is None:
        return {"status": "no_data", "reason": "no YAML files in dir"}

    # Read recorded state. ``None`` = first boot, ingest. ``(-1, "")``
    # = transient DB failure, skip and try again next boot rather
    # than re-embedding the whole corpus on a flaky read.
    stored = _read_state(store)
    if stored is not None and stored[0] == -1:
        return {"status": "error", "reason": "could not read system state"}
    stored_version = stored[0] if stored else 0
    stored_sha = stored[1] if stored else ""

    if not force:
        if local.version < stored_version:
            log.info(
                "oracle_sync: local version %d < stored %d; skipping "
                "(peer has newer corpus)",
                local.version,
                stored_version,
            )
            return {
                "status": "older_local",
                "local_version": local.version,
                "stored_version": stored_version,
            }
        if local.version == stored_version and local.sha256 == stored_sha:
            return {
                "status": "up_to_date",
                "version": local.version,
                "sha256": local.sha256,
            }

    # Serialise the ingest across concurrent boots. A SESSION-level
    # advisory lock is UNSAFE through pgbouncer transaction pooling: the
    # lock is acquired on whatever backend serves the statement, then that
    # backend is recycled to another client while the lock lingers — and
    # session locks are re-entrant, so a second host's acquire landing on
    # the same backend gets a false success. Net: no mutual exclusion, and
    # concurrent re-ingests orphaned oracle refs in prod. A
    # TRANSACTION-scoped lock held for one tx that spans the whole
    # re-ingest is pinned to that tx's backend (which pooling keeps
    # stable) and auto-releases on commit — real exclusion, no leak.
    pool = getattr(store, "pool", None)
    if pool is None:
        # Non-Postgres store (test stub / no pool): can't hold a lock or
        # span a tx. Ingest directly — mirrors the old degrade-gracefully
        # path; the version+sha gate above is the only serialisation.
        return _do_ingest(store, embedder, src_dir, local, force=force, conn=None)

    lock_id = _advisory_lock_id()
    try:
        with store.tx() as conn:
            got = conn.execute(
                "SELECT pg_try_advisory_xact_lock(%s)", (lock_id,)
            ).fetchone()
            if not (got and got[0]):
                # A peer holds the ingest lock; it writes the same content,
                # so bail. The (empty) tx commits and releases nothing.
                return {"status": "locked"}
            # All writes below run inside this one tx: atomic + lock-held.
            return _do_ingest(store, embedder, src_dir, local, force=force, conn=conn)
    except Exception as exc:
        # Any ingest error rolls the whole re-ingest back (no half-state,
        # no orphans) — boot continues; the next boot retries.
        log.warning("oracle_sync: ingest failed: %s", exc)
        return {"status": "error", "reason": str(exc)}


def _do_ingest(
    store: Any,
    embedder: Any,
    src_dir: Path,
    local: CorpusState,
    *,
    force: bool,
    conn: Connection | None,
) -> dict[str, Any]:
    """Post-lock re-check + ingest + state write.

    Shared by the Postgres (``conn`` set → atomic, lock-held) and the
    non-PG degrade (``conn=None``) paths. Re-reads committed state so a
    racer that finished between the pre-lock check and here is a no-op.
    """
    post_stored = _read_state(store)
    if post_stored is not None and post_stored[0] == -1:
        return {"status": "error", "reason": "could not read system state"}
    post_version = post_stored[0] if post_stored else 0
    post_sha = post_stored[1] if post_stored else ""
    if not force:
        if local.version < post_version:
            return {
                "status": "older_local",
                "local_version": local.version,
                "stored_version": post_version,
            }
        if local.version == post_version and local.sha256 == post_sha:
            return {
                "status": "up_to_date",
                "version": local.version,
                "sha256": local.sha256,
            }

    log.info(
        "oracle_sync: ingesting bundled oracle dir (local=%d/%s, stored=%d/%s)",
        local.version,
        local.sha256[:8],
        post_version,
        (post_sha or "—")[:8],
    )

    agg = ingest_directory(
        src_dir,
        store=store,
        embedder=embedder,
        overwrite=True,
        dry_run=False,
        conn=conn,
    )

    # Persist state atomically with the data on the PG path (same tx);
    # best-effort on the degrade path.
    if conn is not None:
        _write_state_conn(conn, local)
    else:
        _write_state(store, local)
    return {
        "status": "ingested",
        "version": local.version,
        "sha256": local.sha256,
        "files": agg.get("files", 0),
        "created": agg.get("created", 0),
        "replaced": agg.get("replaced", 0),
        "errors": agg.get("errors", 0),
    }


# ---------------------------------------------------------------------------
# Module-load disable knob
# ---------------------------------------------------------------------------


def is_disabled_by_env() -> bool:
    """True when ``PRECIS_ORACLE_AUTO_REINGEST`` is set to ``0``/``false``.

    Default is on. The escape hatch lets operators temporarily
    disable boot-time reconciliation when debugging migrations or
    running offline tests against a production-like dump.
    """
    val = os.environ.get("PRECIS_ORACLE_AUTO_REINGEST", "1").strip().lower()
    return val in ("0", "false", "no", "off", "")
