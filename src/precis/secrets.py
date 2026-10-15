"""Secrets resolver — the thin Python wrapper over the DB secrets vault.

Why DB-resident (not env vars or an external vault service): env reads
scatter across call sites and leak ambiently — a worker spawning
``claude -p``/ansible/ruff hands every child the whole environment —
and env can't scope per role or per name; a dedicated vault service is
new infra to run when Postgres is already the audited, cluster-wide
system of record every process holds a connection to.

Postgres is the authority: values are pgcrypto-encrypted in
``vault.secrets`` and reached only through the ``vault.*`` SECURITY DEFINER
functions (list / mask / reveal / set_secret / delete_secret — reveal is the
sole decrypt path and always writes an audit row; there is deliberately no
bulk-plaintext function). The passphrase lives in server config
(``ALTER SYSTEM SET app.secret_key`` → ``postgresql.auto.conf``), a file
``pg_dump`` structurally never emits — so a **logical** dump is safe to
share (ciphertext + function source that only *names* the key). Physical
backups (``pg_basebackup`` / WAL) copy the key file; the guarantee is
logical-dump-only. Trust model v1 (migration 0059): the functions are
granted to PUBLIC — holding a DSN *is* the boundary; the per-role split +
per-name ACL (a ``vault.acl`` table) are designed and deferrable
one-liners. The
secrets TCB is therefore every package imported into a DSN-holding process:
keep those processes to hash-pinned, audited deps, and push heavy/untrusted
code out-of-process (:func:`adopt_process_store` scrubs the DSN from env so
spawned subprocesses never inherit it). This module holds **no policy** —
it is transport plus ergonomics (a boot-bound store, a version-checked
reveal cache, and a legible resolution order).

Resolution order for :func:`get_secret` (env-override-wins is the migration
safety net — a call site can move onto ``get_secret`` with zero behaviour
change while its value still lives in the environment):

1. explicit environment variable ``name`` (bootstrap, tests, transition);
2. ``vault.reveal(name)`` over the boot-bound store, cached in process
   memory only: re-checked against ``vault.list()``'s ``updated_at`` every
   60 s (no decrypt, no audit row) and re-decrypted on change or after 1 h;
3. a file ``<PRECIS_SECRETS_FILE_DIR>/<name>`` (default ``~/.secrets/pw``);
4. ``default``.

``require_vault_secret`` is the explicit fail-closed consumer seam: audited
vault only, no env/file/cache override, redacted error details. SSH credentials
need origin proof; transition-friendly API consumers keep ``get_secret``.

Everything below the env layer is best-effort: a missing vault schema, an
unset ``app.secret_key``, or an unreachable DB all fall through to the file /
default rather than raise, so the vault can ship dark and be populated
incrementally.
"""

from __future__ import annotations

import getpass
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: Boot-bound store used by call sites that don't thread one through (the
#: staticmethod handlers reading a single API key). ``build_runtime`` binds it;
#: CLI one-shots and tests pass ``store=`` explicitly or rely on env/file.
_STORE: Store | None = None

#: DSN captured when ``adopt_process_store`` scrubs ``PRECIS_DATABASE_URL``
#: from the environment. Lets a later ``build_runtime()`` (e.g. a tool path
#: that lost the env race) fall back to the already-connected store's DSN.
_ADOPTED_DSN: str | None = None

#: Revalidation window. After it, a cached value is re-checked against
#: ``vault.list()``'s ``updated_at`` (no decrypt, no audit row), so a rotation
#: or delete in any process propagates within this window without LISTEN
#: plumbing. Misses are never cached, so a freshly-``set`` secret appears
#: immediately.
_CACHE_TTL_SECONDS = 60.0
#: Hard bound on how long one decrypt is reused, version check or not: each
#: process still writes one ``vault.events`` row per name per hour, so the
#: audit trail keeps showing who holds a key. A 60 s plain TTL decrypted once
#: a minute in every process (~22k/day for one key across the cluster).
_CACHE_MAX_AGE_SECONDS = 3600.0


