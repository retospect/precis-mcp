"""``precis doctor`` — read-only install verification, one engine for two surfaces.

Each check returns a :class:`Check`: ``ok`` when the install is healthy,
``fail`` when something the setup path owns is broken (counts toward the
CLI's non-zero exit), ``warn`` for everything in between — an optional
extra not installed, a worker that has not run yet, a provider secret
nobody configured. The CLI (``precis.cli.doctor``) prints one line per
check and exits non-zero when any ``fail`` is present; the web
``/status`` Setup tab renders the same table for admins.

Nothing here writes. The one DB-touching heavy check — the kinds sweep —
runs :func:`precis.dispatch.boot` under ``PRECIS_MCP_READ_ONLY=1`` so the
boot-time kinds upsert and the oracle/checklist sync threads stay off,
exactly as they do against a read-replica DSN.

Secrets are never printed: the vault check goes through
:func:`precis.secrets.list_secrets` (masked inventory) and pepper /
provider checks resolve to booleans via ``is_available`` /
``resolve_pepper`` — presence, never values. ``--probe`` (CLI only) runs
the :mod:`precis.secret_status` provider probes, whose ``CheckResult``
contract already guarantees safe-to-render detail strings.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import socket
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.config import PrecisConfig
    from precis.store import Store


@dataclass(frozen=True, slots=True)
class WebTarget:
    """The ``(host, port)`` the web check probes — duck-typed.

    Core ``precis`` code must not import ``precis_web`` (layering), so
    the ``/status`` route passes its ``WebConfig`` in and the CLI falls
    back to reading the same two env vars itself.
    """

    host: str = "127.0.0.1"
    port: int = 9100


log = logging.getLogger(__name__)

#: Check outcome levels. ``FAIL`` alone moves the CLI exit code.
OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass(frozen=True, slots=True)
class Check:
    """One doctor check result.

    ``next`` is the remediation command printed on failure — the spec's
    "one line per check with the next command on failure". It is empty
    for passing checks and for warnings that need no command.
    """

    name: str
    level: str
    detail: str = ""
    next: str = ""


#: The four Postgres roles ``precis setup --roles`` creates
#: (multi-user-overhaul.md); ``doctor --roles`` asserts they exist.
_TIERED_DB_ROLES: tuple[str, ...] = (
    "agent_ro",
    "agent_rw",
    "precis_ingress",
    "precis_system",
)

#: Extensions the schema requires (migrate bootstrap creates the latter
#: two; ``vector`` must already be installed by the operator / setup).
_REQUIRED_EXTENSIONS: tuple[str, ...] = ("vector", "pg_trgm", "btree_gist")

#: Fallback Python floor when the installed dist's Requires-Python
#: metadata is unreadable (running from a checkout, not a wheel).
_MIN_PYTHON: tuple[int, int] = (3, 12)

#: Heartbeat freshness budget, seconds. Same order as the /status
#: panel's stale threshold; generous so a timer-skewed host isn't
#: flagged for a beat that is merely late.
_HEARTBEAT_STALE_S = 900

#: Connect deadline for the DSN and the TCP/HTTP probes (web port,
#: remote embedder). Doctor is an interactive command; a dark endpoint
#: must fail fast, not hang the report.
_PROBE_TIMEOUT_S = 5.0


# ── small helpers ──────────────────────────────────────────────────────────


def _guard(fn, *args: Any, **kwargs: Any) -> Check:
    """Run one check, converting a crash into a FAIL line.

    A check that raises (missing table, unexpected schema, half-set-up
    install) is precisely what doctor exists to surface — it must not
    take the whole report down with it.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        name = getattr(fn, "_check_name", getattr(fn, "__name__", "check"))
        # First line only: psycopg connect failures can be multi-line
        # (one entry per resolved address), which would break the
        # one-line-per-check report shape.
        short = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return Check(
            name=name.removeprefix("_check_"),
            level=FAIL,
            detail=f"check failed: {type(exc).__name__}: {short[:160]}",
            next="precis setup",
        )


