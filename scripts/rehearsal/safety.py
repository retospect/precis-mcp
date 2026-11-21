"""The fences: what makes the harness unable to reach production.

Two things are enforced and tested (``tests/test_factory_rehearsal.py``):

1. **The database fence.** :func:`assert_scratch_dsn` judges a DSN the way
   libpq reads it (``psycopg.conninfo.conninfo_to_dict``, not a hand-rolled
   split of the string): exactly one ``host``, loopback, no ``hostaddr`` /
   ``service`` / ``options`` or any key beyond user, password, host, port and
   dbname, ``dbname`` equal to :data:`DB_NAME`, and the ``port`` equal to the
   one Docker published for this run (:func:`pin_port`, called by
   :mod:`.db` with what ``compose port`` answered). The harness never reads a
   DSN from the environment: the only DSN any code here uses is the one
   :mod:`.db` built for its own container, and every consumer calls this
   function first.
2. **The environment allowlist.** :func:`worker_env` builds the worker's
   (and every child's) environment from nothing: a short list of names
   copied from the caller's env, then the harness's own values. No
   ``PRECIS_*``, ``PG*``, ``DATABASE_URL``, token or vault name of the
   caller's survives, whatever it is called. :func:`scrub_process_env`
   does the same to the harness's own process before ``precis`` is
   imported.
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping

#: The scratch database's name; a DSN must name exactly this.
DB_NAME = "precis_rehearsal"

#: Host names accepted as loopback besides an IP in 127/8 or ``::1``.
_LOOPBACK_NAMES = frozenset({"localhost"})

#: The only keys a scratch DSN may carry.
_ALLOWED_KEYS = frozenset({"user", "password", "host", "port", "dbname"})

_pinned_port: int | None = None


class Refused(RuntimeError):
    """A safety fence refused; the message says which and what to do."""


def pin_port(port: int | None) -> None:
    """Record the port Docker published for this run's container (``None``
    forgets it). Until it is pinned every DSN is refused."""
    global _pinned_port
    _pinned_port = port


def _is_loopback(host: str) -> bool:
    host = host.strip("[]").lower()
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def assert_scratch_dsn(dsn: str, *, port: int | None = None) -> str:
    """Return ``dsn`` when libpq would connect it to the scratch database
    and nothing else; raise :class:`Refused` otherwise. ``port`` overrides
    the pinned port (tests)."""
    expected = port if port is not None else _pinned_port
    if expected is None:
        raise Refused(
            "refusing to start: no scratch database has been started in this "
            "process, so there is no published port to compare the DSN with"
        )
    text = (dsn or "").strip()
    if not text:
        raise Refused("refusing to start: no database DSN given")
    if not re.match(r"(?i)postgres(ql)?://", text):
        raise Refused(
            "refusing to start: the database DSN must be a postgresql:// URL "
            "(key/value DSNs and services can name another host)"
        )
    from psycopg.conninfo import conninfo_to_dict

    try:
        info = {k: str(v) for k, v in conninfo_to_dict(text).items()}
    except Exception as exc:
        raise Refused(f"refusing to start: libpq cannot parse the DSN ({exc})") from exc
    extra = sorted(set(info) - _ALLOWED_KEYS)
    if extra:
        raise Refused(
            f"refusing to start: the DSN carries {extra} (only user, password, "
            "host, port and dbname are accepted; the rest can redirect it)"
        )
    host = info.get("host", "")
    if not host:
        raise Refused(
            "refusing to start: the DSN has no host (a default or socket host "
            "could be anything)"
        )
    if "," in host:
        raise Refused("refusing to start: the DSN names several hosts")
    if not _is_loopback(host):
        raise Refused(
            f"refusing to start: database host {host!r} is not loopback; the "
            "rehearsal database is a local container only"
        )
    if info.get("port") != str(expected):
        raise Refused(
            f"refusing to start: DSN port {info.get('port')!r} is not the port "
            f"Docker published for this run ({expected})"
        )
    if info.get("dbname") != DB_NAME:
        raise Refused(
            f"refusing to start: database name {info.get('dbname')!r} is not "
            f"{DB_NAME!r}"
        )
    return text


#: Names copied from the caller's environment into the worker's. Nothing
#: else is. ``HOME`` is NOT here: the worker gets a scratch home so no
#: ``~/.secrets``, keychain-adjacent file or CLI login is readable.
ENV_PASSTHROUGH: tuple[str, ...] = (
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
)

#: Name shapes that must never appear in a worker env unless the harness
#: itself set them (checked on the final env, belt and braces on top of the
#: allowlist).
_FORBIDDEN = re.compile(
    r"^(?:PG[A-Z_]*|DATABASE_URL|DB_URL|PRECIS_FACTORY_GIT_TOKEN|GH_TOKEN|"
    r"GITHUB_TOKEN|GH_ENTERPRISE_TOKEN|ANTHROPIC_[A-Z_]*|OPENAI_[A-Z_]*|"
    r"SSH_AUTH_SOCK|AWS_[A-Z_]*|KIMI_MODEL_[A-Z_]*)$"
)


def worker_env(
    caller: Mapping[str, str],
    *,
    own: Mapping[str, str],
    dsn: str,
) -> dict[str, str]:
    """The environment for the worker and everything it spawns.

    ``caller`` supplies only :data:`ENV_PASSTHROUGH` names; ``own`` is the
    harness's complete list of everything else (PATH, HOME, PRECIS_*...);
    ``dsn`` is checked with :func:`assert_scratch_dsn` and becomes
    ``PRECIS_DATABASE_URL``. Raises :class:`Refused` when ``own`` tries to
    set a forbidden name or a ``PRECIS_*`` connection variable other than
    the DSN."""
    assert_scratch_dsn(dsn)
    env = {k: caller[k] for k in ENV_PASSTHROUGH if caller.get(k)}
    for key, value in own.items():
        if _FORBIDDEN.match(key):
            raise Refused(f"refusing to hand {key} to the worker")
        if key == "PRECIS_DATABASE_URL":
            raise Refused("the DSN is passed as dsn=, never through own=")
        env[key] = value
    env["PRECIS_DATABASE_URL"] = dsn
    leaked = sorted(k for k in env if _FORBIDDEN.match(k))
    if leaked:
        raise Refused(f"worker env would carry {', '.join(leaked)}")
    return env


#: Process-env names :func:`scrub_process_env` removes: every ``PRECIS_*``
#: (the product reads its DSN, vault pointers and token names from there),
#: libpq's own variables and the usual DSN names.
_SCRUB = re.compile(
    r"^(?:PRECIS_.*|PG[A-Z_]*|DATABASE_URL|DB_URL|GH_TOKEN|GITHUB_TOKEN|"
    r"CLAUDE_CODE_OAUTH_TOKEN|KIMI_.*)$"
)


def scrub_process_env(environ: dict[str, str] | None = None) -> list[str]:
    """Remove from ``environ`` (default ``os.environ``) every name a
    production connection or credential could arrive in. Returns the names
    removed. The harness calls this before importing ``precis``: its
    configuration layer reads ``PRECIS_DATABASE_URL`` (and a ``.env`` in the
    working directory, which is why the harness also changes to its scratch
    directory) at import and call time."""
    target = os.environ if environ is None else environ
    gone = sorted(k for k in target if _SCRUB.match(k))
    for k in gone:
        del target[k]
    return gone