class _Entry:
    """One cached plaintext. The value never reaches a repr, a pickle or a log.

    ``version`` is the ``vault.list()`` ``updated_at`` seen *before* the
    decrypt, or ``None`` when it could not be read (then the entry expires at
    the revalidation window, as the plain 60 s cache did). Reading the version
    first means a rotation racing the decrypt costs one extra reveal, never a
    stale value.
    """

    __slots__ = ("max_age", "recheck", "value", "version")

    def __init__(
        self, value: str, *, now: float, version: object | None, max_age: float
    ) -> None:
        self.value = value
        self.version = version
        self.recheck = now + _CACHE_TTL_SECONDS
        self.max_age = now + max_age

    def __repr__(self) -> str:
        return "<secrets._Entry redacted>"

    def __reduce__(self) -> Any:
        raise TypeError("cached secrets are not picklable")


_cache: dict[str, _Entry] = {}
_cache_lock = threading.Lock()

#: Fill locks, striped by name: concurrent misses on one name share one
#: decrypt, and the lock count stays fixed however many names are asked for.
_FILL_STRIPES = 64
_fill_locks: list[threading.Lock] = [threading.Lock() for _ in range(_FILL_STRIPES)]

#: When a fill last ended in a miss, per name (monotonic). Callers that were
#: already queued on that fill take its miss instead of re-revealing one by
#: one; later callers still ask the vault, so a freshly-``set`` secret shows
#: at once. Cleared wholesale past :data:`_MISS_RECORDS_MAX` names.
_fill_miss: dict[str, float] = {}
_MISS_RECORDS_MAX = 1024

#: Last ``vault.list()`` snapshot, ``name -> updated_at``, and when it was
#: taken (monotonic). One snapshot serves every name for one window.
_versions: dict[str, object] = {}
_versions_at: float | None = None

#: Expiry of a recent :func:`reveal_unbound` miss, per name (monotonic).
_unbound_miss: dict[str, float] = {}


def _reset_after_fork() -> None:
    """Fresh locks and an empty cache in a forked child.

    A fork while another thread holds ``_cache_lock`` or a fill lock leaves
    that lock held forever in the child, so its first ``get_secret`` would
    deadlock. Clearing also drops the child's references to inherited
    plaintext; the copied pages stay in its memory until reused (Python
    strings cannot be zeroed). The child re-reveals under its own pid, so the
    audit row names it. ``subprocess`` with ``preexec_fn`` (the render
    sandbox) runs this hook too, so it only rebinds and clears.
    """
    global _cache_lock, _versions_at
    _cache_lock = threading.Lock()
    _fill_locks[:] = [threading.Lock() for _ in range(_FILL_STRIPES)]
    _fill_miss.clear()
    _cache.clear()
    _unbound_miss.clear()
    _versions.clear()
    _versions_at = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)

#: Warn-once guard so a persistently-misconfigured vault (no key, no schema)
#: doesn't spam the log on every resolve.
_warned: set[str] = set()

#: Cached ``(host, os_user, pid, ppid, process)`` for the audit row. Fixed for
#: the process's lifetime, so it is computed once — see :func:`_client_identity`.
_IDENTITY: tuple[str, str, int, int, str] | None = None


def bind_store(store: Store | None) -> None:
    """Bind the process-wide store the resolver reveals through (or clear it)."""
    global _STORE
    _STORE = store
    invalidate()


def adopt_process_store(store: Store) -> None:
    """Wire a long-lived process to the vault: bind ``store`` as the resolver's
    store AND scrub ``PRECIS_DATABASE_URL`` from the environment so
    default-inheriting subprocess spawns (claude -p, plan_tick, shell-outs) do
    not receive the DSN. Call once per long-lived process after connecting —
    the server (``build_runtime``) and every ``precis worker`` do. The DSN
    survives as a parameter on the frozen config + the open pool; no post-boot
    code re-derives it from env."""
    bind_store(store)
    global _ADOPTED_DSN
    _ADOPTED_DSN = store.dsn
    os.environ.pop("PRECIS_DATABASE_URL", None)