@contextmanager
def _quiet_and_read_only() -> Iterator[None]:
    """Mute boot chatter and suppress boot's write sites for the sweep.

    Only the ``precis`` logger is muted — a global ``logging.disable``
    would also eat a web process's concurrent request logs while the
    ``/status`` Setup tab runs the same sweep.
    """
    prev = os.environ.get("PRECIS_MCP_READ_ONLY")
    os.environ["PRECIS_MCP_READ_ONLY"] = "1"
    precis_log = logging.getLogger("precis")
    prev_level = precis_log.level
    precis_log.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        precis_log.setLevel(prev_level)
        if prev is None:
            os.environ.pop("PRECIS_MCP_READ_ONLY", None)
        else:
            os.environ["PRECIS_MCP_READ_ONLY"] = prev


def _min_python() -> tuple[int, int]:
    """The ``>=X.Y`` floor from Requires-Python, else :data:`_MIN_PYTHON`."""
    try:
        from importlib.metadata import metadata

        spec = metadata("precis-mcp").get("Requires-Python") or ""
        m = re.search(r">=\s*(\d+)\.(\d+)", spec)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return _MIN_PYTHON


def _extra_for_module(module: str) -> str | None:
    """Map an import name to its ``[extra]`` via O3's extras table.

    ``precis.cli.main._EXTRA_FOR_MODULE`` lands with O3
    (worktree-g-ingest-metadata); until that commit is on main the
    lookup simply returns ``None`` and the report names the bare
    module. Import is lazy so this module stays importable either way.
    """
    try:
        from precis.cli import main as _cli_main
    except ImportError:
        return None
    table = getattr(_cli_main, "_EXTRA_FOR_MODULE", None)
    if not table:
        return None
    return table.get(module.split(".")[0])


# ── individual checks ──────────────────────────────────────────────────────


def _check_python() -> Check:
    floor = _min_python()
    ver = platform.python_version()
    if sys.version_info >= floor:
        return Check("python", OK, f"{ver} (>= {floor[0]}.{floor[1]})")
    return Check(
        "python",
        FAIL,
        f"{ver} < required {floor[0]}.{floor[1]}",
        next="install a newer Python",
    )


def _check_dsn(dsn: str | None) -> Check:
    """DSN configured + answers a bare connect. No DSN text is printed —
    it may carry a password."""
    if not dsn:
        return Check(
            "database",
            FAIL,
            "no PRECIS_DATABASE_URL configured",
            next="precis setup (or set PRECIS_DATABASE_URL)",
        )
    import psycopg

    try:
        with psycopg.connect(dsn, connect_timeout=int(_PROBE_TIMEOUT_S)) as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as exc:
        short = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return Check(
            "database",
            FAIL,
            f"connect failed: {type(exc).__name__}: {short[:160]}",
            next="precis setup (or fix PRECIS_DATABASE_URL)",
        )
    return Check("database", OK, "connected")


def _check_extensions(store: Store) -> Check:
    with store.pool.connection() as conn:
        rows = conn.execute("SELECT extname FROM pg_extension").fetchall()
    have = {str(r[0]) for r in rows}
    missing = [e for e in _REQUIRED_EXTENSIONS if e not in have]
    if missing:
        return Check(
            "extensions",
            FAIL,
            "missing: " + ", ".join(missing),
            next="precis setup (or CREATE EXTENSION " + ", ".join(missing) + ")",
        )
    return Check("extensions", OK, ", ".join(_REQUIRED_EXTENSIONS))


def _check_migrations(dsn: str) -> Check:
    from precis.store.migrate import packaged_migrator

    status = packaged_migrator(dsn).schema_status()
    if status.pending:
        return Check(
            "migrations",
            FAIL,
            f"{len(status.pending)} pending",
            next="precis migrate",
        )
    if status.ahead:
        return Check(
            "migrations",
            WARN,
            f"{len(status.ahead)} applied migration(s) newer than this build",
        )
    return Check("migrations", OK, "ledger at head")


def _check_vault(store: Store) -> Check:
    """``vault.list()`` succeeding proves ``app.secret_key`` is set and the
    vault is readable. Prints only the count — never names or hints."""
    from precis import secrets as vault

    try:
        entries = vault.list_secrets(store=store)
    except Exception as exc:
        return Check(
            "vault",
            FAIL,
            f"vault.list() failed: {type(exc).__name__} — app.secret_key unset?",
            next="precis setup",
        )
    return Check(
        "vault", OK, f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'}"
    )


