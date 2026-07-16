"""Secrets resolver — the thin Python wrapper over the DB secrets vault.

Design-of-record: ``docs/design/secrets-vault.md`` (ADR 0055). Postgres is the
authority: values are pgcrypto-encrypted in ``vault.secrets`` and reached only
through the ``vault.*`` SECURITY DEFINER functions (list / mask / reveal /
set_secret / delete_secret). This module holds **no policy** — it is transport
plus ergonomics (a boot-bound store, a small TTL cache, and a legible
resolution order).

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

import logging
import os
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
        text = (_file_dir() / name).read_text().strip()
    except OSError:
        return None
    return text or None


def _reveal(store: Store, name: str) -> str | None:
    """One ``vault.reveal`` call. Returns None on any vault error (schema
    absent, key unset, DB down) so callers fall through rather than crash."""
    try:
        with store.pool.connection() as conn:
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
    "delete_secret",
    "get_secret",
    "invalidate",
    "is_available",
    "list_secrets",
    "require_secret",
    "set_secret",
]