def get_adopted_dsn() -> str | None:
    """Return the DSN captured by the most recent ``adopt_process_store``,
    or ``None`` if no store has been adopted in this process."""
    return _ADOPTED_DSN


def _split_pgpass_line(line: str) -> list[str]:
    """Split one pgpass line on unescaped ``:`` (``\\:`` and ``\\\\`` escape).

    Only the first four separators split (libpq semantics): the fifth field —
    the password — is the raw remainder of the line, so an unescaped colon in
    the password survives (escapes are still processed throughout)."""
    fields: list[str] = []
    cur: list[str] = []
    it = iter(line)
    for ch in it:
        if ch == "\\":
            nxt = next(it, "")
            cur.append(nxt)
        elif ch == ":" and len(fields) < 4:
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    fields.append("".join(cur))
    return fields


def _pgpass_lookup(*, host: str, port: str, dbname: str, user: str) -> str | None:
    """Password for (host, port, dbname, user) from the pgpass file
    (``PGPASSFILE`` or ``~/.pgpass``), honoring ``*`` wildcards and
    first-match-wins, per libpq's own rules. ``None`` when absent."""
    path = Path(os.environ.get("PGPASSFILE") or (Path.home() / ".pgpass"))
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = _split_pgpass_line(line)
        if len(fields) < 5:
            continue
        if all(
            f == "*" or f == v for f, v in zip(fields[:4], (host, port, dbname, user))
        ):
            return fields[4]
    return None


def complete_dsn_password(dsn: str) -> str:
    """Return ``dsn`` with its password filled in from the host's pgpass file.

    The worker's DSN is password-free by design (§L, gr171431): libpq resolves
    the password from ``PGPASSFILE`` at connect time. That works for any
    process on this host — but a DSN handed *across an isolation boundary*
    (the §13 agent container) lands where no pgpass exists, and every
    connection dies ``fe_sendauth: no password supplied`` (the 2026-08-15
    plan_tick zombie-loop outage). Complete the password here, on the host,
    before the DSN crosses. A DSN that already carries a password, or whose
    pgpass entry can't be found, is returned unchanged (the latter fails at
    connect time exactly as before — no new failure mode).
    """
    try:
        from psycopg.conninfo import conninfo_to_dict

        params = conninfo_to_dict(dsn)
    except Exception:
        return dsn
    if params.get("password"):
        return dsn
    user = str(params.get("user") or "")
    pw = _pgpass_lookup(
        host=str(params.get("host") or "localhost"),
        port=str(params.get("port") or "5432"),
        dbname=str(params.get("dbname") or ""),
        user=user,
    )
    if pw is None:
        return dsn
    from urllib.parse import quote, urlsplit, urlunsplit

    parts = urlsplit(dsn)
    if parts.scheme in ("postgresql", "postgres") and "@" in parts.netloc:
        userinfo, _, hostpart = parts.netloc.rpartition("@")
        # ``user:@host`` (explicit empty password) parses as password="" —
        # falsy, so the guard above falls through; strip the trailing ``:``
        # or the rebuild would emit ``user::pw@host``.
        netloc = f"{userinfo.rstrip(':')}:{quote(pw, safe='')}@{hostpart}"
        return urlunsplit(parts._replace(netloc=netloc))
    # Keyword conninfo (or a URL without userinfo): merge via psycopg, which
    # normalizes to keyword form — equally valid to libpq and psycopg.
    try:
        from psycopg.conninfo import make_conninfo

        return make_conninfo(dsn, password=pw)
    except Exception:
        return dsn


