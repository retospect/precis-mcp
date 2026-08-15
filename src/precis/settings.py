"""DB-resident settings — the non-secret sibling of :mod:`precis.secrets`.

Why (``docs/backlog/db-resident-settings.md``): non-secret config reaches
code as env vars, and env has to survive a chain of launchers before code
sees it — ansible template → launchd plist / systemd unit → asa's stdio MCP
``env`` block → spawned ``precis serve`` → sometimes a ``claude -p`` child.
Every link is a place a value dies silently: a kind gated on ``requires_env``
then just doesn't register, with no error anywhere. Two live incidents
(2026-08) trace to exactly this — asa lost the paper kind, and an API caller
broke for want of its polite-pool email. Fixing ansible repairs one link in
one chain, once; the class recurs with every new spawn path. The DB
connection is the one dependency that provably survives every chain: a
process that can't reach the store crashes at boot by design
(``build_runtime`` retry-then-raise), so anything resolved through the store
is either available uniformly fleet-wide or the process isn't up at all.

**Precedence: DB row → registry env var → compiled default — DB wins.**
This is deliberately the *opposite* of :mod:`precis.secrets` (env-override-
wins there, so a call site can adopt the vault with zero behaviour change
while the value still lives in env). Settings inverts it on purpose: one DB
write repairs every host at once, while a stale env var on a drifted host
would otherwise keep silently winning over the fleet-wide fix. Env stays the
bootstrap/test escape hatch and the ansible baseline becomes the fallback
tier, not the authority.

This module is the promotion of ``precis.budget.settings`` (private to the
budget caps since migration 0067/0070) into a first-class, registered
config layer — a **key registry** (:data:`REGISTRY`, one :class:`SettingSpec`
per known key: name, type, env var, compiled default, one-line doc) plus
typed getters over the same generic ``app_settings`` KV table. A read of a
key *not* in the registry warns once (kills the key-typo-silently-defaults
failure mode) and degrades to ``env(name) → the call's default`` — no DB
tier, because the DB tier needs the env-var name and compiled default the
registry declares; an unregistered key has neither.

A ~60s TTL cache mirrors :mod:`precis.secrets`' ``_CACHE_TTL_SECONDS``
pattern (misses never cached, so a freshly-``set`` value appears
immediately; a hit is reused for the TTL so hot paths don't hit the DB per
read) — a flag flip propagates fleet-wide within a minute without restarts.

**Boundary — what never moves here.** Bootstrap config stays env forever:
``PRECIS_DATABASE_URL``, ``db_connect_retry_seconds``, boot log level —
anything needed before or without the DB, or governing DB-outage behaviour.
Per-host topology facts (``PRECIS_ROOT``, ``corpus_dir``, ``python_roots``,
``embedder_url``, runner designations) stay in the ansible inventory —
declarative, versioned, per-host by construction; this table's ``scope``
column (migration 0067) is reserved for host-scoped rows but resolution
semantics for it are **not implemented** here. Test knobs
(``embedder="mock"``, …) stay env too — tests must not need a DB row.

**Mixed-version fleet discipline**, same rule as the SQL migrations this
sits next to: every read has a sane compiled default (an old process reading
a key a newer process wrote still degrades cleanly), and a semantic change
to what a key *means* mints a **new** key — never repurpose one. Forward-
only.

``precis.budget.settings`` remains the generic, unregistered ``app_settings``
KV surface (arbitrary keys — live_config chain overrides, dream_throttle
intervals, health_digest timestamps, …) that don't fit a static registry
entry; only the four budget-cap keys that motivated ``app_settings`` in the
first place are promoted into :data:`REGISTRY` here. See the module's own
docstring for that boundary.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

SettingType = Literal["str", "float", "int", "bool"]


@dataclass(frozen=True, slots=True)
class SettingSpec:
    """One declared key in :data:`REGISTRY` — the ``PrecisConfig``
    field-docstring discipline, relocated here so a typo'd key name warns
    instead of silently resolving to a default."""

    key: str
    type: SettingType
    env_var: str | None
    default: object
    doc: str


#: Known settings. Seeded from the four keys ``precis.budget.settings``
#: already resolved DB → env → compiled-default for the budget caps
#: (env var names / defaults pulled from ``precis.budget.meter`` and
#: ``precis.budget.quota`` so behaviour is unchanged).
REGISTRY: dict[str, SettingSpec] = {
    spec.key: spec
    for spec in (
        SettingSpec(
            key="budget.hourly_usd",
            type="float",
            env_var="PRECIS_BUDGET_HOURLY_USD",
            default=5.0,
            doc="Hourly USD spend cap for the budget circuit breaker "
            "(precis.budget.meter).",
        ),
        SettingSpec(
            key="budget.daily_usd",
            type="float",
            env_var="PRECIS_BUDGET_DAILY_USD",
            default=20.0,
            doc="24h USD spend cap for the budget circuit breaker "
            "(precis.budget.meter).",
        ),
        SettingSpec(
            key="budget.quota_ceiling_pct",
            type="float",
            env_var="PRECIS_QUOTA_CEILING_PCT",
            default=100.0,
            doc="used_percentage ceiling that pauses the claude-OAuth lane "
            "(precis.budget.quota); 100 = pause only on an explicit rejection.",
        ),
        SettingSpec(
            key="budget.resume_until",
            type="str",
            env_var=None,
            default=None,
            doc="Manual 'resume paid work now' override instant (ISO-8601 "
            "UTC), set from the /budget page. DB-only — no env fallback "
            "makes sense for a one-shot manual override.",
        ),
        SettingSpec(
            key="contact.crossref_mailto",
            type="str",
            env_var="PRECIS_CROSSREF_MAILTO",
            default=None,
            doc="Crossref/habanero polite-pool mailto; unset means the "
            "anonymous pool (94s vs 13s reference walks).",
        ),
        SettingSpec(
            key="contact.polite_email",
            type="str",
            env_var="PRECIS_UNPAYWALL_EMAIL",
            default=None,
            doc="General polite-pool contact email (Unpaywall, OpenAlex, "
            "download User-Agent).",
        ),
        SettingSpec(
            key="contact.edgar_user_agent",
            type="str",
            env_var="PRECIS_EDGAR_USER_AGENT",
            default="precis-mcp (+https://github.com/retospect/precis-mcp)",
            doc="SEC EDGAR requires a User-Agent with contact info.",
        ),
        SettingSpec(
            key="contact.wikipedia_ua",
            type="str",
            env_var="PRECIS_WIKIPEDIA_UA",
            default="precis-mcp/2.0 (+https://github.com/retospect/precis-mcp)",
            doc="Wikimedia requires a descriptive User-Agent with contact "
            "info; generic/empty UAs get hard-blocked.",
        ),
    )
}

#: Process-bound store call sites don't thread one through use. ``bind_store``
#: sets it at boot (mirrors ``precis.secrets``); CLI one-shots and tests pass
#: ``store=`` explicitly or rely on env/compiled defaults.
_STORE: Store | None = None

#: Short cache so a hot getter doesn't hit the DB every call, while a write
#: still propagates within one TTL without LISTEN plumbing. Misses are never
#: cached, so a freshly-``set`` value appears immediately.
_CACHE_TTL_SECONDS = 60.0
_cache: dict[str, tuple[float, str]] = {}
_cache_lock = threading.Lock()

#: Warn-once guard so a persistently-typo'd key doesn't spam the log.
_warned: set[str] = set()


def bind_store(store: Store | None) -> None:
    """Bind the process-wide store settings resolve through (or clear it)."""
    global _STORE
    _STORE = store
    invalidate()


def invalidate(key: str | None = None) -> None:
    """Drop cached values — one key, or all. Call after a write."""
    with _cache_lock:
        if key is None:
            _cache.clear()
        else:
            _cache.pop(key, None)


def _warn_once(guard: str, msg: str) -> None:
    if guard not in _warned:
        _warned.add(guard)
        log.warning(msg)


def _db_read(store: Store, key: str) -> str | None:
    """One ``app_settings`` read. Best-effort: a missing table / unreachable
    DB returns ``None`` (caller falls through to env / default) rather than
    raising, so this module ships dark on an un-migrated DB."""
    try:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = %s", (key,)
            ).fetchone()
    except Exception:
        log.debug(
            "settings: app_settings read failed for %s (table missing?)",
            key,
            exc_info=True,
        )
        return None
    return str(row[0]) if row else None


def _identity_summary() -> str:
    """Compact ``updated_by`` string for a write — reuses the same client
    identity the vault audit row computes (:func:`precis.secrets.client_identity`,
    migration 0111) rather than duplicating the host/user/pid/argv logic."""
    from precis.secrets import client_identity

    host, user, pid, _ppid, process = client_identity()
    return f"{user}@{host} pid={pid} {process}"


def _resolve_raw(
    key: str, *, store: Store | None, default: object | None
) -> tuple[object | None, str]:
    """Tiered resolution returning the raw (uncoerced) value plus the layer
    it came from. DB values are always ``str`` (the column type); the
    ``"default"`` layer returns ``default`` (or the registry default)
    exactly as given, so a typed getter can coerce either shape."""
    entry = REGISTRY.get(key)
    if entry is None:
        _warn_once(
            f"unregistered:{key}",
            f"settings: read of unregistered key {key!r} — register it in "
            "precis.settings.REGISTRY. Resolving env-only (no DB tier).",
        )
        env_val = os.environ.get(key)
        if env_val:
            return env_val, "env"
        return default, "default"

    st = store if store is not None else _STORE
    if st is not None:
        now = time.monotonic()
        with _cache_lock:
            hit = _cache.get(key)
            if hit is not None and hit[0] > now:
                return hit[1], "db"
        val = _db_read(st, key)
        if val is not None:
            with _cache_lock:
                _cache[key] = (now + _CACHE_TTL_SECONDS, val)
            return val, "db"

    if entry.env_var:
        env_val = os.environ.get(entry.env_var)
        if env_val:
            return env_val, "env"

    eff_default = default if default is not None else entry.default
    return eff_default, "default"


def _coerce_float(raw: object | None, key: str) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return float(str(raw))
    except ValueError:
        _warn_once(f"badfloat:{key}", f"settings: {key} value {raw!r} is not a float")
        return None


def _coerce_int(raw: object | None, key: str) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    try:
        return int(str(raw))
    except ValueError:
        _warn_once(f"badint:{key}", f"settings: {key} value {raw!r} is not an int")
        return None


_TRUE_STRINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_STRINGS = frozenset({"0", "false", "no", "off"})


def _coerce_bool(raw: object | None, key: str) -> bool | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in _TRUE_STRINGS:
        return True
    if s in _FALSE_STRINGS:
        return False
    _warn_once(f"badbool:{key}", f"settings: {key} value {raw!r} is not a bool")
    return None


def get_str(
    key: str, *, store: Store | None = None, default: str | None = None
) -> str | None:
    """Resolve ``key`` as a string. See the module docstring for the order."""
    raw, _layer = _resolve_raw(key, store=store, default=default)
    return None if raw is None else str(raw)


def get_float(
    key: str, *, store: Store | None = None, default: float | None = None
) -> float | None:
    """Resolve ``key`` as a float, or ``None`` when unset / unparsable."""
    raw, _layer = _resolve_raw(key, store=store, default=default)
    return _coerce_float(raw, key)


def get_int(
    key: str, *, store: Store | None = None, default: int | None = None
) -> int | None:
    """Resolve ``key`` as an int, or ``None`` when unset / unparsable."""
    raw, _layer = _resolve_raw(key, store=store, default=default)
    return _coerce_int(raw, key)


def get_bool(
    key: str, *, store: Store | None = None, default: bool | None = None
) -> bool | None:
    """Resolve ``key`` as a bool (``1/true/yes/on`` / ``0/false/no/off``,
    case-insensitive), or ``None`` when unset / unparsable."""
    raw, _layer = _resolve_raw(key, store=store, default=default)
    return _coerce_bool(raw, key)


def get(
    key: str, *, store: Store | None = None, default: object | None = None
) -> object | None:
    """Resolve ``key`` coerced to its registered type (``str`` for an
    unregistered key). Convenience for a caller that doesn't want to pick a
    specific typed getter."""
    value, _layer = resolve(key, store=store, default=default)
    return value


def resolve(
    key: str, *, store: Store | None = None, default: object | None = None
) -> tuple[object | None, str]:
    """Like :func:`get` but also returns the layer the value resolved from —
    ``"db" | "env" | "default"`` — for the CLI/web/doctor surfaces."""
    entry = REGISTRY.get(key)
    raw, layer = _resolve_raw(key, store=store, default=default)
    kind: SettingType = entry.type if entry else "str"
    if kind == "float":
        return _coerce_float(raw, key), layer
    if kind == "int":
        return _coerce_int(raw, key), layer
    if kind == "bool":
        return _coerce_bool(raw, key), layer
    return (None if raw is None else str(raw)), layer


def register(spec: SettingSpec) -> None:
    """Register a key declared outside this module — plugin packages
    (``precis_bio``, ``precis_chem``) own their enable flags and register
    them at import time, before their kind specs reach the gate. Idempotent
    for an identical re-registration (module re-import); a *conflicting*
    respec raises — two owners disagreeing about a key is a bug, not a race
    to win."""
    existing = REGISTRY.get(spec.key)
    if existing == spec:
        return
    if existing is not None:
        raise ValueError(
            f"settings key {spec.key!r} already registered with a different spec"
        )
    REGISTRY[spec.key] = spec


def is_available(key: str, *, store: Store | None = None) -> bool:
    """True iff ``key`` resolves to a usable value anywhere: non-``None``,
    non-empty, and — for a bool-typed key — actually ``True`` (an enable
    flag resolving ``False`` must gate its kind *off*, not count as set).
    Used by the kind-availability gate (parallel to
    ``KindSpec.requires_env`` / ``precis.secrets.is_available``)."""
    value = get(key, store=store)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and not value.strip():
        return False
    return True


def advertised_env_presence() -> list[str]:
    """Registered keys whose env var is set in *this process's* environment
    — self-reported into ``host_heartbeat.meta`` (``settings_env_present``)
    so the condition registry can flag a host still carrying a value now
    shadowed by a DB row (db-resident-settings.md slice 4 visibility).
    Presence only, never the value."""
    return sorted(
        key
        for key, spec in REGISTRY.items()
        if spec.env_var and os.environ.get(spec.env_var)
    )


def list_settings(*, store: Store | None = None) -> list[dict[str, object]]:
    """The registered inventory: ``[{key, value, layer, type, doc, env_var,
    updated_at, updated_by}]`` — one row per :data:`REGISTRY` entry, sorted by
    key. ``updated_at``/``updated_by`` are ``None`` unless a DB row exists."""
    out: list[dict[str, object]] = []
    st = store if store is not None else _STORE
    for key in sorted(REGISTRY):
        entry = REGISTRY[key]
        value, layer = resolve(key, store=store)
        updated_at: object | None = None
        updated_by: str | None = None
        if st is not None and layer == "db":
            updated_at, updated_by = _db_meta(st, key)
        out.append(
            {
                "key": key,
                "value": value,
                "layer": layer,
                "type": entry.type,
                "doc": entry.doc,
                "env_var": entry.env_var,
                "updated_at": updated_at,
                "updated_by": updated_by,
            }
        )
    return out


def _db_meta(store: Store, key: str) -> tuple[object | None, str | None]:
    """Best-effort ``(updated_at, updated_by)`` for ``list_settings`` — a
    separate, uncached query since listing wants a live view. Falls back to
    ``updated_at``-only on a DB that hasn't taken 0125 yet."""
    import psycopg

    try:
        with store.pool.connection() as conn:
            try:
                row = conn.execute(
                    "SELECT updated_at, updated_by FROM app_settings WHERE key = %s",
                    (key,),
                ).fetchone()
            except psycopg.errors.UndefinedColumn:
                conn.rollback()
                row = conn.execute(
                    "SELECT updated_at, NULL FROM app_settings WHERE key = %s",
                    (key,),
                ).fetchone()
    except Exception:
        return None, None
    if row is None:
        return None, None
    return row[0], row[1]


