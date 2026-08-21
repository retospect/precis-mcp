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
it is transport plus ergonomics (a boot-bound store, a small TTL cache, and
a legible resolution order).

Resolution order for :func:`get_secret` (env-override-wins is the migration
safety net — a call site can move onto ``get_secret`` with zero behaviour
change while its value still lives in the environment):

1. explicit environment variable ``name`` (bootstrap, tests, transition);
2. ``vault.reveal(name)`` over the boot-bound store (cached briefly);
3. a file ``<PRECIS_SECRETS_FILE_DIR>/<name>`` (default ``~/.secrets/pw``);
4. ``default``.

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
from typing import TYPE_CHECKING

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

#: Short cache so a hot ``get_secret`` doesn't hit the DB every call, while
#: rotation still propagates within one TTL without LISTEN plumbing. Misses are
#: never cached, so a freshly-``set`` secret appears immediately.
_CACHE_TTL_SECONDS = 60.0
_cache: dict[str, tuple[float, str]] = {}
_cache_lock = threading.Lock()

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
    with _cache_lock:
        if name is None:
            _cache.clear()
        else:
            _cache.pop(name, None)


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


def _reveal(store: Store, name: str) -> str | None:
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


def get_secret(
    name: str, *, store: Store | None = None, default: str | None = None
) -> str | None:
    """Resolve a secret by name. See module docstring for the order."""
    env = os.environ.get(name)
    if env:
        return env

    st = store if store is not None else _STORE
    if st is not None:
        now = time.monotonic()
        with _cache_lock:
            hit = _cache.get(name)
            if hit is not None and hit[0] > now:
                return hit[1]
        val = _reveal(st, name)
        if val is not None:
            with _cache_lock:
                _cache[name] = (now + _CACHE_TTL_SECONDS, val)
            return val

    from_file = _from_file(name)
    if from_file is not None:
        return from_file

    return default


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
    "adopt_process_store",
    "bind_store",
    "client_identity",
    "complete_dsn_password",
    "delete_secret",
    "get_secret",
    "invalidate",
    "is_available",
    "list_secrets",
    "require_secret",
    "set_secret",
]