def invalidate(name: str | None = None) -> None:
    """Drop cached plaintext — one name, or all. Call after a rotation."""
    global _versions_at
    with _cache_lock:
        if name is None:
            _cache.clear()
            _unbound_miss.clear()
            _fill_miss.clear()
            _versions.clear()
            _versions_at = None
        else:
            _cache.pop(name, None)
            _unbound_miss.pop(name, None)
            _fill_miss.pop(name, None)
            # The snapshot predates the write: the next fill re-lists.
            _versions_at = None


def _warn_once(key: str, msg: str) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(msg)


def _file_dir() -> Path:
    return Path(
        os.environ.get("PRECIS_SECRETS_FILE_DIR") or (Path.home() / ".secrets" / "pw")
    )


def _from_file(name: str) -> str | None:
    try:
        text = (_file_dir() / name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def mounted_secret(name: str) -> str | None:
    """``name`` from the secrets directory, only when one is mounted explicitly.

    For the values a process needs before it can reach the vault — the DSN
    itself, the MCP bearer token. A container that receives them as ``-e``
    values shows them in ``docker inspect``, ``ps`` and
    ``/proc/<pid>/environ`` (gr458350); one that is given
    ``PRECIS_SECRETS_FILE_DIR`` and a read-only mount reads them here
    instead. Unlike :func:`get_secret`'s file layer there is no default
    directory: a host process must not pick up the prod DSN from
    ``~/.secrets/pw`` just because the file exists."""
    if not os.environ.get("PRECIS_SECRETS_FILE_DIR"):
        return None
    return _from_file(name)


def _client_identity() -> tuple[str, str, int, int, str]:
    """Who we are, for the ``vault.events`` audit row (migration 0111).

    Computed once per process and cached: the values are fixed for a process's
    lifetime, and a reveal must not pay for a ``ps``-style lookup.

    ``process`` is a compact argv summary, not ``argv[0]`` alone — every daemon
    here is some flavour of ``python3.13``, and the subcommand ("worker
    --profile agent") is the part that actually names which one. Truncated,
    and only the first few tokens, so a secret or a path can't ride into the
    audit log through a command line.
    """
    global _IDENTITY
    if _IDENTITY is None:
        try:
            user = getpass.getuser()
        except Exception:
            # getuser() raises when no passwd entry matches the uid — happens
            # in slim containers. The numeric uid still identifies the caller.
            # os.getuid() is POSIX-only, so don't let the fallback itself
            # raise on Windows — the pid is the only handle left there.
            _getuid = getattr(os, "getuid", None)
            user = f"uid:{_getuid()}" if _getuid else f"uid:pid-{os.getpid()}"
        argv = " ".join(
            os.path.basename(a) if i == 0 else _scrub_argv(a)
            for i, a in enumerate(sys.argv[:4])
        )
        _IDENTITY = (
            socket.gethostname(),
            user,
            os.getpid(),
            os.getppid(),
            argv[:200],
        )
    return _IDENTITY


#: Flag names whose *value* must never reach the audit log. Nothing in precis
#: passes a secret on a command line today (every call site uses env or the
#: vault), so this guards a surface that does not yet leak — cheap insurance,
#: because 0111 is the first thing to persist argv into the DB at all and an
#: audit row is exactly the wrong place to discover a token later.
_SECRETY = ("token", "key", "secret", "password", "passwd", "pw", "cred", "dsn")


def _scrub_argv(tok: str) -> str:
    """Redact anything in an argv token that could be a credential."""
    head, sep, _ = tok.partition("=")
    if sep and any(s in head.lower() for s in _SECRETY):
        return f"{head}=<redacted>"
    # A bare high-entropy-looking blob (an inline token, a DSN) — keep the
    # shape for legibility, drop the content.
    if len(tok) > 48 and not tok.startswith("-"):
        return f"<redacted:{len(tok)}>"
    return tok


def _reveal(store: Store, name: str, *, redact_errors: bool = False) -> str | None:
    """One ``vault.reveal`` call. Returns None on any vault error (schema
    absent, key unset, DB down) so callers fall through rather than crash.

    Passes this process's identity so the audit row says *which process* asked,
    not just that the shared ``agent_rw`` role did (migration 0111). Falls back
    to the 1-arg overload against a DB that hasn't taken 0111 yet — a rolling
    deploy runs both for a while, and a secret resolving is far more important
    than its audit row being complete.
    """
    import psycopg

    ident = _client_identity()
    try:
        with store.pool.connection() as conn:
            try:
                row = conn.execute(
                    "SELECT vault.reveal(%s, %s, %s, %s, %s, %s)", (name, *ident)
                ).fetchone()
            except psycopg.errors.UndefinedFunction:
                # Narrow on purpose. A blanket ``except Exception`` here would
                # also swallow a genuine bug in this path (a bad param type, a
                # NUL byte in ``process`` that psycopg rejects client-side) —
                # the retry would then succeed, so the outer handler never
                # fires and the process silently writes NULL-identity rows
                # forever, indistinguishable from a genuinely un-migrated DB.
                # That failure mode defeats the whole point of 0111, quietly.
                # Anything that isn't "the 6-arg function doesn't exist" must
                # reach the outer ``_warn_once``.
                #
                # The failed statement aborts the transaction, so roll back
                # before the retry or the 1-arg call dies with
                # InFailedSqlTransaction — turning an audit-detail gap into a
                # secret that won't resolve at all.
                conn.rollback()
                row = conn.execute("SELECT vault.reveal(%s)", (name,)).fetchone()
    except Exception as exc:
        if redact_errors:
            log.warning(
                "secrets: vault-only reveal unavailable (%s)", type(exc).__name__
            )
            return None
        _warn_once(
            f"reveal:{type(exc).__name__}",
            f"secrets: vault reveal unavailable ({type(exc).__name__}: {exc}); "
            "falling back to file/default. Is the migration applied and "
            "app.secret_key set?",
        )
        return None
    if row is None or row[0] is None:
        return None
    return str(row[0])


class VaultSecretUnavailable(RuntimeError):
    """A vault-only credential cannot be resolved; carries no private detail."""


def require_vault_secret(name: str, *, store: Store) -> str:
    """Audited vault-only lookup, without env/file/cache fallback or error detail.

    SSH credentials must prove vault origin. The transition-friendly resolver
    remains unchanged for existing API consumers; this path fails closed.
    """
    value = _reveal(store, name, redact_errors=True)
    if not value:
        raise VaultSecretUnavailable("credential_unavailable")
    return value


#: The recheck's one round trip: may this role still call the reveal it uses
#: (the 6-arg form, migration 0111), plus ``vault.list()``'s versions. The
#: LEFT JOIN keeps one row when the vault is empty, so the privilege answer
#: always comes back. Neither part decrypts or writes an audit row.
_VERSIONS_SQL = (
    "SELECT has_function_privilege(current_user, "
    "'vault.reveal(text, text, text, integer, integer, text)', 'EXECUTE'), "
    "l.name, l.updated_at "
    "FROM (SELECT 1) AS one LEFT JOIN vault.list() AS l ON true"
)


def _list_versions(store: Store) -> dict[str, object] | None:
    """``name -> updated_at`` from ``vault.list()``: no decrypt, no audit row.

    ``None`` when this role can no longer execute ``vault.reveal`` or on any
    error; the caller then drops every cached plaintext, so a revoked grant
    stops serving within one window, not at the hour's max age. The detail
    stays out of the log (type name only)."""
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(_VERSIONS_SQL).fetchall()
    except Exception as exc:
        _warn_once(
            f"versions:{type(exc).__name__}",
            f"secrets: vault recheck unavailable ({type(exc).__name__}); "
            "dropped cached secrets, falling back to a 60 s reveal",
        )
        return None
    if not rows or rows[0][0] is not True:
        _warn_once(
            "versions:revoked",
            "secrets: this role can no longer execute vault.reveal; "
            "dropped cached secrets",
        )
        return None
    return {str(r[1]): r[2] for r in rows if r[1] is not None}


def _current_versions(
    store: Store, now: float
) -> tuple[dict[str, object], float] | None:
    """The version snapshot and when it was taken (monotonic), refreshed at
    most once per window per process. ``None`` when the recheck fails or the
    reveal grant is gone, and then every cached plaintext is dropped."""
    global _versions_at
    with _cache_lock:
        if _versions_at is not None and now - _versions_at < _CACHE_TTL_SECONDS:
            return _versions, _versions_at
    snap = _list_versions(store)
    with _cache_lock:
        _versions.clear()
        if snap is None:
            _versions_at = None
            _cache.clear()
            return None
        _versions.update(snap)
        _versions_at = now
        return _versions, now


def _fill_lock(name: str) -> threading.Lock:
    return _fill_locks[hash(name) % _FILL_STRIPES]


def _cached_reveal(store: Store, name: str) -> str | None:
    """``vault.reveal`` behind the version-checked cache.

    Fresh entry: served. Past the revalidation window: one ``vault.list()``
    per window confirms the version and extends it to one window past that
    snapshot, until the hard max age.
    Otherwise (miss, rotated, deleted, aged out, unverifiable) one thread per
    name decrypts while concurrent callers wait for its result.
    """
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(name)
    if hit is not None and now < hit.recheck:
        return hit.value
    if hit is not None and hit.version is not None and now < hit.max_age:
        snap = _current_versions(store, now)
        if snap is not None and snap[0].get(name) == hit.version:
            # The snapshot only proves the version as of when it was taken,
            # so the extension runs from then, not from now: a rotation is
            # seen within one window of happening, never two.
            hit.recheck = snap[1] + _CACHE_TTL_SECONDS
            return hit.value
    arrived = now
    with _fill_lock(name):
        now = time.monotonic()
        with _cache_lock:
            filled = _cache.get(name)
            missed = _fill_miss.get(name)
        if filled is not None and now < filled.recheck:
            return filled.value
        if missed is not None and missed > arrived:
            return None
        snap = _current_versions(store, now)
        version = snap[0].get(name) if snap is not None else None
        val = _reveal(store, name)
        with _cache_lock:
            if val is None:
                _cache.pop(name, None)
                if len(_fill_miss) >= _MISS_RECORDS_MAX:
                    _fill_miss.clear()
                _fill_miss[name] = time.monotonic()
            else:
                _fill_miss.pop(name, None)
                _cache[name] = _Entry(
                    val, now=now, version=version, max_age=_CACHE_MAX_AGE_SECONDS
                )
        return val


def get_secret(
    name: str, *, store: Store | None = None, default: str | None = None
) -> str | None:
    """Resolve a secret by name. See module docstring for the order."""
    env = os.environ.get(name)
    if env:
        return env

    st = store if store is not None else _STORE
    if st is not None:
        val = _cached_reveal(st, name)
        if val is not None:
            return val

    from_file = _from_file(name)
    if from_file is not None:
        return from_file

    return default


def reveal_unbound(name: str) -> str | None:
    """Vault reveal for a process that never bound a store — a CLI one-shot.

    Daemons bind a store at boot (:func:`adopt_process_store`), so
    :func:`get_secret` reaches the vault. A one-shot such as ``precis taproot
    repair-evidence`` run from an ssh shell binds none, and its lookups stop at
    env/file. Credentials that must come from the vault (the ``claude -p``
    OAuth token, Reto 2026-10-11, gr260050) use this instead: one
    short-lived connection to the configured database, one audited reveal, closed.

    Returns ``None`` when a store is already bound (the caller's
    :func:`get_secret` already asked it), when no database is configured, or on
    any connect/reveal failure — never raises.
    """
    if _STORE is not None:
        return None
    # A repair pass spawns claude once per item: reuse the outcome within one
    # TTL rather than open a connection per spawn. Misses are remembered too,
    # so a down DB costs one connect timeout per TTL, not one per spawn.
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(name)
        if hit is not None and now < hit.recheck:
            return hit.value
        if _unbound_miss.get(name, 0.0) > now:
            return None
    val = _reveal_unbound_once(name)
    with _cache_lock:
        if val is not None:
            # No bound store to check versions against: a plain 60 s entry.
            _cache[name] = _Entry(
                val, now=now, version=None, max_age=_CACHE_TTL_SECONDS
            )
        else:
            _unbound_miss[name] = now + _CACHE_TTL_SECONDS
    return val


def _reveal_unbound_once(name: str) -> str | None:
    """One connect + reveal + close for :func:`reveal_unbound`; never raises."""
    try:
        from precis.config import load_config

        dsn = load_config().database_url
    except Exception as exc:
        _warn_once("unbound-config", f"secrets: no config for vault ({exc})")
        return None
    if not dsn:
        return None
    try:
        from precis.store import Store

        store = Store.connect(dsn, min_size=1, max_size=1)
    except Exception as exc:
        _warn_once(
            f"unbound-connect:{type(exc).__name__}",
            f"secrets: vault connect failed ({type(exc).__name__})",
        )
        return None
    try:
        return _reveal(store, name)
    finally:
        try:
            store.close()
        except Exception:
            log.debug("secrets: closing the one-shot vault store raised")


def require_secret(name: str, *, store: Store | None = None) -> str:
    """Like :func:`get_secret` but raises ``KeyError`` when unresolved — for
    call sites that must fail loudly rather than degrade."""
    val = get_secret(name, store=store)
    if val is None:
        raise KeyError(name)
    return val


def is_available(name: str, *, store: Store | None = None) -> bool:
    """True iff ``name`` resolves to a non-empty value anywhere. Used by the
    kind-availability gate (parallel to ``KindSpec.requires_env``)."""
    return get_secret(name, store=store) is not None


def client_identity() -> tuple[str, str, int, int, str]:
    """Public wrapper over :func:`_client_identity` — the
    ``(host, os_user, pid, ppid, process)`` tuple computed for the vault
    audit row (migration 0111). :mod:`precis.settings` reuses this for its
    own ``app_settings.updated_by`` column (migration 0125) so both write
    paths share one identity computation rather than duplicating it."""
    return _client_identity()


# ── write side (CLI + web editor) ─────────────────────────────────────────


def set_secret(name: str, value: str, *, store: Store) -> None:
    """Encrypt-and-store ``value`` under ``name``; invalidate the cache."""
    with store.pool.connection() as conn:
        conn.execute("SELECT vault.set_secret(%s, %s)", (name, value))
        conn.commit()
    invalidate(name)


def delete_secret(name: str, *, store: Store) -> None:
    """Remove ``name`` from the vault; invalidate the cache."""
    with store.pool.connection() as conn:
        conn.execute("SELECT vault.delete_secret(%s)", (name,))
        conn.commit()
    invalidate(name)


def list_secrets(*, store: Store) -> list[dict[str, object]]:
    """The masked inventory — ``[{name, hint, updated_at}]``, no plaintext."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT name, hint, updated_at FROM vault.list()"
        ).fetchall()
    return [{"name": r[0], "hint": r[1], "updated_at": r[2]} for r in rows]


__all__ = [
    "VaultSecretUnavailable",
    "adopt_process_store",
    "bind_store",
    "client_identity",
    "complete_dsn_password",
    "delete_secret",
    "get_secret",
    "invalidate",
    "is_available",
    "list_secrets",
    "mounted_secret",
    "require_secret",
    "require_vault_secret",
    "set_secret",
]
