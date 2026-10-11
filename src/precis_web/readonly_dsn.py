"""Read-only DSN rewrite + verification for ``scripts/guide-web --db prod``.

The launcher used to ``sed`` the stored DSN's user from ``agent_rw`` to
``agent_ro`` and then *claim* read-only without checking (gr461387): any
other DSN spelling passed through unchanged and booted as the writer.
Here the DSN is parsed structurally (libpq URL or ``key=value`` form),
forced to the read-only login, and the effective role is verified on a
real connection before the server starts. The DSN is never printed on
error.

CLI: ``python -m precis_web.readonly_dsn <dsn-file>`` — prints the verified
read-only DSN on stdout, exits 1 with a message on stderr otherwise.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from psycopg.conninfo import conninfo_to_dict, make_conninfo

RO_USER = "agent_ro"


class ReadOnlyRefused(RuntimeError):
    """The target is not a verified read-only connection."""


def rewrite_dsn(dsn: str) -> str:
    """Return ``dsn`` forced to ``agent_ro`` on the host-side loopback.

    Handles URL userinfo and ``user=`` keyword spellings alike. Drops the
    inline password (pgpass supplies agent_ro's) and any ``options``
    startup parameter (pgbouncer rejects it).
    """
    try:
        d: dict[str, Any] = conninfo_to_dict(dsn.strip())
    except Exception as exc:  # psycopg raises ProgrammingError
        raise ReadOnlyRefused("stored DSN is not a parsable conninfo") from exc
    if not d.get("host"):
        raise ReadOnlyRefused("stored DSN has no host")
    if d["host"] == "host.docker.internal":
        d["host"] = "127.0.0.1"
    d["user"] = RO_USER
    d.pop("password", None)
    d.pop("options", None)
    return make_conninfo(**d)


def verify_read_only(dsn: str, connect: Callable[..., Any] | None = None) -> None:
    """Connect via ``dsn``; refuse unless agent_ro without INSERT on kinds."""
    if connect is None:
        import psycopg

        connect = psycopg.connect
    try:
        with connect(dsn, connect_timeout=10) as conn:
            row = conn.execute(
                "SELECT current_user, has_table_privilege(current_user, 'kinds', 'INSERT')"
            ).fetchone()
    except Exception as exc:
        raise ReadOnlyRefused(
            f"could not verify the effective role ({type(exc).__name__})"
        ) from exc
    if row is None or row[0] != RO_USER or row[1] is not False:
        who = row[0] if row else "?"
        raise ReadOnlyRefused(
            f"effective role is {who!r} (INSERT on kinds: "
            f"{row[1] if row else '?'}), need {RO_USER!r} with no INSERT"
        )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m precis_web.readonly_dsn <dsn-file>", file=sys.stderr)
        return 2
    try:
        ro = rewrite_dsn(Path(argv[1]).read_text(encoding="utf-8"))
        verify_read_only(ro)
    except (ReadOnlyRefused, OSError) as exc:
        print(f"guide-web: REFUSING to start against prod: {exc}", file=sys.stderr)
        return 1
    print(ro)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