def _check_pepper(store: Store) -> Check:
    from precis.users import resolve_pepper

    if resolve_pepper(store=store):
        return Check("pepper", OK, "configured")
    return Check(
        "pepper",
        WARN,
        "no PRECIS_WEB_PASSWORD_PEPPER — new accounts hash unpeppered "
        "(scrypt-v1); precis users add mints one",
    )


def _check_roster(store: Store) -> Check:
    n = store.count_web_users()
    if n == 0:
        return Check(
            "roster",
            FAIL,
            "no web users — every page is a 503",
            next="precis users add <login> --abbrev <ab>",
        )
    # Multi-user S3 adds the role column + the "at least one enabled
    # admin" half of this check; before it lands every account is
    # fully authorised, so non-empty is the whole verdict.
    return Check("roster", OK, f"{n} user{'s' if n != 1 else ''}")


def _bge_m3_cached() -> bool:
    """True when the HF hub cache holds a BAAI/bge-m3 snapshot on disk."""
    cache = os.environ.get("HF_HUB_CACHE") or os.path.join(
        os.environ.get("HF_HOME", "~/.cache/huggingface"), "hub"
    )
    snapshots = os.path.join(
        os.path.expanduser(cache), "models--BAAI--bge-m3", "snapshots"
    )
    try:
        return any(os.scandir(snapshots))
    except OSError:
        return False


def _check_embedder(cfg: PrecisConfig) -> Check:
    name = str(getattr(cfg, "embedder", "mock"))
    if name == "mock":
        return Check("embedder", OK, "mock")
    if name == "bge-m3":
        import importlib.util

        if importlib.util.find_spec("sentence_transformers") is None:
            return Check(
                "embedder",
                FAIL,
                "bge-m3 configured but sentence_transformers is not installed",
                next="pip install 'precis-mcp[embed]'",
            )
        if _bge_m3_cached():
            return Check("embedder", OK, "bge-m3 cached")
        return Check(
            "embedder",
            FAIL,
            "bge-m3 weights not in the HF cache — first embed would fetch ~2 GB",
            next="precis setup --embedder bge-m3",
        )
    if name == "remote":
        url = getattr(cfg, "embedder_url", None)
        if not url:
            return Check(
                "embedder",
                FAIL,
                "embedder=remote but PRECIS_EMBEDDER_URL is unset",
                next="set PRECIS_EMBEDDER_URL",
            )
        import httpx

        from precis.utils.http import call_with_backoff

        endpoints = [e.strip().rstrip("/") for e in url.split(",") if e.strip()]
        for ep in endpoints:

            def _get(ep: str = ep) -> httpx.Response:
                return httpx.get(f"{ep}/healthz", timeout=_PROBE_TIMEOUT_S)

            try:
                resp = call_with_backoff(_get, service="doctor-probe")
            except Exception:
                continue
            if resp.status_code == 200:
                return Check("embedder", OK, f"remote answers ({ep})")
        return Check(
            "embedder",
            FAIL,
            f"no remote embedder endpoint answers /healthz ({len(endpoints)} tried)",
            next="precis serve-embeddings (on the embedder host)",
        )
    return Check("embedder", FAIL, f"unknown embedder {name!r}")