# ── write side (CLI + web editor) ─────────────────────────────────────────


def set_setting(key: str, value: object, *, store: Store) -> None:
    """Upsert ``key`` = ``str(value)`` into ``app_settings`` and invalidate
    the cache. Records ``updated_by`` (migration 0125); tolerates a DB that
    hasn't taken 0125 yet by retrying without it (rolling deploy) — mirrors
    :func:`precis.secrets._reveal`'s fallback to the 1-arg ``vault.reveal``."""
    import psycopg

    sval = str(value)
    ident = _identity_summary()
    with store.pool.connection() as conn:
        try:
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at, updated_by) "
                "VALUES (%s, %s, now(), %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
                "updated_at = now(), updated_by = EXCLUDED.updated_by",
                (key, sval, ident),
            )
        except psycopg.errors.UndefinedColumn:
            conn.rollback()
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) "
                "VALUES (%s, %s, now()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
                "updated_at = now()",
                (key, sval),
            )
        conn.commit()
    invalidate(key)


def clear_setting(key: str, *, store: Store) -> None:
    """Delete one setting (revert to the env / compiled default) and
    invalidate the cache."""
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = %s", (key,))
        conn.commit()
    invalidate(key)


__all__ = [
    "REGISTRY",
    "SettingSpec",
    "advertised_env_presence",
    "bind_store",
    "clear_setting",
    "get",
    "get_bool",
    "get_float",
    "get_int",
    "get_str",
    "invalidate",
    "is_available",
    "list_settings",
    "register",
    "resolve",
    "set_setting",
]