def _check_kinds(store: Store | None, cfg: PrecisConfig) -> Check:
    """Reproduce the server's boot sweep and list the kinds it would log.

    Runs ``precis.dispatch.boot`` read-only (``PRECIS_MCP_READ_ONLY``)
    with the configured roots, then reads ``hub.loadabilities`` — the
    same verdicts the cold-start banner prints. ImportError-derived
    failures are annotated with the ``[extra]`` that provides the
    missing module (O3's table). Always WARN: a minimal install
    legitimately darks kinds.
    """
    from precis.dispatch import boot
    from precis.kind_gate import parse_disabled, parse_disabled_reasons

    with _quiet_and_read_only():
        hub = boot(
            store=store,
            embedder=None,
            precis_root=cfg.root,
            precis_roots=cfg.roots,
            python_roots=cfg.python_roots,
            md_roots=cfg.md_roots,
            kinds_disabled=parse_disabled(cfg.kinds_disabled),
            kinds_disabled_reasons=parse_disabled_reasons(cfg.kinds_disabled),
        )
    dark = {k: v for k, v in hub.loadabilities.items() if not v.loaded}
    if not dark:
        return Check("kinds", OK, f"{len(hub.kinds)} kinds live, none dark")
    notes: list[str] = []
    extras: set[str] = set()
    missing_re = re.compile(r"No module named ['\"]([^'\"]+)['\"]")
    for kind in sorted(dark):
        reason = dark[kind].reason or "?"
        extra = None
        m = missing_re.search(reason)
        if m:
            extra = _extra_for_module(m.group(1))
        if extra:
            extras.add(extra)
            notes.append(f"{kind} ({reason} — needs [{extra}])")
        else:
            notes.append(f"{kind} ({reason})")
    detail = f"{len(hub.kinds)} live, {len(dark)} unavailable: " + "; ".join(notes)
    nxt = f"uv sync --extra {' --extra '.join(sorted(extras))}" if extras else ""
    return Check("kinds", WARN, detail, next=nxt)


def _check_heartbeat(store: Store) -> Check:
    from datetime import UTC, datetime

    beats = store.recent_heartbeats()
    live = [b for b in beats if not (b.meta or {}).get("retired")]
    if not live:
        return Check(
            "heartbeat",
            WARN,
            "no host_heartbeat rows — worker never reported",
        )
    newest = max(b.ts for b in live)
    age = (datetime.now(UTC) - newest).total_seconds()
    if age <= _HEARTBEAT_STALE_S:
        return Check("heartbeat", OK, f"last beat {int(age)}s ago")
    return Check(
        "heartbeat",
        WARN,
        f"last beat {int(age // 60)} min ago — worker not running?",
    )


def _default_web_target() -> WebTarget:
    """Env-derived web target when ``precis_web`` can't be imported."""
    host = os.environ.get("PRECIS_WEB_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("PRECIS_WEB_PORT", "9100"))
    except ValueError:
        port = 9100
    return WebTarget(host=host, port=port)


def _check_web(web: Any) -> Check:
    """Configured ``precis web`` port: listening, free, or squatted on."""
    import httpx

    from precis.utils.http import call_with_backoff

    host, port = web.host, web.port
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host

    def _get() -> httpx.Response:
        return httpx.get(
            f"http://{probe_host}:{port}/healthz", timeout=_PROBE_TIMEOUT_S
        )

    try:
        resp = call_with_backoff(_get, service="doctor-probe")
        if resp.status_code == 200:
            return Check("web", OK, f"precis web listening on {host}:{port}")
    except Exception:
        pass
    try:
        with socket.socket() as s:
            s.bind((probe_host, port))
    except OSError as exc:
        return Check(
            "web",
            FAIL,
            f"{host}:{port} held by another process ({exc.strerror or exc})",
            next="pick a free PRECIS_WEB_PORT",
        )
    return Check("web", OK, f"{host}:{port} free (precis web not running)")


def _check_secrets(store: Store, *, probe: bool) -> Check:
    """Presence-only by default; ``--probe`` runs the provider probes.

    Only names are reported — a missing name is a configuration fact,
    not a secret value.
    """
    from precis import secret_status
    from precis.secrets import is_available

    present = [
        s.name for s in secret_status.KNOWN_SECRETS if is_available(s.name, store=store)
    ]
    total = len(secret_status.KNOWN_SECRETS)
    detail = f"{len(present)}/{total} provider secrets configured"
    missing = [s.name for s in secret_status.KNOWN_SECRETS if s.name not in present]
    level = OK if not missing else WARN
    if missing:
        detail += " (absent: " + ", ".join(missing) + ")"

    if probe and present:
        import asyncio

        results = asyncio.run(secret_status.run_checks(store))
        bad = {g: r for g, r in results.items() if r.state == "bad"}
        unknown = {g: r for g, r in results.items() if r.state == "unknown"}
        detail += f"; probes: {len(results) - len(bad) - len(unknown)} ok"
        if bad:
            level = WARN
            detail += ", rejected: " + ", ".join(sorted(bad))
        if unknown:
            detail += ", unverified: " + ", ".join(sorted(unknown))
    return Check("secrets", level, detail)


def _check_roles(store: Store) -> Check:
    """Assert the four tiered DB roles exist (deployments that ran
    ``precis setup --roles`` or the ansible postgres role)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
            (list(_TIERED_DB_ROLES),),
        ).fetchall()
    have = {str(r[0]) for r in rows}
    missing = [r for r in _TIERED_DB_ROLES if r not in have]
    if missing:
        return Check(
            "roles",
            FAIL,
            "missing DB roles: " + ", ".join(missing),
            next="precis setup --roles",
        )
    return Check("roles", OK, ", ".join(_TIERED_DB_ROLES))


# ── the sweep ──────────────────────────────────────────────────────────────


def run_checks(
    *,
    cfg: PrecisConfig | None = None,
    dsn: str | None = None,
    store: Store | None = None,
    web: Any = None,
    roles: bool = False,
    probe: bool = False,
) -> list[Check]:
    """Run every doctor check, in report order.

    ``store`` is the caller's already-connected store (the ``/status``
    route passes its runtime's); when ``None`` the engine opens and
    closes its own from ``dsn``/``PRECIS_DATABASE_URL``. A DB-dependent
    check against an unreachable or unconfigured DSN reports ``fail``
    with "skipped — database unavailable" rather than vanishing from
    the report.
    """
    if cfg is None:
        from precis.config import load_config

        cfg = load_config()
    if web is None:
        # Core must not import precis_web (layering); the /status route
        # passes its own WebConfig, the CLI derives host/port from env.
        web = _default_web_target()
    if dsn is None:
        dsn = getattr(store, "dsn", None) or cfg.database_url

    checks: list[Check] = [_guard(_check_python)]

    if store is not None:
        # Caller's live store (the /status route): connectivity proven.
        checks.append(Check("database", OK, "connected"))
    else:
        checks.append(_guard(_check_dsn, dsn))

    own_store: Store | None = None
    if store is None and dsn and checks[-1].level != FAIL:
        try:
            from precis.store import Store as _Store

            own_store = _Store.connect(dsn)
            store = own_store
        except Exception:
            log.debug("doctor: store connect failed after DSN probe", exc_info=True)
        if store is not None:
            # Same bind every DB-touching command performs (the static
            # walk in tests/test_cli_binds_store.py demands it before a
            # callee that can reach the router — the kinds sweep's boot
            # references handler methods that do). Also genuinely
            # useful: ``requires_setting`` gates then read live DB
            # overrides, matching what ``precis serve`` sees.
            from precis.cli._common import bind_process_store

            bind_process_store(store)

    def _db(fn, *args: Any) -> Check:
        if store is None:
            return Check(
                fn.__name__.removeprefix("_check_"),
                FAIL,
                "skipped — database unavailable",
            )
        return _guard(fn, store, *args)

    checks.append(_db(_check_extensions))
    # The ledger diff opens its own psycopg connection — run it only
    # after a successful connect, or a dead host costs a second long
    # connect wait inside the report.
    if store is None:
        checks.append(Check("migrations", FAIL, "skipped — database unavailable"))
    elif dsn:
        checks.append(_guard(_check_migrations, dsn))
    else:
        checks.append(Check("migrations", FAIL, "skipped — no DSN for the ledger diff"))
    checks.append(_db(_check_vault))
    checks.append(_db(_check_pepper))
    checks.append(_db(_check_roster))
    checks.append(_guard(_check_embedder, cfg))
    checks.append(_guard(_check_kinds, store, cfg))
    checks.append(_db(_check_heartbeat))
    checks.append(_guard(_check_web, web))
    if store is not None:
        checks.append(_guard(_check_secrets, store, probe=probe))
    else:
        checks.append(Check("secrets", FAIL, "skipped — database unavailable"))
    if roles:
        checks.append(_db(_check_roles))

    if own_store is not None:
        own_store.close()
    return checks


def has_failures(checks: list[Check]) -> bool:
    """True when any check failed — the CLI's exit-code signal."""
    return any(c.level == FAIL for c in checks)


__all__ = [
    "FAIL",
    "OK",
    "WARN",
    "Check",
    "WebTarget",
    "has_failures",
    "run_checks",
]
